from __future__ import annotations

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import audit, auth, chat, leave, notifications, operations, users
from app.core.config import settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import request_context_middleware
from app.db.redis import ping_redis
from app.services import health_service


configure_logging()
app = FastAPI(title=settings.app_name)
install_exception_handlers(app)

app.middleware("http")(request_context_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(leave.router, prefix="/leave", tags=["leave"])
app.include_router(operations.router, prefix="/operations", tags=["operations"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])
app.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
app.include_router(audit.router, prefix="/audit", tags=["audit"])


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    database_ok = health_service.check_database()
    redis_ok = health_service.check_redis()
    return {
        "status": "ok" if database_ok and redis_ok else "degraded",
        "application": "ok",
        "database": "ok" if database_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }


@app.get("/ready", tags=["system"], response_model=None)
def readiness_check() -> dict[str, str] | JSONResponse:
    if health_service.check_database():
        return {"status": "ready", "database": "ok"}
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "database": "error"},
    )


@app.get("/health/redis", tags=["system"])
def redis_health_check() -> dict[str, str]:
    ping_redis()
    return {"status": "ok"}
