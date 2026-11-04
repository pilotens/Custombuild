# Automatic trestle construction B1

The previous trestle example is a 13-part reference study without joinery. It is
preserved as a reference. B1 is a separate **chosen construction**, inspired by the
reference photograph, with side-mounted crossmembers and visible bolts. It is not
a claim that the photograph contains these hidden joints.

## User flow

Open `/assembly`, choose dimensions and the central screening load, then select
**Skapa konstruktion**. The server generates every part and paired machining
operation. The user can open the parts in the existing inspection register or
select **Skapa CAD-kontrollerat ritningspaket**. This queues a durable furniture
review job with an immutable `TrestleWorkspace` source. It does not convert a
bookcase or a furniture-studio draft into another schema.

The default request is explicitly provisional: 2200 × 950 × 750 mm, American
black walnut, 300 N central screening load, 6–12% moisture. It generates:

- 13 individually identified timber components;
- 24 through-bolt connections and five tabletop screws;
- 79 operations, including paired bores, eight lap cuts, recessed fastener access,
  four movement slots and five blind tabletop pilots;
- a 36 mm tabletop selected by the stated deflection calculation;
- grain-oriented blank requirements and +20/+4/+4 mm purchasing allowances;
- a fastener bill counting every nut, washer, bolt and screw once per connection.

The UI gives the selected construction, material and sizing assumptions before
export. Confirmation of dimensions changes only that input's acceptance; it does
not qualify load capacity, supplier stock, fasteners or machining.

## Exact geometry and export

`measured_machining` subtracts actual cylindrical bores, stadium slots, recesses
and lap cuts from native CAD solids, including sloped profiles and circular
tabletop corners. The unmachined reference adapter still rejects operations.
Each finished timber component must remain one valid solid. The audit checks all
78 timber pairs, the 29 fastening lines, two contiguous material intervals per
fastener, at least 2 mm geometric material around its shaft, screw penetration,
nut/thread reach, hardware-to-timber and hardware-to-hardware interference, and
connectivity of all 13 parts. These are nominal geometry checks; the 2 mm check
is not a structural edge-distance criterion or tolerance acceptance.

The tabletop movement check covers all corners of the declared combined X/Y
movement envelope for the four sliding attachments, including washers and access
recesses. The convex capsule constraints then cover its continuous interior.
This proves geometric clearance, not friction-free sliding in assembled timber.

The export contains a PDF, 13 machined component STEPs and one assembled STEP,
78 DXF and 78 SVG face drawings, source JSON, operations, connections, hardware,
cut requirements and validation reports. Each component STEP is imported again;
volume difference must be below 1e-8 relative and bounding-box difference below
0.005 mm. Every declared file is hashed in the manifest. There is no CAM program.

Face drawings use positive local axes, show blank contours and machining features,
and do not claim to be complete hidden-line projections of finished geometry.
Through-hole exits are references on `REFERENCE_EXIT_*` layers, not another
machining instruction. STEP contains the finished solid. Lap recesses specify
square inner corners; tool choice and corner finishing are explicit workshop items.

The API image remains CAD-free. Exact checks run in the worker via the existing
tenant-scoped jobs, transactional outbox, leases, storage quota ledger, immutable
objects and verified download path. Failed geometry or STEP import produces no
accepted package. Preview hash mismatch is rejected before enqueueing. Identical
requests with the same idempotency key reuse the same job; conflicting reuse is
rejected. Edits abort obsolete browser downloads.

After export the UI can open the exact inspection report. The API reads that
evidence from the committed, digest-verified ZIP, verifies the construction and
assembly identities and every joint/bearing result, then returns the inspected
parts. It never promotes a browser-supplied report to CAD evidence. Changes in
the part/room editor invalidate the construction export controls and return to
the ordinary measured review; existing source edits remain in that work file.

The construction hash covers the request, sizing assumptions, component model,
hardware, purchasing allowances and assembly instructions. The separate assembly
hash identifies the geometry. A load change that selects the same thickness still
changes the construction identity and invalidates an earlier export request.

## Qualification boundary

Species-average E from AHEC is reduced by 40% and deflection is multiplied by 1.5
for a declared screening model: simply supported 300 mm strip, central point load
and self-weight, target L/300. No support from the longitudinal beam is credited.
Computed bending stress is reported without inventing a material design strength.
Thickness is selected from 32, 36, 40, 45, 50, 55, 60, 65 and 70 mm.

USDA Wood Handbook chapter 13, table 13-5 supplies the black-walnut tangential
movement coefficient 0.00274 per moisture percentage point. Full interval travel
in both directions plus 1 mm allowance is used. Longitudinal coefficient 0.0001
is explicitly a model assumption. Sources and assumptions travel with the package.

This implementation does **not** establish a whole-table load rating. Joint
strength, racking, tipping, withdrawal, timber/lamination quality, accepted
tolerances and actual hardware product data remain qualification work. Nominal
hardware names are dimensional requirements, not fabricated supplier SKUs.
Minimum purchasing blanks are not claims of stock availability. No physical
measurement, supplier approval, engineering approval or test certificate is
fabricated. `physical_cutting_authorized` remains false throughout.

## Regression work orders

`make product-acceptance PRODUCT=constructed-trestle` runs the five levels:

1. Easy: complete default parts, connections, operations and selected thickness.
2. Medium: 2600 × 1100 × 800 mm with 1500 N screening load; resize and exact CAD.
3. Hard: move a leg into its crossmember; reject, restore and rerun exact CAD.
4. Very hard: export/reimport, 78 DXFs, independent top-pilot coordinates and
   actual empty pilot volumes in reimported STEP.
5. Impossible: 100000 N or zero width is rejected without clamping; the original
   request remains reproducible.

Additional tests cover one-micrometre drilling mismatch, a short bolt, an
overlong screw, a hole outside a sloped profile, compound movement, stale UI
responses, altered snapshots, duplicate exports, tenant isolation and the real
worker/storage/download path. The catalog now contains 80 product work orders.
