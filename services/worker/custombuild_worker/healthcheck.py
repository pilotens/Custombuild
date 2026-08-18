from __future__ import annotations

import socket
from typing import Any

from .tasks import celery_app


def worker_is_responsive(*, timeout_seconds: float = 5.0) -> bool:
    node_name = f"celery@{socket.gethostname()}"
    inspector: Any = celery_app.control.inspect(
        destination=[node_name],
        timeout=timeout_seconds,
    )
    responses = inspector.ping()
    if not isinstance(responses, dict):
        return False
    response = responses.get(node_name)
    return isinstance(response, dict) and response.get("ok") == "pong"


def main() -> None:
    if not worker_is_responsive():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
