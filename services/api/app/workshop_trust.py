"""Cryptographic trust core for external workshop attestations.

The attestations verified here are immutable *sidecars*.  They bind the exact
base bundle and manifest, but are never members of either object (which would
create a hash cycle).  This module deliberately does not authorize physical
cutting.  A successfully verified three-stage chain is only eligible for the
separate release-review boundary that may be integrated later.

No issuer keys, workshop evidence, nonces, or permissive defaults are bundled
with the application.  Every trusted key and every expected one-time nonce is
server-owned input to verification.  Atomic nonce issuance and consumption are
deliberately a persistence-boundary responsibility; successful verification
returns the three nonce digests that the integrating transaction must consume.

Typed evidence records cryptographically commit to the structured facts signed
by maker and checker.  The signatures are the authority for those facts; the
records are immutable audit evidence, not a claim that this software directly
observed a physical measurement.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

WORKSHOP_TRUST_REGISTRY_SCHEMA_VERSION = "custombuild.workshop-trust-registry.v2"
SIGNED_WORKSHOP_ATTESTATION_SCHEMA_VERSION = "custombuild.signed-workshop-attestation.v2"
WORKSHOP_ATTESTATION_STATEMENT_SCHEMA_VERSION = "custombuild.workshop-attestation.v2"
WORKSHOP_VERIFICATION_POLICY_SCHEMA_VERSION = "custombuild.workshop-verification-policy.v2"
WORKSHOP_EVIDENCE_RECORD_SCHEMA_VERSION = "custombuild.workshop-evidence-record.v2"
WORKSHOP_RUN_SCHEMA_VERSION = "custombuild.workshop-run.v2"
WORKSHOP_MACHINE_PROGRAM_SET_SCHEMA_VERSION = "custombuild.workshop-machine-program-set.v1"
WORKSHOP_EVIDENCE_CLAIM_SCHEMA_VERSION = "custombuild.workshop-evidence-claim.v1"
WORKSHOP_EVIDENCE_MEDIA_TYPE = "application/vnd.custombuild.workshop-evidence+json"
WORKSHOP_MAKER_ROLE = "workshop_maker"
WORKSHOP_CHECKER_ROLE = "workshop_checker"
WORKSHOP_SUPERVISOR_ROLE = "workshop_supervisor"
SIDECAR_EVIDENCE_PLACEMENT = "SIDECAR_OUTSIDE_BASE_BUNDLE"
EXECUTABLE_MACHINE_PROGRAM_KIND = "EXECUTABLE"
VALIDATION_ONLY_MACHINE_PROGRAM_KIND = "VALIDATION_ONLY"

MAX_ATTESTATION_BYTES = 4 * 1024 * 1024
MAX_CHAIN_BYTES = 12 * 1024 * 1024
MAX_REGISTRY_ISSUERS = 2_048
MAX_REVOCATIONS = 100_000
MAX_TOOLS = 128
MAX_STOCK_ITEMS = 1_024
MAX_KEEPOUTS = 1_024
MAX_MEASUREMENTS = 2_048
MAX_COUPONS = 256
MAX_MACHINE_PROGRAMS = 4_096
MAX_EVIDENCE_ATTACHMENTS = 256
MAX_EVIDENCE_OBJECT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_CHAIN_BYTES = 256 * 1024 * 1024

_CANONICAL_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$"
_SHA256_RE = r"^[a-f0-9]{64}$"

Token = Annotated[str, Field(min_length=1, max_length=160, pattern=_SAFE_ID_PATTERN)]
Sha256 = Annotated[str, Field(pattern=_SHA256_RE)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0, le=1_000_000_000)]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
SignedCoordinateUm = Annotated[
    int,
    Field(strict=True, ge=-100_000_000, le=100_000_000),
]
PositiveLengthUm = Annotated[int, Field(strict=True, gt=0, le=100_000_000)]
NonNegativeLengthUm = Annotated[int, Field(strict=True, ge=0, le=100_000_000)]
MoisturePpm = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]


class WorkshopTrustError(ValueError):
    """Raised whenever evidence fails the workshop trust boundary."""


class WorkshopStage(StrEnum):
    PRE_CUT = "PRE_CUT"
    REFERENCE_PART = "REFERENCE_PART"
    FINAL_WORKSHOP = "FINAL_WORKSHOP"


STAGE_ORDER = (
    WorkshopStage.PRE_CUT,
    WorkshopStage.REFERENCE_PART,
    WorkshopStage.FINAL_WORKSHOP,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_utc_before(value: object) -> object:
    if isinstance(value, datetime):
        if (
            value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
            or value.microsecond != 0
        ):
            raise ValueError("timestamp must be UTC with whole-second precision")
        return value
    if not isinstance(value, str) or _CANONICAL_UTC_RE.fullmatch(value) is None:
        raise ValueError("timestamp must use canonical YYYY-MM-DDTHH:MM:SSZ form")
    return value


def _validate_human_text(value: str) -> str:
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise ValueError("text must be trimmed canonical NFC")
    if not value or any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("text must be non-empty and contain no control characters")
    return value


class EvidenceReference(_StrictModel):
    """An immutable evidence pointer with an explicit absence state.

    ``MISSING`` and ``NOT_APPLICABLE`` are representable for intake and audit,
    but neither state can cross final chain verification for a required item.
    """

    status: Literal["VERIFIED", "MISSING", "NOT_APPLICABLE"]
    evidence_id: Token | None = None
    evidence_version: Token | None = None
    claim_type: Token | None = None
    claim_sha256: Sha256 | None = None
    sha256: Sha256 | None = None
    size_bytes: StrictPositiveInt | None = None
    media_type: Token | None = None
    observed_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("observed_at", mode="before")
    @classmethod
    def canonical_observed_at(cls, value: object) -> object:
        return None if value is None else _canonical_utc_before(value)

    @field_validator("reason")
    @classmethod
    def canonical_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_human_text(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        immutable_fields = (
            self.evidence_id,
            self.evidence_version,
            self.claim_type,
            self.claim_sha256,
            self.sha256,
            self.size_bytes,
            self.media_type,
            self.observed_at,
        )
        if self.status == "VERIFIED":
            if any(value is None for value in immutable_fields) or self.reason is not None:
                raise ValueError("verified evidence requires a complete immutable reference")
            if self.media_type != WORKSHOP_EVIDENCE_MEDIA_TYPE:
                raise ValueError("verified evidence must use the typed workshop record media type")
        elif any(value is not None for value in immutable_fields) or self.reason is None:
            raise ValueError("absent evidence requires only an explicit reason")
        return self


class EvidenceAttachment(_StrictModel):
    attachment_id: Token
    purpose: Token
    sha256: Sha256
    size_bytes: StrictPositiveInt
    media_type: Token


class WorkshopEvidenceRecord(_StrictModel):
    schema_version: Literal["custombuild.workshop-evidence-record.v2"]
    evidence_id: Token
    evidence_version: Token
    claim_type: Token
    claim_sha256: Sha256
    observed_at: datetime
    attachments: tuple[EvidenceAttachment, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_ATTACHMENTS,
    )

    @field_validator("observed_at", mode="before")
    @classmethod
    def canonical_observed_at(cls, value: object) -> object:
        return _canonical_utc_before(value)

    @field_validator("attachments")
    @classmethod
    def canonical_attachments(
        cls,
        value: tuple[EvidenceAttachment, ...],
    ) -> tuple[EvidenceAttachment, ...]:
        ids = tuple(item.attachment_id for item in value)
        digests = tuple(item.sha256 for item in value)
        if ids != tuple(sorted(set(ids))) or len(set(digests)) != len(digests):
            raise ValueError("evidence attachments must be sorted with unique IDs and digests")
        return value


class TrustedWorkshopIssuer(_StrictModel):
    organization: Token
    principal_id: Token
    key_id: Token
    role: Literal["workshop_maker", "workshop_checker", "workshop_supervisor"]
    qualified_stages: tuple[WorkshopStage, ...] = Field(min_length=1, max_length=3)
    public_key_base64: str = Field(min_length=44, max_length=44)
    not_before: datetime
    not_after: datetime
    # Required even when null: omitting revocation state must never silently
    # reactivate a key while hydrating an immutable registry snapshot.
    revoked_at: datetime | None

    @field_validator("not_before", "not_after", "revoked_at", mode="before")
    @classmethod
    def canonical_timestamps(cls, value: object) -> object:
        return None if value is None else _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_validity_and_qualifications(self) -> Self:
        if self.not_before >= self.not_after:
            raise ValueError("issuer validity window must be increasing")
        canonical = tuple(stage for stage in STAGE_ORDER if stage in self.qualified_stages)
        if self.qualified_stages != canonical:
            raise ValueError("issuer stage qualifications must be canonical and unique")
        return self


class WorkshopTrustRegistry(_StrictModel):
    schema_version: Literal["custombuild.workshop-trust-registry.v2"]
    issuers: tuple[TrustedWorkshopIssuer, ...] = Field(max_length=MAX_REGISTRY_ISSUERS)
    revoked_statement_sha256: tuple[Sha256, ...] = Field(
        max_length=MAX_REVOCATIONS,
    )
    revoked_run_sha256: tuple[Sha256, ...] = Field(max_length=MAX_REVOCATIONS)
    revoked_evidence_sha256: tuple[Sha256, ...] = Field(
        max_length=MAX_REVOCATIONS,
    )
    revoked_evidence_claim_sha256: tuple[Sha256, ...] = Field(
        max_length=MAX_REVOCATIONS,
    )
    revoked_evidence_attachment_sha256: tuple[Sha256, ...] = Field(
        max_length=MAX_REVOCATIONS,
    )

    @field_validator(
        "revoked_statement_sha256",
        "revoked_run_sha256",
        "revoked_evidence_sha256",
        "revoked_evidence_claim_sha256",
        "revoked_evidence_attachment_sha256",
    )
    @classmethod
    def canonical_revocations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("revocation digests must be sorted and unique")
        return value


class MachineProgramIdentity(_StrictModel):
    """One logical executable with path, setup and coordinate-system identity."""

    program_id: Token
    purpose: Token
    relative_path: Token
    setup_id: Token
    wcs_id: Token
    stock_id: Token
    part_ids: tuple[Token, ...] = Field(min_length=1, max_length=MAX_STOCK_ITEMS)
    operation_set_sha256: Sha256
    sha256: Sha256
    size_bytes: StrictPositiveInt
    media_type: Token

    @field_validator("relative_path")
    @classmethod
    def canonical_relative_path(cls, value: str) -> str:
        if "\\" in value or ":" in value or value.startswith("/"):
            raise ValueError("machine program path must be a safe POSIX relative path")
        segments = value.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("machine program path must be a canonical POSIX relative path")
        if "/".join(segments) != value:
            raise ValueError("machine program path must be a canonical POSIX relative path")
        return value

    @field_validator("part_ids")
    @classmethod
    def canonical_part_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("machine program part IDs must be sorted and unique")
        return value


class WorkshopRun(_StrictModel):
    """Exact immutable identity of one generated production run."""

    schema_version: Literal["custombuild.workshop-run.v2"]
    organization: Token
    project: Token
    design_version: Token
    design_review_release: Token
    design_hash: Sha256
    generation_job: Token
    generation_finished_at: datetime
    production_context_hash: Sha256
    manifest_sha256: Sha256
    bundle_sha256: Sha256
    operations_sha256: Sha256
    generation_plan_sha256: Sha256
    workshop_policy_sha256: Sha256
    machine_program_kind: Literal["EXECUTABLE", "VALIDATION_ONLY"]
    machine_programs: tuple[MachineProgramIdentity, ...] = Field(
        min_length=1,
        max_length=MAX_MACHINE_PROGRAMS,
    )
    machine_program_set_sha256: Sha256
    postprocessor_id: Token
    postprocessor_version: Token
    postprocessor_binary_sha256: Sha256
    postprocessor_config_sha256: Sha256

    @field_validator("generation_finished_at", mode="before")
    @classmethod
    def canonical_generation_finished_at(cls, value: object) -> object:
        return _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_program_set(self) -> Self:
        keys = tuple(
            (item.setup_id, item.wcs_id, item.program_id, item.relative_path)
            for item in self.machine_programs
        )
        if keys != tuple(sorted(keys)):
            raise ValueError("workshop machine programs must use canonical order")
        if len({item.program_id for item in self.machine_programs}) != len(
            self.machine_programs
        ) or len({item.relative_path for item in self.machine_programs}) != len(
            self.machine_programs
        ):
            raise ValueError("workshop machine program IDs and paths must be unique")
        if (
            workshop_machine_program_set_sha256(self.machine_programs)
            != self.machine_program_set_sha256
        ):
            raise ValueError("workshop machine program set digest does not match its members")
        return self


class SignerIdentity(_StrictModel):
    principal_id: Token
    key_id: Token


class MachineIdentity(_StrictModel):
    machine_id: Token
    manufacturer: Token
    model: Token
    serial_number: Token
    controller_id: Token
    controller_version: Token
    profile_id: Token
    profile_version: Token
    profile_sha256: Sha256
    calibration_id: Token
    calibrated_at: datetime | None = None
    calibration_expires_at: datetime | None = None
    calibration_evidence: EvidenceReference

    @field_validator("calibrated_at", "calibration_expires_at", mode="before")
    @classmethod
    def canonical_timestamps(cls, value: object) -> object:
        return None if value is None else _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_calibration_state(self) -> Self:
        if self.calibration_evidence.status == "VERIFIED":
            if self.calibrated_at is None or self.calibration_expires_at is None:
                raise ValueError("verified calibration requires its exact validity window")
            if self.calibrated_at >= self.calibration_expires_at:
                raise ValueError("machine calibration validity window must be increasing")
        elif self.calibrated_at is not None or self.calibration_expires_at is not None:
            raise ValueError("absent calibration cannot carry asserted validity dates")
        return self


class WorkCoordinateSystem(_StrictModel):
    wcs_id: Token
    convention_version: Token
    origin_x_um: SignedCoordinateUm
    origin_y_um: SignedCoordinateUm
    origin_z_um: SignedCoordinateUm
    axes_definition_sha256: Sha256
    verification_evidence: EvidenceReference


class FixtureIdentity(_StrictModel):
    fixture_id: Token
    fixture_version: Token
    serial_number: Token
    setup_sha256: Sha256
    clamping_plan_sha256: Sha256
    verification_evidence: EvidenceReference


class KeepoutVolume(_StrictModel):
    keepout_id: Token
    minimum_x_um: SignedCoordinateUm
    minimum_y_um: SignedCoordinateUm
    minimum_z_um: SignedCoordinateUm
    maximum_x_um: SignedCoordinateUm
    maximum_y_um: SignedCoordinateUm
    maximum_z_um: SignedCoordinateUm

    @model_validator(mode="after")
    def validate_volume(self) -> Self:
        if not (
            self.minimum_x_um < self.maximum_x_um
            and self.minimum_y_um < self.maximum_y_um
            and self.minimum_z_um < self.maximum_z_um
        ):
            raise ValueError("keepout bounds must define a positive volume")
        return self


class KeepoutSet(_StrictModel):
    volumes: tuple[KeepoutVolume, ...] = Field(max_length=MAX_KEEPOUTS)
    review_evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_keepouts(self) -> Self:
        ids = tuple(item.keepout_id for item in self.volumes)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("keepouts must be sorted and unique")
        if self.review_evidence.status != "VERIFIED" and self.volumes:
            raise ValueError("unverified keepouts cannot assert known volumes")
        return self


class ToolMeasurement(_StrictModel):
    tool_id: Token
    tool_version: Token
    serial_number: Token
    holder_id: Token
    pocket_number: Annotated[int, Field(strict=True, gt=0, le=10_000)]
    measured_diameter_um: PositiveLengthUm | None = None
    measured_length_offset_um: SignedCoordinateUm | None = None
    measured_runout_um: NonNegativeLengthUm | None = None
    measured_stickout_um: PositiveLengthUm | None = None
    measured_usable_flute_length_um: PositiveLengthUm | None = None
    measurement_evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_measurement_state(self) -> Self:
        values = (
            self.measured_diameter_um,
            self.measured_length_offset_um,
            self.measured_runout_um,
            self.measured_stickout_um,
            self.measured_usable_flute_length_um,
        )
        if self.measurement_evidence.status == "VERIFIED":
            if any(value is None for value in values):
                raise ValueError("verified tool measurement requires every exact value")
        elif any(value is not None for value in values):
            raise ValueError("absent tool measurement cannot carry asserted values")
        return self


class StockDimensions(_StrictModel):
    length_um: PositiveLengthUm
    width_um: PositiveLengthUm
    thickness_um: PositiveLengthUm


class StockIdentity(_StrictModel):
    stock_id: Token
    material_id: Token
    material_version: Token
    supplier_batch_id: Token
    supplier_lot_id: Token
    grain_orientation: Literal["X", "Y", "NON_DIRECTIONAL"]
    dimensions: StockDimensions | None = None
    moisture_content_ppm: MoisturePpm | None = None
    material_certificate_evidence: EvidenceReference
    measurement_evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_measurement_state(self) -> Self:
        if self.measurement_evidence.status == "VERIFIED":
            if self.dimensions is None or self.moisture_content_ppm is None:
                raise ValueError("verified stock measurement requires dimensions and moisture")
        elif self.dimensions is not None or self.moisture_content_ppm is not None:
            raise ValueError("absent stock measurement cannot carry asserted measurements")
        return self


class WorkshopSetup(_StrictModel):
    machine: MachineIdentity
    wcs: WorkCoordinateSystem
    fixture: FixtureIdentity
    keepouts: KeepoutSet
    tools: tuple[ToolMeasurement, ...] = Field(min_length=1, max_length=MAX_TOOLS)
    stock: tuple[StockIdentity, ...] = Field(min_length=1, max_length=MAX_STOCK_ITEMS)

    @model_validator(mode="after")
    def validate_canonical_collections(self) -> Self:
        tool_keys = tuple((item.pocket_number, item.tool_id) for item in self.tools)
        if tool_keys != tuple(sorted(set(tool_keys))):
            raise ValueError("tools must be sorted and unique by pocket and identity")
        stock_keys = tuple(item.stock_id for item in self.stock)
        if stock_keys != tuple(sorted(set(stock_keys))):
            raise ValueError("stock identities must be sorted and unique")
        return self


class DimensionalMeasurement(_StrictModel):
    measurement_id: Token
    nominal_um: SignedCoordinateUm
    measured_um: SignedCoordinateUm
    minimum_acceptable_um: SignedCoordinateUm
    maximum_acceptable_um: SignedCoordinateUm

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if not self.minimum_acceptable_um <= self.nominal_um <= self.maximum_acceptable_um:
            raise ValueError("measurement limits must contain the nominal value")
        return self

    @property
    def within_limits(self) -> bool:
        return self.minimum_acceptable_um <= self.measured_um <= self.maximum_acceptable_um


class MeasurementRequirement(_StrictModel):
    measurement_id: Token
    nominal_um: SignedCoordinateUm
    minimum_acceptable_um: SignedCoordinateUm
    maximum_acceptable_um: SignedCoordinateUm

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if not self.minimum_acceptable_um <= self.nominal_um <= self.maximum_acceptable_um:
            raise ValueError("measurement requirement limits must contain the nominal value")
        return self


class RequiredMachine(_StrictModel):
    machine_id: Token
    manufacturer: Token
    model: Token
    serial_number: Token
    controller_id: Token
    controller_version: Token
    profile_id: Token
    profile_version: Token
    profile_sha256: Sha256


class RequiredWcs(_StrictModel):
    wcs_id: Token
    convention_version: Token
    origin_x_um: SignedCoordinateUm
    origin_y_um: SignedCoordinateUm
    origin_z_um: SignedCoordinateUm
    axes_definition_sha256: Sha256


class RequiredFixture(_StrictModel):
    fixture_id: Token
    fixture_version: Token
    serial_number: Token
    setup_sha256: Sha256
    clamping_plan_sha256: Sha256
    keepout_volumes_sha256: Sha256


class RequiredTool(_StrictModel):
    tool_id: Token
    tool_version: Token
    serial_number: Token
    holder_id: Token
    pocket_number: Annotated[int, Field(strict=True, gt=0, le=10_000)]
    minimum_diameter_um: PositiveLengthUm
    maximum_diameter_um: PositiveLengthUm
    minimum_length_offset_um: SignedCoordinateUm
    maximum_length_offset_um: SignedCoordinateUm
    maximum_runout_um: NonNegativeLengthUm
    minimum_stickout_um: PositiveLengthUm
    maximum_stickout_um: PositiveLengthUm
    minimum_usable_flute_length_um: PositiveLengthUm

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if (
            self.minimum_diameter_um > self.maximum_diameter_um
            or self.minimum_length_offset_um > self.maximum_length_offset_um
            or self.minimum_stickout_um > self.maximum_stickout_um
        ):
            raise ValueError("required tool measurement ranges must be increasing")
        return self


class RequiredStock(_StrictModel):
    stock_id: Token
    material_id: Token
    material_version: Token
    supplier_batch_id: Token
    supplier_lot_id: Token
    grain_orientation: Literal["X", "Y", "NON_DIRECTIONAL"]
    minimum_length_um: PositiveLengthUm
    maximum_length_um: PositiveLengthUm
    minimum_width_um: PositiveLengthUm
    maximum_width_um: PositiveLengthUm
    minimum_thickness_um: PositiveLengthUm
    maximum_thickness_um: PositiveLengthUm
    minimum_moisture_content_ppm: MoisturePpm
    maximum_moisture_content_ppm: MoisturePpm

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if (
            self.minimum_length_um > self.maximum_length_um
            or self.minimum_width_um > self.maximum_width_um
            or self.minimum_thickness_um > self.maximum_thickness_um
            or self.minimum_moisture_content_ppm > self.maximum_moisture_content_ppm
        ):
            raise ValueError("required stock measurement ranges must be increasing")
        return self


class RequiredIndependentEngine(_StrictModel):
    engine_id: Token
    engine_version: Token
    binary_sha256: Sha256
    config_sha256: Sha256


class WorkshopVerificationPolicy(_StrictModel):
    """Server-owned acceptance criteria bound into ``WorkshopRun`` by hash."""

    schema_version: Literal["custombuild.workshop-verification-policy.v2"]
    policy_id: Token
    policy_version: Token
    setup_evidence_not_before: datetime
    stage_evidence_not_before: datetime
    maximum_setup_evidence_age_seconds: StrictPositiveInt
    maximum_stage_evidence_age_seconds: StrictPositiveInt
    maximum_attestation_validity_seconds: StrictPositiveInt
    maximum_chain_duration_seconds: StrictPositiveInt
    machine: RequiredMachine
    wcs: RequiredWcs
    fixture: RequiredFixture
    tools: tuple[RequiredTool, ...] = Field(min_length=1, max_length=MAX_TOOLS)
    stock: tuple[RequiredStock, ...] = Field(min_length=1, max_length=MAX_STOCK_ITEMS)
    machine_programs: tuple[MachineProgramIdentity, ...] = Field(
        min_length=1,
        max_length=MAX_MACHINE_PROGRAMS,
    )
    machine_program_set_sha256: Sha256
    coupon_material_batch_ids: tuple[Token, ...] = Field(min_length=1, max_length=MAX_COUPONS)
    coupon_specification_sha256: Sha256
    coupon_measurements: tuple[MeasurementRequirement, ...] = Field(
        min_length=1,
        max_length=MAX_MEASUREMENTS,
    )
    independent_engine: RequiredIndependentEngine
    expected_removal_sha256: Sha256
    maximum_removal_deviation_um: NonNegativeLengthUm
    minimum_air_cut_clearance_um: PositiveLengthUm
    air_cut_supervisor: SignerIdentity
    reference_part_program_id: Token
    reference_part_program_sha256: Sha256
    reference_part_measurements: tuple[MeasurementRequirement, ...] = Field(
        min_length=1,
        max_length=MAX_MEASUREMENTS,
    )
    load_test_plan_sha256: Sha256
    minimum_applied_load_n: StrictPositiveInt
    minimum_load_duration_seconds: StrictPositiveInt
    maximum_deflection_um: NonNegativeLengthUm
    maximum_residual_deflection_um: NonNegativeLengthUm

    @field_validator(
        "setup_evidence_not_before",
        "stage_evidence_not_before",
        mode="before",
    )
    @classmethod
    def canonical_evidence_boundaries(cls, value: object) -> object:
        return _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_canonical_collections(self) -> Self:
        if self.setup_evidence_not_before > self.stage_evidence_not_before:
            raise ValueError("setup evidence boundary cannot follow the stage evidence boundary")
        tool_keys = tuple((item.pocket_number, item.tool_id) for item in self.tools)
        if tool_keys != tuple(sorted(set(tool_keys))):
            raise ValueError("policy tools must be sorted and unique")
        stock_keys = tuple(item.stock_id for item in self.stock)
        if stock_keys != tuple(sorted(set(stock_keys))):
            raise ValueError("policy stock must be sorted and unique")
        program_keys = tuple(
            (item.setup_id, item.wcs_id, item.program_id, item.relative_path)
            for item in self.machine_programs
        )
        if (
            program_keys != tuple(sorted(program_keys))
            or len({item.program_id for item in self.machine_programs})
            != len(self.machine_programs)
            or len({item.relative_path for item in self.machine_programs})
            != len(self.machine_programs)
        ):
            raise ValueError("policy machine programs must be canonical and unique")
        if (
            workshop_machine_program_set_sha256(self.machine_programs)
            != self.machine_program_set_sha256
        ):
            raise ValueError("policy machine program set digest does not match its members")
        if self.coupon_material_batch_ids != tuple(sorted(set(self.coupon_material_batch_ids))):
            raise ValueError("policy coupon batches must be sorted and unique")
        stock_batches = tuple(sorted(set(item.supplier_batch_id for item in self.stock)))
        if self.coupon_material_batch_ids != stock_batches:
            raise ValueError("policy coupons must exactly cover production stock batches")
        for label, measurements in (
            ("coupon", self.coupon_measurements),
            ("reference-part", self.reference_part_measurements),
        ):
            ids = tuple(item.measurement_id for item in measurements)
            if ids != tuple(sorted(set(ids))):
                raise ValueError(f"policy {label} measurements must be sorted and unique")
        return self


class CouponResult(_StrictModel):
    coupon_id: Token
    stock_id: Token
    material_batch_id: Token
    supplier_lot_id: Token
    specification_sha256: Sha256
    measurements: tuple[DimensionalMeasurement, ...] = Field(
        min_length=1,
        max_length=MAX_MEASUREMENTS,
    )
    outcome: Literal["PASS", "FAIL"]

    @model_validator(mode="after")
    def validate_measurements(self) -> Self:
        ids = tuple(item.measurement_id for item in self.measurements)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("coupon measurements must be sorted and unique")
        if self.outcome == "PASS" and not all(item.within_limits for item in self.measurements):
            raise ValueError("passing coupon has an out-of-tolerance measurement")
        return self


class CouponQualification(_StrictModel):
    evidence: EvidenceReference
    coupons: tuple[CouponResult, ...] = Field(max_length=MAX_COUPONS)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        ids = tuple(item.coupon_id for item in self.coupons)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("coupons must be sorted and unique")
        if self.evidence.status == "VERIFIED" and not self.coupons:
            raise ValueError("verified coupon qualification requires coupon results")
        if self.evidence.status != "VERIFIED" and self.coupons:
            raise ValueError("absent coupon evidence cannot carry asserted results")
        return self


class RemovalComparison(_StrictModel):
    evidence: EvidenceReference
    comparison_engine_id: Token | None = None
    comparison_engine_version: Token | None = None
    comparison_engine_binary_sha256: Sha256 | None = None
    comparison_engine_config_sha256: Sha256 | None = None
    expected_removal_sha256: Sha256 | None = None
    observed_removal_sha256: Sha256 | None = None
    maximum_deviation_um: NonNegativeLengthUm | None = None
    allowed_deviation_um: NonNegativeLengthUm | None = None
    outcome: Literal["PASS", "FAIL"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        values = (
            self.comparison_engine_id,
            self.comparison_engine_version,
            self.comparison_engine_binary_sha256,
            self.comparison_engine_config_sha256,
            self.expected_removal_sha256,
            self.observed_removal_sha256,
            self.maximum_deviation_um,
            self.allowed_deviation_um,
            self.outcome,
        )
        if self.evidence.status == "VERIFIED":
            if any(value is None for value in values):
                raise ValueError("verified removal comparison requires complete results")
            if (
                self.outcome == "PASS"
                and self.maximum_deviation_um is not None
                and self.allowed_deviation_um is not None
                and self.maximum_deviation_um > self.allowed_deviation_um
            ):
                raise ValueError("passing removal comparison exceeds allowed deviation")
        elif any(value is not None for value in values):
            raise ValueError("absent removal comparison cannot carry asserted results")
        return self


class AirCutAssessment(_StrictModel):
    evidence: EvidenceReference
    machine_program_set_sha256: Sha256 | None = None
    supervisor: SignerIdentity | None = None
    supervisor_signature_base64: str | None = Field(default=None, min_length=88, max_length=88)
    minimum_clearance_um: PositiveLengthUm | None = None
    outcome: Literal["PASS", "FAIL"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        values = (
            self.machine_program_set_sha256,
            self.supervisor,
            self.supervisor_signature_base64,
            self.minimum_clearance_um,
            self.outcome,
        )
        if self.evidence.status == "VERIFIED":
            if any(value is None for value in values):
                raise ValueError("verified air cut requires complete results")
        elif any(value is not None for value in values):
            raise ValueError("absent air cut cannot carry asserted results")
        return self


class PreCutEvidence(_StrictModel):
    coupons: CouponQualification
    independent_removal_comparison: RemovalComparison
    supervised_air_cut: AirCutAssessment


class ReferencePartAssessment(_StrictModel):
    evidence: EvidenceReference
    part_id: Token | None = None
    machine_program_id: Token | None = None
    machine_program_sha256: Sha256 | None = None
    metrology: tuple[DimensionalMeasurement, ...] = Field(default=(), max_length=MAX_MEASUREMENTS)
    outcome: Literal["PASS", "FAIL"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.evidence.status == "VERIFIED":
            if (
                self.part_id is None
                or self.machine_program_id is None
                or self.machine_program_sha256 is None
                or self.outcome is None
            ):
                raise ValueError("verified reference part requires complete identity and result")
            if not self.metrology:
                raise ValueError("verified reference part requires metrology")
            ids = tuple(item.measurement_id for item in self.metrology)
            if ids != tuple(sorted(set(ids))):
                raise ValueError("reference metrology must be sorted and unique")
            if self.outcome == "PASS" and not all(item.within_limits for item in self.metrology):
                raise ValueError("passing reference part has out-of-tolerance metrology")
        elif any(
            (
                self.part_id is not None,
                self.machine_program_id is not None,
                self.machine_program_sha256 is not None,
                bool(self.metrology),
                self.outcome is not None,
            )
        ):
            raise ValueError("absent reference-part evidence cannot carry asserted results")
        return self


class PrototypeAssessment(_StrictModel):
    evidence: EvidenceReference
    prototype_id: Token | None = None
    build_manifest_sha256: Sha256 | None = None
    inspection_sha256: Sha256 | None = None
    outcome: Literal["PASS", "FAIL"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        values = (
            self.prototype_id,
            self.build_manifest_sha256,
            self.inspection_sha256,
            self.outcome,
        )
        if self.evidence.status == "VERIFIED":
            if any(value is None for value in values):
                raise ValueError("verified prototype requires complete results")
        elif any(value is not None for value in values):
            raise ValueError("absent prototype evidence cannot carry asserted results")
        return self


class LoadTestAssessment(_StrictModel):
    evidence: EvidenceReference
    prototype_id: Token | None = None
    prototype_build_manifest_sha256: Sha256 | None = None
    prototype_inspection_sha256: Sha256 | None = None
    prototype_evidence_sha256: Sha256 | None = None
    test_plan_sha256: Sha256 | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    applied_load_n: StrictPositiveInt | None = None
    duration_seconds: StrictPositiveInt | None = None
    maximum_deflection_um: NonNegativeLengthUm | None = None
    allowed_deflection_um: NonNegativeLengthUm | None = None
    residual_deflection_um: NonNegativeLengthUm | None = None
    allowed_residual_deflection_um: NonNegativeLengthUm | None = None
    outcome: Literal["PASS", "FAIL"] | None = None

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def canonical_timestamps(cls, value: object) -> object:
        return None if value is None else _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        values = (
            self.prototype_id,
            self.prototype_build_manifest_sha256,
            self.prototype_inspection_sha256,
            self.prototype_evidence_sha256,
            self.test_plan_sha256,
            self.started_at,
            self.completed_at,
            self.applied_load_n,
            self.duration_seconds,
            self.maximum_deflection_um,
            self.allowed_deflection_um,
            self.residual_deflection_um,
            self.allowed_residual_deflection_um,
            self.outcome,
        )
        if self.evidence.status == "VERIFIED":
            if any(value is None for value in values):
                raise ValueError("verified load test requires complete results")
            if (
                self.started_at is not None
                and self.completed_at is not None
                and self.duration_seconds is not None
                and (
                    self.started_at >= self.completed_at
                    or self.completed_at - self.started_at
                    < timedelta(seconds=self.duration_seconds)
                )
            ):
                raise ValueError("load-test timestamps do not support the claimed duration")
            if (
                self.outcome == "PASS"
                and self.maximum_deflection_um is not None
                and self.allowed_deflection_um is not None
                and self.residual_deflection_um is not None
                and self.allowed_residual_deflection_um is not None
                and (
                    self.maximum_deflection_um > self.allowed_deflection_um
                    or self.residual_deflection_um > self.allowed_residual_deflection_um
                )
            ):
                raise ValueError("passing load test exceeds an acceptance limit")
        elif any(value is not None for value in values):
            raise ValueError("absent load-test evidence cannot carry asserted results")
        return self


class FinalWorkshopEvidence(_StrictModel):
    prototype_build: PrototypeAssessment
    load_test: LoadTestAssessment


class WorkshopAttestationStatement(_StrictModel):
    schema_version: Literal["custombuild.workshop-attestation.v2"]
    attestation_id: Token
    stage: WorkshopStage
    run: WorkshopRun
    evidence_placement: Literal["SIDECAR_OUTSIDE_BASE_BUNDLE"]
    previous_attestation_sha256: Sha256 | None
    server_nonce: Annotated[str, Field(min_length=32, max_length=256, pattern=_SAFE_ID_PATTERN)]
    issued_at: datetime
    expires_at: datetime
    maker: SignerIdentity
    checker: SignerIdentity
    setup: WorkshopSetup
    pre_cut: PreCutEvidence | None = None
    reference_part: ReferencePartAssessment | None = None
    final_workshop: FinalWorkshopEvidence | None = None

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def canonical_timestamps(cls, value: object) -> object:
        return _canonical_utc_before(value)

    @model_validator(mode="after")
    def validate_stage_shape(self) -> Self:
        if self.issued_at >= self.expires_at:
            raise ValueError("attestation validity window must be increasing")
        if self.maker.principal_id == self.checker.principal_id:
            raise ValueError("maker and checker identities must be distinct")
        expected_presence = {
            WorkshopStage.PRE_CUT: (True, False, False),
            WorkshopStage.REFERENCE_PART: (False, True, False),
            WorkshopStage.FINAL_WORKSHOP: (False, False, True),
        }[self.stage]
        actual_presence = (
            self.pre_cut is not None,
            self.reference_part is not None,
            self.final_workshop is not None,
        )
        if actual_presence != expected_presence:
            raise ValueError("attestation evidence must match exactly one stage")
        if self.stage is WorkshopStage.PRE_CUT:
            if self.previous_attestation_sha256 is not None:
                raise ValueError("PRE_CUT cannot reference a prior attestation")
        elif self.previous_attestation_sha256 is None:
            raise ValueError("later workshop stages require a prior attestation digest")
        return self


class SignedWorkshopAttestation(_StrictModel):
    schema_version: Literal["custombuild.signed-workshop-attestation.v2"]
    statement: WorkshopAttestationStatement
    maker_signature_base64: str = Field(min_length=88, max_length=88)
    checker_signature_base64: str = Field(min_length=88, max_length=88)


@dataclass(frozen=True, slots=True)
class VerifiedWorkshopChain:
    """Complete chain result whose nonce digests still require atomic consumption."""

    run: WorkshopRun
    run_sha256: str
    attestation_sha256: tuple[str, str, str]
    statement_sha256: tuple[str, str, str]
    server_nonce_sha256: tuple[str, str, str]
    final_attestation_id: str
    final_eligibility: Literal["VERIFIED_FOR_RELEASE_REVIEW"] = "VERIFIED_FOR_RELEASE_REVIEW"

    @property
    def physical_cutting_authorized(self) -> Literal[False]:
        """Remain false until a separate physical-release capability exists."""

        return False


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a JSON object in the one accepted signed representation."""

    _reject_unsafe_json_scalars(value, label="canonical JSON value")
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkshopTrustError("value is not canonical JSON data") from exc


def workshop_evidence_claim_bytes(
    *,
    claim_type: str,
    payload: Mapping[str, Any],
) -> bytes:
    """Serialize one typed structured claim with explicit domain separation."""

    if re.fullmatch(_SAFE_ID_PATTERN, claim_type) is None:
        raise WorkshopTrustError("workshop evidence claim type is invalid")
    claim = {
        "schema_version": WORKSHOP_EVIDENCE_CLAIM_SCHEMA_VERSION,
        "claim_type": claim_type,
        "payload": payload,
    }
    return canonical_json_bytes(claim)


def workshop_evidence_claim_sha256(
    *,
    claim_type: str,
    payload: Mapping[str, Any],
) -> str:
    """Hash one typed structured claim with explicit domain separation."""

    return hashlib.sha256(
        workshop_evidence_claim_bytes(claim_type=claim_type, payload=payload)
    ).hexdigest()


def workshop_machine_program_set_sha256(
    programs: Sequence[MachineProgramIdentity | Mapping[str, Any]],
) -> str:
    """Hash the exact canonical executable-program inventory for one run."""

    if isinstance(programs, str | bytes | bytearray):
        raise WorkshopTrustError("machine program inventory must be a sequence of objects")
    raw_programs = tuple(programs)
    if not raw_programs or len(raw_programs) > MAX_MACHINE_PROGRAMS:
        raise WorkshopTrustError("machine program inventory size is invalid")
    try:
        parsed = tuple(
            item
            if isinstance(item, MachineProgramIdentity)
            else MachineProgramIdentity.model_validate(item)
            for item in raw_programs
        )
    except ValidationError as exc:
        raise WorkshopTrustError("machine program inventory has an invalid entry") from exc
    keys = tuple(
        (item.setup_id, item.wcs_id, item.program_id, item.relative_path) for item in parsed
    )
    if (
        keys != tuple(sorted(keys))
        or len({item.program_id for item in parsed}) != len(parsed)
        or len({item.relative_path for item in parsed}) != len(parsed)
    ):
        raise WorkshopTrustError("machine program inventory must be canonical and unique")
    payload = {
        "schema_version": WORKSHOP_MACHINE_PROGRAM_SET_SCHEMA_VERSION,
        "programs": [item.model_dump(mode="json") for item in parsed],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def workshop_run_sha256(run: WorkshopRun) -> str:
    """Return the canonical revocation identity of an immutable run."""

    payload = run.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def workshop_policy_sha256(policy: WorkshopVerificationPolicy) -> str:
    """Return the digest that binds server acceptance criteria into a run."""

    payload = policy.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _reject_unsafe_json_scalars(value: object, *, label: str, depth: int = 0) -> None:
    if depth > 64:
        raise WorkshopTrustError(f"{label} exceeds the maximum nesting depth")
    if isinstance(value, bool | float):
        raise WorkshopTrustError(f"{label} contains an unsafe boolean or floating-point number")
    if value is None or isinstance(value, str | int):
        return
    if isinstance(value, Mapping):
        if len(value) > 100_000:
            raise WorkshopTrustError(f"{label} contains too many object members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkshopTrustError(f"{label} contains a non-string object key")
            _reject_unsafe_json_scalars(item, label=label, depth=depth + 1)
        return
    if isinstance(value, list | tuple):
        if len(value) > 100_000:
            raise WorkshopTrustError(f"{label} contains too many array members")
        for item in value:
            _reject_unsafe_json_scalars(item, label=label, depth=depth + 1)
        return
    raise WorkshopTrustError(f"{label} contains a non-JSON value")


def _parse_canonical_mapping(data: bytes, *, label: str, max_bytes: int) -> Mapping[str, Any]:
    if type(data) is not bytes or not data or len(data) > max_bytes:
        raise WorkshopTrustError(f"{label} is empty or exceeds its size limit")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkshopTrustError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise WorkshopTrustError(f"{label} must contain a JSON object")
    _reject_unsafe_json_scalars(raw, label=label)
    if data != canonical_json_bytes(raw):
        raise WorkshopTrustError(f"{label} must use canonical JSON bytes")
    return raw


def _decode_canonical_base64(value: str, *, label: str, expected_bytes: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkshopTrustError(f"{label} is not canonical base64") from exc
    if len(decoded) != expected_bytes or base64.b64encode(decoded).decode("ascii") != value:
        raise WorkshopTrustError(f"{label} is not canonical {expected_bytes}-byte material")
    return decoded


def _load_registry(value: Mapping[str, Any]) -> WorkshopTrustRegistry:
    _reject_unsafe_json_scalars(value, label="workshop trust registry")
    try:
        registry = WorkshopTrustRegistry.model_validate(value)
    except ValidationError as exc:
        raise WorkshopTrustError("workshop trust registry is invalid") from exc
    issuer_keys = tuple((item.principal_id, item.key_id) for item in registry.issuers)
    if issuer_keys != tuple(sorted(set(issuer_keys))):
        raise WorkshopTrustError("trusted workshop issuers must be sorted and unique")
    roles_by_principal: dict[str, str] = {}
    public_keys: set[bytes] = set()
    for issuer in registry.issuers:
        public_key = _decode_canonical_base64(
            issuer.public_key_base64,
            label="workshop issuer public key",
            expected_bytes=32,
        )
        if public_key in public_keys:
            raise WorkshopTrustError("workshop issuer public keys must be unique")
        public_keys.add(public_key)
        previous_role = roles_by_principal.setdefault(issuer.principal_id, issuer.role)
        if previous_role != issuer.role:
            raise WorkshopTrustError("a workshop principal cannot hold conflicting signer roles")
    return registry


def _load_attestation(
    attestation_bytes: bytes,
) -> tuple[SignedWorkshopAttestation, Mapping[str, Any], Mapping[str, Any]]:
    raw = _parse_canonical_mapping(
        attestation_bytes,
        label="signed workshop attestation",
        max_bytes=MAX_ATTESTATION_BYTES,
    )
    try:
        attestation = SignedWorkshopAttestation.model_validate(raw)
    except ValidationError as exc:
        raise WorkshopTrustError("signed workshop attestation has an invalid schema") from exc
    statement_raw = raw.get("statement")
    if not isinstance(statement_raw, Mapping):
        raise WorkshopTrustError("signed workshop attestation statement is invalid")
    return attestation, raw, statement_raw


def validate_signed_workshop_attestation_structure(attestation_bytes: bytes) -> None:
    """Validate bounded canonical structure before immutable sidecar storage."""

    _load_attestation(attestation_bytes)


def _load_expected_run(value: WorkshopRun | Mapping[str, Any]) -> WorkshopRun:
    if isinstance(value, WorkshopRun):
        return value
    _reject_unsafe_json_scalars(value, label="expected workshop run")
    try:
        return WorkshopRun.model_validate(value)
    except ValidationError as exc:
        raise WorkshopTrustError("expected workshop run is invalid") from exc


def _load_expected_policy(
    value: WorkshopVerificationPolicy | Mapping[str, Any],
) -> WorkshopVerificationPolicy:
    if isinstance(value, WorkshopVerificationPolicy):
        return value
    _reject_unsafe_json_scalars(value, label="expected workshop verification policy")
    try:
        return WorkshopVerificationPolicy.model_validate(value)
    except ValidationError as exc:
        raise WorkshopTrustError("expected workshop verification policy is invalid") from exc


def _normalize_now(value: datetime | None) -> datetime:
    now = datetime.now(UTC) if value is None else value
    if now.tzinfo is None or now.utcoffset() is None:
        raise WorkshopTrustError("verification time must include a timezone")
    return now.astimezone(UTC)


def _expected_nonces(value: Mapping[str | WorkshopStage, str]) -> dict[WorkshopStage, str]:
    result: dict[WorkshopStage, str] = {}
    for raw_stage, nonce in value.items():
        try:
            stage = WorkshopStage(raw_stage)
        except ValueError as exc:
            raise WorkshopTrustError("expected nonces contain an unknown workshop stage") from exc
        if stage in result:
            raise WorkshopTrustError("expected nonces contain an aliased stage")
        if (
            not isinstance(nonce, str)
            or len(nonce) < 32
            or len(nonce) > 256
            or re.fullmatch(_SAFE_ID_PATTERN, nonce) is None
        ):
            raise WorkshopTrustError("expected workshop nonce is invalid")
        result[stage] = nonce
    if tuple(stage for stage in STAGE_ORDER if stage in result) != STAGE_ORDER:
        raise WorkshopTrustError("one exact expected nonce is required for every workshop stage")
    if len(set(result.values())) != len(STAGE_ORDER):
        raise WorkshopTrustError("expected workshop nonces must be unique")
    return result


def _find_issuer(
    registry: WorkshopTrustRegistry,
    identity: SignerIdentity,
    *,
    required_role: str,
    organization: str,
    stage: WorkshopStage,
    issued_at: datetime,
    now: datetime,
) -> TrustedWorkshopIssuer:
    issuer = next(
        (
            candidate
            for candidate in registry.issuers
            if candidate.principal_id == identity.principal_id
            and candidate.key_id == identity.key_id
        ),
        None,
    )
    if issuer is None or issuer.role != required_role:
        raise WorkshopTrustError(f"{required_role} identity is not trusted for its signer role")
    if issuer.organization != organization:
        raise WorkshopTrustError(f"{required_role} identity is not trusted for this organization")
    if stage not in issuer.qualified_stages:
        raise WorkshopTrustError(f"{required_role} identity is not qualified for {stage.value}")
    if issuer.revoked_at is not None and issuer.revoked_at <= now:
        raise WorkshopTrustError(f"{required_role} issuer key is revoked")
    if not issuer.not_before <= issued_at <= issuer.not_after:
        raise WorkshopTrustError(f"{required_role} signature was issued outside key validity")
    if not issuer.not_before <= now <= issuer.not_after:
        raise WorkshopTrustError(f"{required_role} issuer key is not currently valid")
    return issuer


def _verify_signature(
    issuer: TrustedWorkshopIssuer,
    signature_base64: str,
    statement_bytes: bytes,
    *,
    label: str,
) -> None:
    public_key = _decode_canonical_base64(
        issuer.public_key_base64,
        label=f"{label} public key",
        expected_bytes=32,
    )
    signature = _decode_canonical_base64(
        signature_base64,
        label=f"{label} signature",
        expected_bytes=64,
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, statement_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise WorkshopTrustError(f"{label} signature is invalid") from exc


def _verify_air_cut_supervisor(
    registry: WorkshopTrustRegistry,
    statement: WorkshopAttestationStatement,
    *,
    now: datetime,
) -> None:
    if statement.stage is not WorkshopStage.PRE_CUT or statement.pre_cut is None:
        return
    air_cut = statement.pre_cut.supervised_air_cut
    if air_cut.supervisor is None or air_cut.supervisor_signature_base64 is None:
        raise WorkshopTrustError("supervised air cut lacks an authenticated supervisor")
    issuer = _find_issuer(
        registry,
        air_cut.supervisor,
        required_role=WORKSHOP_SUPERVISOR_ROLE,
        organization=statement.run.organization,
        stage=statement.stage,
        issued_at=statement.issued_at,
        now=now,
    )
    claim_bytes = workshop_evidence_claim_bytes(
        claim_type="supervised-air-cut-supervision",
        payload={
            "run_sha256": workshop_run_sha256(statement.run),
            "evidence": air_cut.evidence.model_dump(mode="json"),
            "assessment": _air_cut_claim_payload(air_cut),
        },
    )
    _verify_signature(
        issuer,
        air_cut.supervisor_signature_base64,
        claim_bytes,
        label="air-cut supervisor",
    )


def _all_setup_references(setup: WorkshopSetup) -> tuple[tuple[str, EvidenceReference], ...]:
    references: list[tuple[str, EvidenceReference]] = [
        ("machine calibration", setup.machine.calibration_evidence),
        ("WCS verification", setup.wcs.verification_evidence),
        ("fixture verification", setup.fixture.verification_evidence),
        ("keepout review", setup.keepouts.review_evidence),
    ]
    references.extend(
        (f"tool measurement {tool.tool_id}", tool.measurement_evidence) for tool in setup.tools
    )
    for stock in setup.stock:
        references.extend(
            (
                (f"material certificate {stock.stock_id}", stock.material_certificate_evidence),
                (f"stock measurement {stock.stock_id}", stock.measurement_evidence),
            )
        )
    return tuple(references)


def _stage_references(
    statement: WorkshopAttestationStatement,
) -> tuple[tuple[str, EvidenceReference], ...]:
    if statement.stage is WorkshopStage.PRE_CUT and statement.pre_cut is not None:
        return (
            ("coupon qualification", statement.pre_cut.coupons.evidence),
            (
                "independent removal comparison",
                statement.pre_cut.independent_removal_comparison.evidence,
            ),
            ("supervised air cut", statement.pre_cut.supervised_air_cut.evidence),
        )
    if statement.stage is WorkshopStage.REFERENCE_PART and statement.reference_part is not None:
        return (("reference part and metrology", statement.reference_part.evidence),)
    if statement.stage is WorkshopStage.FINAL_WORKSHOP and statement.final_workshop is not None:
        return (
            ("prototype build", statement.final_workshop.prototype_build.evidence),
            ("prototype load test", statement.final_workshop.load_test.evidence),
        )
    return ()


def _air_cut_claim_payload(air_cut: AirCutAssessment) -> Mapping[str, Any]:
    return air_cut.model_dump(
        mode="json",
        exclude={"evidence", "supervisor_signature_base64"},
    )


def _claim_bindings(
    statement: WorkshopAttestationStatement,
) -> tuple[tuple[str, EvidenceReference, str, Mapping[str, Any]], ...]:
    setup = statement.setup
    bindings: list[tuple[str, EvidenceReference, str, Mapping[str, Any]]] = [
        (
            "machine calibration",
            setup.machine.calibration_evidence,
            "machine-calibration",
            setup.machine.model_dump(mode="json", exclude={"calibration_evidence"}),
        ),
        (
            "WCS verification",
            setup.wcs.verification_evidence,
            "wcs-verification",
            setup.wcs.model_dump(mode="json", exclude={"verification_evidence"}),
        ),
        (
            "fixture verification",
            setup.fixture.verification_evidence,
            "fixture-verification",
            setup.fixture.model_dump(mode="json", exclude={"verification_evidence"}),
        ),
        (
            "keepout review",
            setup.keepouts.review_evidence,
            "keepout-review",
            {"volumes": [item.model_dump(mode="json") for item in setup.keepouts.volumes]},
        ),
    ]
    bindings.extend(
        (
            f"tool measurement {tool.tool_id}",
            tool.measurement_evidence,
            "tool-measurement",
            tool.model_dump(mode="json", exclude={"measurement_evidence"}),
        )
        for tool in setup.tools
    )
    for stock in setup.stock:
        material_identity = {
            "stock_id": stock.stock_id,
            "material_id": stock.material_id,
            "material_version": stock.material_version,
            "supplier_batch_id": stock.supplier_batch_id,
            "supplier_lot_id": stock.supplier_lot_id,
            "grain_orientation": stock.grain_orientation,
        }
        bindings.extend(
            (
                (
                    f"material certificate {stock.stock_id}",
                    stock.material_certificate_evidence,
                    "material-certificate",
                    material_identity,
                ),
                (
                    f"stock measurement {stock.stock_id}",
                    stock.measurement_evidence,
                    "stock-measurement",
                    stock.model_dump(
                        mode="json",
                        exclude={"material_certificate_evidence", "measurement_evidence"},
                    ),
                ),
            )
        )
    if statement.pre_cut is not None:
        bindings.extend(
            (
                (
                    "coupon qualification",
                    statement.pre_cut.coupons.evidence,
                    "coupon-qualification",
                    statement.pre_cut.coupons.model_dump(mode="json", exclude={"evidence"}),
                ),
                (
                    "independent removal comparison",
                    statement.pre_cut.independent_removal_comparison.evidence,
                    "independent-removal-comparison",
                    statement.pre_cut.independent_removal_comparison.model_dump(
                        mode="json",
                        exclude={"evidence"},
                    ),
                ),
                (
                    "supervised air cut",
                    statement.pre_cut.supervised_air_cut.evidence,
                    "supervised-air-cut",
                    _air_cut_claim_payload(statement.pre_cut.supervised_air_cut),
                ),
            )
        )
    elif statement.reference_part is not None:
        bindings.append(
            (
                "reference part and metrology",
                statement.reference_part.evidence,
                "reference-part-metrology",
                statement.reference_part.model_dump(mode="json", exclude={"evidence"}),
            )
        )
    elif statement.final_workshop is not None:
        bindings.extend(
            (
                (
                    "prototype build",
                    statement.final_workshop.prototype_build.evidence,
                    "prototype-build",
                    statement.final_workshop.prototype_build.model_dump(
                        mode="json",
                        exclude={"evidence"},
                    ),
                ),
                (
                    "prototype load test",
                    statement.final_workshop.load_test.evidence,
                    "prototype-load-test",
                    statement.final_workshop.load_test.model_dump(
                        mode="json",
                        exclude={"evidence"},
                    ),
                ),
            )
        )
    return tuple(bindings)


def _verify_claim_bindings(statement: WorkshopAttestationStatement) -> None:
    for label, reference, claim_type, payload in _claim_bindings(statement):
        if reference.status != "VERIFIED":
            continue
        expected_digest = workshop_evidence_claim_sha256(
            claim_type=claim_type,
            payload=payload,
        )
        if reference.claim_type != claim_type or reference.claim_sha256 != expected_digest:
            raise WorkshopTrustError(f"{label} record is not bound to its structured claim")


def _verify_evidence_object(
    reference: EvidenceReference,
    *,
    label: str,
    evidence_objects: Mapping[tuple[str, str], bytes],
    evidence_attachments: Mapping[tuple[str, str, str], bytes],
    revoked_evidence_sha256: frozenset[str],
    revoked_evidence_claim_sha256: frozenset[str],
    revoked_evidence_attachment_sha256: frozenset[str],
    remaining_evidence_bytes: int,
    verified_keys: set[tuple[str, str]],
    verified_attachment_keys: set[tuple[str, str, str]],
    role_by_key: dict[tuple[str, str], str],
    role_by_digest: dict[str, str],
) -> int:
    if reference.status != "VERIFIED":
        return 0
    if (
        reference.evidence_id is None
        or reference.evidence_version is None
        or reference.claim_type is None
        or reference.claim_sha256 is None
        or reference.sha256 is None
        or reference.size_bytes is None
    ):
        raise WorkshopTrustError("verified evidence reference is incomplete")
    if reference.sha256 in revoked_evidence_sha256:
        raise WorkshopTrustError("workshop evidence record is revoked")
    if reference.claim_sha256 in revoked_evidence_claim_sha256:
        raise WorkshopTrustError("workshop evidence claim is revoked")
    key = (reference.evidence_id, reference.evidence_version)
    try:
        content = evidence_objects[key]
    except KeyError as exc:
        raise WorkshopTrustError("required workshop evidence object is unavailable") from exc
    if type(content) is not bytes or not content or len(content) > MAX_EVIDENCE_OBJECT_BYTES:
        raise WorkshopTrustError("workshop evidence object is empty or exceeds its size limit")
    if (
        len(content) != reference.size_bytes
        or hashlib.sha256(content).hexdigest() != reference.sha256
    ):
        raise WorkshopTrustError("workshop evidence object does not match its signed identity")
    raw_record = _parse_canonical_mapping(
        content,
        label="workshop evidence record",
        max_bytes=MAX_EVIDENCE_OBJECT_BYTES,
    )
    try:
        record = WorkshopEvidenceRecord.model_validate(raw_record)
    except ValidationError as exc:
        raise WorkshopTrustError("workshop evidence record has an invalid schema") from exc
    if (
        record.evidence_id,
        record.evidence_version,
        record.claim_type,
        record.claim_sha256,
        record.observed_at,
    ) != (
        reference.evidence_id,
        reference.evidence_version,
        reference.claim_type,
        reference.claim_sha256,
        reference.observed_at,
    ):
        raise WorkshopTrustError("workshop evidence record does not match its typed claim")
    previous_key_role = role_by_key.setdefault(key, label)
    previous_digest_role = role_by_digest.setdefault(reference.sha256, label)
    if previous_key_role != label or previous_digest_role != label:
        raise WorkshopTrustError("one workshop evidence record cannot satisfy different roles")
    if key in verified_keys:
        return 0

    declared_bytes = len(content)
    for attachment in record.attachments:
        if attachment.sha256 in revoked_evidence_attachment_sha256:
            raise WorkshopTrustError("workshop evidence attachment is revoked")
        if attachment.size_bytes > MAX_EVIDENCE_OBJECT_BYTES:
            raise WorkshopTrustError(
                "workshop evidence attachment is empty or exceeds its size limit"
            )
        declared_bytes += attachment.size_bytes
    if declared_bytes > remaining_evidence_bytes:
        raise WorkshopTrustError("workshop evidence objects exceed their total size limit")

    verified_bytes = len(content)
    for attachment in record.attachments:
        attachment_key = (*key, attachment.attachment_id)
        try:
            attachment_content = evidence_attachments[attachment_key]
        except KeyError as exc:
            raise WorkshopTrustError(
                "required workshop evidence attachment is unavailable"
            ) from exc
        if (
            type(attachment_content) is not bytes
            or not attachment_content
            or len(attachment_content) > MAX_EVIDENCE_OBJECT_BYTES
            or len(attachment_content) != attachment.size_bytes
            or hashlib.sha256(attachment_content).hexdigest() != attachment.sha256
        ):
            raise WorkshopTrustError(
                "workshop evidence attachment does not match its signed identity"
            )
        previous_attachment_role = role_by_digest.setdefault(attachment.sha256, label)
        if previous_attachment_role != label:
            raise WorkshopTrustError(
                "one workshop evidence attachment cannot satisfy different roles"
            )
        if attachment_key not in verified_attachment_keys:
            verified_attachment_keys.add(attachment_key)
            verified_bytes += len(attachment_content)
    verified_keys.add(key)
    return verified_bytes


def _require_verified(
    reference: EvidenceReference,
    *,
    label: str,
    issued_at: datetime,
    observed_after: datetime | None = None,
    observed_not_before: datetime | None = None,
    maximum_age_seconds: int | None = None,
) -> None:
    if reference.status != "VERIFIED":
        raise WorkshopTrustError(f"required {label} evidence is {reference.status.lower()}")
    if reference.observed_at is None or reference.observed_at > issued_at:
        raise WorkshopTrustError(f"required {label} evidence was not observed before attestation")
    if observed_after is not None and reference.observed_at <= observed_after:
        raise WorkshopTrustError(f"required {label} evidence predates its prior workshop stage")
    if observed_not_before is not None and reference.observed_at < observed_not_before:
        raise WorkshopTrustError(f"required {label} evidence predates server policy")
    if maximum_age_seconds is not None and issued_at - reference.observed_at > timedelta(
        seconds=maximum_age_seconds
    ):
        raise WorkshopTrustError(f"required {label} evidence exceeds server-policy age")


def _measurement_specifications(
    measurements: Sequence[DimensionalMeasurement | MeasurementRequirement],
) -> tuple[tuple[str, int, int, int], ...]:
    return tuple(
        (
            item.measurement_id,
            item.nominal_um,
            item.minimum_acceptable_um,
            item.maximum_acceptable_um,
        )
        for item in measurements
    )


def _verify_setup_policy(setup: WorkshopSetup, policy: WorkshopVerificationPolicy) -> None:
    machine = setup.machine
    required_machine = policy.machine
    if (
        machine.machine_id,
        machine.manufacturer,
        machine.model,
        machine.serial_number,
        machine.controller_id,
        machine.controller_version,
        machine.profile_id,
        machine.profile_version,
        machine.profile_sha256,
    ) != (
        required_machine.machine_id,
        required_machine.manufacturer,
        required_machine.model,
        required_machine.serial_number,
        required_machine.controller_id,
        required_machine.controller_version,
        required_machine.profile_id,
        required_machine.profile_version,
        required_machine.profile_sha256,
    ):
        raise WorkshopTrustError("workshop machine does not match server policy")
    wcs = setup.wcs
    required_wcs = policy.wcs
    if (
        wcs.wcs_id,
        wcs.convention_version,
        wcs.origin_x_um,
        wcs.origin_y_um,
        wcs.origin_z_um,
        wcs.axes_definition_sha256,
    ) != (
        required_wcs.wcs_id,
        required_wcs.convention_version,
        required_wcs.origin_x_um,
        required_wcs.origin_y_um,
        required_wcs.origin_z_um,
        required_wcs.axes_definition_sha256,
    ):
        raise WorkshopTrustError("workshop WCS does not match server policy")
    fixture = setup.fixture
    required_fixture = policy.fixture
    keepout_payload = {"volumes": [item.model_dump(mode="json") for item in setup.keepouts.volumes]}
    keepout_digest = hashlib.sha256(canonical_json_bytes(keepout_payload)).hexdigest()
    if (
        fixture.fixture_id,
        fixture.fixture_version,
        fixture.serial_number,
        fixture.setup_sha256,
        fixture.clamping_plan_sha256,
        keepout_digest,
    ) != (
        required_fixture.fixture_id,
        required_fixture.fixture_version,
        required_fixture.serial_number,
        required_fixture.setup_sha256,
        required_fixture.clamping_plan_sha256,
        required_fixture.keepout_volumes_sha256,
    ):
        raise WorkshopTrustError("workshop fixture or keepouts do not match server policy")

    actual_tool_keys = tuple((item.pocket_number, item.tool_id) for item in setup.tools)
    required_tool_keys = tuple((item.pocket_number, item.tool_id) for item in policy.tools)
    if actual_tool_keys != required_tool_keys:
        raise WorkshopTrustError("workshop tools do not exactly cover server policy")
    for tool, required_tool in zip(setup.tools, policy.tools, strict=True):
        if (
            tool.tool_id,
            tool.tool_version,
            tool.serial_number,
            tool.holder_id,
            tool.pocket_number,
        ) != (
            required_tool.tool_id,
            required_tool.tool_version,
            required_tool.serial_number,
            required_tool.holder_id,
            required_tool.pocket_number,
        ):
            raise WorkshopTrustError("physical tool identity does not match server policy")
        if (
            tool.measured_diameter_um is None
            or tool.measured_length_offset_um is None
            or tool.measured_runout_um is None
            or tool.measured_stickout_um is None
            or tool.measured_usable_flute_length_um is None
            or not required_tool.minimum_diameter_um
            <= tool.measured_diameter_um
            <= required_tool.maximum_diameter_um
            or not required_tool.minimum_length_offset_um
            <= tool.measured_length_offset_um
            <= required_tool.maximum_length_offset_um
            or tool.measured_runout_um > required_tool.maximum_runout_um
            or not required_tool.minimum_stickout_um
            <= tool.measured_stickout_um
            <= required_tool.maximum_stickout_um
            or tool.measured_usable_flute_length_um < required_tool.minimum_usable_flute_length_um
        ):
            raise WorkshopTrustError("physical tool measurements violate server policy")

    actual_stock_keys = tuple(item.stock_id for item in setup.stock)
    required_stock_keys = tuple(item.stock_id for item in policy.stock)
    if actual_stock_keys != required_stock_keys:
        raise WorkshopTrustError("workshop stock does not exactly cover server policy")
    for stock, required_stock in zip(setup.stock, policy.stock, strict=True):
        if (
            stock.stock_id,
            stock.material_id,
            stock.material_version,
            stock.supplier_batch_id,
            stock.supplier_lot_id,
            stock.grain_orientation,
        ) != (
            required_stock.stock_id,
            required_stock.material_id,
            required_stock.material_version,
            required_stock.supplier_batch_id,
            required_stock.supplier_lot_id,
            required_stock.grain_orientation,
        ):
            raise WorkshopTrustError("physical stock identity does not match server policy")
        dimensions = stock.dimensions
        moisture = stock.moisture_content_ppm
        if (
            dimensions is None
            or moisture is None
            or not required_stock.minimum_length_um
            <= dimensions.length_um
            <= required_stock.maximum_length_um
            or not required_stock.minimum_width_um
            <= dimensions.width_um
            <= required_stock.maximum_width_um
            or not required_stock.minimum_thickness_um
            <= dimensions.thickness_um
            <= required_stock.maximum_thickness_um
            or not required_stock.minimum_moisture_content_ppm
            <= moisture
            <= required_stock.maximum_moisture_content_ppm
        ):
            raise WorkshopTrustError("physical stock measurements violate server policy")


def _verify_stage_evidence(
    statement: WorkshopAttestationStatement,
    *,
    now: datetime,
    previous_issued_at: datetime | None,
    policy: WorkshopVerificationPolicy,
) -> None:
    setup = statement.setup
    stage_evidence_not_before = max(
        policy.stage_evidence_not_before,
        statement.run.generation_finished_at,
    )
    for label, reference in _all_setup_references(setup):
        _require_verified(
            reference,
            label=label,
            issued_at=statement.issued_at,
            observed_not_before=policy.setup_evidence_not_before,
            maximum_age_seconds=policy.maximum_setup_evidence_age_seconds,
        )
    _verify_setup_policy(setup, policy)
    calibration_expires_at = setup.machine.calibration_expires_at
    calibrated_at = setup.machine.calibrated_at
    if (
        calibrated_at is None
        or calibration_expires_at is None
        or not calibrated_at <= statement.issued_at <= calibration_expires_at
        or now > calibration_expires_at
        or setup.machine.calibration_evidence.observed_at is None
        or setup.machine.calibration_evidence.observed_at < calibrated_at
    ):
        raise WorkshopTrustError("machine calibration is not valid for final eligibility")

    if statement.stage is WorkshopStage.PRE_CUT:
        evidence = statement.pre_cut
        if evidence is None:
            raise WorkshopTrustError("PRE_CUT evidence is missing")
        _require_verified(
            evidence.coupons.evidence,
            label="coupon qualification",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        if not evidence.coupons.coupons or any(
            item.outcome != "PASS" for item in evidence.coupons.coupons
        ):
            raise WorkshopTrustError("every required coupon must pass")
        coupon_stock = tuple(
            sorted(
                set(
                    (item.stock_id, item.material_batch_id, item.supplier_lot_id)
                    for item in evidence.coupons.coupons
                )
            )
        )
        required_coupon_stock = tuple(
            (item.stock_id, item.supplier_batch_id, item.supplier_lot_id) for item in policy.stock
        )
        if coupon_stock != required_coupon_stock:
            raise WorkshopTrustError("coupon stock, batch and lot do not match server policy")
        expected_coupon_measurements = _measurement_specifications(policy.coupon_measurements)
        if any(
            item.specification_sha256 != policy.coupon_specification_sha256
            or _measurement_specifications(item.measurements) != expected_coupon_measurements
            for item in evidence.coupons.coupons
        ):
            raise WorkshopTrustError("coupon specification does not match server policy")
        comparison = evidence.independent_removal_comparison
        _require_verified(
            comparison.evidence,
            label="independent removal comparison",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        if comparison.outcome != "PASS":
            raise WorkshopTrustError("independent removal comparison did not pass")
        required_engine = policy.independent_engine
        if (
            comparison.comparison_engine_id,
            comparison.comparison_engine_version,
            comparison.comparison_engine_binary_sha256,
            comparison.comparison_engine_config_sha256,
            comparison.expected_removal_sha256,
            comparison.allowed_deviation_um,
        ) != (
            required_engine.engine_id,
            required_engine.engine_version,
            required_engine.binary_sha256,
            required_engine.config_sha256,
            policy.expected_removal_sha256,
            policy.maximum_removal_deviation_um,
        ) or (
            comparison.comparison_engine_id == statement.run.postprocessor_id
            or comparison.comparison_engine_binary_sha256
            == statement.run.postprocessor_binary_sha256
            or comparison.comparison_engine_config_sha256
            == statement.run.postprocessor_config_sha256
        ):
            raise WorkshopTrustError(
                "removal comparison is not the independent server-policy check"
            )
        air_cut = evidence.supervised_air_cut
        _require_verified(
            air_cut.evidence,
            label="supervised air cut",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        if air_cut.outcome != "PASS":
            raise WorkshopTrustError("supervised air cut did not pass")
        if air_cut.supervisor != policy.air_cut_supervisor:
            raise WorkshopTrustError("air cut supervisor does not match server policy")
        if air_cut.machine_program_set_sha256 != statement.run.machine_program_set_sha256:
            raise WorkshopTrustError("air cut used another machine program set")
        if (
            air_cut.minimum_clearance_um is None
            or air_cut.minimum_clearance_um < policy.minimum_air_cut_clearance_um
        ):
            raise WorkshopTrustError("air cut clearance violates server policy")
    elif statement.stage is WorkshopStage.REFERENCE_PART:
        reference_part = statement.reference_part
        if reference_part is None:
            raise WorkshopTrustError("reference-part evidence is missing")
        _require_verified(
            reference_part.evidence,
            label="reference part and metrology",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        if reference_part.outcome != "PASS" or not reference_part.metrology:
            raise WorkshopTrustError("reference part and metrology did not pass")
        if (
            reference_part.machine_program_id != policy.reference_part_program_id
            or reference_part.machine_program_sha256 != policy.reference_part_program_sha256
            or not any(
                program.program_id == policy.reference_part_program_id
                and program.purpose == "REFERENCE_PART"
                and program.sha256 == policy.reference_part_program_sha256
                and program.wcs_id == policy.wcs.wcs_id
                and reference_part.part_id in program.part_ids
                for program in statement.run.machine_programs
            )
            or _measurement_specifications(reference_part.metrology)
            != _measurement_specifications(policy.reference_part_measurements)
        ):
            raise WorkshopTrustError("reference part does not match server policy")
    else:
        final = statement.final_workshop
        if final is None:
            raise WorkshopTrustError("final-workshop evidence is missing")
        _require_verified(
            final.prototype_build.evidence,
            label="prototype build",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        _require_verified(
            final.load_test.evidence,
            label="prototype load test",
            issued_at=statement.issued_at,
            observed_after=previous_issued_at,
            observed_not_before=stage_evidence_not_before,
            maximum_age_seconds=policy.maximum_stage_evidence_age_seconds,
        )
        if final.prototype_build.outcome != "PASS" or final.load_test.outcome != "PASS":
            raise WorkshopTrustError("prototype build and load test must both pass")
        if final.prototype_build.build_manifest_sha256 != statement.run.manifest_sha256:
            raise WorkshopTrustError("prototype was built from another manifest")
        load_test = final.load_test
        prototype_observed_at = final.prototype_build.evidence.observed_at
        load_observed_at = load_test.evidence.observed_at
        if (
            load_test.prototype_id != final.prototype_build.prototype_id
            or load_test.prototype_build_manifest_sha256
            != final.prototype_build.build_manifest_sha256
            or load_test.prototype_inspection_sha256 != final.prototype_build.inspection_sha256
            or load_test.prototype_evidence_sha256 != final.prototype_build.evidence.sha256
            or load_test.test_plan_sha256 != policy.load_test_plan_sha256
            or load_test.applied_load_n is None
            or load_test.applied_load_n < policy.minimum_applied_load_n
            or load_test.duration_seconds is None
            or load_test.duration_seconds < policy.minimum_load_duration_seconds
            or load_test.allowed_deflection_um != policy.maximum_deflection_um
            or load_test.allowed_residual_deflection_um != policy.maximum_residual_deflection_um
            or prototype_observed_at is None
            or load_test.started_at is None
            or load_test.completed_at is None
            or load_observed_at is None
            or load_test.started_at <= prototype_observed_at
            or load_test.completed_at > load_observed_at
        ):
            raise WorkshopTrustError("prototype load test does not match server policy")


def verify_workshop_attestation_chain(
    *,
    trust_registry: Mapping[str, Any],
    attestation_bytes: Sequence[bytes],
    expected_run: WorkshopRun | Mapping[str, Any],
    expected_policy: WorkshopVerificationPolicy | Mapping[str, Any],
    expected_server_nonces: Mapping[str | WorkshopStage, str],
    evidence_objects: Mapping[tuple[str, str], bytes],
    evidence_attachments: Mapping[tuple[str, str, str], bytes],
    now: datetime | None = None,
) -> VerifiedWorkshopChain:
    """Verify the exact three-stage sidecar chain and derive review eligibility.

    This function cannot authorize a machine.  In particular, its result keeps
    ``physical_cutting_authorized`` permanently false even for a complete chain.
    The caller must atomically consume ``server_nonce_sha256`` with any later
    persisted state transition; this stateless verifier cannot safely do so.
    """

    current_time = _normalize_now(now)
    registry = _load_registry(trust_registry)
    run = _load_expected_run(expected_run)
    policy = _load_expected_policy(expected_policy)
    if workshop_policy_sha256(policy) != run.workshop_policy_sha256:
        raise WorkshopTrustError("workshop verification policy does not match the immutable run")
    if run.generation_finished_at > current_time:
        raise WorkshopTrustError("workshop run generation finished in the future")
    if run.machine_program_kind != EXECUTABLE_MACHINE_PROGRAM_KIND:
        raise WorkshopTrustError("an executable machine program identity is required")
    if (
        run.machine_programs != policy.machine_programs
        or run.machine_program_set_sha256 != policy.machine_program_set_sha256
    ):
        raise WorkshopTrustError(
            "workshop executable program manifest does not match server policy"
        )
    if len(evidence_objects) > 100_000:
        raise WorkshopTrustError("workshop evidence object index exceeds its size limit")
    if len(evidence_attachments) > 100_000:
        raise WorkshopTrustError("workshop evidence attachment index exceeds its size limit")
    expected_nonces = _expected_nonces(expected_server_nonces)
    if isinstance(attestation_bytes, bytes | bytearray | str):
        raise WorkshopTrustError("workshop attestation chain must be a sequence of three bytes")
    chain = tuple(attestation_bytes)
    if len(chain) != len(STAGE_ORDER):
        raise WorkshopTrustError("workshop attestation chain must contain exactly three stages")
    if sum(len(item) for item in chain if isinstance(item, bytes)) > MAX_CHAIN_BYTES:
        raise WorkshopTrustError("workshop attestation chain exceeds its total size limit")

    run_digest = workshop_run_sha256(run)
    if run_digest in registry.revoked_run_sha256:
        raise WorkshopTrustError("workshop run is revoked")

    parsed: list[tuple[SignedWorkshopAttestation, Mapping[str, Any], Mapping[str, Any]]] = []
    attestation_digests: list[str] = []
    statement_digests: list[str] = []
    previous_issued_at: datetime | None = None
    first_issued_at: datetime | None = None
    previous_attestation_digest: str | None = None
    shared_setup: WorkshopSetup | None = None
    attestation_ids: set[str] = set()
    verified_evidence_keys: set[tuple[str, str]] = set()
    verified_attachment_keys: set[tuple[str, str, str]] = set()
    evidence_role_by_key: dict[tuple[str, str], str] = {}
    evidence_role_by_digest: dict[str, str] = {}
    revoked_evidence_sha256 = frozenset(registry.revoked_evidence_sha256)
    revoked_evidence_claim_sha256 = frozenset(registry.revoked_evidence_claim_sha256)
    revoked_evidence_attachment_sha256 = frozenset(registry.revoked_evidence_attachment_sha256)
    verified_evidence_bytes = 0

    for expected_stage, item in zip(STAGE_ORDER, chain, strict=True):
        if type(item) is not bytes:
            raise WorkshopTrustError("every workshop attestation must be exact bytes")
        attestation, raw, statement_raw = _load_attestation(item)
        statement = attestation.statement
        if statement.stage is not expected_stage:
            raise WorkshopTrustError("workshop attestation stages are out of order")
        if statement.attestation_id in attestation_ids:
            raise WorkshopTrustError("workshop attestation IDs must be unique")
        attestation_ids.add(statement.attestation_id)
        if statement.run != run:
            raise WorkshopTrustError("workshop attestation is bound to another immutable run")
        if statement.evidence_placement != SIDECAR_EVIDENCE_PLACEMENT:
            raise WorkshopTrustError("workshop evidence must remain outside the base bundle")
        if statement.server_nonce != expected_nonces[expected_stage]:
            raise WorkshopTrustError("workshop attestation server nonce does not match")
        if statement.previous_attestation_sha256 != previous_attestation_digest:
            raise WorkshopTrustError("workshop attestation hash chain is broken")
        if previous_issued_at is not None and statement.issued_at <= previous_issued_at:
            raise WorkshopTrustError("workshop stage issue times must be strictly increasing")
        if statement.issued_at <= run.generation_finished_at:
            raise WorkshopTrustError("workshop attestation predates executable generation")
        if statement.issued_at > current_time:
            raise WorkshopTrustError("workshop attestation was issued in the future")
        if statement.expires_at <= current_time:
            raise WorkshopTrustError("workshop attestation is expired")
        if statement.expires_at - statement.issued_at > timedelta(
            seconds=policy.maximum_attestation_validity_seconds
        ):
            raise WorkshopTrustError("workshop attestation validity exceeds server policy")
        if first_issued_at is None:
            first_issued_at = statement.issued_at
        elif statement.issued_at - first_issued_at > timedelta(
            seconds=policy.maximum_chain_duration_seconds
        ):
            raise WorkshopTrustError("workshop attestation chain exceeds server-policy duration")
        if shared_setup is None:
            shared_setup = statement.setup
        elif statement.setup != shared_setup:
            raise WorkshopTrustError("workshop setup changed within the attestation chain")

        statement_bytes = canonical_json_bytes(statement_raw)
        statement_digest = hashlib.sha256(statement_bytes).hexdigest()
        if statement_digest in registry.revoked_statement_sha256:
            raise WorkshopTrustError("workshop attestation statement is revoked")
        maker_issuer = _find_issuer(
            registry,
            statement.maker,
            required_role=WORKSHOP_MAKER_ROLE,
            organization=run.organization,
            stage=statement.stage,
            issued_at=statement.issued_at,
            now=current_time,
        )
        checker_issuer = _find_issuer(
            registry,
            statement.checker,
            required_role=WORKSHOP_CHECKER_ROLE,
            organization=run.organization,
            stage=statement.stage,
            issued_at=statement.issued_at,
            now=current_time,
        )
        _verify_signature(
            maker_issuer,
            attestation.maker_signature_base64,
            statement_bytes,
            label="workshop maker",
        )
        _verify_signature(
            checker_issuer,
            attestation.checker_signature_base64,
            statement_bytes,
            label="workshop checker",
        )
        _verify_air_cut_supervisor(registry, statement, now=current_time)
        _verify_claim_bindings(statement)
        for evidence_label, reference in (
            *_all_setup_references(statement.setup),
            *_stage_references(statement),
        ):
            verified_evidence_bytes += _verify_evidence_object(
                reference,
                label=evidence_label,
                evidence_objects=evidence_objects,
                evidence_attachments=evidence_attachments,
                revoked_evidence_sha256=revoked_evidence_sha256,
                revoked_evidence_claim_sha256=revoked_evidence_claim_sha256,
                revoked_evidence_attachment_sha256=revoked_evidence_attachment_sha256,
                remaining_evidence_bytes=(MAX_EVIDENCE_CHAIN_BYTES - verified_evidence_bytes),
                verified_keys=verified_evidence_keys,
                verified_attachment_keys=verified_attachment_keys,
                role_by_key=evidence_role_by_key,
                role_by_digest=evidence_role_by_digest,
            )
            if verified_evidence_bytes > MAX_EVIDENCE_CHAIN_BYTES:
                raise WorkshopTrustError("workshop evidence objects exceed their total size limit")
        _verify_stage_evidence(
            statement,
            now=current_time,
            previous_issued_at=previous_issued_at,
            policy=policy,
        )

        attestation_digest = hashlib.sha256(item).hexdigest()
        parsed.append((attestation, raw, statement_raw))
        attestation_digests.append(attestation_digest)
        statement_digests.append(statement_digest)
        previous_attestation_digest = attestation_digest
        previous_issued_at = statement.issued_at

    final_statement = parsed[-1][0].statement
    return VerifiedWorkshopChain(
        run=run,
        run_sha256=run_digest,
        attestation_sha256=(
            attestation_digests[0],
            attestation_digests[1],
            attestation_digests[2],
        ),
        statement_sha256=(
            statement_digests[0],
            statement_digests[1],
            statement_digests[2],
        ),
        server_nonce_sha256=(
            hashlib.sha256(expected_nonces[STAGE_ORDER[0]].encode("utf-8")).hexdigest(),
            hashlib.sha256(expected_nonces[STAGE_ORDER[1]].encode("utf-8")).hexdigest(),
            hashlib.sha256(expected_nonces[STAGE_ORDER[2]].encode("utf-8")).hexdigest(),
        ),
        final_attestation_id=final_statement.attestation_id,
    )
