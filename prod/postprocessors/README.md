# Reference postprocessor

`LinuxCNCValidationPostprocessor` targets the documented syntax of a generic
LinuxCNC three-axis controller, but only in `VALIDATION_DRY_RUN` mode. It emits
safe-Z positioning traces without spindle start, coolant, tool changes, feed
moves or negative Z. The parser rejects those constructs on re-entry.

It is intentionally **not production-approved**. Production use requires a
calibrated machine profile, golden-file review, independent backplot and removal
simulation, air-cut, test material, reference parts and operator approval.
