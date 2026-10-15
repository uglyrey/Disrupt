"""Session-bound feedback. The administrator inbox lives in app.routes.admin."""
import hashlib
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.error_logging import log_error
from app.feedback_models import Feedback
from app.mailer import MailError
from app.schemas import Counter, StrictModel, SupportRecovery
from app.security import check_origin, get_login_session, throttle


class FeedbackCreate(StrictModel):
    kind: Literal['bug', 'idea', 'question', 'other']
    subject: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=5000)
    steps: str = Field(default='', max_length=3000)
    expected: str = Field(default='', max_length=2000)
    contact: str = Field(default='', max_length=200)

    @field_validator('subject', 'description', 'steps', 'expected', 'contact')
    @classmethod
    def no_null_bytes(cls, value):
        if '\x00' in value:
            raise ValueError('Text cannot contain null bytes')
        return value

    @field_validator('subject', 'description')
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or '\x00' in value:
            raise ValueError('Expected nonblank text')
        return value


class FeedbackReceipt(StrictModel):
    id: str


RECOVERY_TITLE = 'Восстановление аккаунта'
RECOVERY_ANSWER = {'status': 'ok', 'message': 'Запрос отправлен. Мы напишем вам на почту'}


class FeedbackView(FeedbackCreate):
    kind: Literal['bug', 'idea', 'question', 'other', 'recovery']
    id: str
    user_id: str | None
    status: Literal['new', 'in_progress', 'resolved']
    version: int
    created_at: float
    updated_at: float


class FeedbackUpdate(StrictModel):
    status: Literal['new', 'in_progress', 'resolved']
    version: Counter


def view(row):
    fields = FeedbackView.model_fields
    return {key: getattr(row, key) for key in fields}


def build_router(database):
    router = APIRouter(tags=['feedback'])

    @router.post('/api/v1/support/recovery')
    def recovery(body: SupportRecovery, request: Request, db=Depends(database)):
        """A request from someone who cannot sign in. Stored in the inbox; mailed to support when mail is on."""
        check_origin(request)
        if body.accept_processing is not True:
            raise HTTPException(422, 'Чтобы отправить запрос, согласитесь на обработку данных из него')
        ip = request.client.host if request.client else 'unknown'
        wait = 'Вы уже отправляли запросы. Подождите час и попробуйте снова'
        throttle(db, 'support-recovery-ip:' + ip, limit=5, window=3600, message=wait)
        throttle(db, 'support-recovery-email:' + body.email, limit=3, window=3600, message=wait)
        row = Feedback(
            user_id=None, idempotency_key=secrets.token_urlsafe(24),
            payload_hash=hashlib.sha256(body.model_dump_json().encode()).hexdigest(),
            kind='recovery', subject=RECOVERY_TITLE, description=body.text, steps='', expected='',
            contact=body.email,
        )
        db.add(row)
        db.commit()
        mailer = request.app.state.mailer
        if mailer.enabled:
            letter = (
                f'{RECOVERY_TITLE}\n\nОтвет нужен на {body.email}\nОбращение {row.id}\n\n{body.text}\n'
            )
            try:
                mailer.send(
                    request.app.state.settings.support_email, RECOVERY_TITLE + ' в beresta', letter,
                    reply_to=body.email,
                )
            except MailError as exc:
                # The request is already stored. The person is not told about the mail.
                log_error('mail_send_failed', 'mail_error', log_file=request.app.state.settings.error_log_file, exception=exc)
        return RECOVERY_ANSWER

    @router.post('/api/v1/feedback', response_model=FeedbackReceipt, status_code=201)
    def create(body: FeedbackCreate, request: Request, db=Depends(database),
               key: str = Header(alias='Idempotency-Key', min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')):
        session = get_login_session(request, db, write=True)
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()

        def replay():
            row = db.scalar(select(Feedback).where(Feedback.user_id == session.user_id, Feedback.idempotency_key == key))
            if row and row.payload_hash != digest:
                raise HTTPException(409, 'Это обращение уже отправлено с другим содержимым.')
            return {'id': row.id} if row else None

        existing = replay()
        if existing:
            return existing
        throttle(db, 'feedback:' + session.user_id, limit=5)
        row = Feedback(user_id=session.user_id, idempotency_key=key, payload_hash=digest, **body.model_dump())
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = replay()
            if existing:
                return existing
            raise
        return {'id': row.id}

    return router
