# Security model

- Tenant identity comes only from a verified bearer token. Request bodies and
  path parameters never supply a trusted organization identifier.
- Every tenant query contains an application-level organization predicate and
  PostgreSQL applies a second Row-Level Security policy using transaction-local
  `app.current_organization_id`.
- The API, migrator, worker and storage-attestor database roles are
  `NOSUPERUSER NOBYPASSRLS`. The long-running capacity attestor never uses the
  migrator login: `custombuild_storage_attestor` has no role memberships,
  receives SELECT only on `organizations` plus the four storage inventory
  tables (`storage_global_quotas`, `storage_tenant_quotas`, `stored_objects` and
  `storage_object_tombstones`) and can execute only the three fixed-search-path
  capacity functions. A narrow security-definer lock function gives it the
  global writer fence without granting table UPDATE.
  The short-lived `storage-recovery` startup service uses the migrator login
  only after migrations, exits before the attestor and all writers, and is
  never restarted as a daemon. PostgreSQL also disables container-runtime
  automatic restart; a deliberate Compose-managed database update propagates
  one ordered restart through migration, recovery, attestation and the writers.
  Its failure blocks the entire writer chain without a privileged restart loop.
  Worker transactions bind the persisted organization context before tenant
  reads or writes; a background process is not exempt from the database boundary.
  Every storage delete first binds the configured bucket to the attested ledger
  and binds provider `HEAD` metadata to the claim's exact SHA-256 and byte size.
  Deferred PostgreSQL constraint triggers require domain references to match a
  fully committed ledger identity (project, owner and idempotency key included).
  A separate immediate liveness trigger and KEY SHARE fence prevent generation-
  job retries or live leases from racing an active token-bound reaper claim.
  A successful reap atomically replaces the live ledger row with an append-only
  `storage_object_tombstones` identity before quota counters are released. The
  tombstone retains bucket, key, tenant/project, checksum, size, media type,
  owner, idempotency identity, accounting origin, claim token and retirement
  time without foreign keys or cascades. Its trigger rejects both UPDATE and
  DELETE, and reservations reject a key or idempotency identity already retired
  in the attested bucket. Reaping may be reclaimed after an expired reaper lease,
  but it can never be rebound to `reserved` or `committed`.
  Neither `custombuild_api` nor `custombuild_worker` has any table privilege on
  the tombstone registry; they can reach only their allow-listed fixed-search-path
  storage functions. The attestor has SELECT only on that exact five-table
  allow-list. This prevents a compromised runtime from erasing the anti-ABA
  history that those functions enforce.
- Roles gate design, review, production and release actions. Released revisions
  are immutable and changes create new revisions. Production additionally
  requires four-eyes approval: design and CAM reviews must come from different
  reviewer identities, and release revalidates that separation.
- Bearer authentication does not use ambient browser cookies, so cross-site
  requests cannot inherit authentication; CORS is allow-listed. If cookie-based
  sessions are introduced later, synchronizer-token CSRF protection is required.
- The production browser uses OIDC Authorization Code + PKCE as a public client.
  Its access token is retained only for the browser tab in `sessionStorage` and
  is removed on expiry or logout. No client secret is embedded in the web build
  or accepted by its public runtime-config allow-list. API/OIDC destinations are
  server-read when the container handles a request, so production configuration
  does not create a different image digest.
- Each document response gets a cryptographically random CSP nonce in the Next.js
  request proxy. Production permits inline framework scripts and style elements only
  through that nonce. Script attributes remain forbidden; style attributes are
  restricted to the single content-addressed positioning style emitted by Next
  Image's `fill` mode via `unsafe-hashes`, never general `unsafe-inline`. The policy includes no
  `unsafe-eval`. API and HTTPS OIDC origins are reduced to explicit origins in
  `connect-src`.
- Artifact links are tenant-bound HMACs with five-minute expiry and remain on
  the authenticated API origin. The API rechecks revision freshness, verifies
  the complete persisted object into bounded private storage and streams only
  checksum-matching bytes; the object store has no browser-facing URL contract.
- Imported files are size-, MIME-, signature- and filename-checked. Document
  content is untrusted data. The MVP inspection endpoint records only a hash and
  explicit unknown assumptions; it does not decode, render or convert the file.
  Every new physical import uses a server-issued UUID incarnation in both its
  object key and idempotency identity. Likewise, each generation lease derives a
  distinct attempt UUID and per-kind artifact UUID. A retry can therefore never
  overwrite or reclaim bytes belonging to an earlier attempt, even when the
  content digest is identical.
  Future converters must run non-root in the read-only worker container with
  dropped capabilities, bounded PIDs and disposable `/tmp`.
- Development bearer tokens are rejected when production configuration is
  validated. Production requires HTTPS OIDC and CORS origins, password-authenticated
  PostgreSQL and replaced signing/database/object-store credentials. The
  object-store host port is loopback-only and exists solely for backup/restore
  operations. Render `compose.yml` together with
  `compose.external-production.yml`, then run
  `python scripts/check_external_production.py --repo .`; a non-zero result is a
  deployment blocker and includes the missing operator action without echoing secrets.
  The web runtime additionally requires `APP_ENV=production`, an exact HTTPS API
  origin, a complete HTTPS OIDC issuer/client-id/root-callback tuple and an empty
  `CUSTOMBUILD_WEB_DEMO_TOKEN`. Partial or malformed configuration blocks the
  document before either CSP or client configuration is served.
  The preflight recomputes the canonical build/control source manifest
  (Docker-visible application inputs plus the release-control workflows),
  verifies its SHA-256 across every application image, and keeps that identity separate from the
  required Git commit in `VCS_REF`.
- Candidate and pinned runtime images are scanned for every High/Critical finding,
  including findings without an upstream fix. A scanner suppression is accepted only
  when its exact CVE/package/version/type tuple has a current ledger entry with severity,
  owner, rationale, mitigation, HTTPS source and review deadline; mismatch or expiry
  blocks release readiness.
- Promotion input is canonical, digest-only descriptor v3 JSON. It binds the final
  release-evidence hash, Git/source-manifest identity, workflow run and attempt, four
  application registry manifests, component publication-evidence hashes, three
  reviewed runtime deployment digests, exact Compose service roles and an exact
  GitHub OIDC signer/attestation policy. Archive SHA-256, image config digest and
  registry manifest digest remain separate identity domains: archive bytes are
  checked across artifact transfer, config digest links load/runtime/scan to the raw
  GHCR manifest, and the registry manifest is signed and attested as the sole
  immutable subject eligible for a later external deployment. A local
  daemon scan manifest is never equated with a registry manifest. OCI index-pinned
  runtimes additionally prove the selected `linux/amd64` child manifest and config.
  Verification rejects tags, duplicate keys, non-canonical encodings, unknown fields,
  digest reuse between components and evidence from another source or run. Only a
  successfully verified descriptor may produce the non-secret image environment
  consumed by `compose.registry.yml`, and the runtime command must use `--no-build`.
  The release workflow runs its unprivileged build/test path on pull requests but
  keeps GHCR publication and descriptor generation main-push-only, reloads the same
  archived image object instead of rebuilding it, and
  applies exact-identity Cosign and GitHub artifact attestations. It emits promotion
  input but never deploys. Branch protection,
  protected environments, hosting and secrets remain external controls, and no
  workflow result grants commercial or physical-machine authority.
- A `design_review` lock proves only that the recorded software checks and named
  human review steps were completed. It is not a physical machine authorization;
  workshop evidence and external operator authority remain out of band.

Development uses an in-process rate limiter. Production uses a shared Redis
counter keyed by a digest of the authenticated principal (or client IP before
authentication) and fails closed if that counter is unavailable. The ingress
should still enforce a coarser connection and request-volume limit before the
application.
