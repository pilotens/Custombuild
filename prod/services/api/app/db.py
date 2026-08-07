from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    pool_args = {"poolclass": StaticPool} if url.endswith(":memory:") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args, **pool_args)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def session_scope() -> Generator[Session, None, None]:
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
