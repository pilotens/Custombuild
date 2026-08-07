from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import DesignStatus, JobStatus


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=4000)
    furniture_type: Literal["bookcase"] = "bookcase"


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    furniture_type: str
    current_revision: int
    archived: bool
    created_at: datetime
    updated_at: datetime


class BookcasePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float = Field(default=900, ge=250, le=4000)
    height_mm: float = Field(default=2000, ge=300, le=4000)
    depth_mm: float = Field(default=320, ge=100, le=1200)
    material_id: Literal["mdf", "birch-plywood"] = "mdf"
    nominal_thickness_mm: float = Field(
        default=18,
        ge=18,
        le=18,
        description="The production MVP catalogue contains nominal 18 mm carcass sheets.",
    )
    measured_thickness_mm: float = Field(
        default=18,
        ge=17,
        le=19,
        description="MVP catalogue range for nominal 18 mm carcass sheet material.",
    )
    shelf_count: int = Field(default=5, ge=0, le=40)
    shelf_mount: Literal["fixed", "adjustable"] = "fixed"
    load_per_shelf_kg: float = Field(default=30, ge=0, le=500)
    back_panel: bool | Literal["none", "surface_mounted", "inset_groove"] = True
    plinth: bool = True
    plinth_height_mm: float | None = Field(default=None, ge=0, le=500)
    divider_count: int = Field(default=0, ge=0, le=16)
    edge_band_mm: float = Field(default=1, ge=0, le=5)
    joint_system: Literal["dado"] = Field(
        default="dado",
        description=(
            "Only DADO has a verified domain-to-CAM-to-assembly path in the production MVP. "
            "Other catalogue joint types are explicitly capability-blocked."
        ),
    )
    reinforcement_mode: Literal["manual", "auto"] = "manual"
    wall_anchor_required: bool = False
    wall_anchor_verified: bool = False


class DesignVersionCreate(BaseModel):
    spec: BookcasePreviewInput


class DesignVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    revision: int
    status: DesignStatus
    design_hash: str
    context_hash: str
    spec_json: dict[str, Any]
    result_json: dict[str, Any]
    engine_version: str
    template_version: str
    rule_version: str
    immutable: bool
    created_at: datetime


class GenerationRequest(BaseModel):
    stock_width_mm: float = Field(default=2440, gt=0, le=10_000)
    stock_height_mm: float = Field(default=1220, gt=0, le=5_000)
    stock_count: int = Field(default=4, ge=1, le=100)
    back_stock_width_mm: float = Field(default=2440, gt=0, le=10_000)
    back_stock_height_mm: float = Field(default=1220, gt=0, le=5_000)
    back_stock_count: int = Field(default=2, ge=1, le=100)
    machine_profile_id: str = "custombuild-router-1325-linuxcnc"
    postprocessor_id: str = "linuxcnc-validation-1.0.0"
    include_step: bool = True
    include_validation_program: bool = True


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    design_version_id: str
    status: JobStatus
    production_context_hash: str
    production_engine_context_json: dict[str, Any]
    attempts: int
    error: str | None
    result_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ArtifactRead(BaseModel):
    id: str
    kind: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    content_type: str
    download_url: str
    download_path: str


class WarningOverrideCreate(BaseModel):
    rule_id: str = Field(pattern=r"^CB-[A-Z]+-[0-9]{3}$")
    reason: str = Field(min_length=10, max_length=2000)


class ApprovalCreate(BaseModel):
    approval_type: Literal["design", "cam"]
    reason: str = Field(min_length=5, max_length=2000)
    generation_job_id: str | None = Field(default=None, min_length=36, max_length=36)
    warning_overrides: list[WarningOverrideCreate] = Field(default_factory=list, max_length=20)


class ReleaseCreate(BaseModel):
    release_number: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,39}$")
    confirmation: Literal["RELEASE"]


class ReleaseRead(BaseModel):
    release_id: str
    release_number: str
    status: Literal["released"]
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    machine_use: Literal["validation_only"]


class ImportInspection(BaseModel):
    import_id: str
    media_type: str
    size_bytes: int
    furniture_type: Literal["bookcase"] | None
    furniture_type_confidence: float
    status: Literal["needs_calibration"]
    assumptions: list[dict[str, Any]]
    unknown_fields: list[str]
