"""Authoritative CAD export and optional FreeCAD interchange boundaries."""

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
from .freecad_bridge import (
    FREECAD_BRIDGE_VERSION,
    FREECAD_PROJECT_CONTRACT_VERSION,
    FreeCADBridgeError,
    FreeCADDependencyUnavailable,
    FreeCADImportError,
    FreeCADProjectArtifacts,
    FreeCADProjectBridge,
    freecad_bridge_status,
    render_freecad_import_script,
)

__all__ = [
    "CADQUERY_ADAPTER_VERSION",
    "CADQUERY_DISTRIBUTION_VERSION",
    "CAD_KERNEL_CONTRACT_VERSION",
    "CADArtifacts",
    "CADDependencyUnavailable",
    "CADExportError",
    "CadQueryAdapter",
    "FREECAD_BRIDGE_VERSION",
    "FREECAD_PROJECT_CONTRACT_VERSION",
    "FreeCADBridgeError",
    "FreeCADDependencyUnavailable",
    "FreeCADImportError",
    "FreeCADProjectArtifacts",
    "FreeCADProjectBridge",
    "OPENCASCADE_DISTRIBUTION",
    "OPENCASCADE_DISTRIBUTION_VERSION",
    "UnsupportedCADFeatureError",
    "cad_capability_status",
    "freecad_bridge_status",
    "render_freecad_import_script",
]
