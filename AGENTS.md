# Repository working agreement

## Source and contracts

- The repository root is the only runtime/release source. Do not recreate a `prod/` copy.
- `/` is the bookcase/design-review workspace (`CustombuildWorkspace`). `/furniture`
  is the multi-family workspace (`FurnitureWorkspacePage`). They are distinct editors;
  do not silently migrate saved drafts between their schemas.
- Reuse the existing tenant-scoped jobs, transactional outbox, leases, storage ledger
  and immutable artifact pipeline. Do not add Redis-only export state or base64 ZIP
  payloads to status responses.
- New responsibilities belong in focused modules, not additional unrelated sections
  in `api.py`, `production-workflow.tsx` or `globals.css`.
- Regenerate OpenAPI and TypeScript contracts after API changes (`make contracts`).
  Never hand-edit generated contracts to hide a server/client mismatch.

## Safety boundaries

- Browser geometry, image interpretation and AI proposals are not manufacturing truth.
- Preserve tenant predicates, RLS, revision/hash checks, fail-closed rules, exact
  warning acknowledgements, immutable storage identities and bounded retries.
- `physical_cutting_authorized` stays false. Passing tests or merging a PR does not
  authorize machining, validate physical construction or replace workshop acceptance.
- Do not relax vulnerability thresholds, flaky-test rejection, pixel tolerances,
  coverage requirements or production evidence checks to make CI green.
- Never commit credentials, `.env`, generated artifacts or local databases.

## Verification and review

- Run focused regression tests while developing; run `make check` and the relevant
  live Compose/browser gates before declaring the change release-ready.
- Linux CI is authoritative for Linux image/Compose behavior and visual baselines.
  Report unavailable checks rather than treating Windows-only checks as equivalent.
- Stage intended source files, then run
  `python scripts/source_manifest.py --repo . --update-production-semantic-root`
  and stage its updated control file. Its `authorization: false` is deliberate;
  updating this integrity record is not external semantic approval.
- Use a PR with exact test evidence and a human review for production-sensitive
  changes. CODEOWNERS identifies the repository owner, not an independent qualified
  engineering reviewer. Never invent an approval or approve on another person's behalf.
- Preserve unrelated local changes. Back up a dirty checkout before any replacement.

See `docs/FRONTEND_ARCHITECTURE.md` and `docs/ARCHITECTURE.md` for current boundaries.
