from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .config import get_settings
from .db import Base, get_engine, session_scope
from .design_service import assert_rule_engine_available
from .observability import RequestContextMiddleware, request_id_context
from .readiness import probe_dependencies
from .security import RateLimitMiddleware, SecurityHeadersMiddleware
from .seed import seed_development


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "request_id": request_id_context.get(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("custombuild.api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Construction screening is a mandatory safety dependency.  Abort startup
    # rather than serving designs with an empty rule list that appears to PASS.
    assert_rule_engine_available()
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(get_engine())
    if settings.auth_mode == "development":
        for session in session_scope():
            seed_development(session)
    logger.info("api_started env=%s auth_mode=%s", settings.app_env, settings.auth_mode)
    yield


app = FastAPI(
    title="Custombuild API",
    version="0.1.0",
    description=(
        "Deterministic bookcase design-to-production API. Construction calculations are "
        "screening only; reference machine programs are validation-only."
    ),
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    redis_url=settings.redis_url,
    distributed_required=settings.app_env == "production",
    trusted_proxy_cidrs=settings.trusted_proxy_networks,
)
app.add_middleware(RequestContextMiddleware)
app.include_router(router)


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.get("/ready", tags=["operations"])
def readiness() -> dict[str, str]:
    """Check every dependency required to accept and complete application work."""

    statuses, failures = probe_dependencies(get_settings())
    if failures:
        failed_dependencies = [failure.name for failure in failures]
        for failure in failures:
            logger.error(
                "readiness_failed dependency=%s error_type=%s",
                failure.name,
                failure.error_type,
            )
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "message": "Required dependencies are unavailable",
                "failed_dependencies": failed_dependencies,
            },
        )
    return {"status": "ready", "version": app.version, **statuses}
