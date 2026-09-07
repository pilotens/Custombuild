"""Server-owned catalogues for multi-family design planning.

Layout hardware profiles intentionally have no invented manufacturer SKU,
capacity, boring pattern or production qualification. They allow dimensional
design to proceed before a real, documented system is selected.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal

from pydantic import Field

from .furniture import HardwareSelection, MaterialSelection
from .identity import content_hash
from .materials import (
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)
from .models import FrozenModel, MaterialVersion, StableKey

CATALOG_VERSION = "furniture-catalog-1.0.0"
MATERIAL_CATALOG = MappingProxyType(
    {
        item.material_id: item
        for item in (
            screening_birch_plywood_18(),
            screening_birch_plywood_6(),
            screening_mdf_18(),
            screening_mdf_6(),
        )
    }
)


class HardwareProfile(FrozenModel):
    catalog_id: StableKey
    version: StableKey = "layout-1.0.0"
    name: str
    family: Literal["table", "chest_of_drawers"]
    production_qualified: Literal[False] = False
    source: str = "Dimensional planning fixture; no manufacturer qualification"
    runner_length_um: int = Field(default=0, ge=0)
    side_clearance_um: int = Field(default=0, ge=0)
    travel_um: int = Field(default=0, ge=0)
    rear_clearance_um: int = Field(default=10_000, ge=0)
    minimum_thickness_um: int = 17_000
    maximum_thickness_um: int = 19_000
    required_evidence: tuple[str, ...]


HARDWARE_CATALOG = MappingProxyType(
    {
        item.catalog_id: item
        for item in (
            HardwareProfile(
                catalog_id="panel-table-connectors-layout",
                name="Demonterbart gavelbord – beslag återstår att välja",
                family="table",
                required_evidence=(
                    "connector_geometry",
                    "boring_pattern",
                    "joint_capacity",
                    "racking_test",
                    "assembly_sequence",
                ),
            ),
            HardwareProfile(
                catalog_id="drawer-side-mount-450-layout",
                name="Lådlayout 450 mm – tillverkarsystem återstår att välja",
                family="chest_of_drawers",
                runner_length_um=450_000,
                side_clearance_um=12_700,
                travel_um=450_000,
                required_evidence=(
                    "manufacturer_sku",
                    "boring_pattern",
                    "runner_capacity",
                    "drawer_retention",
                    "open_drawer_stability",
                ),
            ),
            HardwareProfile(
                catalog_id="drawer-side-mount-400-layout",
                name="Lådlayout 400 mm – tillverkarsystem återstår att välja",
                family="chest_of_drawers",
                runner_length_um=400_000,
                side_clearance_um=13_000,
                travel_um=400_000,
                required_evidence=(
                    "manufacturer_sku",
                    "boring_pattern",
                    "runner_capacity",
                    "drawer_retention",
                    "open_drawer_stability",
                ),
            ),
        )
    }
)


def resolve_material(selection: MaterialSelection) -> MaterialVersion:
    material = MATERIAL_CATALOG.get(selection.material_id)
    if material is None or material.version != selection.version:
        raise ValueError("unknown material profile or retired material version")
    if not (
        material.min_supported_thickness_um
        <= selection.measured_thickness_um
        <= material.max_supported_thickness_um
    ):
        raise ValueError("measured thickness is outside the selected material profile")
    return material


def resolve_hardware(selection: HardwareSelection, family: str) -> HardwareProfile:
    hardware = HARDWARE_CATALOG.get(selection.catalog_id)
    if hardware is None or hardware.version != selection.version:
        raise ValueError("unknown hardware profile or retired hardware version")
    if hardware.family != family:
        raise ValueError("hardware profile is incompatible with the furniture family")
    return hardware


def furniture_catalog() -> dict[str, object]:
    payload: dict[str, object] = {
        "version": CATALOG_VERSION,
        "families": [
            {
                "id": "shelving",
                "name": "Hyllsystem",
                "geometry_supported": True,
                "qualification": "structural_screening",
            },
            {
                "id": "table",
                "name": "Bord med gavlar",
                "geometry_supported": True,
                "qualification": "layout_only",
            },
            {
                "id": "chest_of_drawers",
                "name": "Byrå",
                "geometry_supported": True,
                "qualification": "layout_only",
            },
        ],
        "materials": [item.model_dump(mode="json") for item in MATERIAL_CATALOG.values()],
        "hardware": [item.model_dump(mode="json") for item in HARDWARE_CATALOG.values()],
    }
    return {**payload, "fingerprint": content_hash(payload)}
