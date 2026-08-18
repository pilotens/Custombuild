from __future__ import annotations

import os

os.environ["APP_ENV"] = "test"
os.environ["AUTH_MODE"] = "development"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["RATE_LIMIT_REQUESTS"] = "10000"
os.environ.setdefault(
    "ARTIFACT_SIGNING_SECRET", "test-artifact-signing-secret-32-bytes-minimum"
)
