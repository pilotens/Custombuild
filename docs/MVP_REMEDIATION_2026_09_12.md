# MVP remediation after the 2026-09-12 audit

The target is a reproducible design-to-CAM workflow for a measured first article.
A software PASS is not physical acceptance. Do not increase readiness scores
based on test counts alone.

## Priority 1 — trustworthy material removal

Implemented:

- Finish every area operation's inset boundary at every cutting depth. Raster
  lane spacing alone does not clear the scallops at lane ends.
- Independently require full-depth boundary segments as well as raster coverage
  and the existing exact dogbone cycles. Old raster-only candidates fail.
- Regression tests measure distance from a nominal edge point to actual cutter
  centre segments for both raster orientations, with and without dogbones.
  The old path leaves more than 0.20 mm at the sample; the new path clears it.
- The complete shelving integration case samples all 45 grooves' straight edges
  against actual toolpath segments, independently of the PASS label.
- Open groove mouths extend cutter centres to the actual part edge. Independent
  samples include the mouth corners in both orientations, and the verifier
  rejects the former inset-only path that left approximately 0.70 mm there.
- An open-edge declaration must correspond to a real boundary of its source
  part. Generator, source validation and verifier reject false internal openings.
  Stock, neighbours and fixtures include the cutter's overhang; DXF extents
  continue to describe the actual drawn geometry.

Recompile and reverify any existing area-cutting candidate using this version.
Old review results do not establish correct removal. The existing source and
verifier provenance bindings remain in force. Physical cutter measurement,
material response, workholding and joint-fit measurements remain required.

## Priority 2 — recoverable editing

Implemented: preview availability no longer controls JSON workspace download or
attempting to save a revision. Revision saving still goes through the server's
validation. Invalid numeric inputs and unapplied profile changes still block
these actions, since exporting would otherwise silently omit entered values.
Production and review exports retain their existing revision/preview gates.

## Priority 3 — a buildable customer design

The current 4340 × 2540 × 280 mm example is an unapproved envelope, not a
manufacturing design. Preserve its explicit unknowns. Do not silently replace
material, choose installation allowances, reduce book loading, add hidden
splices, or treat a stock-fit result as structural qualification.

Implemented: [explicit module planning](MODULE_PLANNING.md) produces bounded,
separate carcass drafts with canonical geometry, exact overall dimensions,
preserved material/installation requirements and load accounting. The complete
plan and individual workspaces can be downloaded without changing the source
project. Their stock fit, rules and unresolved assembly requirements remain
visible. This supports design review; it does not qualify intermodule joints,
stacking loads, anchoring or production.

Required decisions and evidence, in order:

1. Confirm the arrangement, actual useful book load, trim location/function,
   installation allowances and wall/floor anchoring conditions.
2. Choose continuous stock with demonstrated machine capacity, or explicitly
   design separate modules with their assembly and anchoring. Shelf-bay
   suggestions do not split full-width top and bottom panels.
3. Select visible/hidden materials with actual thickness, batch and supported
   structural properties. Solid oak cannot inherit plywood properties.
4. Model and qualify the actual mechanical joint system and its complete
   machining features. Keep unsupported furniture families blocked.
5. Bind the selected shop's machine, controller, tools, recipes, stock,
   workholding and coordinate registration to the exact design revision.
6. Run the positive authenticated production workflow and independently verify
   its actual executable package, including runtime machine requirements.
7. Cut and measure a joint coupon, then a representative assembled section.
   Record agreed tolerances, actual measurements and load/retention results.
8. Release the full first article only after the preceding evidence passes.

No physical evidence or workshop selection is supplied by this change. The
full customer bookcase is not released for the approximately SEK 20,000 trial.
