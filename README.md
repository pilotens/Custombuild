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
- Guided semantic editor with draggable shelf rows, vertical dividers, back panel
  and plinth. Furniture-aware snap targets compile to the canonical `DesignSpec`;
  visual pointer positions never become CNC coordinates.
- AI-safe semantic intent contract: AI may propose an attributed furniture change
  but explicit user confirmation and the full deterministic validation chain are
  required before it may mutate a design.
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
  FCStd derivative. FreeCAD remains hidden, replaceable and explicitly prohibited
  from becoming the editable or CNC-authoritative source model.
- AssemblyGraph-derived manual pages use the parts' authoritative box geometry,
  explicit incoming/fixed groups, one- or two-segment motion paths, exact part
  IDs, per-step hardware/tools, checkpoints and conservative lift/pinch warnings.
- Machine-neutral `operations.json` and a LinuxCNC 3-axis validation profile. Its
  dry-run program cannot enable the spindle or command a negative cutting Z.
- Explicit design approval, warning overrides, exact-job CAM approval, immutable
  release, supersession and stale-artifact blocking.
- PostgreSQL RLS plus application tenant predicates, RBAC, OIDC validation,
  transactional outbox/Celery jobs, Redis and S3-compatible artifact storage.
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
- MinIO development console: `http://localhost:9001`

The idempotent development seed creates two isolated organizations and a real
bookcase project in each:

- `Authorization: Bearer demo-nordic-owner`
- `Authorization: Bearer demo-atelier-owner`

These tokens work only with `AUTH_MODE=development`. Set `APP_ENV=production`
for any production deployment. That mode requires HTTPS OIDC/CORS/artifact
endpoints and rejects development authentication, placeholder credentials and a
non-PostgreSQL database. The supplied `.env.example` is development-only.

## Product flow

1. In guided mode, drag furniture components into compatible snap zones or edit
   exact bookcase parameters. Custombuild compiles the intent and regenerates the
   deterministic preview; expert mode keeps the complete parameter controls.
2. Resolve all blocking construction/DFM findings. Autofix is displayed as a diff.
3. Save a real server revision and validate it.
4. Enter a review reason and explicitly approve the design. Every WARNING needs
   a matching attributed override.
5. Generate the frozen context through the worker and inspect its setup/backplot.
6. Enter a separate CAM review reason and approve that exact successful job.
7. Confirm release. The revision becomes immutable and exposes the signed ZIP.
8. Any later design revision supersedes the old release, cancels unfinished old
   work and prevents new access to its stale artifacts.

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

CI additionally starts the full Compose topology—PostgreSQL, Redis, MinIO, API,
worker and web—and verifies tenant isolation, a genuine CadQuery generation,
package hashes, validation-only machine code, approval/release and stale-output
invalidation without mutating the database directly. A Chromium acceptance then
drives the built workbench through dimensions, validation, both approvals, the
real worker job, immutable release and ZIP download without route mocks.

## Production package

The deterministic ZIP includes `manifest.json` with SHA-256 for every payload;
STEP/GLB; A/B DXF and SVG per part; BOM, cut, material, hardware and tool lists;
nesting maps; semantic operations; setup sheets; validation backplot/program;
labels; geometry-derived assembly manual; and construction, DFM and QA reports.

## Repository map

- `apps/web` — Next.js/React/Three.js semantic workbench
- `services/api` — FastAPI, RLS-aware persistence, revisions and approvals
- `services/worker` — Celery/outbox generation and durable artifacts
- `packages/domain` — canonical bookcase model, semantic intents, joints and assembly graph
- `packages/rule-engine` — construction screening and deterministic correction
- `packages/manufacturing` — DFM, nesting, exports and package generation
- `packages/template-sdk` — documented versioned template contract
- `cad` — authoritative CadQuery/OpenCascade geometry plus optional FreeCAD interchange
- `cam` — semantic operations, setup validation and backplot
- `postprocessors` — validation-only LinuxCNC reference postprocessor
- `docs` — architecture, security, operations, safety and licence gates

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/SEMANTIC_DESIGN_ARCHITECTURE.md](docs/SEMANTIC_DESIGN_ARCHITECTURE.md),
[docs/PRODUCTION_SAFETY.md](docs/PRODUCTION_SAFETY.md) and
[docs/LICENSE_REVIEW.md](docs/LICENSE_REVIEW.md) before any commercial or
workshop evaluation. The repository currently has no application `LICENSE`;
repository access is not a third-party distribution or deployment grant.
