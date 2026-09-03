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

Use a new empty directory for every run. The command pauses API and the singleton
scheduler, then gives the worker its full task budget to drain before stopping it.
With every writer quiesced it runs the one-shot storage recovery gate, requires a
new database-bound capacity attestation, and only then pauses the attestor for the
capture. It lists and downloads every S3 object to produce a
key/size/SHA-256/media-type/immutable-metadata inventory, records the PostgreSQL
timestamp, WAL LSN, exact per-table row counts and Alembic head, and makes a
custom-format database dump. It then stops SeaweedFS cleanly, archives its
quiescent volume using a digest-pinned helper image, restarts SeaweedFS and
confirms that its complete inventory is unchanged. A second exact attestation is
required before the worker is started and the scheduler and API are unpaused, in
that order. Any failed or ambiguous gate leaves application writers stopped or
paused; a partial restart is rolled back and reported explicitly.

The v5 manifest binds the exact repository-built SeaweedFS tag and image ID,
source-manifest SHA, Git revision, database counts and checksums for both backup
payloads and every S3 object. Its
`database_snapshot.tombstone_history` is
`custombuild.storage-tombstone-history.v1`: an exact row count plus SHA-256 over
the complete, C-sorted retired-key history. The hash covers bucket, object key,
tenant/project, object digest and size, media type, owner identity, idempotency
identity, accounting origin, reaper claim token and UTC retirement timestamp.
The count must equal the exact `storage_object_tombstones` table count. Legacy
manifests deliberately fail verification because they do not contain sufficient
recovery evidence.
Existing backup directories and source volumes are never overwritten or
deleted. Run this during a maintenance window that also prevents new uploads or
generation work from reaching an application writer. This local mechanism is not a substitute
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

Treat `database.dump`, `artifacts.tar` and their v5 manifest as one indivisible
recovery point. Never restore or rewind PostgreSQL independently of its paired S3
snapshot, or vice versa: an independently rewound database can forget a retired
key and permit reuse, while an independently rewound bucket can resurrect retired
bytes or omit bytes still referenced by the database. Keep every
`storage_object_tombstones` row permanently. UPDATE,
DELETE, TRUNCATE, retention expiry, partition drop and "tombstone trimming" are
forbidden maintenance operations; offline copies supplement but never replace
the live registry. Include its monotonic growth in database capacity planning.

## Restore

The digest-pinned database runtime is PostgreSQL 18. Never attach it to a
PostgreSQL 17 data volume. For an upgrade, freeze the PostgreSQL 17 deployment
and create its logical custom-format backup before changing the image. Restore
that backup into a fresh PostgreSQL 18 volume, where indexes and collation data
are rebuilt by the logical restore, then require the complete restore drill and
tenant acceptance probes before traffic resumes. Keep the old volume read-only
until the new recovery point has been independently accepted.

1. Stop API, the singleton scheduler and every worker.
2. Restore PostgreSQL into an isolated environment.
3. Restore the matching object-store snapshot.
4. Compare the restored Alembic revision with both the backup and repository
   head; run an explicitly reviewed migration only when restoring an older
   compatible backup.
5. Boot the exact manifest-pinned SeaweedFS image against the restored volume,
   then list and download every object and verify key, size, SHA-256, media type
   and immutable metadata against the manifest.
6. Require the restored tombstone count and exact history SHA-256 to equal the
   v5 manifest before any writer starts. Also require the restored ACL proof:
   API and worker have no tombstone privilege, and the attestor has SELECT only.
7. Run the exact `storage-recovery` one-shot with the migrator role. Require a
   successful exit for the restored PostgreSQL boot and object-store snapshot;
   never bypass or manually clear its maintenance gate.
8. Start the dedicated storage-capacity attestor and require a fresh heartbeat
   bound to the restored database evidence and exact bucket inventory.
9. Compare the restored `joint_retention_registry_state` epoch, exact canonical
   registry SHA-256 and operator-reference SHA-256 with the latest approved
   off-database change record. A database backup and deployment secret can be
   rolled back together, so the database high-water mark alone cannot detect
   that paired rollback. Keep API/workers stopped if the restored epoch or digest
   is older or unknown; review and explicitly activate the latest monotonic
   registry before continuing. Never edit the singleton row directly.
10. Start one worker, let queued/idempotent jobs settle, then scale workers.
11. Run tenant-isolation and seeded acceptance probes before starting the
   scheduler and reopening API traffic.

The disposable local database/object-volume restore probe is:

```bash
uv run python scripts/restore_drill.py \
  --backup test-results/backups/2026-08-11T1200 \
  --output test-results/backups/2026-08-11T1200/restore-drill.json
```

It verifies the v5 manifest and restores as the non-superuser
`custombuild_migrator`, so public tables, sequences and Alembic state retain the
correct owner. It requires exact per-table row counts, a real schema mutation,
the exact tombstone-history count and SHA-256, safe role/ACL attributes and
tenant RLS through both API and worker logins. It then boots the manifest's exact
SeaweedFS image ID on a random loopback-only port and verifies the full S3
inventory by downloading it. The v4 restore evidence records
`database_tombstone_history` and cannot set
`database_tombstone_history_verified=true` or report `PASS` before the restored
history is byte-for-byte equal to the backup proof. It removes only narrowly named
`custombuild-restore-<8 hex>` containers and volumes, including on failure. It
does not reopen traffic; tenant and HTTP acceptance remain mandatory after a
platform restore. Every Docker invocation, log/readiness probe, large payload
restore and cleanup attempt has an explicit timeout. A hung inspection still
triggers one separately bounded removal attempt for that exact validated name,
then cleanup proceeds to the remaining disposable resources.

### Joint-retention registry rollout and rollback

The registry JSON stays at
`custombuild.joint-retention-trust-registry.v1`; activation does not rewrite the
file or existing revision snapshot hashes. Migration `0018` creates one global
singleton because the same registry controls every tenant and both runtimes.
Tenant-scoped state would leave dormant tenants vulnerable to an older registry.

Every production rollout follows this order:

1. Validate and independently approve the candidate registry and SHA-256.
2. Ensure every deployed and rollback image understands revision `0018`, then
   stop or gate generation and physical-release writers.
3. Migrate the database and run `scripts.activate_joint_retention_registry` once
   with the protected registry file and a non-secret change reference.
4. Put the exact same bytes in both API and worker secret configuration, deploy
   both, and require the `joint_retention_registry` readiness dependency to be
   `ok` before reopening work.
5. Preserve the CLI output, registry bytes and digest in the external change
   record. Do not treat an application log or development/SQLite run as proof.

Production readiness requires both that exact activated registry and at least
one certifier key whose validity window includes the current instant and whose
revocation has not taken effect. This proves policy availability only. It does
not prove that an external certifier is currently engaged or that valid signed
evidence exists for any particular design. Development skips this production
proof and must not report that skip as certification or physical authorization.

Activation is one-way. Exact retries are idempotent; revocations can only grow,
existing keys cannot be removed or rewritten, and a set `revoked_at` cannot be
cleared or moved later; it may move earlier to tighten a compromise response.
The exclusive activation lock waits for in-flight shared runtime assertions,
so no request can silently cross the policy transition.
There is no runtime auto-pin path and neither API nor worker has table or install-
function privileges.

An application-image rollback is allowed only when that image supports `0018`
and is configured with the currently activated registry. Do not roll the registry
or database high-water row backward to accommodate an older image. Alembic
downgrade is blocked after first activation. If disaster recovery restores an
older database, compare it to the independent latest change record and activate
the latest reviewed monotonic registry while all writers remain stopped. If the
latest external record or exact bytes are unavailable, production retention
readiness remains closed.

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

The web image contains no deployment-specific API, demo-token or OIDC build
arguments. At request time the Next server reads only the explicitly public
`CUSTOMBUILD_WEB_API_URL`, `CUSTOMBUILD_WEB_DEMO_TOKEN`,
`CUSTOMBUILD_WEB_OIDC_ISSUER`, `CUSTOMBUILD_WEB_OIDC_CLIENT_ID` and
`CUSTOMBUILD_WEB_OIDC_REDIRECT_URI` values, validates them and passes that exact
allow-list to the browser. `APP_ENV=production` requires an HTTPS API origin, a
complete HTTPS OIDC public-client tuple and an empty demo token. Invalid runtime
configuration fails the document request and container healthcheck; it never
falls back to local authentication. Do not place a client secret, database URL,
object-store credential or other private value in any `CUSTOMBUILD_WEB_*` value.
Changing these environment values must not rebuild the image, which allows the
same tested web digest to be promoted between environments.

Inject database, OIDC and object-store secrets through the deployment secret
manager; never commit `.env`. Export structured logs and OpenTelemetry at the
platform layer. Alert on repeated job failures, RLS violations, artifact hash
mismatches and attempted release with blocking rules. Also alert on storage
reservation/reaper lease exhaustion, identity mismatch, a failed tombstone
finalization, any live-ledger/tombstone overlap, tombstone-history drift and a
failed startup recovery or capacity attestation. A reaper failure is not a reason
to edit ledger state manually: leave the key fenced in `reaping`, correct the
provider/database cause and let the token-bound recovery path reclaim it.

Provision `CAPACITY_ATTESTOR_DATABASE_USER=custombuild_storage_attestor` with a
separate `CAPACITY_ATTESTOR_DATABASE_PASSWORD`; the attestor URL must use that
exact login and secret. PostgreSQL init scripts provision and rotate it on a new
cluster. They do not rerun for an existing data directory, so before applying
migration `0013_storage_quota_security_functions` to an older cluster, a
bootstrap administrator must create (or harden and rotate) that fixed role as
`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
NOBYPASSRLS` and remove every membership to or from it. The migration fails
closed when the role is absent and then rebuilds its exact table/function ACL;
never substitute `MIGRATION_DATABASE_URL` for the attestor URL.

Compose deliberately orders storage startup as `migrate` → `storage-recovery`
→ `storage-capacity-attestor` → writers. `storage-recovery` is a hardened
one-shot API-image process that reuses `MIGRATION_DATABASE_URL`, connects only
to the internal object store and exits. A nonzero recovery exit keeps the
attestor, API, worker and scheduler stopped; do not bypass the
`service_completed_successfully` dependency or restart it as a daemon. The
long-running attestor always retains its separate least-privilege URL.

PostgreSQL deliberately uses `restart: "no"`: a container-runtime restart after
a database crash would otherwise revive a new database boot behind an already
completed recovery barrier. Never use `docker start` on the PostgreSQL container.
Recover or replace it only during explicit downtime. First quiesce every process
with a database credential, then select the complete recovery/writer graph.
Compose evaluates the `depends_on` completion and health conditions within the
selected graph; a command that targets `postgres` alone does not select or
restart its dependants. Production recovery must reuse the exact verified image
environment, external-production controls, registry overlay and secret-manager
environment of the running deployment. For the digest-only deployment described
below, create and independently review a new canonical capacity operator config
at a new protected path immediately before recovery. Preserve every volume,
capacity, bucket and deploy-descriptor value, change only `requested_at` to the
current whole-second UTC time, then export the new
`STORAGE_CAPACITY_OPERATOR_CONFIG_PATH` and
`STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256`. Do not rewrite or reuse the running
deployment's stale request: every new production attestor requires this fresh,
hash-bound operator authorization. With that environment in place, the complete
operation is:

```bash
production_compose=(
  docker compose
  --env-file artifacts/deploy-images.env
  --file compose.yml
  --file compose.external-production.yml
  --file compose.registry.yml
)
"${production_compose[@]}" stop --timeout 60 api worker maintenance-worker storage-reaper-worker scheduler storage-capacity-attestor
"${production_compose[@]}" up --no-build --detach --force-recreate \
  postgres migrate storage-recovery storage-capacity-attestor api worker maintenance-worker storage-reaper-worker scheduler
"${production_compose[@]}" up --no-build --detach --wait --wait-timeout 900
```

If recovery fails permanently it remains exited without a restart loop and the
writers remain unavailable; diagnose and correct the cause before repeating
the complete operation. Never omit a listed database client, recovery service,
writer, Compose file, verified image environment or `--no-build`.

The deploy descriptor must map both `storage-recovery` and
`storage-capacity-attestor` to the descriptor's exact digest-pinned API image;
neither role may use a separately rebuilt tag. Before deployment, create
`STORAGE_CAPACITY_OPERATOR_CONFIG_PATH` as strict UTF-8 canonical JSON with
sorted keys and compact separators. It must contain exactly
`schema_version`, `volume_identity`, `provisioned_bytes`,
`metadata_overhead_bytes`, `emergency_reserve_bytes`, `headroom_bytes`,
`byte_limit`, `object_limit`, `bucket`, `deploy_descriptor_sha256` and
`requested_at`; the schema is `custombuild.storage-capacity-operator.v1` and
`requested_at` is a whole-second UTC `...Z` timestamp. Hash the canonical bytes
without a trailing newline using SHA-256 and set
`STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256` to that lowercase digest. All mirrored
environment values must match byte-for-byte. The request expires after ten
minutes (with at most 30 seconds of future clock skew), so regenerate and review
it immediately before each deployment, database recovery/replacement or explicit
capacity change.

The API exposes `/health` for liveness and `/ready` for bounded PostgreSQL,
authenticated Redis and configured S3-bucket checks. Every response includes a
validated `X-Request-ID`; JSON logs include
the same value, method, path, status and duration so ingress, API and worker
events can be correlated.

## Worker and scheduler topology

The `worker` service consumes only the `generation` queue. The singleton
`maintenance-worker` consumes only the time-critical `maintenance` queue and executes
transactional outbox dispatch plus stale-lease recovery. The separate singleton
`storage-reaper-worker` consumes only `storage-reaper`, so slow or unavailable S3
operations cannot head-of-line block job dispatch or lease recovery. The singleton
`scheduler` is beat-only: it schedules each periodic task onto its explicit queue but
executes none of them. Every worker healthcheck verifies the exact active queue as
well as Celery responsiveness. Unknown tasks route to an intentionally unconsumed
`unrouted` queue, so a missing route fails closed instead of silently entering a
worker pool.

Never add `--beat` to a worker command. Scale generation with
`docker compose up -d --scale worker=3 worker`; keep exactly one
`maintenance-worker`, one `storage-reaper-worker` and one `scheduler`. Before
upgrading an environment that used
the legacy default `celery` queue, pause API and beat, drain that queue completely
with the old worker image, and then replace all three Celery roles atomically. An
already-dispatched legacy message has no database outbox row to republish, so leaving
the old queue behind is not a safe cutover.

Retryable generation failures are requeued through the same PostgreSQL transaction
that records `queued`. `outbox_events.available_at` preserves storage-lease backoff,
and failed Redis publication remains pending with bounded exponential delay rather
than being dead-lettered during an ordinary broker outage. Duplicate publication is
safe because claiming and completion are fenced by job identity, attempt budget,
lease token and deadline.

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
`.dockerignore` (notably excluding the live `apps/web/e2e` harness). Reviewed Compose
and workflow controls are included, while deployment environment values are not; the
same tested image can therefore be promoted between isolated environments without
rebuilding. The canonical manifest contains sorted paths, file types, normalized
portable permission modes, sizes and content hashes, with no host, clock or filesystem
timestamp metadata.

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
image IDs and their scans, coordinated backup v5 and restore evidence v4 to the
same Git/source-manifest identity. Also retain SBOMs and environment-specific
configuration approval.

### Digest-only deployment descriptor

`scripts/deploy_descriptor.py` is the repository-owned boundary between completed
release evidence and `.github/workflows/cd.yml`. It renders canonical
JSON for exactly four application images (`api`, `worker`, `web`, `seaweedfs`) plus
the reviewed PostgreSQL, Redis and volume-initializer digests. The descriptor also
binds the full source commit, source-manifest digest, final release-evidence digest,
workflow run/attempt, exact Compose service-to-image roles and the expected GitHub
OIDC/Cosign and artifact-attestation identities. Tags, additional fields, duplicate
JSON keys, non-canonical bytes, substituted component digests and evidence from a
different run are rejected.

Rendering and verification require the same trusted values. The release workflow
supplies them directly from GitHub and the completed release evidence. Its read-only
quality, build and exact-image acceptance jobs run for pull requests targeting
`main`, so the full candidate path is proven before merge. Only a direct push to
`main` in this repository can enter GHCR publication or descriptor generation;
`workflow_run`, fork credentials and PR write authority are absent. Four unprivileged
matrix jobs build the application images once, scan them and
archive those exact Docker objects. A separate unprivileged job loads the archives,
starts Compose with `--no-build`, and captures same-commit browser, WCAG, live
design-review/CAM blocking, backup, restore, SBOM and vulnerability evidence. Only
after that gate passes may the four least-privilege publication jobs load the same
archives and push them to GHCR. The archive SHA-256 proves the bytes transferred
between jobs; the image config digest links archive load, running containers, scans
and the raw GHCR manifest; the registry manifest digest is the independent immutable
subject used by signatures, attestations, the descriptor and deployment. These three
identities are never compared as though they were interchangeable. In particular,
the daemon-local scan manifest digest is not a GHCR manifest digest. For pinned
multi-platform runtimes the evidence separately binds the deployment index digest to
its unique `linux/amd64` child manifest and that child's config digest. The workflow
then creates both a keyless Cosign signature and a GitHub artifact attestation with
the exact workflow identity, and hashes each canonical component publication record
into descriptor v2.

The final read-only job renders and verifies `verified-promotion-input-<sha>-<run>-<attempt>`.
That artifact contains the canonical descriptor, checksum, final software release
evidence and non-secret digest-only Compose environment. The following commands show
the same verifier boundary for an operator audit:

```bash
python3 scripts/deploy_descriptor.py render \
  --repository pilotens/Custombuild \
  --git-revision "$VCS_REF" \
  --source-manifest-sha256 "$SOURCE_MANIFEST_SHA256" \
  --workflow-run-id "$GITHUB_RUN_ID" \
  --workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --release-evidence artifacts/release-evidence/release-readiness.json \
  --publication-directory artifacts/published \
  --api-image "ghcr.io/pilotens/custombuild-api@sha256:<digest>" \
  --worker-image "ghcr.io/pilotens/custombuild-worker@sha256:<digest>" \
  --web-image "ghcr.io/pilotens/custombuild-web@sha256:<digest>" \
  --seaweedfs-image "ghcr.io/pilotens/custombuild-seaweedfs@sha256:<digest>" \
  --output artifacts/deploy-descriptor.json \
  --sha256-output artifacts/deploy-descriptor.sha256

python3 scripts/deploy_descriptor.py verify \
  --repository pilotens/Custombuild \
  --git-revision "$VCS_REF" \
  --source-manifest-sha256 "$SOURCE_MANIFEST_SHA256" \
  --workflow-run-id "$GITHUB_RUN_ID" \
  --workflow-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --release-evidence artifacts/release-evidence/release-readiness.json \
  --publication-directory artifacts/published \
  --compose-env-output artifacts/deploy-images.env \
  artifacts/deploy-descriptor.json
```

Only the verified command may emit `deploy-images.env`. Use it with the registry
overlay and the external-production controls, and keep `--no-build` on the actual
`up` invocation:

```bash
docker compose --env-file artifacts/deploy-images.env \
  -f compose.yml -f compose.external-production.yml -f compose.registry.yml \
  config --quiet
docker compose --env-file artifacts/deploy-images.env \
  -f compose.yml -f compose.external-production.yml -f compose.registry.yml \
  up --no-build --detach --wait
```

The overlay deliberately contains no registry credentials or deployment secrets.
The workflow publishes, signs and attests images, but deliberately performs no
deployment. Repository branch protection, a protected production environment,
hosting, secrets, ingress and rollback ownership must be configured and reviewed in
GitHub and on the target platform before an operator may use the artifact. If GHCR
package creation or `GITHUB_TOKEN` package permission is disabled, publication fails
closed and that repository-administration blocker must be resolved externally.
Neither a successful software release nor its descriptor grants commercial release
authority or physical-machine authorization; both remain explicitly `false`.

### Bootstrap the first production OIDC administrator

Migrations intentionally create no organization, user or membership, and the API
never auto-provisions a signed OIDC subject. Before the first API start, provision
one exact `owner` or `admin` with the production image's one-shot CLI. There is no
HTTP bootstrap route and development demo tokens are not accepted by this command.
OIDC production readiness remains closed with
`ProductionIdentityBootstrapRequiredError` until at least one issuer-bound identity
exists for the exact configured issuer. It also fails closed if any identity is
still unbound or is bound to another issuer; one deployment therefore cannot
silently mix identity-provider namespaces.

First record two newly generated, canonical UUIDs for the organization and user.
Read the subject and lowercase email from the production identity provider; the
subject is case-sensitive and must not be guessed from the email. Choose and record
the immutable organization slug and an operator-controlled change-ticket reference.
Put them in a process-owned private request file; do not put identity values in
shell arguments, environment variables or a heredoc:

```bash
umask 077
export IDENTITY_REQUEST="$PWD/initial-production-identity.json"
install -m 600 /dev/null "$IDENTITY_REQUEST"
${EDITOR:?Set EDITOR} "$IDENTITY_REQUEST"
test "$(stat -c '%a:%u' "$IDENTITY_REQUEST")" = "600:$(id -u)"
```

The file is strict JSON with exactly these fields. Every `__REPLACE_...__` value
is deliberately invalid and must be replaced before execution:

```json
{
  "schema_version": "custombuild.production-identity-bootstrap.v1",
  "organization_id": "__REPLACE_ORGANIZATION_UUID__",
  "organization_slug": "__REPLACE_ORGANIZATION_SLUG__",
  "organization_name": "__REPLACE_ORGANIZATION_NAME__",
  "user_id": "__REPLACE_USER_UUID__",
  "oidc_issuer": "__REPLACE_HTTPS_OIDC_ISSUER__",
  "oidc_subject": "__REPLACE_EXACT_OIDC_SUBJECT__",
  "email": "__REPLACE_LOWERCASE_EMAIL__",
  "user_name": "__REPLACE_USER_DISPLAY_NAME__",
  "role": "__REPLACE_WITH_owner_OR_admin__",
  "operator_reference": "__REPLACE_CHANGE_TICKET_REFERENCE__"
}
```

While writers remain stopped and after the database is migrated to the image's
Alembic head, read only the non-secret issuer into the deployment environment and
mount the protected file read-only. Running the container as the file owner lets
the CLI verify both ownership and mode:

```bash
export OIDC_ISSUER="$(python -c \
  'import json,os; print(json.load(open(os.environ["IDENTITY_REQUEST"]))["oidc_issuer"])')"

docker compose --env-file artifacts/deploy-images.env \
  -f compose.yml -f compose.external-production.yml -f compose.registry.yml \
  run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$IDENTITY_REQUEST:/tmp/custombuild-initial-identity.json:ro" \
  -e AUTH_MODE=oidc -e OIDC_ISSUER="$OIDC_ISSUER" \
  migrate python -m scripts.bootstrap_production_identity \
  --request-file /tmp/custombuild-initial-identity.json \
  --confirm-initial-admin
```

The command has no identity defaults. Inject `MIGRATION_DATABASE_URL` through the
normal deployment secret environment and never put that URL or its password on the
command line. The CLI refuses symlinks, relative paths, non-regular files, files not
owned by its process user, group/world permissions and files other than mode `0400`
or `0600`. It requires `APP_ENV=production`, the exact password-authenticated
`custombuild_migrator` role, no role memberships or privilege drift, the current
schema head, an exact `pg_catalog,public` search path, correctly owned identity
tables and an HTTPS issuer byte-for-byte equal to `OIDC_ISSUER`. It sets the tenant
RLS context, takes a transaction-scoped bootstrap lock and commits the organization,
user, membership and audit event atomically. Runtime lookup stores and checks an
opaque SHA-256 key over the exact `(issuer, subject)` pair, so an equal subject from
a different issuer cannot inherit the provisioned role.

A successful first run prints only the recorded database IDs, role and `created`.
Repeating the exact command prints `unchanged` and creates no rows. Any changed
UUID, slug, name, issuer, subject, email, role or operator reference is refused;
the initial-admin mode never renames, rebinds or promotes an existing identity.
The audit explicitly records that the
operation was performed by the external bootstrap operator and that its required
`actor_id` is the provisioned target rather than the operator; issuer, subject and
operator reference are stored only as SHA-256 bindings. Preserve the private request
and output in an encrypted, access-controlled deployment record if exact replay is
required; otherwise remove the request using the organization's approved secret-file
cleanup procedure and unset `IDENTITY_REQUEST` and `OIDC_ISSUER`. Never weaken its
permissions to work around a rootless/non-Linux bind-mount mapping: copy it into a
private mounted volume with the numeric process owner and verify the in-container
mode instead. If a failed run reports conflicting or partial identity state,
leave writers stopped and investigate the database/audit history instead of deleting
or editing rows manually.

Finally configure the IdP token mapping so its signed `organization_id` and `role`
claims exactly equal the bootstrapped values (and, when emitted, `user_id` equals the
recorded user UUID). The API continues to fail closed when any subject, tenant,
user or role claim differs from the provisioned membership.

Production role separation cannot reuse the initial administrator. Create a second
private request file with the exact same organization fields and issuer but a new
user UUID, subject, email and name with role `designer`. Create two further files
for two different people, each with role `reviewer`: one will perform design
approval and the other CAM approval. Run all three through the same verified image,
mount and environment command, replacing only the final confirmation flag with:

```text
--confirm-additional-member
```

This mode accepts only `designer` or `reviewer`, requires the exact organization to
already contain an `owner` or `admin` bound to that same issuer, and atomically
creates one issuer-bound user, membership and separately identified audit event.
Exact retries are no-ops; partial rows, reused IDs/subjects/emails and role drift
are refused. Confirm that the IdP
maps the three distinct signed subjects to their exact user, organization and role
claims. The design-approval and CAM-approval reviewer user IDs must remain distinct;
`designer` has no review capability, and possession of the initial owner credential
is not a substitute for either named reviewer.

### Upgrade a legacy or unmarked OIDC identity

Migration `0017_oidc_issuer_binding` adds a nullable issuer-hash marker without
changing existing `oidc_sub` values. The current runtime treats a marked `oidc_sub`
as an opaque issuer+subject binding. Production readiness explicitly fails with
`LegacyUnscopedOIDCIdentityError` while any older row has no marker, and authentication
returns a clear service-unavailable binding error for an exact legacy raw subject or
an exact pre-marker opaque key.
The schema upgrade therefore cannot silently make such a row authenticate under a
replacement issuer or merely turn it into an unexplained 403.

Before starting the upgraded API, prepare one protected request file per distinct
unmarked user that belongs to exactly one organization. Its organization, user, raw
provider subject, email, name and current role must
exactly match the existing row and membership; `oidc_issuer` must be the issuer that
originally authenticated that subject. Run the same command with:

```text
--confirm-legacy-issuer-binding
```

This migration mode accepts both a legacy raw `oidc_sub` and an already opaque but
unmarked `oidc_sub`. It accepts every existing application role but creates no user,
organization or membership and changes no role. It only replaces the exact raw
subject with the opaque issuer-scoped key, sets the issuer-hash marker and adds a
hash-bound external-operator audit event in the same transaction. A byte-identical
retry is a no-op; missing, ambiguous, already conflicting or partially audited state
is refused. Repeat for
each raw-subject user until the database readiness probe passes. Preserve the
pre-upgrade backup and the private requests under the normal change-control policy;
never mass-update `oidc_sub` with ad-hoc SQL. A global user with memberships in
multiple organizations is deliberately refused because one global authentication
mutation cannot be truthfully authorized and audited from a single tenant context;
escalate that case for a separately reviewed all-tenant migration.

After any legacy binding, Alembic downgrade from `0017` is deliberately blocked:
the one-way opaque key cannot reconstruct the raw provider subject required by the
older runtime. Roll back application and schema only by restoring the approved
pre-binding backup, then verify its Alembic head and identity row counts before
starting the older application. A downgrade remains available on a clean schema
where every issuer marker is still `NULL`.

Register the web application as an OIDC public client with Authorization Code and
PKCE (`S256`). The callback URI must be the public web origin's exact root (for
example `https://app.example.com/`); no separate `/callback` route is implemented.
Configure the same issuer in the API and web runtime. Discovery authorization and
token endpoints must stay on that issuer's HTTPS origin, and no browser client
secret may be provided. Before allowing a CAM reviewer to
lock a revision, confirm that the job exposes `operations`,
`validation_backplot` and at least `setup_sheet_001` as separate evidence
artifacts. A locked review package must still be treated as validation-only under
`PRODUCTION_SAFETY.md`.

## Trusted offline package verifier

Never execute a received production ZIP or anything contained in it. The ZIP is
untrusted data. Obtain `scripts/verify_production_package.py` separately from a
trusted, commit-pinned checkout or authenticated release channel. The file is the
standalone Python-standard-library verifier; the byte-parity gate keeps it identical
to the canonical reviewed implementation.

From that trusted checkout, record its SHA-256 and install the exact bytes into a
protected path outside any downloaded package:

```bash
EXPECTED_VERIFIER_SHA256="$(make -s trusted-package-verifier-sha256)"
sudo make install-trusted-package-verifier \
  TRUSTED_VERIFIER_DEST=/trusted/verify_production_package.py
ACTUAL_VERIFIER_SHA256="$(sha256sum /trusted/verify_production_package.py | awk '{print $1}')"
test "$ACTUAL_VERIFIER_SHA256" = "$EXPECTED_VERIFIER_SHA256"
```

Verify the ZIP as data, using project, revision and design hash obtained independently
from the authenticated order record:

```bash
python3 -I /trusted/verify_production_package.py package.zip \
  --expect-project-id '<project-id>' \
  --expect-revision '<revision>' \
  --expect-design-hash '<64-char-design-hash>'
```

Accept only exit code 0 and JSON `status: PASS`, and retain that JSON in the shop
review record. A pass proves internal manifest consistency and can detect accidental
corruption. It cannot detect a malicious coordinated rewrite of both the unsigned
manifest and payloads, authenticate Custombuild or an evidence issuer, establish
current evidence revocation/expiry, or authorize physical cutting, machining or
assembly. The expected-identity options compare unsigned manifest claims only; they
do not independently reconstruct design semantics or establish authenticity. Use the
authenticated server for those trust checks and the shop's own controlled release
process for any machine operation.

## Optional FreeCAD worker

FreeCAD is not required for the authoritative CadQuery/OpenCascade production path.
The hardened supplied worker deliberately excludes FreeCAD's operating-system package
graph. If a reviewer requests an FCStd derivative on that runtime, the generation job
fails closed with an actionable dependency error.

An organization that needs native, non-authoritative `.FCStd` derivatives must build
and vulnerability-scan a separate worker variant that provides a compatible headless
FreeCAD executable, then promote that exact image through the same SBOM and release
evidence gates. A successful request must expose `design_fcstd` and a
`validation/cad-interchange-status.json` document whose status is `GENERATED`; CAM
approval is rejected when either evidence item is absent. The status document must pin
the runtime version and source STEP checksum. Never feed an edited FCStd back into
machine operations.
