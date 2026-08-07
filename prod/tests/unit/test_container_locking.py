from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("dockerfile", ("services/api/Dockerfile", "services/worker/Dockerfile"))
def test_python_images_install_the_exact_uv_lock(dockerfile: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock ./" in source
    assert "uv==0.8.3" in source
    assert "uv sync --locked --no-dev" in source
    assert "--no-install-project" in source
    assert "pip install --no-cache-dir -r" not in source


def test_worker_image_installs_locked_cad_group_but_api_does_not() -> None:
    api = Path("services/api/Dockerfile").read_text(encoding="utf-8")
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")

    assert "--group cad" not in api
    assert "--group cad" in worker
    assert "--schedule=/tmp/celerybeat-schedule" in worker


def test_postgres_healthcheck_waits_for_the_tcp_server() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert "pg_isready -h 127.0.0.1" in compose
    assert "start_period: 5s" in compose
