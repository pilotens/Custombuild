from __future__ import annotations

import os
from collections.abc import Mapping

from alembic.config import main as alembic_main
from app.config_guards import validate_production_database_url
from app.db import session_scope
from app.seed import seed_development

DEVELOPMENT_SEED_FLAG = "SEED_DEVELOPMENT_DATA"


def validate_migration_environment(environment: Mapping[str, str]) -> None:
    if environment.get("APP_ENV", "development") != "production":
        return
    database_url = environment.get("DATABASE_URL", "")
    validate_production_database_url(
        database_url,
        expected_username="custombuild_migrator",
        setting_name="MIGRATION_DATABASE_URL",
    )


def development_seed_requested(environment: Mapping[str, str]) -> bool:
    """Allow demo-data writes only through an explicit non-production opt-in."""

    return (
        environment.get("APP_ENV", "development") in {"development", "test"}
        and environment.get(DEVELOPMENT_SEED_FLAG) == "true"
    )


def main() -> None:
    validate_migration_environment(os.environ)
    alembic_main(argv=["-c", "services/api/alembic.ini", "upgrade", "head"])
    # PostgreSQL runtime roles are deliberately unable to bootstrap global
    # organizations, users or memberships. Only local Compose explicitly opts
    # into seeding through the migrator after least-privilege migration. SQLite
    # keeps its in-process seed in the API lifespan because it has no DB roles.
    if development_seed_requested(os.environ):
        for session in session_scope():
            seed_development(session)


if __name__ == "__main__":
    main()
