# Operations

## Backup

Back up PostgreSQL and the S3-compatible artifact bucket together. Encrypt
backups, test restore quarterly and retain at least one offline copy. Redis is a
queue/cache and is not the system of record.

For a coordinated backup of the local production candidate, use a new empty
directory:

```bash
uv run python scripts/compose_backup.py --output test-results/backups/2026-08-11T1200
uv run python scripts/compose_backup.py \
  --output test-results/backups/2026-08-11T1200 \
  --verify-only
```

Use a new empty directory for every run. The command pauses API, worker and the
singleton scheduler, lists and downloads every S3 object to produce a
key/size/SHA-256/media-type/immutable-metadata inventory, records the PostgreSQL
timestamp, WAL LSN, exact per-table row counts and Alembic head, and makes
a custom-format database dump. It then stops SeaweedFS cleanly, archives its
quiescent volume using a digest-pinned helper image, restarts SeaweedFS and
confirms that its complete inventory is unchanged before application writers
are resumed. Restart and unpause operations run across failure paths and report
any recovery error explicitly.

The v4 manifest binds the exact repository-built SeaweedFS tag and image ID,
source-manifest SHA, Git revision, database counts and checksums for both backup
payloads and every S3 object. Legacy manifests deliberately fail verification
because they do not contain sufficient recovery evidence.
Existing backup directories and source volumes are never overwritten or
deleted. Run this during a maintenance window that also prevents use of
previously issued direct S3 upload URLs. This local mechanism is not a substitute
for encrypted, versioned/offline platform backups. On POSIX hosts the command
forces the backup directory to mode `0700` and every dump, archive and manifest
to `0600`, even under a permissive `022` umask.

Run the fail-closed freshness probe from the platform scheduler and alert on any
non-zero exit status:

```bash
uv run python scripts/backup_freshness.py \
  --root /var/backups/custombuild \
  --max-age-hours 24 \
  --verify-payloads \
  --output /var/lib/custombuild/backup-freshness.json
```

The probe returns `OK`, `MISSING`, `STALE` or `INVALID` together with a concrete
operator action. Freshness is the age of the PostgreSQL recovery point recorded
in `database_snapshot.captured_at`, not the later manifest completion time. Store
backups encrypted off-host; the checked-in Compose stack does not provide
scheduling, encryption, off-site replication or alert delivery.

## Restore

1. Stop API and workers.
2. Restore PostgreSQL into an isolated environment.
3. Restore the matching object-store snapshot.
4. Compare the restored Alembic revision with both the backup and repository
   head; run an explicitly reviewed migration only when restoring an older
   compatible backup.
5. Boot the exact manifest-pinned SeaweedFS image against the restored volume,
   then list and download every object and verify key, size, SHA-256, media type
   and immutable metadata against the manifest.
6. Start one worker, let queued/idempotent jobs settle, then scale workers.
7. Run tenant-isolation and seeded acceptance probes before reopening traffic.

The disposable local database/object-volume restore probe is:

```bash
uv run python scripts/restore_drill.py \
  --backup test-results/backups/2026-08-11T1200 \
  --output test-results/backups/2026-08-11T1200/restore-drill.json
```

It verifies the v4 manifest and restores as the non-superuser
`custombuild_migrator`, so public tables, sequences and Alembic state retain the
correct owner. It requires exact per-table row counts, a real schema mutation,
safe role attributes and tenant RLS through both API and worker logins. It then
boots the manifest's exact SeaweedFS image ID on a random loopback-only port and
verifies the full S3 inventory by downloading it. The v3 restore evidence cannot
report `PASS` before these probes succeed. It removes only narrowly named
`custombuild-restore-<8 hex>` containers and volumes, including on failure. It
does not reopen traffic; tenant and HTTP acceptance remain mandatory after a
platform restore. Every Docker invocation, log/readiness probe, large payload
restore and cleanup attempt has an explicit timeout. A hung inspection still
triggers one separately bounded removal attempt for that exact validated name,
then cleanup proceeds to the remaining disposable resources.

## Secrets and observability

The checked-in `compose.yml` is a loopback-only local candidate and defaults to
development authentication. It is not an Internet-production configuration.
For an externally reachable deployment, render both Compose files and supply
every required value from a secret manager:

```bash
docker compose -f compose.yml -f compose.external-production.yml config --quiet
python scripts/check_external_production.py --repo .
```

`compose.external-production.yml` refuses missing OIDC settings, release
identity, four-eyes approval enforcement and secrets. API, web and artifact
ports remain loopback-only so a
separately reviewed HTTPS reverse proxy can expose them; PostgreSQL and Redis
remain unpublished. The checker validates the rendered configuration without
printing secret values. IdP configuration, certificates, secret rotation and
proxy operation remain deployment-platform evidence and cannot be proven by
this repository alone. Set `TRUSTED_PROXY_CIDRS` to only the private network(s)
used by that reviewed proxy. The proxy must replace `X-Forwarded-For` with one
canonical client IP; untrusted peers and ambiguous forwarding chains deliberately
fall back to the socket peer's shared rate-limit bucket.

Inject database, OIDC and object-store secrets through the deployment secret
manager; never commit `.env`. Export structured logs and OpenTelemetry at the
platform layer. Alert on repeated job failures, RLS violations, artifact hash
mismatches and attempted release with blocking rules.

The API exposes `/health` for liveness and `/ready` for bounded PostgreSQL,
authenticated Redis and configured S3-bucket checks. Every response includes a
validated `X-Request-ID`; JSON logs include
the same value, method, path, status and duration so ingress, API and worker
events can be correlated.

## Worker and scheduler topology

Celery workers execute jobs only. Periodic outbox dispatch and recovery run in the
separate `scheduler` service, which is the single beat instance for one environment.
Never add `--beat` to a worker command when scaling workers; doing so creates duplicate
schedulers. Scale with `docker compose up -d --scale worker=3 worker` while keeping
exactly one `scheduler` service. The scheduler healthcheck verifies that its persistent
schedule file is being refreshed.

Worker S3 requests use explicit connect/read timeouts and two total attempts. A
stalled object store therefore becomes a generic, actionable job error well before
the server-owned generation deadline; provider details and private endpoints are not
persisted in the job error.

Compose assigns explicit CPU, memory and positive PID limits to every long-running
service, including PostgreSQL, Redis and SeaweedFS. The release-readiness check reads
the resolved Compose model and fails closed if any limit is missing. Treat the
checked-in values as safe local defaults, then load-test and review them for the target
platform rather than silently removing them.

## Release identity and promotion

Local default builds deliberately carry an `APP_VERSION` ending in `-local`,
`VCS_REF=uncommitted`, and unknown build/source/lock identity. They are suitable for
quick local verification, not promotion. `VCS_REF` always means the full Git commit;
it must not be replaced by an ad hoc working-tree hash. `SOURCE_MANIFEST_SHA256`
separately identifies the exact shared application sources consumed by the three
Dockerfiles: root dependency manifests plus `apps/web`, `packages`, `cad`, `cam`,
`postprocessors`, `services`, and `scripts`. It includes uncommitted files and applies
`.dockerignore` (notably excluding the live `apps/web/e2e` harness), while intentionally
excluding deployment-only Compose/environment files so one tested image can be promoted
between isolated environments. The canonical manifest contains sorted paths, file
types, normalized portable permission modes, sizes and content hashes, with no host,
clock or filesystem timestamp metadata.

Freeze a local candidate only after all source edits are complete. On PowerShell:

```powershell
$env:VCS_REF = (git rev-parse HEAD)
$env:SOURCE_MANIFEST_SHA256 = (python scripts/source_manifest.py --repo . --output artifacts/source-manifest.json --sha256-output artifacts/source-manifest.sha256)
python scripts/source_manifest.py --repo . --expect-sha256 $env:SOURCE_MANIFEST_SHA256
```

The equivalent POSIX shell command is:

```bash
export VCS_REF="$(git rev-parse HEAD)"
export SOURCE_MANIFEST_SHA256="$(python3 scripts/source_manifest.py --repo . --output artifacts/source-manifest.json --sha256-output artifacts/source-manifest.sha256)"
python3 scripts/source_manifest.py --repo . --expect-sha256 "$SOURCE_MANIFEST_SHA256"
```

`artifacts/` is ignored by Docker, so writing the evidence does not recursively change
the manifest. The verification command is the source-freeze gate: rerun the freeze and
rebuild every application image if it fails.

CI sets `APP_VERSION`, the full commit `VCS_REF`, `BUILD_DATE`, `SOURCE_URL`,
`SOURCE_MANIFEST_SHA256`, and `DEPENDENCY_LOCK_SHA256` (the SHA-256 of `uv.lock`) for
Python images. Web images separately bind `FRONTEND_LOCK_SHA256` to `pnpm-lock.yaml`.
These values are stored as runtime environment and OCI labels; all shared backend
identity values are also stored in the hashed production engine context nested in
every generated manifest. API/worker disagreement therefore blocks generation before
artifacts are written. The external-production preflight also recomputes the manifest,
checks `VCS_REF` against Git `HEAD`, and rejects any configured mismatch.

The source identity is deliberately separate from the OCI content digest. CI records
the exact candidate digest in the image manifest. Promotion must deploy that digest,
not rebuild or resolve a mutable tag, and the deployment platform must compare the
running container digest with the approved manifest before accepting runtime traffic.

Before promotion, require a clean reviewed commit and the final readiness report
that binds static controls, Compose design-review acceptance, the exact running
image IDs and their scans, coordinated backup v4 and restore evidence v3 to the
same Git/source-manifest identity. Also retain SBOMs and environment-specific
configuration approval. Signature and platform provenance remain
deployment-platform responsibilities until a registry and trust root have been selected.

Register the web application as an OIDC public client with Authorization Code and
PKCE (`S256`). The callback URI must be the public web origin's exact root (for
example `https://app.example.com/`); no separate `/callback` route is implemented.
Configure the same issuer in the API and web build. Discovery authorization and
token endpoints must stay on that issuer's HTTPS origin, and no browser client
secret may be provided. Before allowing a CAM reviewer to
lock a revision, confirm that the job exposes `operations`,
`validation_backplot` and at least `setup_sheet_001` as separate evidence
artifacts. A locked review package must still be treated as validation-only under
`PRODUCTION_SAFETY.md`.

## Optional FreeCAD worker

FreeCAD is not required for the authoritative CadQuery/OpenCascade production path.
To let reviewers request a native, non-authoritative `.FCStd` derivative, build the
worker with the optional runtime and recreate that service:

```bash
INSTALL_FREECAD=true docker compose build worker
INSTALL_FREECAD=true docker compose up -d worker
```

The supplied Compose stack enables it by default. Set `INSTALL_FREECAD=false` for
smaller workers that do not offer FCStd export. If a
reviewer requests FreeCAD on such a worker, the generation job deliberately fails with
an actionable dependency error. A successful request must expose `design_fcstd` and a
`validation/cad-interchange-status.json` document whose status is `GENERATED`; CAM
approval is rejected when either evidence item is absent. The status document must pin
the runtime version and source STEP checksum. Never feed an edited FCStd back into
machine operations.
