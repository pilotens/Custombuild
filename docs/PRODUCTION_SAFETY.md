# Production safety gates

The reference LinuxCNC postprocessor emits **validation-only dry-run code**. It
must not enable a spindle, command coolant or move below the configured safe Z.
The generated bundle is not approved for physical cutting.

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
  evenly distributed static row load. It does not verify glue strength, edge
  breakout, impact, fatigue, moisture/batch effects or the complete cabinet load
  path. Physical coupons and a prototype remain mandatory.
- Adjustable shelf pins cannot pass construction validation until exact,
  versioned manufacturer capacity and material/thickness applicability are added;
  the MVP intentionally reports missing evidence instead of assuming a value.
- Only rectangular bookcase/cabinet parts are authoritative.
- PNG/JPEG/PDF/DXF inspection currently stops at a validated upload, content hash,
  explicit `null` assumptions and a calibration requirement. Image interpretation
  and calibrated DesignSpec conversion are not implemented in the production MVP;
  an uploaded file can never become CAM directly.
- The named reference machine is a generic LinuxCNC 3-axis router profile. No
  Biesse, Homag or other proprietary format is claimed.
- CadQuery/OpenCascade must be present in the worker for STEP generation. A
  failed CAD export blocks completion; no placeholder STEP/GLB is emitted.
