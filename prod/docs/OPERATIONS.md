# Operations

## Backup

Back up PostgreSQL and the S3-compatible artifact bucket together. Capture the
database first, record its transaction timestamp, then version/snapshot the
bucket. Encrypt backups, test restore quarterly and retain at least one offline
copy. Redis is a queue/cache and is not the system of record.

## Restore

1. Stop API and workers.
2. Restore PostgreSQL into an isolated environment.
3. Restore the matching object-store snapshot.
4. Run `alembic upgrade head` without starting workers.
5. Verify artifact checksums against manifests.
6. Start one worker, let queued/idempotent jobs settle, then scale workers.
7. Run tenant-isolation and seeded acceptance probes before reopening traffic.

## Secrets and observability

Inject database, OIDC and object-store secrets through the deployment secret
manager; never commit `.env`. Export structured logs and OpenTelemetry at the
platform layer. Alert on repeated job failures, RLS violations, artifact hash
mismatches and attempted release with blocking rules.
