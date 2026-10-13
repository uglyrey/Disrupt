from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator

from app.digests import available_identity, settings_view, update_settings
from app.schemas import StrictModel
from app.security import get_login_session


class DigestSettingsPatch(StrictModel):
    morning_enabled: bool
    evening_enabled: bool
    morning_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    evening_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Выберите часовой пояс из базы IANA") from None
        return value


def build_router(database):
    router = APIRouter(prefix="/api/v1/telegram/digest-settings", tags=["telegram"])

    @router.get("")
    def read(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return settings_view(db, session.user_id)

    @router.patch("")
    def change(body: DigestSettingsPatch, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        if (body.morning_enabled or body.evening_enabled) and available_identity(db, session.user_id) is None:
            raise HTTPException(409, "Сначала подключите Telegram и разрешите уведомления")
        return update_settings(db, session.user_id, body)

    return router
