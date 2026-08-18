# Architecture

Custombuild is a modular monolith with an isolated CAD/CAM worker. The canonical
boundary is a frozen, versioned `DesignSpec`; visual meshes and generated files
are derived artifacts and never become editable source data.

## Deterministic flow

1. Normalize external millimetres to integer micrometres.
2. Validate the template schema and calculate the canonical design hash.
3. Build stable part instances, joint pairs and the assembly graph.
4. Evaluate versioned construction-screening and DFM rules.
5. Freeze design, material, machine, tool and postprocessor context.
6. Build BOM/cut list, nesting, setups and semantic CAM operations.
7. Export authoritative CAD, drawings and assembly views.
8. Reparse validation machine code and package byte-stable artifacts with SHA-256.

The browser provides a responsive preview but cannot generate review artifacts.
Its mutable workspace is stored as a project draft containing the canonical
`DesignSpec`, semantic editor state, selected template and reference provenance.
The API recalculates the draft's authoritative preview and hash before persisting
it. A numbered revision is still the only frozen input to worker generation.

A reference image never becomes CAD geometry directly. Detection produces a
concept plus an overlay for human review. Promotion requires four explicit
confirmations: measured dimensions, interpreted layout, selected material and
hidden construction assumptions. A local fingerprint binds those confirmations
to the exact parametric editor state and automatically restores concept blocking
after a relevant change. The API validates the complete confirmation contract,
freezes it in `DesignVersion.source_provenance_json`, includes it in the revision
context hash and exports it as review evidence. This records who confirmed which
inputs; it does not turn image recognition into engineering proof.

## Semantic editing boundary

The Furniture Studio operates above `DesignSpec` through furniture-aware commands
and snap targets. A drag/drop interaction represents an intent such as “place a
shelf row at this height” or “split this bay with a divider”. It is never treated
as an untyped box transform or as a drill, pocket or machine coordinate.

The domain package exposes a frozen `SemanticDesignDocument`, versioned snap
relations and a fail-closed compiler. The web model extends the production schema
with validated bay-width and shelf-height ratios. Symmetry is the default: an
off-centre placement creates or moves its mirrored partner and all connected
shelf segments, joints and structural spans are regenerated from the same model.

AI is restricted to attributed semantic proposals. An AI-originated proposal
requires explicit user confirmation and then passes through the same deterministic
domain, rule, CAD, DFM and approval gates. AI cannot write manufacturing features,
operations, toolpaths or G-code.

See [SEMANTIC_DESIGN_ARCHITECTURE.md](SEMANTIC_DESIGN_ARCHITECTURE.md) for the
complete interaction and trust model.

## CAD boundaries

CadQuery/OpenCascade remains the authoritative CAD adapter. An optional headless
FreeCAD bridge may import its authoritative STEP into a native FCStd derivative
for future TechDraw, Assembly and CAM integrations. The derivative is explicitly
non-authoritative; edits in FreeCAD never flow back into `DesignSpec` or directly
into manufacturing operations.

Every generated review bundle contains `validation/cad-interchange-status.json`.
It records the bridge/contract versions and reports `OPTIONAL_NOT_REQUESTED` with
`runtime_probe_performed=false` unless the reviewer explicitly requests an FCStd
derivative. A request requires STEP export and a headless FreeCAD runtime. The worker
then imports that exact STEP, normalises the FCStd container for reproducible packaging,
records its source checksum and exposes it as separate `design_fcstd` review evidence.
The status and FCStd metadata also pin the actual FreeCAD runtime version used. The
request fails closed if conversion is unavailable or incomplete; it never falls back
to treating FreeCAD geometry as authoritative. The CadQuery STEP path and
validation-only CAM pipeline remain separate.

The production worker also emits a versioned workshop-readiness report for the
exact generation context. It records which digital evidence exists and lists
machine calibration, WCS, measured tooling, material batch, coupons, independent
simulation, air-cut, reference part, prototype and named human approvals as
external evidence. API-side CAM approval rechecks the persisted artifact set and
the report contract; a design-review lock cannot be confused with physical
machine authorization.

The assembly graph is also production data. Each `AssemblyStep` names the exact
incoming part or rigid subassembly and a verified motion path. The manual places
only that group at one common exploded offset opposite the path, preserving both
its internal geometry and the already assembled `FIX` group. Adjustable shelves
use a front approach followed by a vertical placement; the first carcass side
requires a named panel-positioning jig. Unknown joint sequences fail closed.
`Joint.assembly_direction` remains the canonical first-member mating axis;
`AssemblyStep.motion_path` is authoritative for the actual operator movement.

`CB-JOINT-001@1.1.0` screens each fixed shelf-support DADO using the generated
groove depth and overlapping shelf depth as bearing area. The per-support reaction
is derived from the declared row load and number of bays, then compared with the
material version's shear strength after uncertainty, creep and structural safety
reductions. This is a conservative local bearing/shear screen, not a certified
joint model. Adjustable shelf pins have no versioned supplier capacity in the MVP
and therefore produce `BLOCK`; no fallback value is fabricated.

The deterministic integer calculation is:

- `R_support = ceil(W_row / (2 * (divider_count + 1)))`;
- `A_bearing = generated_dado_depth * min(shelf_depth, generated_groove_length)`;
- `F_allow = shear_strength * A_bearing * (1 - uncertainty) /
  ((1 + creep) * structural_safety_factor)` after explicit µm²-to-mm² conversion.

`R_support < 0.8 * F_allow` is `PASS`, the interval through `F_allow` is
`WARNING`, and demand above capacity is `BLOCK`. A missing generated DADO area
also blocks; it is never replaced by nominal thickness. The evaluation records
the exact joint ID, material ID/version and property-source revision used.

## Isolation and authorization

The authenticated token determines `organization_id`; no request schema contains
a trusted tenant field. Every application query includes the tenant, and
PostgreSQL RLS binds the transaction to `app.current_organization_id`. The API
role is a non-superuser without `BYPASSRLS`. The worker has `BYPASSRLS` only to
discover global outbox work and still verifies the job tenant before every write.

Review-locked design revisions are immutable. A relevant edit always creates a
new revision and therefore cannot reuse old approvals, CAM or artifacts. The API
labels the lock as `design_review`; it is deliberately not a machine release.

The browser is a public OIDC client and uses Authorization Code + PKCE. Access
tokens are held in `sessionStorage`, never local storage or a cookie, and every
API request sends an explicit bearer token. Development demo tokens remain
available only when the API is running in development authentication mode.

## Background jobs

Generation uses a transactional outbox. The idempotency key covers the design
hash plus the complete production context. Jobs claim work atomically, retry in
a bounded manner and use content-addressed object keys, preventing duplicates
after worker restarts. A vanished worker lease is requeued before the fourth
attempt and terminalized as failed when the bounded attempt budget is exhausted.

Every content-addressed worker artifact, imported source and external evidence
object is written with an S3 conditional create (`If-None-Match: *`). A 409/412
race is accepted only after a fresh HEAD and streamed SHA-256 prove that digest,
size and content type are exact; a mismatch blocks as non-deterministic and no
code path overwrites the object. This is WORM-like application behavior, not an
object-store retention guarantee. Production still requires provider Object
Lock/versioning, retention policy and exact OCI-image digest verification at
promotion/runtime when regulatory or adversarial deletion protection is needed.

## Version boundaries

The manifest records application, engine, template, rule, material, joint,
machine and postprocessor versions. Changing any production-affecting version
changes the production context hash and creates a distinct job and bundle. The
API also exposes the operations document, validation backplot and setup sheets as
first-class evidence artifacts; CAM review remains locked until all are present.
