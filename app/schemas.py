import re
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    field_validator,
)

NonBlank = Annotated[str, StringConstraints(min_length=1)]

# Version and generation counters live in 32-bit database columns. A bigger number can never match and
# would only break the query, so it is refused as an ordinary invalid value. The JSON schema stays the same.
MAX_COUNTER = 2**31 - 1


def _counter_in_range(value):
    if value > MAX_COUNTER:
        raise ValueError("Counter is too large")
    return value


Counter = Annotated[int, Field(ge=1), AfterValidator(_counter_in_range)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Credentials(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=128)


class Registration(Credentials):
    source_token: str | None = Field(default=None, max_length=200)
    # Consent is checked in the route so that the message can say what is missing.
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False


class TelegramLoginStart(StrictModel):
    source_token: str | None = Field(default=None, max_length=200)
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False
    marketing_opt_in: bool = False


class RecoveryRequest(StrictModel):
    # No username pattern: every input gets the same answer, so older accounts are not singled out.
    username: str = Field(min_length=1, max_length=64)


class PasswordReset(StrictModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=128)


class TokenBody(StrictModel):
    token: str = Field(min_length=1, max_length=200)


class AcceptPolicy(StrictModel):
    policy_version: str = Field(min_length=1, max_length=32)


class MarketingConsent(StrictModel):
    enabled: bool


class DeletionRequest(StrictModel):
    password: str = Field(min_length=1, max_length=128)


EMAIL_PATTERN = re.compile(r"[^@\s\x00-\x1f]{1,64}@[^@\s\x00-\x1f]+\.[^@\s\x00-\x1f]{2,}")


class EmailBody(StrictModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Invalid email address")
        return value


class EmailCredentials(EmailBody):
    password: str = Field(min_length=10, max_length=128)


class EmailRegistrationStart(EmailCredentials):
    source_token: str | None = Field(default=None, max_length=200)
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False
    marketing_opt_in: bool = False


class EmailRegistrationConfirm(StrictModel):
    registration_id: str = Field(min_length=36, max_length=36)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class EmailCodeConfirm(StrictModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
    # Needed only for a new address. The server asks for them with the code «consent_required».
    accept_policy: bool = False
    policy_version: str | None = Field(default=None, max_length=32)
    confirm_age: bool = False
    marketing_opt_in: bool = False


class PasswordChange(StrictModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class SupportRecovery(EmailBody):
    text: str = Field(min_length=1, max_length=2000)
    accept_processing: bool = False

    @field_validator("text")
    @classmethod
    def readable(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must contain non-whitespace characters and no null bytes")
        return value


class TextCapture(StrictModel):
    text: str = Field(min_length=1, max_length=12000)
    processing_mode: Literal["ai", "manual"] = "ai"

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must contain non-whitespace characters and no null bytes")
        return value  # Preserve the exact original, including whitespace.


class AudioJobResponse(StrictModel):
    id: str
    capture_id: str
    kind: Literal["capture", "merge"] = "capture"
    merge_note_ids: list[str] | None = None
    status: Literal["queued", "running", "succeeded", "failed"]
    provider: Literal["mock", "cloudru"]
    error_code: str | None
    note_id: str | None
    # True when the record was added to an existing note: note_id is then that note (docs/attach-to-note-v1.md).
    attached: bool = False
    original_text: str
    created_at: float
    finished_at: float | None
    attempts: int
    retry_at: float | None


class AudioMetadata(StrictModel):
    input_kind: Literal["text", "audio"] = "text"
    transcript: str | None = None
    transcript_version: int = 1
    transcript_origin: Literal["stt", "user", "legacy"] | None = None
    audio_seconds: float | None = None
    audio_media_type: str | None = None


class ImageMetadata(StrictModel):
    id: str
    url: str
    width: int
    height: int
    byte_size: int


class CaptureResponse(AudioMetadata):
    capture_id: str
    original_text: str
    processing_mode: Literal["ai", "manual"]
    note_id: str | None
    job: AudioJobResponse | None
    images: list[ImageMetadata] = Field(default_factory=list)


class TranscriptEdit(StrictModel):
    version: Counter
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty transcript or null bytes")
        return value


class TranscriptRevisionResponse(StrictModel):
    version: int
    text: str
    origin: Literal["stt", "user", "legacy"]
    created_at: float | None


class ProposedConclusion(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    source_quote: str = Field(min_length=1, max_length=3000)

    @field_validator("text", "source_quote")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


ItemKind = Literal["note", "idea", "task", "goal", "plan"]


LOCAL_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def well_formed_local_minute(value):
    """A local `YYYY-MM-DDTHH:MM` string, or None for anything else (never an error)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not LOCAL_MINUTE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return value


class ProposedItem(ProposedConclusion):
    kind: ItemKind
    due_text: str | None = Field(default=None, max_length=300)
    # Local wall time of the deadline, YYYY-MM-DDTHH:MM, as the model worked it out. Optional and never trusted:
    # a malformed value becomes None here, the reminder code cross-checks the rest (docs/reminder-confirm-v1.md).
    due_at: str | None = None
    # Length of the event in minutes (5..720) as the model estimated it or the person named it. Optional; anything that
    # is not a whole number in that range becomes None here and the person's default length applies (docs/note-style-v1.md).
    duration_minutes: int | None = None

    @field_validator("due_at", mode="before")
    @classmethod
    def well_formed_due_at(cls, value):
        return well_formed_local_minute(value)

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def sane_duration(cls, value):
        from app.note_style import valid_minutes
        return valid_minutes(value)


class ContinuesNote(StrictModel):
    """The model thinks the record continues one of the notes it was shown (docs/attach-to-note-v1.md)."""
    ref: str = Field(min_length=1, max_length=8, pattern=r"^n[0-9]{1,3}$")
    hint: str | None = Field(default=None, max_length=200)
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def number(cls, value):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("confidence must be a number")
        return float(value)

    @field_validator("hint", mode="before")
    @classmethod
    def short_hint(cls, value):
        return value[:200] if isinstance(value, str) else None


class StructuredNote(StrictModel):
    # Server-only processing metadata, never accepted from model JSON or exported to the contract.
    _grounding_details_omitted: bool = PrivateAttr(default=False)
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=24000)
    conclusions: list[ProposedConclusion] = Field(max_length=5)
    items: list[ProposedItem] = Field(default_factory=list, max_length=30)
    category_name: str | None = Field(default=None, min_length=1, max_length=100)
    # Optional hint about a note this record continues. A malformed value is dropped: it never spoils the note.
    continues_note: ContinuesNote | None = None

    @field_validator("continues_note", mode="before")
    @classmethod
    def lenient_continues(cls, value):
        if value is None or isinstance(value, ContinuesNote):
            return value
        try:
            return ContinuesNote.model_validate(value)
        except ValueError:
            return None

    @field_validator("title", "markdown")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value

    @field_validator("category_name")
    @classmethod
    def valid_category(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Empty category or null bytes")
        return value.strip() if value is not None else None


class NoteEdit(StrictModel):
    version: Counter
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=24000)
    # Человек закончил правку: следующее сохранение начнёт новую версию в истории.
    checkpoint: bool = False
    # Случайная метка вкладки. Версии разных вкладок в историю не склеиваются.
    editor: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("title", "markdown")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ConclusionEdit(StrictModel):
    version: Counter
    status: Literal["accepted", "rejected"]


class VersionRequest(StrictModel):
    version: Counter


class ReminderContent(StrictModel):
    scheduled_at: str = Field(min_length=20, max_length=40)
    timezone: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("scheduled_at")
    @classmethod
    def absolute_time(cls, value):
        date = datetime.fromisoformat(value)
        if "T" not in value or date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("An absolute ISO timestamp with an offset is required")
        date.timestamp()
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone") from None
        return value

    @field_validator("text")
    @classmethod
    def reminder_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ReminderCreate(ReminderContent):
    item_id: str | None = Field(default=None, min_length=1, max_length=36)


class ReminderEdit(ReminderContent):
    generation: Counter


class ReminderCancel(StrictModel):
    generation: Counter


class ReminderResponse(StrictModel):
    id: str
    note_id: str
    item_id: str | None
    scheduled_at: str
    timezone: str
    text: str
    status: str
    generation: int
    confirmed_at: str
    delivery_status: str | None
    previous_attempt_unknown: bool
    # ask or unclear: the time was a guess and the person has not said «Верно» yet. Empty otherwise.
    time_check: Literal["ask", "unclear", "confirmed", "corrected"] | None = None
    # Google Calendar page with the event filled in. Set only for a coming reminder when the account offers the calendar
    # (`calendar_offer`) and the reminder is not an advance one (docs/profile-v1.md).
    calendar_url: str | None = None
    # Length of the calendar event in minutes when the reminder has its own; empty means the person's default.
    duration_minutes: int | None = None
    # The advance reminder «Через час: ...» carries the id of the main reminder it belongs to.
    advance_of: str | None = None


class ReminderTimeRequest(StrictModel):
    local_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?$")
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        return ReminderContent.known_timezone(value)


class ReminderTimeChoice(StrictModel):
    scheduled_at: str
    local_at: str
    utc_offset: str
    is_future: bool


class ReminderTimeResponse(StrictModel):
    local_time: str
    timezone: str
    ambiguous: bool
    choices: list[ReminderTimeChoice]


class ItemCreate(VersionRequest):
    kind: ItemKind
    text: str = Field(min_length=1, max_length=1500)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value


class ItemEdit(ItemCreate):
    status: Literal["open", "completed"]


class CategoryCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty category or null bytes")
        return value.strip()


class CategoryEdit(CategoryCreate, VersionRequest):
    pass


class NoteCategoryEdit(VersionRequest):
    category_id: str | None = Field(max_length=36)


class UserAction(StrictModel):
    operation_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class NoteOpened(UserAction):
    reminder_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    search_operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class ProposedReminder(StrictModel):
    local_time: str
    timezone: str
    label: str
    # False when the spoken time had two readings: the form asks the person to confirm. Other readings are local ISO times.
    certain: bool = True
    alternatives: list[str] = Field(default_factory=list, max_length=4)


class ItemResponse(StrictModel):
    id: str
    note_id: str
    kind: ItemKind
    text: str
    status: Literal["open", "completed"]
    version: int
    source_quote: str | None
    due_text: str | None
    due_at: float | None
    proposed_reminder: ProposedReminder | None = None


class CategoryResponse(StrictModel):
    id: str
    name: str
    version: int
    # «Общее»: it cannot be renamed or deleted.
    is_general: bool = False


class ConclusionResponse(ProposedConclusion):
    id: str
    status: Literal["proposed", "accepted", "rejected"]


class AttachOffer(StrictModel):
    """Under a processed record: «Похоже, это продолжение «X». Добавить туда?» Open for 24 hours."""
    capture_id: str
    note_id: str
    title: str
    expires_at: float


class AttachedSource(StrictModel):
    """A record that was added to this note. Its own text stays here as an extra source."""
    capture_id: str
    created_at: float
    added_at: float | None
    channel: Literal["web", "telegram"]
    input_kind: Literal["text", "audio"]
    original_text: str
    can_undo: bool
    undo_until: float | None


class AttachResult(StrictModel):
    capture_id: str
    state: Literal["attached", "separate"]
    # The note to open: the target when the record was added, its own note otherwise.
    note_id: str
    note_title: str
    # Undo only: the added section had been edited, so it stays in the target. Nothing is lost.
    target_text_kept: bool = False


class AttachRequest(StrictModel):
    note_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")


class NoteResponse(AudioMetadata):
    id: str
    capture_id: str
    channel: Literal["web", "telegram"]
    original_text: str
    title: str
    markdown: str
    conclusions: list[ConclusionResponse]
    items: list[ItemResponse]
    category_id: str | None
    category_name: str | None
    structure_confirmed_at: float | None
    version: int
    provider: str
    created_at: float
    updated_at: float
    attach_offer: AttachOffer | None = None
    attached: list[AttachedSource] = Field(default_factory=list)


class NoteSummary(StrictModel):
    id: str
    title: str
    version: int
    updated_at: float
    category_id: str | None
    channel: Literal["web", "telegram"]
    input_kind: Literal["text", "audio"]


TelegramID = Annotated[int, Field(ge=1, le=9223372036854775807)]


class TelegramOperation(StrictModel):
    bot_id: TelegramID
    update_id: int = Field(ge=0, le=9223372036854775807)
    telegram_user_id: TelegramID
    chat_id: TelegramID


class TelegramText(TelegramOperation, TextCapture):
    pass


class TelegramLoginConfirm(TelegramOperation):
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    telegram_username: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    first_name: str | None = Field(default=None, max_length=128)


class TelegramZoneSet(TelegramOperation):
    timezone: str = Field(min_length=1, max_length=64)


class TelegramZoneAsked(TelegramOperation):
    pass


class TelegramReminderConfirm(TelegramOperation):
    """A button under a message that asks «Верно?» about the time of a reminder."""
    reminder_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")
    generation: Counter
    action: Literal["ok", "edit", "alt"]


class TelegramAttach(TelegramOperation):
    """A button under the reply about a record that may continue a note: add, keep apart, or undo."""
    capture_id: str = Field(min_length=36, max_length=36, pattern=r"^[0-9a-f-]+$")
    action: Literal["attach", "separate", "undo"]


class TelegramReminderTime(TelegramOperation):
    """The written answer to «Изменить время»."""
    reminder_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9-]+$")
    generation: Counter
    text: str = Field(min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Empty text or null bytes")
        return value.strip()


class TelegramZoneFind(StrictModel):
    """A stateless question: which zone does this text or this place mean. Nothing is stored."""
    bot_id: TelegramID
    telegram_user_id: TelegramID
    chat_id: TelegramID
    text: str | None = Field(default=None, max_length=64)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class TelegramLink(TelegramOperation):
    code: str = Field(min_length=22, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LinkCodeResponse(StrictModel):
    link_request_id: str
    code: str
    expires_at: str


class LinkResponse(StrictModel):
    link_request_id: str
    status: Literal["pending"]


class TelegramCaptureResponse(StrictModel):
    capture_id: str
    job_id: str | None
    status: Literal["saved"]
    note_url: str
    ai_limit_exceeded: bool = False
    ai_units_remaining: int | None = None
    ai_limit_resets_at: str | None = None
    # True once: the account has no time zone and was never asked. The bot then asks with city buttons.
    ask_timezone: bool = False
    # A note saved without AI is already in a category («Общее»); the bot names it in its reply.
    category_name: str | None = None


class IntegrationErrorDetail(StrictModel):
    code: Literal[
        "unauthorized", "forbidden", "not_found", "conflict", "invalid_input",
        "rate_limited", "unavailable", "internal_error", "telegram_not_linked",
        "link_expired", "link_conflict", "invalid_link", "input_too_large",
        "unsupported_audio", "empty_input", "action_forbidden", "action_expired", "quota_exceeded",
    ]
    message: str


class IntegrationError(StrictModel):
    error: IntegrationErrorDetail
    operation_id: str


# ---- Trash, merge, manual links, guides (docs/notes-manage-v1.md) ----

class MergeRequest(StrictModel):
    note_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] = Field(min_length=2, max_length=5)
    idempotency_key: str = Field(min_length=1, max_length=90, pattern=r"^[A-Za-z0-9_.:-]+$")


class NoteLinkCreate(StrictModel):
    note_id: str = Field(min_length=1, max_length=36)


class LinkedNote(StrictModel):
    note_id: str
    title: str
    category_id: str | None
    updated_at: float
    created_at: float


class NoteLinksResponse(StrictModel):
    note_id: str
    links: list[LinkedNote]


class TrashNote(StrictModel):
    id: str
    title: str
    version: int
    deleted_at: float
    purge_at: float
    category_id: str | None
    merged_into: str | None
    merged_into_title: str | None


class TrashResponse(StrictModel):
    trash_days: int
    notes: list[TrashNote]


GuideName = Literal["note_tools", "categories"]


class GuideUpdate(StrictModel):
    guide: GuideName
    step: int = Field(ge=0, le=100)
    done: bool


class GuideState(StrictModel):
    step: int
    done: bool


class GuidesResponse(StrictModel):
    guides: dict[str, GuideState]


# ---- Time zone of the account (docs/timezone-v1.md) ----

class ZoneInfoItem(StrictModel):
    id: str
    label: str
    offset: str
    offset_minutes: int


class ZoneList(StrictModel):
    zones: list[ZoneInfoItem]


class AccountTimezone(StrictModel):
    timezone: str | None
    source: Literal["auto", "manual", "telegram"] | None
    label: str | None


class AccountTimezoneUpdate(StrictModel):
    timezone: str = Field(min_length=1, max_length=64)
    source: Literal["auto", "manual"]

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        from app.timezones import valid_zone

        if not valid_zone(value):
            raise ValueError("Unknown IANA timezone")
        return value
