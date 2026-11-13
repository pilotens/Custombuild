from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def create_sqlite_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.database in (None, "", ":memory:"):
        # Independent sessions need independent transactions. A named memdb VFS
        # keeps this engine's DB in memory while allowing real pooled connections.
        parsed = parsed.set(
            database=f"file:/custombuild-{uuid4().hex}",
            query={**parsed.query, "vfs": "memdb", "uri": "true"},
        )
        return create_engine(
            parsed,
            poolclass=QueuePool,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )
    return create_engine(parsed, pool_pre_ping=True, connect_args={"check_same_thread": False})


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    if url.startswith("sqlite"):
        return create_sqlite_engine(url)
    connect_args = (
        {
            "connect_timeout": settings.readiness_timeout_seconds,
            "options": (
                "-c statement_timeout="
                f"{settings.database_statement_timeout_seconds * 1000} "
                "-c lock_timeout="
                f"{settings.database_lock_timeout_seconds * 1000}"
            ),
        }
        if url.startswith("postgresql")
        else {}
    )
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache(maxsize=1)
def get_readiness_engine() -> Engine:
    settings = get_settings()
    if not settings.database_url.startswith("postgresql"):
        return get_engine()
    timeout = settings.readiness_timeout_seconds
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=float(timeout),
        connect_args={
            "connect_timeout": timeout,
            "options": f"-c statement_timeout={timeout * 1000}",
        },
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_tenant_context(session: Session, organization_id: str) -> None:
    """Bind PostgreSQL RLS to the authenticated tenant for this transaction."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.current_organization_id', :tenant, true)"),
            {"tenant": organization_id},
        )
