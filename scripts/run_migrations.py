from __future__ import annotations

import os
from collections.abc import Mapping

from alembic.config import main as alembic_main
from app.config_guards import validate_production_database_url


def validate_migration_environment(environment: Mapping[str, str]) -> None:
    if environment.get("APP_ENV", "development") != "production":
        return
    database_url = environment.get("DATABASE_URL", "")
    validate_production_database_url(
        database_url,
        expected_username="custombuild_migrator",
        setting_name="MIGRATION_DATABASE_URL",
    )


def main() -> None:
    validate_migration_environment(os.environ)
    alembic_main(argv=["-c", "services/api/alembic.ini", "upgrade", "head"])


if __name__ == "__main__":
    main()
