"""Versioned validation and production-candidate postprocessors.

No adapter in this package authorizes a physical machine start.  The LinuxCNC
reference adapter deliberately emits no M3/M4, no feed moves and no negative Z;
the separate production adapter emits executable CAM candidates only from an
exact, workshop-bound toolpath document.
"""

from .linuxcnc_production import LinuxCNCProductionPostprocessor
from .linuxcnc_validation import LinuxCNCValidationPostprocessor
from .model import GCodeWord, MachineProgram, ParsedLine, ParsedProgram
from .parser import (
    GCODE_PARSER_VERSION,
    GCODE_SAFETY_VALIDATOR_VERSION,
    GCodeParseError,
    GCodeSafetyError,
    parse_gcode,
    validate_validation_program,
)
from .production_model import (
    CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY,
    EXTERNAL_AXIS_OFFSET_POLICY,
    FEED_SPINDLE_OVERRIDE_POLICY,
    G52_G92_OFFSET_RESET_POLICY,
    G53_TOOL_CHANGE_PATH_COMPLETE,
    HOMING_PREFLIGHT_POLICY,
    LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
    M6_TOOL_TABLE_POLICY,
    M6_WCS_TABLE_POLICY,
    METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
    PROGRAM_RESTART_POLICY,
    SPINDLE_AT_SPEED_POLICY,
    SPINDLE_DWELL_ROLE,
    LinuxCNCProductionMachineProfile,
    LinuxCNCWCSOffset,
    ProductionMachineProgram,
)
from .production_parser import (
    PHYSICAL_AUTHORIZATION_MARKER,
    PRODUCTION_CANDIDATE_POLICY_MARKER,
    PRODUCTION_GCODE_PARSER_VERSION,
    PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
    WORKSHOP_ACCEPTANCE_MARKER,
    ParsedProductionMove,
    ParsedProductionProgram,
    parse_production_program,
    validate_production_program,
)

__all__ = [
    "GCODE_PARSER_VERSION",
    "GCODE_SAFETY_VALIDATOR_VERSION",
    "CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY",
    "G52_G92_OFFSET_RESET_POLICY",
    "G53_TOOL_CHANGE_PATH_COMPLETE",
    "EXTERNAL_AXIS_OFFSET_POLICY",
    "GCodeParseError",
    "GCodeSafetyError",
    "GCodeWord",
    "LinuxCNCValidationPostprocessor",
    "LinuxCNCProductionPostprocessor",
    "LinuxCNCProductionMachineProfile",
    "LinuxCNCWCSOffset",
    "LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION",
    "LINUXCNC_PRODUCTION_POSTPROCESSOR_ID",
    "LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION",
    "M6_TOOL_TABLE_POLICY",
    "M6_WCS_TABLE_POLICY",
    "METRIC_XYZ_IDENTITY_KINEMATICS_POLICY",
    "FEED_SPINDLE_OVERRIDE_POLICY",
    "PROGRAM_RESTART_POLICY",
    "HOMING_PREFLIGHT_POLICY",
    "SPINDLE_AT_SPEED_POLICY",
    "SPINDLE_DWELL_ROLE",
    "MachineProgram",
    "PHYSICAL_AUTHORIZATION_MARKER",
    "ParsedLine",
    "ParsedProgram",
    "ParsedProductionMove",
    "ParsedProductionProgram",
    "PRODUCTION_CANDIDATE_POLICY_MARKER",
    "PRODUCTION_GCODE_PARSER_VERSION",
    "PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION",
    "ProductionMachineProgram",
    "WORKSHOP_ACCEPTANCE_MARKER",
    "parse_gcode",
    "parse_production_program",
    "validate_production_program",
    "validate_validation_program",
]
