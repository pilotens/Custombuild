# Production safety gates

The authoritative [adhesive-free joining policy](ADHESIVE_FREE_JOINING_POLICY.md)
prohibits glue, epoxy, hot-melt, sealant and other chemically bonded retention.
Dry self-locking construction is preferred; otherwise explicit removable
mechanical retention and its verification evidence are required.

The reference LinuxCNC postprocessor emits **validation-only dry-run code**. It
must not enable a spindle, command coolant or move below the configured safe Z.
The generated bundle is not approved for physical cutting.

Every generation job includes `validation/workshop-readiness.json`. The report
separates checksum-bound software evidence from physical workshop evidence,
names the exact action for every missing item and always sets
`physical_cutting_authorized` to `false`. A checksum-bound package may be made
available for design review with CAM explicitly marked `BLOCKED`; in that state
nesting, operations, setup sheets, backplot and controller programs must be
absent rather than inferred. CAM approval and immutable review release continue
to fail closed until the complete CAM evidence set exists and agrees with the
manifest, package-status document and readiness report. The browser cannot turn
missing software evidence or external workshop requirements into trusted
approvals.

Two supported CAM-only blockers have additional hard requirements:

- `STOCK_PROFILE_MISSING` remains visible in the raw DFM report, package status
  and checksum-bound stock-selection snapshot. Required blank sizes and the
  explicitly selected stock/machine context are preserved. The software must not
  substitute a larger sheet or another machine. Stock purchase, nesting,
  operations, tools, setup sheets, backplot, controller programs, placement-label
  indices and operation-derived QA plans remain absent.
- `DFM-GRAIN-001` means that directional sheet material has no exact structured
  X/Y stock-axis binding. Opaque documents and design-review acknowledgements may
  be retained for traceability but cannot verify the axis or unlock nesting/CAM.
  Catalogue-declared non-directional material such as MDF is explicitly not
  subject to this requirement.

The package status document is mandatory for the current manifest format. A
status-stripped package is invalid; it must never be reinterpreted automatically
as a weaker legacy CAM package.

Before any machine adapter can be marked production-verified, a workshop must
record and approve all of the following outside the client-supplied design:

- calibrated machine profile, WCS convention, spindle and travel limits;
- measured tool diameter, runout, holder, stick-out and usable cutting length;
- material batch, measured thickness and relevant machining tests;
- joint-specific coupons and fit/tolerance results;
- independent backplot and material-removal comparison;
- supervised air-cut, reference part and complete prototype furniture build;
- named CNC operator and furniture constructor approval.

The generated assembly manual uses conservative screening thresholds for
two-person lifting (a moving part at least 20 kg or at least 1800 mm in any
dimension), identifies pinch direction and requires a panel-positioning jig for
the first carcass-side closure. These instructions do not replace the workshop's
task-specific lifting, fixture and hand-access risk assessment.

Custombuild does not bypass guards, interlocks or emergency stops and never
starts a physical machine. A wall anchor is not specified until wall substrate
and an approved anchoring system are explicitly known.

## Explicit MVP limitations

- Construction rules are screening calculations, not certification.
- `CB-JOINT-001` covers only local fixed-DADO bearing/shear under the declared
  evenly distributed static row load. It does not verify self-locking retention,
  mechanical pull-out, edge breakout, impact, fatigue, moisture/batch effects or
  the complete cabinet load path. Physical coupons and a prototype remain
  mandatory.
- Adjustable shelf pins cannot pass construction validation until exact,
  versioned manufacturer capacity and material/thickness applicability are added;
  the MVP intentionally reports missing evidence instead of assuming a value.
- Only rectangular bookcase/cabinet parts are authoritative.
- JPG/PNG/WebP import performs deterministic client-side boundary and grid-line
  detection and creates an editable parametric concept. It cannot infer scale,
  depth, material or hidden joints reliably. Dimensions, layout, material and
  construction assumptions must therefore be confirmed, and the server binds the
  confirmation to the current model fingerprint. A changed or unconfirmed image
  concept can never become CAM directly.
- The named reference machine is a generic LinuxCNC 3-axis router profile. No
  Biesse, Homag or other proprietary format is claimed.
- CadQuery/OpenCascade must be present in the worker for STEP generation. A
  failed CAD export blocks completion; no placeholder STEP/GLB is emitted.
