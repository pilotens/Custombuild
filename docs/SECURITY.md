# Security model

- Tenant identity comes only from a verified bearer token. Request bodies and
  path parameters never supply a trusted organization identifier.
- Every tenant query contains an application-level organization predicate and
  PostgreSQL applies a second Row-Level Security policy using transaction-local
  `app.current_organization_id`.
- The API database role is `NOSUPERUSER NOBYPASSRLS`. Only the isolated worker
  can bypass RLS to discover global outbox events; job reads/writes still include
  the persisted organization ID.
- Roles gate design, review, production and release actions. Released revisions
  are immutable and changes create new revisions.
- Bearer authentication does not use ambient browser cookies, so cross-site
  requests cannot inherit authentication; CORS is allow-listed. If cookie-based
  sessions are introduced later, synchronizer-token CSRF protection is required.
- The production browser uses OIDC Authorization Code + PKCE as a public client.
  Its access token is retained only for the browser tab in `sessionStorage` and
  is removed on expiry or logout. No client secret is embedded in the web build.
- Each document response gets a cryptographically random CSP nonce in the Next.js
  request proxy. Production permits inline framework scripts and style elements only
  through that nonce. Script attributes remain forbidden; style attributes are
  restricted to the single content-addressed positioning style emitted by Next
  Image's `fill` mode via `unsafe-hashes`, never general `unsafe-inline`. The policy includes no
  `unsafe-eval`. API and HTTPS OIDC origins are reduced to explicit origins in
  `connect-src`.
- Artifact links are tenant-bound HMACs with five-minute expiry and lead to a
  second short-lived object-store URL. The API rechecks revision freshness before
  issuing or redirecting a link. An object-store URL already issued cannot be
  revoked synchronously and ages out within the configured maximum TTL.
- Imported files are size-, MIME-, signature- and filename-checked. Document
  content is untrusted data. The MVP inspection endpoint records only a hash and
  explicit unknown assumptions; it does not decode, render or convert the file.
  Future converters must run non-root in the read-only worker container with
  dropped capabilities, bounded PIDs and disposable `/tmp`.
- Development bearer tokens are rejected when production configuration is
  validated. Production requires HTTPS OIDC and CORS origins, password-authenticated
  PostgreSQL, replaced signing/database/object-store credentials and an HTTPS
  public artifact endpoint. Render `compose.yml` together with
  `compose.external-production.yml`, then run
  `python scripts/check_external_production.py --repo .`; a non-zero result is a
  deployment blocker and includes the missing operator action without echoing secrets.
  The preflight recomputes the canonical Docker-context source manifest, verifies its
  SHA-256 across every application image, and keeps that identity separate from the
  required Git commit in `VCS_REF`.
- Candidate and pinned runtime images are scanned for every High/Critical finding,
  including findings without an upstream fix. A scanner suppression is accepted only
  when its exact CVE/package/version/type tuple has a current ledger entry with severity,
  owner, rationale, mitigation, HTTPS source and review deadline; mismatch or expiry
  blocks release readiness.
- A `design_review` lock proves only that the recorded software checks and named
  human review steps were completed. It is not a physical machine authorization;
  workshop evidence and external operator authority remain out of band.

Development uses an in-process rate limiter. Production uses a shared Redis
counter keyed by a digest of the authenticated principal (or client IP before
authentication) and fails closed if that counter is unavailable. The ingress
should still enforce a coarser connection and request-volume limit before the
application.
