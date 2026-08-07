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
  public artifact endpoint.

The included in-process rate limiter is a local safety net. Production must also
enforce distributed rate limits at the ingress or Redis-backed gateway.
