# Codex final checkpoint — 2026-08-11

> **Historical evidence only:** Every runtime health, PASS, commit, image and
> package statement below describes the 2026-08-11 environment. It is not
> evidence for the current commit and cannot satisfy the current release gate.
> Current evidence must be generated after static controls, Compose acceptance,
> vulnerability scans and backup/restore all pass for the same SHA and image IDs.

This is the final local handoff state after the resumed implementation and
acceptance run. No Git commit or push was made.

> **Superseded behavior note — 2026-08-17:** References below to a
> “large-format repair” record historical behavior and must not be replayed.
> Current code never invents or auto-applies 5,000 × 2,500 mm stock or the
> `custombuild-router-5125-linuxcnc` profile. Missing stock produces a stockless
> `READY_FOR_DESIGN_REVIEW` package with CAM `BLOCKED` until the user binds a
> real server-known stock decision. Directional material additionally requires
> an exact structured X/Y stock axis; opaque evidence cannot unlock CAM.

## Environments running

- Production source: repository root
- Test source: isolated test checkout
- Production: web `http://localhost:3000`, API `http://localhost:8000`, S3
  `http://localhost:9000`
- Test: web `http://localhost:3100`, API `http://localhost:8100`, S3
  `http://localhost:9200`
- All 12 Compose services are healthy. API readiness reports PostgreSQL,
  authenticated Redis and SeaweedFS object storage as `ok` in both
  environments. The final app containers have zero restarts.

## Functional and visual acceptance

- Production live workflow `FINAL-P-811D` passed from design through generated
  production package, artifact hashes and locked release
  `ACCEPT-FINAL-P-811D`.
- Test live workflow `FINAL-T-811D` passed the same complete path.
- The lock-flow regression was re-run after the final UX/state-machine fix:
  production `LOCK-P-811E` and test `LOCK-T-811E` both passed validation,
  explicit design approval, generated CAM evidence, CAM approval and locked
  release with verified artifact manifests.
- The production review is now a three-step guided flow: `Kontrollera`,
  `Skapa underlag`, and `Lås`. Only the current action is expanded, so stale
  revisions show one `Spara och kontrollera` action instead of a grid of
  disabled future controls.
- Manual design, warning and CAM motivation fields were removed. Each
  server-authoritative warning is acknowledged with a checkbox, while stable
  technical audit text, actor, timestamp, warning rule, exact job and manifest
  bindings remain in the server revision trail.
- Save + validate, design approval + package generation, and CAM approval +
  release are combined into retry-safe actions. A downstream failure resumes
  from the completed server state rather than creating duplicate revisions or
  approvals. FreeCAD export and release-number override are optional advanced
  controls; FreeCAD is off by default.
- Live Chromium completed the simplified flow through a generated package,
  exact-job review, locked revision and ZIP download in production and test at
  the supplied 966 × 1197 viewport. Evidence is in
  `test-results/simple-review-live-20260811-1545/`.
- Final live Chromium acceptance passed in production and test. Starting from
  an approved current revision, a 50 mm model change produced `Ändrad`, an
  unchecked/disabled confirmation, a disabled lock and an enabled inline save
  action. Saving produced revision 2 as the current draft, cleared prior review
  evidence and kept locking gated at design validation. The runs recorded no
  page, console, request or HTTP error responses. Evidence is in
  `test-results/lock-ux-live-20260811-141002/`.
- Direct PostgreSQL tenant-isolation probes passed in production and test:
  cross-tenant reads were hidden and cross-tenant writes were rejected.
- Upload and clipboard-paste reference images, dynamic width and height drag,
  part editing, project-specific drafts, structural validation, production
  drawer close/Escape, and project switching passed in Chromium in both
  environments without page, console or unexpected network errors.
- Front-first rendering is corrected consistently across the local and
  authoritative models. The physical open front is `+Z`; the back panel is at
  the rear and underskåp fronts are at the front. Fresh first frames and frames
  two seconds later match. The wall-library gate shows five aligned cabinet
  fronts in both environments.
- Final UI evidence and the superseding verdict are in
  `test-results/final-ui-acceptance-20260811-123150/UI_ACCEPTANCE_REPORT.md`.

## Automated verification

- Backend in each repo: Ruff passed; strict mypy passed; 395 tests passed and
  16 environment-dependent tests were skipped as expected.
- Frontend in each repo: ESLint passed, TypeScript passed, 127/127 tests passed,
  and the Next.js production image built successfully.
- Environment isolation passed: production uses `3000/8000/9000`; test uses
  `3100/8100/9200`; internal networks and data volumes remain project-scoped.
- Active environment-neutral source parity passed: 215 matching paths, 214
  existing files byte-identical, with zero state or hash mismatches. The
  excluded differences are the explicit environment identity files, this
  production checkpoint, the test port assertion and the documented historical
  nested `prod/` snapshot.
- `git diff --check` and cached checks passed in both repos. No files are staged.
- Production release readiness reports `software_release_ready=true`. The test
  report intentionally blocks `RELEASE_SOURCE`, because test is not the
  canonical release source; its environment, hardening, CI and supply-chain
  checks pass.

## Backup and recovery

- Production backup and full disposable recovery:
  `test-results/backups/seaweedfs-v2-prod-20260811-1227`
  - Alembic head `0004_design_source_provenance`
  - 37 projects
  - 182 objects / 19,149,931 bytes
  - every object hash verified
- Test backup and full disposable recovery:
  `<isolated-test-checkout>/test-results/backups/seaweedfs-v2-test-20260811-122925`
  - Alembic head `0004_design_source_provenance`
  - 15 projects
  - 146 objects / 16,346,332 bytes
  - every object hash verified
- Both restore drills report `PASS`; no restore containers, networks or volumes
  remain. The old MinIO containers are gone, while the two named data volumes
  remain as rollback evidence.

## Security and supply chain

- The exact deployed production and test API, worker and web images passed the
  digest-pinned Grype `--only-fixed --fail-on high` gate using DB schema 6.1.9.
- Both web images have no known findings. API and worker images have four Medium
  Python findings whose published fixes target future Python 3.15 prereleases;
  there are no actionable High or Critical findings.
- The worker Debian snapshot was advanced to signed snapshot
  `20260810T123307Z`, which supplies fixed `libnss3 ...deb12u4` and removes the
  previously detected Critical CVE.
- Runtime controls are active: non-root app containers, read-only filesystems,
  dropped capabilities, `no-new-privileges`, internal backend networks,
  authenticated Redis, anonymous S3 rejection, loopback-only published ports,
  health checks and restart policies.

## Git and release status

- Production is on `main` at `aabf89b`; 209 working-tree paths are intentionally
  uncommitted from this implementation session.
- Test is on `local/furniture-studio-v2` at `1d068ef`; 146 working-tree paths are
  intentionally uncommitted and the branch has no upstream.
- A commit, push or pull request still requires an explicit user decision.

## External gates that software cannot close

- Physical CNC dry-run, metrology and workshop sign-off.
- Final third-party notices/commercial legal approval.
- Engineering and hardware evidence for concept-only templates. They remain
  production-blocked by design until that evidence exists.
- Production identity-provider provisioning and organization/user assignments,
  if the deployment is moved beyond the current local environment.

## Final simplified review deployment - 2026-08-11 17:16 CEST

- The review drawer is now a compact three-step flow: `Kontrollera`,
  `Skapa underlag`, and `Lås`. It renders one current action at a time and no
  longer asks for free-text design, warning, or CAM motivations.
- Construction warnings use explicit acknowledgement checkboxes. Stable audit
  reasons, actor, timestamp, exact generation job, manifest, and warning-rule
  bindings are still recorded by the server.
- The combined actions are retry-safe after partial completion. Missing or
  corrupt evidence offers a working `Skapa om underlag` action rather than a
  permanently disabled lock control.
- Artifact review now binds every database row to the exact generation result
  and verifies the stored object size, content type, and SHA-256. CAM approval,
  repair, and release stream and hash the actual content. A missing key can be
  repaired; missing buckets, authentication failures, timeouts, and storage
  outages return a non-destructive 503.
- Generation, approval, and release share the same revision lock and require
  the latest exact job. Concurrent initial generation uses a unique-conflict
  winner fallback and does not create duplicate jobs or outbox events.
- Focused verification passed in both repositories: 58 API/storage/worker flow
  tests, 19 final storage-classification tests, 16 frontend workflow tests,
  Ruff, mypy, TypeScript, ESLint, and production image builds.
- Fresh live Chromium runs passed the complete real flow in both environments,
  including worker generation, exact evidence verification, revision lock, and
  ZIP signature/download. Evidence:
  - `test-results/simple-review-final-20260811-prod/`
  - `test-results/simple-review-final-20260811-test/`
- API, worker, and web were rebuilt and deployed in both environments. All 12
  services are healthy with zero restarts; both `/ready` endpoints report
  PostgreSQL, authenticated Redis, and SeaweedFS as `ok`.
- Final deployed image IDs:
  - prod API `sha256:59635d5043b5...`, worker `sha256:2b1d2cfbde35...`,
    web `sha256:a897a637c0af...`
  - test API `sha256:8b964fe58ed9...`, worker `sha256:92b935362630...`,
    web `sha256:a941509163d5...`
- `.dockerignore` now excludes local `.tmp-*`, `.pnpm-store`, and the historical
  nested `prod/` snapshot, preventing QA-only files from entering or delaying
  production build contexts.
- All six exact deployed app images passed the digest-pinned Grype
  `--only-fixed --fail-on high` gate with database schema 6.1.9. Both web
  images have zero findings; API and worker have four documented Medium Python
  findings and zero High/Critical findings. Runtime logs for all 12 services
  since deployment contain zero genuine errors or HTTP 5xx responses.
- Final environment isolation and parity passed: production remains on
  `3000/8000/9000`, test on `3100/8100/9200`, and all 139 active
  environment-neutral paths match. Production reports
  `software_release_ready=true`; test is intentionally blocked only as a
  non-canonical release source.

## Actionable validation and canonical save deployment - 2026-08-11 19:15 CEST

- Every non-passing buildability check now presents the problem, consequence,
  recommended solution, exact required value or evidence, and a real action.
  Deterministic faults can be repaired from the rule card; external wall
  anchoring, hardware/drilling and grain-direction checks open their exact
  evidence path and are never presented as automatically solved.
- The apparent no-op in `Spara och kontrollera` was reproduced and removed.
  Auto mode previously previewed an auto-corrected wall-anchor specification,
  while revision creation used the uncorrected preview hash. The resulting
  revision was immediately marked changed and showed the same save action.
  Draft and revision creation now share the canonical resolver and the client
  binds both the expected design hash and current revision before any mutation.
- Stock format, sheet counts and machine profile are frozen into each revision.
  Generation, worker execution, CAM approval and release all require that exact
  production context and the exact attributed design-review snapshot.
- Save now reports a visible busy, success or actionable error state. The same
  disabled `Spara och kontrollera` action remains mounted with
  `aria-busy=true` through both revision creation and validation, then focus
  advances once to the next step. A partial failure resumes the saved revision
  instead of creating a duplicate.
- Focused verification passed identically in production and test: 59 backend
  tests, 54 workflow/API/guidance tests, and the final busy-state workflow suite
  (20/20), plus Ruff, mypy, TypeScript, full ESLint and Next production builds.
  The complete web suite passed 160/160 tests in each repository. Current
  OpenAPI matches the FastAPI source exactly.
- Live Chromium acceptance passed in both environments for the exact
  4,200 x 2,400 wall-library case: large-format repair, three actionable
  warnings, one revision POST, one validation POST, visible busy/success state,
  no stale loop and transition to step 2. Evidence is in
  `test-results/final-actionable-save-20260811/`.
- The complete real production path also passed in both environments: save,
  validate, acknowledge controls, generate CAD/CAM evidence, review, lock and
  download the signed ZIP. Evidence is in
  `test-results/final-production-flow-20260811/`.
- API, worker and web were rebuilt and deployed in both environments. All 12
  services are healthy with zero restarts; both readiness endpoints report
  PostgreSQL, authenticated Redis and SeaweedFS as `ok`, and both web endpoints
  return HTTP 200.
- Current deployed image IDs:
  - prod API `sha256:c7fe78579a3a...`, worker `sha256:a0ef3b69661b...`,
    web `sha256:9aa2ad2e766c...`
  - test API `sha256:679120b9836e...`, worker `sha256:f3d00049814a...`,
    web `sha256:0b81e994f7f5...`
- All six exact deployed image IDs pass the digest-pinned Grype
  `--only-fixed --fail-on high` gate with database schema 6.1.9. Web has zero
  findings; API and worker have four documented Medium Python findings and no
  High or Critical findings. Runtime logs since rollout contain no application
  errors, exceptions, failed requests or HTTP 5xx responses.
- Environment isolation remains `3000/8000/9000` for production and
  `3100/8100/9200` for test. Production release readiness is PASS; test remains
  intentionally blocked as a non-canonical release source. Twenty-one touched
  source/contract files are byte-identical and `git diff --check` passes in both
  worktrees.
