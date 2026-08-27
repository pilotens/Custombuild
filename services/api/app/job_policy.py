from __future__ import annotations

from datetime import timedelta

# One server-owned policy is shared by the API and worker so clients never
# invent a shorter timeout than the generation system can actually honor.
GENERATION_JOB_TIMEOUT = timedelta(hours=2)
GENERATION_LEASE_TTL = timedelta(minutes=2)
GENERATION_HEARTBEAT_INTERVAL_SECONDS = 30.0
GENERATION_RECOVERY_INTERVAL_SECONDS = 30.0
LEGACY_STALE_LEASE_THRESHOLD = timedelta(minutes=30)

# Celery's soft limit gives the generation task one bounded opportunity to
# record a terminal, tenant-fenced failure.  If native CAD code cannot unwind,
# the hard limit kills the worker child after this short grace period so one
# poisoned design can never occupy a worker slot forever.
GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS = int(GENERATION_JOB_TIMEOUT.total_seconds())
GENERATION_TASK_TERMINATION_GRACE_SECONDS = 60
GENERATION_TASK_HARD_TIME_LIMIT_SECONDS = (
    GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS + GENERATION_TASK_TERMINATION_GRACE_SECONDS
)
