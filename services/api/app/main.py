from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .config import get_settings
from .db import Base, get_engine, session_scope
from .security import InMemoryRateLimitMiddleware, SecurityHeadersMiddleware
from .seed import seed_development


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
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
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(InMemoryRateLimitMiddleware)
app.include_router(router)


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}
