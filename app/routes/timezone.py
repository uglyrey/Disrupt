"""Time zone of the account: the list of zones and the zone of the signed-in person."""

from fastapi import APIRouter, Depends, Request, Response

from app.account_timezone import set_account_timezone, timezone_state
from app.models import User
from app.schemas import AccountTimezone, AccountTimezoneUpdate, ZoneList
from app.security import get_login_session
from app.timezones import zone_list

TIMEZONES_CACHE = "private, max-age=3600"
DOC = "Requires a browser session. Changes also require CSRF and a same-origin request."


def build_router(database):
    router = APIRouter(prefix="/api/v1", tags=["timezone"])

    @router.get(
        "/timezones", response_model=ZoneList,
        description="Canonical IANA zones with Russian names for Russia, the CIS and common cities. "
        "Offsets are for the current moment. Sorted by offset, then name. " + DOC,
    )
    def zones(request: Request, response: Response, db=Depends(database)):
        get_login_session(request, db)
        response.headers["Cache-Control"] = TIMEZONES_CACHE
        return {"zones": zone_list()}

    @router.get("/account/timezone", response_model=AccountTimezone, description=DOC)
    def read_timezone(request: Request, db=Depends(database)):
        session = get_login_session(request, db)
        return timezone_state(db.get(User, session.user_id))

    @router.patch(
        "/account/timezone", response_model=AccountTimezone,
        description="`manual` always writes. `auto` (the browser clock) only fills an empty value and never "
        "overwrites an existing one. Reminders made by guess are moved to the new zone. " + DOC,
    )
    def write_timezone(body: AccountTimezoneUpdate, request: Request, db=Depends(database)):
        session = get_login_session(request, db, write=True)
        user, _moved = set_account_timezone(db, session.user_id, body.timezone, body.source)
        db.commit()
        return timezone_state(user)

    return router
