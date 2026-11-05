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

## Optional construction checks in the editor

Open `/assembly`, choose the S1 front layout and enter the work order's loads.
Front shares are whole percentages of the five **finished wooden fronts**
excluding the six gaps. The photo preset uses 21/17/23/20/19 as estimates.
At 2400 mm this gives 490.14, 396.78, 536.82, 466.80 and 443.46 mm.
A seam moves the divider, dowel/plate/pin bores, shelf, full back, latch and
corresponding drawings together. The default retains equal internal openings.
Confirming the dimensions fixes that input on export. Editing the form clears
the confirmation and discards previous results. Legacy requests without review
data retain their original construction hashes and export/inspection behavior.

### Loads and workshop measurements

The review compares the actual generated shelf span and depth against the chosen
uniform payload plus shelf self weight. It uses a simply supported beam,
I = b t³ / 12, long-term deflection 5 W L³ / (384 E I), and bending stress
3 W L / (4 b t²). The span limit is min(3 mm, L/200). One pin at each end is
screened for the full end reaction W/2. This is a beam estimate for symmetric
loading. Local bearing damage, plate behavior at point supports, joints and
unusual load positions require separate qualification.

By default the existing screening material uses 680 kg/m³, 6500 MPa, 20%
stiffness uncertainty and 50% creep allowance. A sourced supplier profile may
instead specify density, **effective service modulus including creep** along
the span, and allowable service bending stress. Optional pin working load must
include a source. Missing declarations stay visible. The four measured panel
thicknesses are checked at model precision. Edge-position error is counted at
both meeting edges. Bore overshoot is counted on both opposing divider holes.
A batch that differs from the actual modeled thickness requires revision.

### Hinge geometry and file import

The automatic gap check uses the Blum 2024/25 catalogue p.77 for R1, MD0, the
chosen TB6/TB7 arms and the side-adjustment allowance. The bounded table covers
18–20 mm fronts, rounding thickness up to the next tabulated value. For 19 mm
the requirement is 0.9 + 0.4 mm. This tests the planning rule at each hinge
edge. For a custom travel analysis import a UTF-8 JSON **array** (maximum two
profiles and 64 KiB). The API exposes the `HingePath` schema. Each entry needs:

| Field | Convention |
|---|---|
| article / plate | 70T3750.TL or 70T3650.TL / 175H7100 |
| front_mm / cup_distance_mm | 19 / 7 for inset, 19 / 6 for half-overlay |
| source | Supplier document or measured fixture and revision |
| poses | Each has angle_deg, dx_mm, dy_mm. Start (0,0,0), finish 110°, strictly increasing integer angles, at most 5° apart |
| Translation origin | Displacement of the front **left-front corner** from its closed position. +X goes right, +Y goes into the cabinet |
| Rotation | Clockwise in XY as seen from above. Interpolate displacement and angle linearly between the supplied poses |

The browser can scrub an imported article path from 0° to 110° in plan view. For each door the other doors stay shut. The checker compares the door's bounding
rectangle against stationary wood envelopes throughout the modeled travel. It
certifies an interval only when its separation exceeds the remaining tolerance
and an upper bound on the motion of every corner. Otherwise it subdivides and
reports the affected part/angle or an inconclusive interval. The result applies
to the interpolated model. A supplier file's provenance and interpolation error
are not independently authenticated. No fixed pivot is provided as a substitute
for Blum mechanism data. Hardware body collisions and the physical fit stay open.

### Native stability and base protocol

The CAD job computes mass and centers from the 38 machined wood solids with
assumed densities. Loads include shelf payloads, a top payload at the adverse
edge and horizontal force at the cabinet top. It compares restoring and
overturning moments at all four support lines of the actual tapered feet under
empty, fully loaded and worst bounded partial loading. This static screen assumes
closed doors, six bearing feet on a level floor, no wall restraint and omits
purchased-hardware mass. Sliding, dynamic loads and open-door stability are
outside the model.

An optional combined base-test record declares the tested assembly SHA, measured
batch ID, source, total vertical force including dead weight and simultaneous
horizontal force for case S1-closed-combined-v1. The worker compares both loads.
A changed assembly, mismatched batch or exceeded force stays visible. A matching
record means only that the calculated demands lie inside the **declared** test
case. The app does not authenticate a workshop's report or assign a general load rating.

The PDF and `validation/planning.json` accompany the immutable export; the
native inspection returns the four tipping reserves to the browser. Changes to
loads, profiles or evidence change the construction identity. Only a change to
modeled wood/assembly evidence changes its separate assembly identity. All
physical cutting and qualification flags stay false.
