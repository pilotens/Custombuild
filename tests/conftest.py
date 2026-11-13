from __future__ import annotations

import os

os.environ["APP_ENV"] = "test"
os.environ["AUTH_MODE"] = "development"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["RATE_LIMIT_REQUESTS"] = "10000"
os.environ.setdefault(
    "ARTIFACT_SIGNING_SECRET", "test-artifact-signing-secret-32-bytes-minimum"
)


def pytest_addoption(parser):
    from tests.product_acceptance.catalog import LEVELS, all_cases

    group = parser.getgroup("product acceptance")
    group.addoption(
        "--product",
        choices=sorted({case.product for case in all_cases()}),
        help="Run one product profile.",
    )
    group.addoption(
        "--product-level",
        choices=[*LEVELS, *LEVELS.values()],
        help="Run one difficulty level (English ID or Swedish name).",
    )
