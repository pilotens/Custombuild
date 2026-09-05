# Reference postprocessor

`LinuxCNCValidationPostprocessor` targets the documented syntax of a generic
LinuxCNC three-axis controller, but only in `VALIDATION_DRY_RUN` mode. It emits
safe-Z positioning traces without spindle start, coolant, tool changes, feed
moves or negative Z. The parser rejects those constructs on re-entry.
The canonical preamble explicitly stops the spindle and coolant, suspends any
inherited LinuxCNC G52/G92 offset with `G92.2`, and selects the bound WCS before
the first motion. After the final safe-Z retract, it stops spindle/coolant again
and restores the suspended offset state with `G92.3` before the standalone
`M2`. `M30` is forbidden because LinuxCNC may use it to exchange pallet
shuttles, behavior that the generic router profile does not attest. The
validator requires that exact order and rejects G10, G52, G92,
G92.1, scaling, rotation commands and any other coordinate mutation.

Selecting G54/G55 cannot prove the controller's persisted WCS XYZ offsets or
its G10 L2 rotation. The generated file therefore carries a canonical
`CUSTOMBUILD_EXECUTION_POLICY` marker that prohibits machine execution until
those values and the complete controller/setup state have been independently
attested. The marker is a review policy, not a substitute for that evidence.
Safety validation is fail-closed unless the caller supplies the exact setup
WCS, XY travel bounds, required safe Z and maximum Z. The reference adapter
binds those values from the versioned operations document and rejects machine,
tool-catalogue, material-thickness, stepdown and cutter-envelope drift before
emitting a program.

It is intentionally **not production-approved**. Production use requires a
calibrated machine profile, golden-file review, independent backplot and removal
simulation, air-cut, test material, reference parts and operator approval.
