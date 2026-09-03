"""Fail-closed production registry gate before the generation consumer starts."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .registry_readiness import GENERATION_QUEUE, require_generation_registry_activation

_CELERY_PREFIX = (
    "celery",
    "--workdir",
    "services/worker",
    "-A",
    "custombuild_worker.tasks:celery_app",
    "worker",
)
_EXPECTED_WORKER_ARGS = (
    "--loglevel=INFO",
    "--concurrency=2",
    "--queues=generation",
)


def main(arguments: Sequence[str] | None = None) -> None:
    worker_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    if worker_arguments != _EXPECTED_WORKER_ARGS:
        raise SystemExit("generation worker command does not match the reviewed queue contract")
    try:
        require_generation_registry_activation(expected_queue=GENERATION_QUEUE)
    except Exception:
        raise SystemExit(
            "generation worker retention registry readiness failed"
        ) from None
    command = (*_CELERY_PREFIX, *worker_arguments)
    os.execvp(command[0], command)  # noqa: S606 - fixed reviewed Celery executable


if __name__ == "__main__":
    main()
