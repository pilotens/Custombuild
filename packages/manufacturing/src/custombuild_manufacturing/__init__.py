"""Deterministic manufacturing engine for Custombuild."""

from typing import Any

from .dfm import DFMValidator
from .errors import ArtifactError, ManufacturingError, NestingError, ProductionBlockedError
from .model import (
    CAMOperation,
    DFMIssue,
    DFMReport,
    FeatureKind,
    MachineProfile,
    ManufacturingFeature,
    NestingLayout,
    OperationKind,
    OperationsDocument,
    PanelAxisMapping,
    PartInstance,
    PartSpec,
    Placement,
    Point2D,
    Rect,
    Setup,
    Severity,
    Side,
    StockSheet,
    ToolSpec,
    canonical_json_bytes,
    expand_part_instances,
    sha256_hex,
    um_to_mm,
)
from .nesting import DeterministicNester, validate_layout
from .operations import generate_operations_document
from .package import (
    MANIFEST_CONTEXT_HASH_FIELDS,
    ArtifactFile,
    ManifestContext,
    build_deterministic_zip,
    build_manifest,
    default_artifacts,
    read_and_verify_package,
)
from .profiles import linuxcnc_reference_router_1325


def build_production_bundle(*args: Any, **kwargs: Any) -> Any:
    """Lazily load the cross-package production pipeline."""

    from .pipeline import build_production_bundle as build

    return build(*args, **kwargs)


__all__ = [
    "ArtifactError",
    "ArtifactFile",
    "CAMOperation",
    "DFMReport",
    "DFMIssue",
    "DFMValidator",
    "DeterministicNester",
    "FeatureKind",
    "MachineProfile",
    "ManifestContext",
    "MANIFEST_CONTEXT_HASH_FIELDS",
    "ManufacturingError",
    "ManufacturingFeature",
    "NestingError",
    "NestingLayout",
    "OperationKind",
    "OperationsDocument",
    "PanelAxisMapping",
    "PartInstance",
    "PartSpec",
    "Placement",
    "Point2D",
    "ProductionBlockedError",
    "Rect",
    "Severity",
    "Setup",
    "Side",
    "StockSheet",
    "ToolSpec",
    "build_deterministic_zip",
    "build_manifest",
    "build_production_bundle",
    "canonical_json_bytes",
    "default_artifacts",
    "expand_part_instances",
    "generate_operations_document",
    "linuxcnc_reference_router_1325",
    "read_and_verify_package",
    "sha256_hex",
    "um_to_mm",
    "validate_layout",
]
