"""API of the questionnaire «Знакомство» and the page «Что beresta знает обо мне» (docs/profile-v1.md).

Every call is scoped to the signed-in person. Writes need the CSRF header. The answers are queued here and turned into
facts by the worker, so no model is called in this process.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app import profile as service
from app.models import User
from app.profile_questions import MAX_ANSWER_CHARS, QUESTION_IDS
from app.schemas import StrictModel
from app.security import get_login_session, throttle


class AnswersBody(StrictModel):
    answers: dict[str, str]


class CalendarOfferBody(StrictModel):
    calendar_offer: bool


class CategoriesBody(StrictModel):
    names: list[str] = Field(min_length=1, max_length=service.MAX_NEW_CATEGORIES)


class FactBody(StrictModel):
    text: str


def checked_answers(answers):
    """Known questions only, each answer up to 1000 characters, at least one not empty."""
    for question_id, text in answers.items():
        if question_id not in QUESTION_IDS:
            raise HTTPException(422, "Неизвестный вопрос")
        if len(text) > MAX_ANSWER_CHARS or "\x00" in text:
            raise HTTPException(422, f"Ответ должен быть не длиннее {MAX_ANSWER_CHARS} знаков")
    clean = service.normalize_answers(answers)
    if not clean:
        raise HTTPException(422, "Напишите хотя бы один ответ")
    return clean


def build_router(database):
    router = APIRouter(prefix="/api/v1/profile", tags=["profile"])

    def edits(db, session):
        throttle(db, "profile-edit:" + session.user_id, limit=60, window=600)

    def runs(db, session):
        throttle(
            db, "profile-run:" + session.user_id, limit=6, window=3600,
            message="Слишком много попыток. Попробуйте через час.",
        )

    def current(db, session):
        return service.profile_view(db, db.get(User, session.user_id))

    def limit_reply(limit):
        return JSONResponse({"detail": str(limit), "limit_resets_at": limit.resets_at}, status_code=429)

    @router.get("")
    def read_profile(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return current(db, session)

    @router.post("/answers", status_code=202)
    def send_answers(body: AnswersBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        answers = checked_answers(body.answers)
        runs(db, session)
        settings = request.app.state.settings
        try:
            state = service.submit_answers(db, settings, session.user_id, answers)
        except service.ProfileLimitReached as limit:
            return limit_reply(limit)
        db.commit()
        return JSONResponse({"state": state}, status_code=202)

    @router.post("/decline")
    def decline_profile(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        state = service.decline(db, session.user_id)
        db.commit()
        return {"state": state}

    @router.post("/retry", status_code=202)
    def retry_profile(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        runs(db, session)
        try:
            state = service.retry(db, request.app.state.settings, session.user_id)
        except service.ProfileLimitReached as limit:
            return limit_reply(limit)
        db.commit()
        return JSONResponse({"state": state}, status_code=202)

    @router.patch("/calendar-offer")
    def change_calendar_offer(body: CalendarOfferBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        service.set_calendar_offer(db, session.user_id, body.calendar_offer)
        db.commit()
        return {"calendar_offer": body.calendar_offer}

    @router.post("/categories")
    def confirm_categories(body: CategoriesBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        created = service.create_categories(db, session.user_id, body.names)
        db.commit()
        return {**current(db, session), "created": created}

    @router.post("/categories/skip")
    def skip_categories(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        service.skip_categories(db, session.user_id)
        db.commit()
        return current(db, session)

    def known_fact(value):
        if not re.fullmatch(r"f[0-9]{1,4}", value):
            raise HTTPException(404, "Пункт не найден")
        return value

    @router.patch("/facts/{fact_id}")
    def change_fact(fact_id: str, body: FactBody, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        service.edit_fact(db, session.user_id, known_fact(fact_id), body.text)
        db.commit()
        return current(db, session)

    @router.delete("/facts/{fact_id}")
    def remove_fact(fact_id: str, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        service.delete_fact(db, session.user_id, known_fact(fact_id))
        db.commit()
        return current(db, session)

    @router.delete("")
    def remove_profile(request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        edits(db, session)
        service.delete_profile(db, session.user_id)
        db.commit()
        return current(db, session)

    return router
