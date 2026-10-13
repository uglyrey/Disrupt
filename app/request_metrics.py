"""Record content-free authenticated failures after response and dependency cleanup."""

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.error_logging import log_error
from app.models import RequestError, User


class RequestMetrics:
    def __init__(self, app, sessions, log_file):
        self.app, self.sessions, self.log_file = app, sessions, log_file

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 0

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, observe)
        actor = scope.get("state", {}).get("analytics_user_id")
        if actor and status >= 400:
            # The business transaction is now closed. Never wait on its locks inside call_next.
            route = getattr(scope.get("route"), "path", "unknown")
            await run_in_threadpool(self.record, actor, route, scope["method"], status)

    def record(self, actor, route, method, status):
        try:
            with self.sessions() as db:
                test = db.scalar(select(User.is_test).where(User.id == actor))
                if test is not None:
                    db.add(
                        RequestError(
                            user_id=actor,
                            route=route[:160],
                            method=method[:8],
                            status_code=status,
                            error_code="http_" + str(status),
                            is_test=test,
                        )
                    )
                    db.commit()
        except Exception:
            log_error("http_error_recording_failed", "internal_error", log_file=self.log_file)
