# Skänk S1

S1 is a chosen construction inspired by the five-front sideboard photograph.
It does not recover dimensions, hidden joints, timber species or hardware from pixels.
The default is 2400 × 450 × 750 mm, with 180 mm legs. The UI under `/assembly`
allows width 1800–2600, depth 400–550, height 650–850 and legs 140–220 mm;
the carcass must remain at least 450 mm high. Six legs include an explicitly
chosen central pair supporting the long carcass.

## Component and hardware identity

The same assembly is used for part selection, isolation, removal, operation lists,
CAD machining, STEP, six-face DXF/SVG, PDF and the standalone HTML viewer.

| Register | Count | Contents |
|---|---:|---|
| Wood parts | 38 | Top/bottom, two sides, four dividers, five backs, five doors, five shelves, six legs, four base rails, five latch blocks |
| Wood joints | 90 | Forty dowels and fifty screws, each with named mating operations |
| Fitting instances | 35 | Ten hinges, twenty shelf pins, five push latches |
| Operations | 310 | Cups, pilots, through holes, head recesses and blind fitting bores |
| Wood pairs | 703 | Native solid intersection checks |

Carcass top/bottom/sides use 24 mm balanced walnut-veneered birch plywood;
dividers/shelves 18 mm, fronts 19 mm and backs 6 mm. Legs, base rails and latch
blocks are solid walnut. All dimensions include finished edge veneer. Grain-aware
purchase blanks include 10 mm length/width allowance for panels, or 20/4/4 mm
for solid timber. Panel thickness is not increased for planing through veneer.
No unverified supplier stock length is called a standard or assumed available.

Front seams are 3 mm. The first door is inset and doors 2–5 overlap the divider
by 7.5 mm. Ten free-swing hinges are selected: two Blum 70T3750.TL and eight
70T3650.TL, each with a 175H7100 mounting plate. Cup drilling is Ø35 × 13 mm,
with TB7/TB6 and the screw-on 45/9.5 pattern. Plate holes have 32 mm pitch and
global Y=57.5 mm. Twenty 609.1500 cup screws and twenty 609.1700 plate screws
are counted once, plus five 609.1500 catch screws.

Five long magnetic TIP-ON 956A1004 latches use Ø10 × 76 mm bores in separate
blocks and a 3.1 mm nominal gap to the front back face. They use free-swing
hinges, not sprung BLUMOTION hinges. Shelf pins are explicitly a nominal
Ø5 × 12 mm specification, with 8 mm in the panel and 4 mm projection;
opposing holes leave a 2 mm web in an 18 mm divider. Shelf pins and generic
wood screws/dowels still require a qualified supplier article and material.

Primary planning sources:

- https://publications.blum.com/2024/catalogue/en/76/
- https://publications.blum.com/2024/catalogue/en/77/
- https://publications.blum.com/2024/catalogue/en/153/
- https://publications.blum.com/2024/catalogue/en/192/
- https://d2.blum.com/services/BEC003/tip0001-ep-234_ep_dok_bau_%24sen_%24aof_%24v6.pdf

## What the audit establishes

`audit_sideboard` builds machined OpenCascade solids, checks that every cut is
inside its blank, detects intersecting cuts and actual wood overlap, and checks
nominal dowel/screw axes, engagement, pilot depth, head recess and clashes with
other fasteners and wood. Purchased fittings are separate graph entities with
explicit mounts and catalog-based drilling checks; they are not fabricated CAD
models of the vendor's complete mechanisms.

Rear coverage is measured against five actual opening volumes. A 150 mm strip
cannot satisfy this check. A 2 mm inward translation of each front is tested
against wood. This is a push-clearance check, **not a 110° hinge-motion test**.
All 38 parts must belong to the contact graph. Every finished STEP is imported
again and checked for a valid single solid, bounding differences below 0.005 mm
and relative volume difference below 1e-8. The mounted STEP contains 38 solids.

The first native trial found leg screws crossing rail dowels and latch-block
screws extending beyond their pilots. Leg screws moved to X-center with Y=16/34
mm, and latch-block pilots became 20 mm deep. Both faults have executable
regression cases that reintroduce the fault and then rerun the corrected design.

`wood_and_boring_checks_passed` is deliberately distinct from unrestricted
geometric approval. The API returns `geometry_method="machined_wood"` with
separate fitting checks. Full hinge kinematics, full purchased-hardware body
collision checks, joint strength, anti-tip qualification, load rating, stock
confirmation and workshop acceptance remain open. `physical_cutting_authorized`
is always false, including after dimensions are confirmed.

## App, persistence and exports

POST `/v1/constructions/sideboard` creates the deterministic chosen design.
POST `/v1/constructions/sideboard/exports` requires its exact construction hash,
uses a tenant-scoped idempotency key and stores a distinct versioned workspace.
The existing durable furniture-review job, transactional outbox, worker lease,
immutable object storage and digest-verified browser download are reused.
Other editors cannot overwrite this workspace schema.

GET `/v1/constructions/sideboard/projects/{project_id}/exports/{job_id}/inspection`
reads bounded CAD evidence from the committed artifact with tenant, identity,
revision, storage metadata and complete check-set validation. Browser-submitted
claims cannot enable native evidence. Changes invalidate the result and cancel
stale export callbacks. Empty fitting registers remain omitted to preserve old
measured-assembly hashes and existing B1 snapshots.

The ZIP contains the standalone `viewer.html`, PDF, exact source JSON, cut and
hardware lists, named connections, 38 part STEP files, 228 DXF files, 228 SVG
files, assembly STEP and per-file digests. DXF uses millimetres; depth in layer
names is micrometres. Opposite-face exits are reference geometry, not duplicate
machining. The HTML overview is selectable but not a replacement for machined STEP.
Its face ordering uses projected overlap and depth on that overlap, shared with
the app's fitting-based assembly view. This fixes long panels being drawn through
fronts when sorting only by a face's average depth. Incomplete cyclic ordering
is labelled instead of being presented as a geometric result.

## Five acceptance levels

Run `make product-acceptance PRODUCT=constructed-sideboard`.

| Level | Independent acceptance |
|---|---|
| Easy | 38/90/35/310 register totals and no manufacturing permission |
| Medium | Native audits at 1800/400/650 and 2600/550/850 mm |
| Hard | Shortened back fails actual coverage; full back passes on rerun |
| Very hard | 38 STEP round trips, 228 DXF; first door cup centers 24.5/80 and 24.5/436 mm, Ø35 × 13, uncut front skin |
| Impossible | Zero width, six doors, too-low carcass and unsupported solid case rejected without silently changing the request |

Focused unit and integration tests additionally exercise crossed fasteners,
short pilots, intersecting shelf bores, shifted hinge plates, authentication,
tenant isolation, idempotency, draft isolation, snapshot tampering, immutable
downloads and the complete real worker export. A passing software test does not
convert the explicit physical acceptance items above into verified facts.
