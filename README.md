# Custombuild

Custombuild is a deterministic, tenant-aware B2B design-to-production system for
parametric casework. The implemented vertical is a configurable bookcase: one
frozen `DesignSpec` drives construction screening, stable parts and joints, BOM,
nesting, CAD/CAM, drawings, assembly data and a checksummed production bundle.

> Machine output is validation-only. It is not safe for physical cutting until
> the exact machine, tooling, material batch, calibration, postprocessor and
> complete prototype have been verified by an experienced furniture constructor
> and CNC operator. Custombuild never starts a machine and does not represent its
> screening calculations as certification.

## Implemented vertical

- Swedish desktop-first workbench with deterministic 2D/3D preview, orthographic
  views, exploded/transparent/isolation modes, undo/redo and server validation.
- Furniture Studio with draggable shelf rows, dividers, lower cabinets, back
  panels and plinths. Semantic snap targets compile to the canonical `DesignSpec`;
  pointer positions never become CNC coordinates. Exact custom bay widths and
  shelf heights remain server-validated and regenerate every connected board.
- AI-safe semantic intent contract: AI may propose an attributed furniture change,
  but explicit confirmation and the deterministic validation chain are required
  before it may mutate a design or reach production.
- Versioned 18 mm MDF and birch-plywood bookcase construction with a separate
  versioned 6 mm back material, stable IDs and integer micrometre units.
- Shelf deflection/stress, stability, tip and wall-anchor screening plus
  `CB-JOINT-001`: fixed DADO shelf supports are checked from the actual generated
  engagement area against the versioned material shear value reduced for source
  uncertainty, creep and structural safety factor. Every result exposes inputs,
  source, trace, margin and deterministic divider/load-reduction actions. Demand
  is the ceiling of row load divided across bays and the two supports per shelf
  segment; generated DADO depth—not nominal thickness—defines the bearing area.
- Adjustable shelf-pin designs remain `BLOCK` because the MVP has no versioned
  manufacturer capacity for the exact pin, drilling pattern, material and measured
  thickness. Custombuild does not infer or substitute a hardware capacity.
- DFM screening with calculation traces, assumptions, PASS/WARNING/BLOCK and
  explicit fixes.
- A physically paired not/groove reference flow, semantic manufacturing
  features, machine/setup/tool validation and deterministic multi-stock nesting.
- CadQuery/OpenCascade STEP and GLB, side-separated DXF, SVG detail drawings,
  PDFs, labels, setup sheets, BOM/cut/material/hardware/tool lists and backplot.
- Optional headless FreeCAD bridge that imports authoritative STEP into a native
  derivative FCStd project. The Compose worker includes it by default; FreeCAD stays
  hidden, replaceable and non-authoritative.
- AssemblyGraph-derived manual pages use the parts' authoritative box geometry,
  explicit incoming/fixed groups, one- or two-segment motion paths, exact part
  IDs, per-step hardware/tools, checkpoints and conservative lift/pinch warnings.
- Machine-neutral `operations.json` and a LinuxCNC 3-axis validation profile. Its
  dry-run program cannot enable the spindle or command a negative cutting Z.
- Explicit design approval, one attributed reason per warning, exact-job CAM
  review, immutable review lock, supersession and stale-artifact blocking. This
  lock is a design-review milestone, never a physical machine approval.
- A checksum-bound workshop-readiness report names every missing calibration,
  material, tooling, test and human-approval item. CAM approval fails closed if
  this report or any separate review artifact is missing; physical cutting stays
  unauthorized.
- PostgreSQL RLS plus application tenant predicates, RBAC, OIDC validation,
  transactional outbox/Celery jobs, Redis and S3-compatible artifact storage.
- Server-authoritative mutable workspace drafts preserve the complete semantic
  editor state and reference-image provenance before a numbered revision is
  frozen. Browser production authentication uses Authorization Code + PKCE and
  keeps its short-lived bearer token in session storage only.
- Reference-image concepts use a fail-closed promotion step: measured outer
  dimensions, layout, material and construction assumptions must all be
  explicitly confirmed. The confirmation is fingerprinted to the exact current
  parametric model, invalidates after any relevant edit and is frozen as source
  provenance in the numbered server revision and review bundle.
- Twenty deterministic golden bookcase fixtures and unit, property, integration,
  RLS, CAD, package, web and live Compose acceptance tests.

Unsupported or not physically verified capabilities are blocked or labelled;
they are not replaced with placeholder geometry or pretend machine output.

## Quick start

Docker with the Compose v2 plugin is required. Copy the development environment
and replace its placeholder secrets before exposing any service outside localhost.

```bash
cp .env.example .env
docker compose up --build
```

Open:

- workbench: `http://localhost:3000`
- API/OpenAPI: `http://localhost:8000/docs`
- SeaweedFS S3 endpoint: `http://localhost:9000`

The root Compose stack uses the isolated project name `custombuild-prod`. A
sibling `DIY-test` working copy uses `custombuild-test` and ports 3100/8100/9200.
Run `python scripts/check_environment_isolation.py compose.yml
--peer ../DIY-test/compose.yml` before starting both. See
[docs/ENVIRONMENTS.md](docs/ENVIRONMENTS.md) for the source-of-truth policy; the
tracked `prod/` directory is a historical release snapshot, not a third runtime.

The idempotent development seed creates two isolated organizations and a real
bookcase project in each:

- `Authorization: Bearer demo-nordic-owner`
- `Authorization: Bearer demo-atelier-owner`

These tokens work only with `AUTH_MODE=development`. Set `APP_ENV=production`
for any production deployment. That mode requires HTTPS OIDC/CORS/artifact
endpoints and rejects development authentication, placeholder credentials and a
non-PostgreSQL database. The supplied `.env.example` is development-only.

## Product flow

1. Choose a starting model, drag furniture components to compatible snap zones
   and refine exact dimensions in the contextual five-step configurator.
2. Resolve every blocking construction finding. A supported, exact CAM-only DFM
   prerequisite may remain visible only inside a strict review-only package; it
   is never treated as solved. Autofix is displayed as a diff and is available
   only when the server can describe a truthful deterministic change.
3. Save a real server revision and validate it.
4. Enter a review reason and explicitly approve the design. Every WARNING needs
   a matching attributed override.
5. Generate the frozen context through the worker. If CAM is available, inspect
   the individual operations, setup sheet and validation backplot evidence. If a
   fail-closed CAM prerequisite is missing, the downloadable design-review package
   identifies that blocker and deliberately omits every CAM, nesting and controller
   artifact. An optional checkbox also creates a checksum-linked,
   non-authoritative FCStd review derivative when the worker was built with
   `INSTALL_FREECAD=true`.
   `STOCK_PROFILE_MISSING` preserves the requested blank requirements and selected
   production context; it never invents or applies replacement sheet dimensions or
   a machine profile. Directional sheet material without an exact structured X/Y
   stock-axis binding remains `DFM-GRAIN-001`; an uploaded document or review
   acknowledgement cannot resolve that blocker.
6. Enter a separate CAM review reason and approve that exact successful job only
   when its complete CAM evidence set exists. A CAM-blocked review package cannot
   enter this step.
7. Lock the design-review revision. It becomes immutable and exposes the signed
   ZIP, but remains validation-only and cannot authorize physical machining.
8. Any later design revision supersedes the old review lock, cancels unfinished
   old work and prevents new access to its stale artifacts.

## Verification

Install exact Python, CAD and web dependencies, then run the complete local gate:

```bash
make install
make check
```

Run the HTTP acceptance against an already running Compose stack:

```bash
python3 scripts/live_acceptance.py --base-url http://127.0.0.1:8000
```

Run the visual furniture gate against the built web app:

```bash
pnpm --dir apps/web build
pnpm --dir apps/web test:visual
```

The gate rejects template previews whose SVG geometry falls outside its image,
does not match the template's shelves, bays or base cabinets, or renders empty.
It also captures the live 3D canvas before and after a dimension-handle drag and
requires both a non-empty render and a changed image. The before/after PNGs are
attached to the Playwright report for human inspection. This visual gate runs in
CI as part of the production workspace acceptance.
The deterministic offline creation, image-import and accessibility flows run in
Chromium, Firefox and WebKit. The state-mutating live release flow runs once in
Chromium so that one acceptance run cannot race another browser against the same
revision.

The web configurator separates production-screened models from concept models.
`Shelving` and `Wall library` can enter the revision-gated production drawer
when their construction rules pass. Exact bay widths and shelf heights are sent
to the authoritative server model and regenerate all connected boards. Other
visual starting models stay in concept mode until their dedicated server
geometry, hardware and machining rules exist.
The selected starting model and complete semantic workspace are saved in the
tenant project through the API, with local storage used only as an offline
fallback. Reopening the workspace therefore resumes the same server-authoritative
draft instead of restarting the model picker.

The workspace also accepts front-facing JPG, PNG and WebP reference images from
the model picker or top toolbar. Screenshots can be pasted directly into the
focused upload area with Ctrl+V (Cmd+V on macOS). A deterministic in-browser image pass detects
the furniture boundary, shelf lines, vertical divisions and a contrasting base
cabinet zone. The result always starts as an editable 3D concept. It may enter
the normal server review pipeline only after the user explicitly confirms the
real measurements, interpreted layout, selected material and hidden construction
assumptions. Those confirmations are bound to the exact parametric model and
become stale after an edit; source, confidence, warnings and confirmations are
then frozen on the server. This is a reviewed model conversion, not proof that a
photograph contains hidden joints, hardware or anchors and not a physical release.

CI additionally starts the full Compose topology—PostgreSQL, Redis, SeaweedFS, API,
worker and web—and verifies tenant isolation, a genuine CadQuery generation,
package hashes, validation-only machine code, approval/release and stale-output
invalidation without mutating the database directly. A Chromium acceptance then
drives the built workbench through dimensions, validation, both approvals, the
real worker job, immutable release and ZIP download without route mocks.

## Production package

The deterministic ZIP includes `manifest.json` with SHA-256 for every payload;
STEP/GLB; A/B DXF and SVG per part; BOM, cut and material lists;
geometry-derived assembly information; construction, DFM and readiness reports;
optional FreeCAD interchange status and, when explicitly requested, a native
FCStd review derivative; and any verified reference-image source provenance
attached to the revision. Tool lists, nesting maps, labels tied to placements,
semantic operations, setup sheets, validation backplot/program and operation-based
QA are included only when CAM validation is complete. An explicitly CAM-blocked
package omits that entire set.

## Repository map

- `apps/web` — Next.js/React/Three.js semantic Furniture Studio
- `services/api` — FastAPI, RLS-aware persistence, revisions and approvals
- `services/worker` — Celery/outbox generation and durable artifacts
- `packages/domain` — canonical furniture model, semantic intents, joints and assembly graph
- `packages/rule-engine` — construction screening and deterministic correction
- `packages/manufacturing` — DFM, nesting, exports and package generation
- `packages/template-sdk` — documented versioned template contract
- `cad` — authoritative CadQuery/OpenCascade geometry plus optional FreeCAD interchange
- `cam` — semantic operations, setup validation and backplot
- `postprocessors` — validation-only LinuxCNC reference postprocessor
- `docs` — architecture, security, operations, safety and licence gates

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/SEMANTIC_DESIGN_ARCHITECTURE.md](docs/SEMANTIC_DESIGN_ARCHITECTURE.md),
[docs/PRODUCTION_SAFETY.md](docs/PRODUCTION_SAFETY.md),
[docs/ADHESIVE_FREE_JOINING_POLICY.md](docs/ADHESIVE_FREE_JOINING_POLICY.md) and
[docs/LICENSE_REVIEW.md](docs/LICENSE_REVIEW.md) before any commercial or
workshop evaluation. The application is covered by the top-level proprietary
`LICENSE`; repository access is not a distribution, hosting or deployment grant.
