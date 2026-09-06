from __future__ import annotations

import os
import socket
from typing import Any

from .registry_readiness import require_generation_registry_activation
from .tasks import celery_app


def worker_is_responsive(*, timeout_seconds: float = 5.0) -> bool:
    expected_queue = os.getenv("CELERY_EXPECTED_QUEUE", "").strip()
    if expected_queue not in {"generation", "maintenance", "storage-reaper"}:
        return False
    try:
        # This check precedes the Celery ping deliberately: a stale production
        # generation worker must never advertise readiness merely because its
        # process and queue consumer are alive.
        require_generation_registry_activation(expected_queue=expected_queue)
    except Exception:
        return False
    node_name = f"celery@{socket.gethostname()}"
    inspector: Any = celery_app.control.inspect(
        destination=[node_name],
        timeout=timeout_seconds,
    )
    responses = inspector.ping()
    if not isinstance(responses, dict):
        return False
    response = responses.get(node_name)
    if not isinstance(response, dict) or response.get("ok") != "pong":
        return False
    active_queues = inspector.active_queues()
    if not isinstance(active_queues, dict):
        return False
    queues = active_queues.get(node_name)
    if not isinstance(queues, list):
        return False
    queue_names = {
        queue.get("name")
        for queue in queues
        if isinstance(queue, dict) and isinstance(queue.get("name"), str)
    }
    return queue_names == {expected_queue}


def main() -> None:
    if not worker_is_responsive():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
