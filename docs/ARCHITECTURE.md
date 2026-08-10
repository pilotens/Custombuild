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

The browser provides a responsive preview but cannot generate release artifacts.

## Semantic editing boundary

The guided browser editor now operates above `DesignSpec` through furniture-aware
commands and snap targets. A drag/drop interaction represents an intent such as
"add a shelf row to this bay" or "add a divider to this carcass". The pointer
location is preview metadata only; it is never interpreted as a drill, pocket or
machine coordinate.

The domain package exposes a frozen `SemanticDesignDocument`, versioned snap
relations and a fail-closed compiler. The compiler may only mutate fields already
represented by the current production template. `DesignSpec 1.0` therefore
supports adding/removing equally distributed shelf rows and dividers plus the
semantic presence of the back and plinth. Arbitrary movement remains blocked
until custom positions are represented and validated end to end.

AI is restricted to attributed semantic proposals. An AI-originated proposal
requires explicit user confirmation and then passes through the same deterministic
domain, rule, CAD, DFM and approval gates. AI cannot write manufacturing features,
operations, toolpaths or G-code.

See [SEMANTIC_DESIGN_ARCHITECTURE.md](SEMANTIC_DESIGN_ARCHITECTURE.md) for the
complete interaction and trust model.

## CAD boundaries

CadQuery/OpenCascade remains the authoritative CAD adapter. It consumes the
resolved domain parts and manufacturing features and atomically produces genuine
STEP and GLB artifacts. Unsupported authoritative features block export.

An optional headless FreeCAD bridge can import the authoritative STEP into a
native FCStd project for future TechDraw, Assembly and CAM integrations. The FCStd
file is explicitly marked as derivative and non-authoritative; edits made in
FreeCAD never flow back into `DesignSpec` or manufacturing operations. This keeps
FreeCAD replaceable and invisible to ordinary users while allowing selected
capabilities to be adopted incrementally.

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

Released design revisions are immutable. A relevant edit always creates a new
revision and therefore cannot reuse old approvals, CAM or artifacts.

## Background jobs

Generation uses a transactional outbox. The idempotency key covers the design
hash plus the complete production context. Jobs claim work atomically, retry in a
bounded manner and use content-addressed object keys, preventing duplicates after
worker restarts. A vanished worker lease is requeued before the fourth attempt
and terminalized as failed when the bounded attempt budget is exhausted.

## Version boundaries

The manifest records application, engine, template, rule, material, joint,
machine and postprocessor versions. Changing any production-affecting version
changes the production context hash and creates a distinct job and bundle.
