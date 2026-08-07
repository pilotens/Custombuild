"""Custombuild CAM validation primitives."""

from .model import MoveKind, RemovalEnvelope, ValidationBackplot, ValidationMove
from .validation import (
    CAM_BACKPLOT_VERSION,
    CAM_VALIDATION_VERSION,
    CAMValidationError,
    CAMValidationResult,
    backplot_svg,
    build_validation_backplot,
    parse_operations_json,
    require_valid_operations,
    theoretical_removal_envelopes,
    validate_operations_document,
)

__all__ = [
    "CAM_BACKPLOT_VERSION",
    "CAM_VALIDATION_VERSION",
    "CAMValidationError",
    "CAMValidationResult",
    "MoveKind",
    "RemovalEnvelope",
    "ValidationBackplot",
    "ValidationMove",
    "backplot_svg",
    "build_validation_backplot",
    "parse_operations_json",
    "require_valid_operations",
    "theoretical_removal_envelopes",
    "validate_operations_document",
]
