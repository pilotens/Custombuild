"""Exact provenance when a saved furniture design enters the production compiler."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .enums import MaterialType
from .furniture import FurnitureWorkspace
from .furniture_dimensions import assert_production_dimensions
from .furniture_engine import FurnitureResult, build_furniture
from .identity import content_hash
from .models import BookcaseDesignSpec, FrozenModel

FURNITURE_PRODUCTION_BRIDGE_VERSION: Literal["furniture-production-1.0.0"] = (
    "furniture-production-1.0.0"
)


def _production_shelving(workspace: FurnitureWorkspace) -> FurnitureResult:
    assert_production_dimensions(workspace.design)
    result = build_furniture(workspace.design)
    if result.shelving_result is None:
        raise ValueError(
            "Tillverkningsberedning stöder ännu endast hyllsystem. "
            "Bord, stolar och byråer bereds via möbelns granskningspaket."
        )
    if result.shelving_result.spec.parameters.door_hinge:
        raise ValueError(
            "Underskåpens gångjärnsinfästning och öppningsrörelse saknar typkvalificering."
        )
    if result.shelving_result.spec.material.material_type == MaterialType.SOLID_WOOD:
        raise ValueError(
            "Massivträ kan exporteras för granskning. Tillverkningsberedning kräver "
            "verifiering av fuktrörelser och förband i den valda materialbatchen."
        )
    return result


def shelving_production_spec(workspace: FurnitureWorkspace) -> BookcaseDesignSpec:
    result = _production_shelving(workspace)
    assert result.shelving_result is not None
    return result.shelving_result.spec


class FurnitureProductionSource(FrozenModel):
    schema_version: Literal["custombuild.furniture-production-source.v1"] = (
        "custombuild.furniture-production-source.v1"
    )
    bridge_version: Literal["furniture-production-1.0.0"] = FURNITURE_PRODUCTION_BRIDGE_VERSION
    workspace: FurnitureWorkspace
    workspace_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    furniture_design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_source(self) -> FurnitureProductionSource:
        if self.workspace_sha256 != content_hash(self.workspace):
            raise ValueError("furniture source workspace checksum differs")
        result = _production_shelving(self.workspace)
        if result.design_hash != self.furniture_design_hash:
            raise ValueError("furniture source does not match the supported saved design")
        return self


def furniture_production_source(workspace: FurnitureWorkspace) -> FurnitureProductionSource:
    result = _production_shelving(workspace)
    return FurnitureProductionSource(
        workspace=workspace,
        workspace_sha256=content_hash(workspace),
        furniture_design_hash=result.design_hash,
    )


def assert_furniture_production_spec(
    source: FurnitureProductionSource, spec: BookcaseDesignSpec
) -> None:
    # Production revision numbers and verified retention are assigned separately.
    # Every geometric, material, load and compiler input must remain identical.
    expected = shelving_production_spec(source.workspace)
    unbound = spec.model_copy(update={"revision": expected.revision, "joint_retention": None})
    if unbound != expected:
        raise ValueError("production inputs differ from the exact saved furniture design")
