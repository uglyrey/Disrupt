"""Public HTTPS access for expiring observers. No content, account management or write routes."""

import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from app import admin_auth as auth
from app.admin_export import build_archive
from app.admin_metrics import aggregate, export_csv
from app.contest_metrics import report
from app.routes.admin import LoginBody

COOKIE = "beresta_review"
DATE = r"^\d{4}-\d{2}-\d{2}$"


def build_router(database, static, settings):
    def gate(request: Request):
        expected = urlsplit(settings.public_origin)
        if not settings.review_enabled or expected.scheme != "https" or request.url.scheme != "https" or request.url.netloc != expected.netloc:
            raise HTTPException(404, "Не найдено")

    router = APIRouter(tags=["review"], dependencies=[Depends(gate)])
    key = settings.analytics_pseudonym_key.encode()

    def observer(request: Request, db=Depends(database)):
        context = auth.authenticate(request, db, role="observer", cookie=COOKIE)
        request.state.review_id = context.admin.id
        db.commit()
        if db.get_bind().dialect.name == "postgresql":
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return db

    @router.get("/review", include_in_schema=False)
    def page():
        return FileResponse(static / "review.html")

    @router.get("/review/assets/{asset}", include_in_schema=False)
    def asset(asset: str):
        files = {"metrics.js": "admin.js", "auth.js": "admin.auth.js", "metrics.css": "admin.css",
                 "tools.js": "admin.tools.js", "navigation.js": "review.navigation.js"}
        if asset not in files:
            raise HTTPException(404, "Не найдено")
        return FileResponse(static / files[asset])

    @router.post("/review-api/v1/login", include_in_schema=False)
    def login(body: LoginBody, request: Request, response: Response, db=Depends(database)):
        if request.headers.get("origin") != settings.public_origin:
            raise HTTPException(403, "Недопустимый источник запроса")
        token, csrf, account = auth.sign_in(db, username=body.username, password=body.password,
                                          code=body.totp, ip=auth.client_ip(request), role="observer")
        response.set_cookie(COOKIE, token, httponly=True, secure=True, samesite="strict", path="/",
                            max_age=min(auth.SESSION_SECONDS, int(account.access_expires_at - time.time())))
        return {"username": account.username, "csrf_token": csrf,
                "expires_at": min(time.time() + auth.SESSION_SECONDS, account.access_expires_at)}

    @router.get("/review-api/v1/me", include_in_schema=False)
    def me(request: Request, db=Depends(database)):
        context = auth.authenticate(request, db, role="observer", cookie=COOKIE)
        return {"username": context.admin.username, "csrf_token": auth.csrf_for(context.token),
                "expires_at": context.session.expires_at, "idle_seconds": auth.IDLE_SECONDS}

    @router.post("/review-api/v1/logout", status_code=204, include_in_schema=False)
    def logout(request: Request, response: Response, db=Depends(database)):
        context = auth.authenticate(request, db, role="observer", cookie=COOKIE, write=True)
        db.delete(context.session)
        auth.audit(db, "logout", admin_id=context.admin.id, ip=auth.client_ip(request))
        db.commit()
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")

    @router.get("/review-api/v1/summary", include_in_schema=False)
    def summary(response: Response, start: str = Query(alias="from", pattern=DATE),
                end: str = Query(alias="to", pattern=DATE), channel: Literal["all", "web", "telegram"] = "all",
                source: str = Query("all", min_length=1, max_length=100),
                usage_limit: int = Query(50, ge=1, le=100), usage_offset: int = Query(0, ge=0),
                db=Depends(observer)):
        value, next_offset = aggregate(db, start, end, channel, source, key,
                                       usage_limit=usage_limit, usage_offset=usage_offset)
        value.contest = report(db, start, end, channel=channel, source=source,
                               counted_components=tuple(settings.contest_counted_components.split(",")))
        if next_offset is not None:
            response.headers["X-Next-Usage-Offset"] = str(next_offset)
        return value

    @router.get("/review-api/v1/contest", include_in_schema=False)
    def contest(db=Depends(observer)):
        return report(db, settings.contest_start, settings.contest_end,
                      counted_components=tuple(settings.contest_counted_components.split(",")))

    @router.get("/review-api/v1/export", include_in_schema=False)
    def csv(request: Request, start: str = Query(alias="from", pattern=DATE),
            end: str = Query(alias="to", pattern=DATE), channel: Literal["all", "web", "telegram"] = "all",
            source: str = Query("all", min_length=1, max_length=100), db=Depends(observer)):
        value, _ = aggregate(db, start, end, channel, source, key, usage_limit=1)
        auth.audit(db, "export_csv", admin_id=request.state.review_id, ip=auth.client_ip(request),
                   details={"from": start, "to": end, "role": "observer"})
        db.commit()
        return Response(export_csv(value, {"from": start, "to": end, "channel": channel, "source": source}),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="beresta-{start}-{end}.csv"'})

    @router.get("/review-api/v1/export.zip", include_in_schema=False)
    def archive(request: Request, start: str = Query(alias="from", pattern=DATE),
                end: str = Query(alias="to", pattern=DATE), db=Depends(observer)):
        if not key:
            raise HTTPException(503, "Стабильные идентификаторы выгрузки не настроены")
        data, stats = build_archive(db, start, end, key, stable_key=True,
                                    counted_components=tuple(settings.contest_counted_components.split(",")),
                                    contest_period=(settings.contest_start, settings.contest_end))
        auth.audit(db, "export_zip", admin_id=request.state.review_id, ip=auth.client_ip(request), details=stats)
        db.commit()
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="beresta-metrics-{start}-{end}.zip"'})

    return router
