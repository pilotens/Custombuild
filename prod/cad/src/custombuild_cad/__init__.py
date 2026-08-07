"""Authoritative CadQuery/OpenCascade export boundary."""

from .adapter import (
    CAD_KERNEL_CONTRACT_VERSION,
    CADQUERY_ADAPTER_VERSION,
    CADQUERY_DISTRIBUTION_VERSION,
    OPENCASCADE_DISTRIBUTION,
    OPENCASCADE_DISTRIBUTION_VERSION,
    CADArtifacts,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    UnsupportedCADFeatureError,
    cad_capability_status,
)

__all__ = [
    "CADQUERY_ADAPTER_VERSION",
    "CADQUERY_DISTRIBUTION_VERSION",
    "CAD_KERNEL_CONTRACT_VERSION",
    "CADArtifacts",
    "CADDependencyUnavailable",
    "CADExportError",
    "CadQueryAdapter",
    "UnsupportedCADFeatureError",
    "OPENCASCADE_DISTRIBUTION",
    "OPENCASCADE_DISTRIBUTION_VERSION",
    "cad_capability_status",
]
