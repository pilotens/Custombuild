"""Deterministic sidecar packages for executable CAM candidates.

The design-review bundle remains immutable and validation-only.  This module
places cutting toolpaths and LinuxCNC programs in a separate archive that is
cryptographically bound to that bundle.  Executable motion is represented
truthfully, while physical machine start remains outside this package's trust
boundary and requires workshop acceptance.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from custombuild_cam.production_model import (
    EXECUTABLE_CAM_CANDIDATE_MODE,
    BoundSetup,
    CuttingRecipe,
    ProductionExecutionContext,
    ProductionMove,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolGeometry,
    ProductionToolpathDocument,
)
from custombuild_cam.production_verification import (
    CuttingProgramStatus,
    verify_production_toolpaths,
)
from custombuild_cam.production_verification import (
    cutting_backplot_svg as independent_cutting_backplot_svg,
)
from custombuild_postprocessors import (
    LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
    PRODUCTION_GCODE_PARSER_VERSION,
    PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
    SPINDLE_DWELL_ROLE,
    GCodeSafetyError,
    LinuxCNCProductionMachineProfile,
    LinuxCNCProductionPostprocessor,
    ProductionMachineProgram,
    validate_production_program,
)

from .artifact_limits import MAX_ARTIFACT_BYTES, MAX_CORE_DOCUMENT_BYTES
from .cam_software_provenance import (
    CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION,
    CAM_CANDIDATE_PACKAGE_BUILDER_VERSION,
    CAMImplementationIdentity,
    CAMSoftwareProvenanceError,
    ProducerBuildIdentity,
    build_cam_software_provenance,
    parse_producer_build_identity,
    parse_supported_cam_implementation_identity,
    test_only_producer_build_identity,
    validate_cam_software_provenance,
)
from .errors import ArtifactError
from .model import (
    CAMOperation,
    MachineProfile,
    OperationKind,
    OperationsDocument,
    Point2D,
    Rect,
    Setup,
    Side,
    ToolSpec,
    canonical_json_bytes,
    sha256_hex,
)
from .package import ArtifactFile, read_and_verify_package
from .production_machine_profile import (
    TEST_ONLY_PROFILE,
    LoadedProductionMachineProfile,
    ProductionMachineProfileError,
    load_production_machine_profile,
)
from .profiles import linuxcnc_reference_router_1325, linuxcnc_reference_router_5125

CAM_CANDIDATE_PROGRAM_INDEX_SCHEMA_VERSION = "custombuild.program-index.v1"
CAM_CANDIDATE_VALIDATION_REPORT_SCHEMA_VERSION = "custombuild.cam-candidate-validation-report.v1"
CAM_CANDIDATE_STATUS = "CUTTING_CANDIDATE_GENERATED"
CAM_CANDIDATE_TOOLPATH_PATH = "cam/toolpaths.v1.json"
CAM_CANDIDATE_SOURCE_OPERATIONS_PATH = "source/operations.v2.json"
CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH = "source/validation-machine-profile.v1.json"
CAM_CANDIDATE_BACKPLOT_PATH = "cam/cutting-backplot.svg"
CAM_CANDIDATE_PROGRAM_INDEX_PATH = "machine-production/program-index.v1.json"
CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH = "machine-production/setup-instructions.v1.json"
CAM_CANDIDATE_REPORT_PATH = "validation/cutting-program-report.json"
CAM_CANDIDATE_PROGRAM_ROOT = "machine-production/linuxcnc/"
CAM_CANDIDATE_MACHINE_PROFILE_PATH = "machine-production/production-machine-profile.v1.json"
CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH = "machine-production/linuxcnc-production-profile.v1.json"
CAM_CANDIDATE_POSTPROCESSOR_ID = LINUXCNC_PRODUCTION_POSTPROCESSOR_ID
CAM_CANDIDATE_POSTPROCESSOR_VERSION = LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION
CAM_CANDIDATE_SETUP_INSTRUCTIONS_SCHEMA_VERSION = "custombuild.production-setup-instructions.v1"

MAX_CAM_CANDIDATE_PACKAGE_BYTES = MAX_ARTIFACT_BYTES
MAX_CAM_CANDIDATE_FILES = 2_048
MAX_CAM_CANDIDATE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_CAM_CANDIDATE_COMPRESSION_RATIO = 1_000

_CHECKSUM_SCOPE = "all payload files; manifest.json excluded to avoid recursive hashing"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PROGRAM_PATH_RE = re.compile(r"machine-production/linuxcnc/[A-Za-z0-9._-]+\.production\.ngc")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "builder_version",
        "status",
        "mode",
        "release_scope",
        "machine_use",
        "physical_cutting_authorized",
        "workshop_acceptance_required",
        "base_design_review",
        "source_machine_profile",
        "machine_profile",
        "materials",
        "production_profile",
        "production_machine_profile",
        "controller",
        "postprocessor",
        "toolpaths",
        "setup_instructions",
        "software_provenance",
        "artifacts",
        "candidate_context_hash",
        "checksum_scope",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "media_type", "role", "size_bytes", "sha256"})


@dataclass(frozen=True, slots=True)
class _BaseDesignReviewBinding:
    bundle_sha256: str
    bundle_size_bytes: int
    manifest_sha256: str
    operations_sha256: str
    project_id: str
    revision: str
    design_hash: str
    machine_profile_id: str
    machine_profile_version: str
    machine_profile_fingerprint: str
    operations_bytes: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_sha256": self.bundle_sha256,
            "bundle_size_bytes": self.bundle_size_bytes,
            "design_hash": self.design_hash,
            "manifest_path": "manifest.json",
            "manifest_sha256": self.manifest_sha256,
            "machine_profile": {
                "id": self.machine_profile_id,
                "version": self.machine_profile_version,
                "fingerprint": self.machine_profile_fingerprint,
            },
            "operations_path": "cam/operations.json",
            "candidate_operations_path": CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
            "operations_size_bytes": len(self.operations_bytes),
            "operations_sha256": self.operations_sha256,
            "project_id": self.project_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class CAMCandidateBundle:
    """A verified sidecar and its exact in-memory source projections."""

    zip_bytes: bytes
    manifest: dict[str, Any]
    artifacts: tuple[ArtifactFile, ...]
    toolpaths: ProductionToolpathDocument
    production_profile: LoadedProductionMachineProfile
    production_machine_profile: LinuxCNCProductionMachineProfile
    programs: tuple[ProductionMachineProgram, ...]
    program_index: dict[str, Any]
    setup_instructions: dict[str, Any]
    cutting_program_report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CAMCandidateVerificationRequest:
    payload: bytes
    archive: zipfile.ZipFile
    infos: tuple[zipfile.ZipInfo, ...]
    manifest_bytes: bytes
    manifest: dict[str, Any]
    base: _BaseDesignReviewBinding
    allow_test_only: bool


@dataclass(frozen=True, slots=True)
class _CAMCandidateVerificationDispatch:
    support_id: str
    verification_dispatch: str
    implementation_digest: str
    verify: Callable[[_CAMCandidateVerificationRequest], dict[str, Any]]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.support_id, self.verification_dispatch, self.implementation_digest)


def build_cam_candidate_bundle(
    base_design_review_bundle: bytes,
    *,
    toolpaths: ProductionToolpathDocument,
    programs: Iterable[ProductionMachineProgram],
    production_profile: LoadedProductionMachineProfile,
    cutting_backplot_svg: bytes | None = None,
    producer_build_identity: ProducerBuildIdentity | Mapping[str, Any] | None = None,
) -> CAMCandidateBundle:
    """Build and self-verify a CAM sidecar bound to one verified review ZIP."""

    base = _verified_base_binding(base_design_review_bundle)
    source_machine_profile = _trusted_source_machine_profile(base)
    _validate_toolpath_base_binding(toolpaths, base)
    source_operations = _parse_operations_document(base.operations_bytes)
    verification = verify_production_toolpaths(toolpaths, source_operations)
    if verification.report.status != CuttingProgramStatus.PASS:
        raise ArtifactError("independent cutting verification blocked the CAM candidate")
    canonical_backplot = independent_cutting_backplot_svg(toolpaths, source_operations)
    if cutting_backplot_svg is not None and cutting_backplot_svg != canonical_backplot:
        raise ArtifactError("cutting backplot differs from independent verification output")
    _validate_loaded_production_profile(toolpaths, production_profile)
    producer_build = _resolve_producer_build_identity(
        producer_build_identity,
        test_only=production_profile.profile_class == TEST_ONLY_PROFILE,
    )
    production_machine_profile = production_profile.postprocessor_profile
    program_values = _validate_programs(toolpaths, programs, production_machine_profile)

    toolpath_bytes = toolpaths.to_json()
    index = _build_program_index(
        toolpaths,
        program_values,
        production_profile,
        production_machine_profile,
    )
    setup_instructions = _build_setup_instructions(
        toolpaths,
        program_values,
        production_profile,
        production_machine_profile,
    )
    report = _build_cutting_program_report(
        toolpaths,
        program_values,
        production_profile,
        production_machine_profile,
        source_operations,
    )
    artifacts: list[ArtifactFile] = [
        ArtifactFile(
            CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
            base.operations_bytes,
            "application/json",
            "SOURCE_MACHINE_NEUTRAL_OPERATIONS",
        ),
        ArtifactFile(
            CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
            canonical_json_bytes(source_machine_profile),
            "application/json",
            "SOURCE_VALIDATION_MACHINE_PROFILE",
        ),
        ArtifactFile(
            CAM_CANDIDATE_TOOLPATH_PATH,
            toolpath_bytes,
            "application/json",
            "PRODUCTION_TOOLPATH_DOCUMENT",
        ),
        ArtifactFile(
            CAM_CANDIDATE_PROGRAM_INDEX_PATH,
            canonical_json_bytes(index),
            "application/json",
            "PRODUCTION_PROGRAM_INDEX",
        ),
        ArtifactFile(
            CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
            canonical_json_bytes(setup_instructions),
            "application/json",
            "PRODUCTION_SETUP_INSTRUCTIONS",
        ),
        ArtifactFile(
            CAM_CANDIDATE_REPORT_PATH,
            canonical_json_bytes(report),
            "application/json",
            "CUTTING_PROGRAM_VALIDATION_REPORT",
        ),
        ArtifactFile(
            CAM_CANDIDATE_MACHINE_PROFILE_PATH,
            production_profile.canonical_document_json,
            "application/json",
            "PRODUCTION_MACHINE_PROFILE_DOCUMENT",
        ),
        ArtifactFile(
            CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
            production_machine_profile.to_json(),
            "application/json",
            "LINUXCNC_POSTPROCESSOR_MACHINE_PROFILE",
        ),
    ]
    _validate_backplot(canonical_backplot)
    artifacts.append(
        ArtifactFile(
            CAM_CANDIDATE_BACKPLOT_PATH,
            canonical_backplot,
            "image/svg+xml",
            "CUTTING_BACKPLOT",
        )
    )
    artifacts.extend(
        ArtifactFile(
            f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}",
            program.content,
            "text/x-gcode",
            "EXECUTABLE_CAM_CANDIDATE_PROGRAM",
        )
        for program in program_values
    )
    artifact_values = tuple(sorted(artifacts, key=lambda item: item.path))
    _require_unique_paths(artifact_values)
    manifest_bytes = _build_manifest(
        base,
        toolpaths,
        program_values,
        production_profile,
        production_machine_profile,
        artifact_values,
        producer_build,
    )
    zip_bytes = _build_deterministic_zip(manifest_bytes, artifact_values)
    manifest = read_and_verify_cam_candidate_package(
        zip_bytes,
        base_design_review_bundle=base_design_review_bundle,
        expected_producer_source_manifest_sha256=(producer_build.source_manifest_sha256),
        allow_test_only=production_profile.profile_class == TEST_ONLY_PROFILE,
    )
    return CAMCandidateBundle(
        zip_bytes=zip_bytes,
        manifest=manifest,
        artifacts=artifact_values,
        toolpaths=toolpaths,
        production_profile=production_profile,
        production_machine_profile=production_machine_profile,
        programs=program_values,
        program_index=index,
        setup_instructions=setup_instructions,
        cutting_program_report=report,
    )


def read_and_verify_cam_candidate_package(
    payload: bytes,
    *,
    base_design_review_bundle: bytes,
    expected_producer_source_manifest_sha256: str,
    allow_test_only: bool = False,
    require_current_implementations: bool = True,
) -> dict[str, Any]:
    """Verify canonical integrity and the external review-bundle binding.

    This low-level reader establishes structural self-consistency, not sender
    authenticity. A release boundary must also compare the candidate digest
    and production-profile identity with its independently authenticated job
    receipt.
    """

    if type(allow_test_only) is not bool:
        raise ArtifactError("allow_test_only must be an explicit boolean")
    if type(require_current_implementations) is not bool:
        raise ArtifactError("require_current_implementations must be an explicit boolean")
    if (
        not isinstance(expected_producer_source_manifest_sha256, str)
        or _HASH_RE.fullmatch(expected_producer_source_manifest_sha256) is None
    ):
        raise ArtifactError(
            "expected producer source-manifest SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if type(payload) is not bytes or not payload or len(payload) > MAX_CAM_CANDIDATE_PACKAGE_BYTES:
        raise ArtifactError("CAM candidate ZIP is empty or exceeds its canonical size limit")
    base = _verified_base_binding(base_design_review_bundle)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
    except zipfile.BadZipFile as exc:
        raise ArtifactError("invalid CAM candidate ZIP") from exc
    with archive:
        infos = archive.infolist()
        _validate_zip_envelope(archive, infos)
        try:
            manifest_bytes = archive.read("manifest.json")
        except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ArtifactError("CAM candidate package has no manifest.json") from exc
        manifest = _canonical_json_object(manifest_bytes, label="CAM candidate manifest")
        try:
            software_provenance = manifest.get("software_provenance")
            if not isinstance(software_provenance, Mapping):
                raise CAMSoftwareProvenanceError("CAM software provenance is not an object")
            embedded_producer_build = validate_cam_software_provenance(
                software_provenance,
                allow_test_only=allow_test_only,
                require_current_implementations=require_current_implementations,
            )
            implementation_identity = parse_supported_cam_implementation_identity(
                software_provenance.get("implementations")
            )
            dispatch = _resolve_cam_candidate_verification_dispatch(implementation_identity)
        except CAMSoftwareProvenanceError as exc:
            raise ArtifactError("CAM candidate software provenance is invalid") from exc
        if (
            embedded_producer_build.source_manifest_sha256
            != expected_producer_source_manifest_sha256
        ):
            raise ArtifactError(
                "CAM candidate producer SOURCE_MANIFEST_SHA256 differs from the expected code root"
            )
        return dispatch.verify(
            _CAMCandidateVerificationRequest(
                payload=payload,
                archive=archive,
                infos=tuple(infos),
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                base=base,
                allow_test_only=allow_test_only,
            )
        )


def _verify_cam_candidate_v1(
    request: _CAMCandidateVerificationRequest,
) -> dict[str, Any]:
    """Verify one package with the frozen v1 parser/verifier implementation."""

    manifest = request.manifest
    _validate_manifest_shape(manifest)
    entries = _validate_artifact_entries(manifest.get("artifacts"))
    artifact_paths = tuple(str(entry["path"]) for entry in entries)
    names = [info.filename for info in request.infos]
    if names != ["manifest.json", *artifact_paths]:
        raise ArtifactError("CAM candidate ZIP inventory/order differs from its manifest")

    artifact_values: list[ArtifactFile] = []
    data_by_path: dict[str, bytes] = {}
    for entry in entries:
        path = cast(str, entry["path"])
        try:
            data = request.archive.read(path)
        except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ArtifactError(f"cannot read CAM candidate artifact: {path}") from exc
        if len(data) != entry["size_bytes"] or sha256_hex(data) != entry["sha256"]:
            raise ArtifactError(f"CAM candidate artifact checksum mismatch: {path}")
        artifact_values.append(
            ArtifactFile(path, data, cast(str, entry["media_type"]), cast(str, entry["role"]))
        )
        data_by_path[path] = data

    base = request.base
    if manifest["base_design_review"] != base.as_dict():
        raise ArtifactError("CAM candidate is not bound to the supplied design-review bundle")
    document = _parse_toolpath_document(data_by_path[CAM_CANDIDATE_TOOLPATH_PATH])
    _validate_toolpath_base_binding(document, base)
    if data_by_path[CAM_CANDIDATE_SOURCE_OPERATIONS_PATH] != base.operations_bytes:
        raise ArtifactError("candidate source operations differ from the review bundle")
    source_operations = _parse_operations_document(
        data_by_path[CAM_CANDIDATE_SOURCE_OPERATIONS_PATH]
    )
    expected_source_profile = canonical_json_bytes(_trusted_source_machine_profile(base))
    if data_by_path[CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH] != expected_source_profile:
        raise ArtifactError(
            "embedded source machine profile differs from the trusted reviewed profile"
        )
    production_profile = _load_embedded_production_profile(
        data_by_path[CAM_CANDIDATE_MACHINE_PROFILE_PATH],
        allow_test_only=request.allow_test_only,
    )
    production_machine_profile = _parse_production_machine_profile(
        data_by_path[CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH]
    )
    _validate_loaded_production_profile(document, production_profile)
    if production_machine_profile != production_profile.postprocessor_profile:
        raise ArtifactError("postprocessor profile differs from the production profile")
    _validate_production_machine_profile(document, production_machine_profile)

    programs = _programs_from_index(
        data_by_path[CAM_CANDIDATE_PROGRAM_INDEX_PATH],
        document=document,
        production_machine_profile=production_machine_profile,
        data_by_path=data_by_path,
    )
    expected_paths = {
        CAM_CANDIDATE_TOOLPATH_PATH,
        CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        CAM_CANDIDATE_PROGRAM_INDEX_PATH,
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
        CAM_CANDIDATE_REPORT_PATH,
        *(f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}" for program in programs),
    }
    _validate_backplot(data_by_path[CAM_CANDIDATE_BACKPLOT_PATH])
    expected_paths.add(CAM_CANDIDATE_BACKPLOT_PATH)
    if set(data_by_path) != expected_paths:
        raise ArtifactError("CAM candidate ZIP contains an unbound extra artifact")
    expected_index = _build_program_index(
        document,
        programs,
        production_profile,
        production_machine_profile,
    )
    if canonical_json_bytes(expected_index) != data_by_path[CAM_CANDIDATE_PROGRAM_INDEX_PATH]:
        raise ArtifactError("production program index differs from canonical toolpath binding")
    expected_instructions = _build_setup_instructions(
        document,
        programs,
        production_profile,
        production_machine_profile,
    )
    if (
        canonical_json_bytes(expected_instructions)
        != data_by_path[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH]
    ):
        raise ArtifactError("production setup instructions differ from bound machine facts")
    expected_report = _build_cutting_program_report(
        document,
        programs,
        production_profile,
        production_machine_profile,
        source_operations,
    )
    if canonical_json_bytes(expected_report) != data_by_path[CAM_CANDIDATE_REPORT_PATH]:
        raise ArtifactError("cutting program report differs from strict program validation")
    expected_backplot = independent_cutting_backplot_svg(document, source_operations)
    if expected_backplot != data_by_path[CAM_CANDIDATE_BACKPLOT_PATH]:
        raise ArtifactError("cutting backplot differs from independent verification")

    expected_manifest = _rebuild_manifest_for_verification(
        base,
        document,
        programs,
        production_profile,
        production_machine_profile,
        tuple(artifact_values),
        manifest["software_provenance"],
    )
    if request.manifest_bytes != expected_manifest:
        raise ArtifactError("CAM candidate manifest differs from canonical bound context")
    rebuilt = _build_deterministic_zip(expected_manifest, tuple(artifact_values))
    if rebuilt != request.payload:
        raise ArtifactError("CAM candidate ZIP is not byte-canonical")
    return manifest


def _dispatch_entry(
    identity: CAMImplementationIdentity,
    verify: Callable[[_CAMCandidateVerificationRequest], dict[str, Any]],
) -> _CAMCandidateVerificationDispatch:
    support_id, verification_dispatch, implementation_digest = identity.dispatch_key
    return _CAMCandidateVerificationDispatch(
        support_id=support_id,
        verification_dispatch=verification_dispatch,
        implementation_digest=implementation_digest,
        verify=verify,
    )


_V1_IMPLEMENTATION_IDENTITY = parse_supported_cam_implementation_identity(
    {
        "toolpath_schema_version": "custombuild.toolpaths.v1",
        "toolpath_engine_version": "production-toolpaths-1.1.0",
        "cutting_verifier_version": "cutting-program-verifier-1.1.0",
        "cutting_backplot_version": "cutting-backplot-1.1.0",
        "postprocessor_id": "linuxcnc-3axis-production",
        "postprocessor_version": "1.1.0",
        "gcode_parser_version": "linuxcnc-production-parser-1.3.0",
        "gcode_safety_validator_version": "linuxcnc-production-safety-1.3.0",
        "candidate_manifest_schema_version": "custombuild.cam-candidate-manifest.v2",
        "candidate_package_builder_version": "deterministic-cam-candidate-package-1.1.0",
    }
)
_V1_VERIFICATION_DISPATCH = _dispatch_entry(
    _V1_IMPLEMENTATION_IDENTITY,
    _verify_cam_candidate_v1,
)
_CAM_CANDIDATE_VERIFICATION_DISPATCHES: Mapping[
    tuple[str, str, str], _CAMCandidateVerificationDispatch
] = MappingProxyType({_V1_VERIFICATION_DISPATCH.key: _V1_VERIFICATION_DISPATCH})


def _resolve_cam_candidate_verification_dispatch(
    identity: CAMImplementationIdentity,
) -> _CAMCandidateVerificationDispatch:
    dispatch = _CAM_CANDIDATE_VERIFICATION_DISPATCHES.get(identity.dispatch_key)
    if dispatch is None or dispatch.key != identity.dispatch_key:
        raise CAMSoftwareProvenanceError(
            "CAM implementation has no exact package verification callable"
        )
    return dispatch


def _verified_base_binding(payload: bytes) -> _BaseDesignReviewBinding:
    if type(payload) is not bytes or not payload:
        raise ArtifactError("base design-review bundle must be non-empty bytes")
    manifest = read_and_verify_package(payload)
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            manifest_bytes = archive.read("manifest.json")
            operations_bytes = archive.read("cam/operations.json")
            generation_plan_bytes = archive.read("validation/generation-plan.json")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ArtifactError("base design-review bundle has no verified CAM operations") from exc
    operations = _canonical_json_object(operations_bytes, label="base CAM operations")
    generation_plan = _canonical_json_object(
        generation_plan_bytes,
        label="base generation plan",
    )
    design_hash = _required_hash(manifest.get("design_hash"), "base design hash")
    if operations.get("design_hash") != design_hash:
        raise ArtifactError("base CAM operations design hash differs from its manifest")
    machine = manifest.get("machine_profile")
    if not isinstance(machine, dict) or frozenset(machine) != {"id", "version"}:
        raise ArtifactError("base design-review machine profile is invalid")
    generation_machine = generation_plan.get("machine_profile")
    if (
        not isinstance(generation_machine, dict)
        or frozenset(generation_machine) != {"id", "version", "fingerprint"}
        or generation_machine.get("id") != machine.get("id")
        or generation_machine.get("version") != machine.get("version")
    ):
        raise ArtifactError("base generation-plan machine binding is invalid")
    return _BaseDesignReviewBinding(
        bundle_sha256=sha256_hex(payload),
        bundle_size_bytes=len(payload),
        manifest_sha256=sha256_hex(manifest_bytes),
        operations_sha256=sha256_hex(operations_bytes),
        project_id=_required_string(manifest.get("project_id"), "base project ID"),
        revision=_required_string(manifest.get("revision"), "base revision"),
        design_hash=design_hash,
        machine_profile_id=_required_string(machine.get("id"), "base machine profile ID"),
        machine_profile_version=_required_string(
            machine.get("version"), "base machine profile version"
        ),
        machine_profile_fingerprint=_required_hash(
            generation_machine.get("fingerprint"),
            "base machine profile fingerprint",
        ),
        operations_bytes=operations_bytes,
    )


def read_operations_document_from_design_review_bundle(
    payload: bytes,
) -> OperationsDocument:
    """Return operations only after the complete review ZIP verifies.

    This deliberately does not accept a standalone operations document.  The
    design, source-machine identity and artifact checksum therefore remain
    anchored in the signed/canonical design-review package boundary.
    """

    return _parse_operations_document(_verified_base_binding(payload).operations_bytes)


def _resolve_producer_build_identity(
    supplied: ProducerBuildIdentity | Mapping[str, Any] | None,
    *,
    test_only: bool,
) -> ProducerBuildIdentity:
    try:
        if supplied is None:
            if not test_only:
                raise CAMSoftwareProvenanceError(
                    "production CAM requires an independently supplied producer build identity"
                )
            identity = test_only_producer_build_identity()
        else:
            identity = parse_producer_build_identity(
                supplied.as_dict() if isinstance(supplied, ProducerBuildIdentity) else supplied,
                allow_test_only=test_only,
            )
        return identity
    except CAMSoftwareProvenanceError as exc:
        raise ArtifactError(f"CAM producer build identity is invalid: {exc}") from exc


def _trusted_source_machine_profile(base: _BaseDesignReviewBinding) -> MachineProfile:
    matches = tuple(
        profile
        for profile in (
            linuxcnc_reference_router_1325(),
            linuxcnc_reference_router_5125(),
        )
        if (
            profile.profile_id == base.machine_profile_id
            and profile.version == base.machine_profile_version
            and sha256_hex(canonical_json_bytes(profile)) == base.machine_profile_fingerprint
        )
    )
    if len(matches) != 1:
        raise ArtifactError("review bundle source machine profile is not trusted")
    return matches[0]


def _validate_toolpath_base_binding(
    document: ProductionToolpathDocument,
    base: _BaseDesignReviewBinding,
) -> None:
    if not isinstance(document, ProductionToolpathDocument):
        raise ArtifactError("CAM candidate requires a ProductionToolpathDocument")
    if document.design_hash != base.design_hash:
        raise ArtifactError("production toolpaths design hash differs from the review bundle")
    if document.operations_sha256 != base.operations_sha256:
        raise ArtifactError("production toolpaths are not bound to the review-bundle operations")
    context = document.execution_context
    if (
        context.source_machine_profile_id != base.machine_profile_id
        or context.source_machine_profile_version != base.machine_profile_version
        or context.source_machine_profile_fingerprint != base.machine_profile_fingerprint
    ):
        raise ArtifactError("production machine profile differs from the reviewed CAM profile")


def _validate_production_machine_profile(
    document: ProductionToolpathDocument,
    profile: LinuxCNCProductionMachineProfile,
) -> None:
    if not isinstance(profile, LinuxCNCProductionMachineProfile):
        raise ArtifactError("CAM candidate requires a LinuxCNC production machine profile")
    context = document.execution_context
    if (
        profile.machine_profile_id != context.machine_profile_id
        or profile.machine_profile_version != context.machine_profile_version
        or profile.controller_id != context.controller_id
        or profile.controller_version != context.controller_version
        or profile.machine_x_min_um != context.machine_x_min_um
        or profile.machine_x_max_um != context.machine_x_max_um
        or profile.machine_y_min_um != context.machine_y_min_um
        or profile.machine_y_max_um != context.machine_y_max_um
        or profile.machine_z_min_um != context.machine_z_min_um
        or profile.machine_z_max_um != context.machine_z_max_um
        or any(setup.wcs not in profile.supported_wcs for setup in context.setups)
    ):
        raise ArtifactError("LinuxCNC production profile differs from the toolpath context")
    offsets = {offset.wcs: offset for offset in profile.wcs_offsets}
    for setup in context.setups:
        offset = offsets.get(setup.wcs)
        if offset is None or (
            offset.machine_x0_um,
            offset.machine_y0_um,
            offset.machine_z0_um,
            offset.machine_xy_rotation_mdeg,
        ) != (
            setup.machine_wcs_origin.x_um,
            setup.machine_wcs_origin.y_um,
            setup.machine_wcs_z0_um,
            setup.machine_wcs_xy_rotation_mdeg,
        ):
            raise ArtifactError("LinuxCNC WCS offsets differ from the bound setup")


def _validate_loaded_production_profile(
    document: ProductionToolpathDocument,
    loaded: LoadedProductionMachineProfile,
) -> None:
    if not isinstance(loaded, LoadedProductionMachineProfile):
        raise ArtifactError("CAM candidate requires a verified production-profile receipt")
    try:
        reparsed = load_production_machine_profile(
            loaded.canonical_document_json,
            allow_test_only=loaded.profile_class == TEST_ONLY_PROFILE,
        )
    except (ProductionMachineProfileError, TypeError, ValueError) as exc:
        raise ArtifactError("production profile receipt cannot be independently reloaded") from exc
    if reparsed != loaded:
        raise ArtifactError("production profile receipt differs from its canonical document")
    if (
        loaded.execution_context != document.execution_context
        or loaded.execution_context.fingerprint != document.execution_context.fingerprint
        or loaded.postprocessor_profile.config_sha256 != loaded.postprocessor_profile.fingerprint
    ):
        raise ArtifactError("production profile facts differ from the immutable toolpaths")
    _validate_production_machine_profile(document, loaded.postprocessor_profile)


def _load_embedded_production_profile(
    payload: bytes,
    *,
    allow_test_only: bool,
) -> LoadedProductionMachineProfile:
    _canonical_json_object(payload, label="production machine profile document")
    try:
        return load_production_machine_profile(payload, allow_test_only=allow_test_only)
    except (ProductionMachineProfileError, TypeError, ValueError) as exc:
        raise ArtifactError("embedded production machine profile is invalid") from exc


def _loaded_production_profile_identity(
    loaded: LoadedProductionMachineProfile,
) -> dict[str, Any]:
    return {
        "path": CAM_CANDIDATE_MACHINE_PROFILE_PATH,
        "schema_version": loaded.schema_version,
        "profile_class": loaded.profile_class,
        "payload_sha256": loaded.payload_sha256,
        "document_sha256": loaded.document_sha256,
        "size_bytes": len(loaded.canonical_document_json),
        "execution_context_sha256": loaded.execution_context.fingerprint,
        "acceptance": {
            "status": loaded.acceptance_status,
            "evidence_id": loaded.acceptance_evidence.evidence_id,
            "evidence_version": loaded.acceptance_evidence.evidence_version,
            "evidence_sha256": loaded.acceptance_evidence.evidence_sha256,
        },
        "postprocessor_profile": {
            "path": CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
            "profile_id": loaded.postprocessor_profile.profile_id,
            "version": loaded.postprocessor_profile.version,
            "config_sha256": loaded.postprocessor_profile.config_sha256,
        },
    }


def _production_machine_profile_identity(
    profile: LinuxCNCProductionMachineProfile,
) -> dict[str, Any]:
    payload = profile.to_json()
    return {
        "path": CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        "profile_id": profile.profile_id,
        "version": profile.version,
        "sha256": sha256_hex(payload),
        "config_sha256": profile.config_sha256,
        "size_bytes": len(payload),
        "machine_profile": {
            "id": profile.machine_profile_id,
            "version": profile.machine_profile_version,
        },
        "controller": {
            "id": profile.controller_id,
            "version": profile.controller_version,
        },
        "runtime_safety": {
            "metric_xyz_identity_kinematics": {
                "policy": profile.metric_xyz_identity_kinematics_policy,
                "evidence": {
                    "id": profile.metric_xyz_identity_kinematics_evidence_id,
                    "version": profile.metric_xyz_identity_kinematics_evidence_version,
                    "sha256": profile.metric_xyz_identity_kinematics_evidence_sha256,
                },
                "linear_units_mm_verified": profile.linear_units_mm_verified,
                "coordinates_xyz_verified": profile.coordinates_xyz_verified,
                "identity_trivkins_verified": profile.identity_trivkins_verified,
                "required_joint_count": 3,
                "exactly_three_joints_verified": profile.exactly_three_joints_verified,
                "joint_0_x_1_y_2_z_verified": profile.joint_0_x_1_y_2_z_verified,
                "no_extra_controlled_axes_verified": (profile.no_extra_controlled_axes_verified),
            },
            "modal_preflight": {
                "required_program_states": ["G8", "G97", "M9", "M49", "M52 P0", "M53 P1"],
                "g8_radius_mode_verified": profile.g8_radius_mode_verified,
                "g97_rpm_mode_verified": profile.g97_rpm_mode_verified,
                "m9_coolant_off_verified": profile.m9_coolant_off_verified,
                "m52_p0_adaptive_feed_disabled_verified": (
                    profile.m52_p0_adaptive_feed_disabled_verified
                ),
                "m53_p1_feed_hold_enabled_verified": profile.m53_p1_feed_hold_enabled_verified,
            },
            "feed_spindle_overrides": {
                "policy": profile.feed_spindle_override_policy,
                "evidence": {
                    "id": profile.feed_spindle_override_evidence_id,
                    "version": profile.feed_spindle_override_evidence_version,
                    "sha256": profile.feed_spindle_override_evidence_sha256,
                },
                "m49_feed_and_spindle_overrides_disabled_verified": (
                    profile.m49_feed_and_spindle_overrides_disabled_verified
                ),
            },
            "external_axis_offsets": {
                "policy": profile.external_axis_offset_policy,
                "evidence": {
                    "id": profile.external_axis_offset_evidence_id,
                    "version": profile.external_axis_offset_evidence_version,
                    "sha256": profile.external_axis_offset_evidence_sha256,
                },
                "external_xyz_offsets_disabled_verified": (
                    profile.external_xyz_offsets_disabled_verified
                ),
            },
            "homing": {
                "policy": profile.homing_preflight_policy,
                "evidence": {
                    "id": profile.homing_preflight_evidence_id,
                    "version": profile.homing_preflight_evidence_version,
                    "sha256": profile.homing_preflight_evidence_sha256,
                },
                "all_xyz_homed_before_auto_verified": (profile.all_xyz_homed_before_auto_verified),
                "no_force_homing_disabled_verified": (profile.no_force_homing_disabled_verified),
            },
            "program_restart": {
                "policy": profile.program_restart_policy,
                "evidence": {
                    "id": profile.program_restart_evidence_id,
                    "version": profile.program_restart_evidence_version,
                    "sha256": profile.program_restart_evidence_sha256,
                },
                "run_from_line_disabled_verified": profile.run_from_line_disabled_verified,
                "full_restart_after_abort_required": profile.full_restart_after_abort_required,
            },
            "tool_change": {
                "policy": profile.m6_tool_table_policy,
                "evidence": {
                    "id": profile.m6_tool_table_evidence_id,
                    "version": profile.m6_tool_table_evidence_version,
                    "sha256": profile.m6_tool_table_evidence_sha256,
                },
                "m6_tool_change_verified": profile.m6_tool_change_verified,
                "m6_preserves_axis_position": profile.m6_preserves_axis_position,
                "m6_preserves_bound_tool_table_verified": (
                    profile.m6_preserves_bound_tool_table_verified
                ),
                "g43_h_length_offset_verified": profile.g43_h_length_offset_verified,
            },
            "wcs_table_preservation": {
                "policy": profile.m6_wcs_table_policy,
                "evidence": {
                    "id": profile.m6_wcs_table_evidence_id,
                    "version": profile.m6_wcs_table_evidence_version,
                    "sha256": profile.m6_wcs_table_evidence_sha256,
                },
                "m6_preserves_bound_wcs_table_verified": (
                    profile.m6_preserves_bound_wcs_table_verified
                ),
                "exact_raw_g5x_xyz_r_preservation_required": True,
                "required_continuity": (
                    "EXACT_RAW_G5X_XYZ_AND_R_FROM_PREFLIGHT_THROUGH_POST_M6_WCS_SELECTION"
                ),
            },
            "spindle_at_speed": {
                "policy": profile.spindle_at_speed_policy,
                "feedback_source": profile.spindle_feedback_source,
                "tolerance_ppm": profile.spindle_at_speed_tolerance_ppm,
                "dwell_role": SPINDLE_DWELL_ROLE,
                "evidence": {
                    "id": profile.spindle_at_speed_evidence_id,
                    "version": profile.spindle_at_speed_evidence_version,
                    "sha256": profile.spindle_at_speed_evidence_sha256,
                },
                "real_feedback_verified": profile.real_spindle_feedback_verified,
                "motion_interlock_verified": (profile.spindle_at_speed_motion_interlock_verified),
                "vfd_fault_motion_inhibit_verified": (profile.vfd_fault_motion_inhibit_verified),
                "vfd_fault_spindle_stop_verified": (profile.vfd_fault_spindle_stop_verified),
                "continuous_cutting_feed_interlock": {
                    "policy": profile.continuous_spindle_speed_interlock_policy,
                    "evidence": {
                        "id": profile.continuous_spindle_speed_interlock_evidence_id,
                        "version": profile.continuous_spindle_speed_interlock_evidence_version,
                        "sha256": profile.continuous_spindle_speed_interlock_evidence_sha256,
                    },
                    "continuous_spindle_speed_feed_inhibit_verified": (
                        profile.continuous_spindle_speed_feed_inhibit_verified
                    ),
                },
            },
        },
    }


def _source_machine_profile_identity(profile: MachineProfile) -> dict[str, Any]:
    payload = canonical_json_bytes(profile)
    return {
        "path": CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
        "profile_id": profile.profile_id,
        "version": profile.version,
        "sha256": sha256_hex(payload),
        "size_bytes": len(payload),
        "mode": "VALIDATION",
    }


def _setup_material_binding(setup: BoundSetup) -> dict[str, Any]:
    """Expose the reviewed source material and accepted shop material separately."""

    return {
        "source_material": {
            "id": setup.source_material_id,
            "version": setup.source_material_version,
        },
        "actual_material": {
            "id": setup.material_id,
            "version": setup.material_version,
            "evidence": {
                "id": setup.material_evidence_id,
                "version": setup.material_evidence_version,
                "sha256": setup.material_evidence_sha256,
            },
        },
    }


def _physical_sheet_material_bindings(
    context: ProductionExecutionContext,
) -> list[dict[str, Any]]:
    """Return one immutable material receipt per physical stock sheet."""

    bindings: dict[tuple[str, int], dict[str, Any]] = {}
    for setup in context.setups:
        key = (setup.stock_id, setup.sheet_index)
        row = {
            "stock_id": setup.stock_id,
            "sheet_index": setup.sheet_index,
            **_setup_material_binding(setup),
        }
        previous = bindings.setdefault(key, row)
        if previous != row:
            raise ArtifactError(
                "production setups disagree on the material binding for one physical sheet"
            )
    return list(bindings.values())


def _validate_programs(
    document: ProductionToolpathDocument,
    programs: Iterable[ProductionMachineProgram],
    production_machine_profile: LinuxCNCProductionMachineProfile,
) -> tuple[ProductionMachineProgram, ...]:
    collected: list[ProductionMachineProgram] = []
    for program in programs:
        if len(collected) == 999:
            raise ArtifactError("CAM candidate cannot contain more than 999 machine programs")
        collected.append(program)
    values = tuple(collected)
    if not values:
        raise ArtifactError("CAM candidate requires at least one machine program")
    if tuple(program.run_order for program in values) != tuple(range(1, len(values) + 1)):
        raise ArtifactError("machine-program execution order must be dense and start at one")
    if len(values) != len(document.programs):
        raise ArtifactError("machine-program inventory does not cover every toolpath program")
    if len({program.filename.casefold() for program in values}) != len(values):
        raise ArtifactError("machine-program filenames contain duplicate case aliases")
    try:
        expected_values = LinuxCNCProductionPostprocessor(production_machine_profile).generate(
            document
        )
    except (GCodeSafetyError, TypeError, ValueError) as exc:
        raise ArtifactError("canonical LinuxCNC programs cannot be generated") from exc
    if values != expected_values:
        raise ArtifactError("machine programs differ from canonical LinuxCNC postprocessor output")
    context = document.execution_context
    for machine_program, planned in zip(values, document.programs, strict=True):
        if (
            machine_program.program_id != planned.program_id
            or machine_program.run_order != planned.run_order
            or machine_program.setup_id != planned.setup_id
            or machine_program.tool_id != planned.tool_id
            or machine_program.source_toolpaths_sha256 != document.fingerprint
            or machine_program.production_machine_profile_sha256
            != production_machine_profile.config_sha256
            or machine_program.controller.casefold() != context.controller_id.casefold()
            or machine_program.controller_version != context.controller_version
            or machine_program.postprocessor_id != CAM_CANDIDATE_POSTPROCESSOR_ID
            or machine_program.postprocessor_version != CAM_CANDIDATE_POSTPROCESSOR_VERSION
            or machine_program.mode != EXECUTABLE_CAM_CANDIDATE_MODE
            or machine_program.machine_executable is not True
            or machine_program.physical_cutting_authorized is not False
            or machine_program.workshop_acceptance_required is not True
        ):
            raise ArtifactError("machine program identity or safety claims differ from toolpaths")
        try:
            validate_production_program(
                machine_program.content,
                document=document,
                program=planned,
                machine_profile=production_machine_profile,
            )
        except GCodeSafetyError as exc:
            raise ArtifactError(
                f"machine program failed strict round-trip validation: {machine_program.program_id}"
            ) from exc
    return values


def _build_program_index(
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
) -> dict[str, Any]:
    context = document.execution_context
    setup_by_id = {setup.setup_id: setup for setup in context.setups}
    tool_by_id = {tool.tool_id: tool for tool in context.tool_bindings}
    planned_by_id = {program.program_id: program for program in document.programs}
    return {
        "schema_version": CAM_CANDIDATE_PROGRAM_INDEX_SCHEMA_VERSION,
        "status": CAM_CANDIDATE_STATUS,
        "mode": EXECUTABLE_CAM_CANDIDATE_MODE,
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "program_count": len(programs),
        "units": {
            "linear": "integer_micrometres",
            "feed": "integer_micrometres_per_minute",
            "spindle": "revolutions_per_minute",
        },
        "machine": {
            "profile_id": context.machine_profile_id,
            "profile_version": context.machine_profile_version,
            "profile_sha256": document.machine_profile_fingerprint,
            "controller_id": context.controller_id,
            "controller_version": context.controller_version,
            "absolute_bounds_um": {
                "x_min": context.machine_x_min_um,
                "x_max": context.machine_x_max_um,
                "y_min": context.machine_y_min_um,
                "y_max": context.machine_y_max_um,
                "z_min": context.machine_z_min_um,
                "z_max": context.machine_z_max_um,
            },
        },
        "toolpaths": {
            "path": CAM_CANDIDATE_TOOLPATH_PATH,
            "sha256": document.fingerprint,
            "fingerprint": document.fingerprint,
        },
        "source_operations": {
            "path": CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
            "sha256": document.operations_sha256,
        },
        "source_machine_profile": {
            "path": CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
            "profile_id": document.execution_context.source_machine_profile_id,
            "version": document.execution_context.source_machine_profile_version,
            "sha256": (document.execution_context.source_machine_profile_fingerprint),
        },
        "materials": _physical_sheet_material_bindings(context),
        "postprocessor": {
            "id": CAM_CANDIDATE_POSTPROCESSOR_ID,
            "version": CAM_CANDIDATE_POSTPROCESSOR_VERSION,
            "identity": (f"{CAM_CANDIDATE_POSTPROCESSOR_ID}@{CAM_CANDIDATE_POSTPROCESSOR_VERSION}"),
        },
        "production_machine_profile": _production_machine_profile_identity(
            production_machine_profile
        ),
        "production_profile": _loaded_production_profile_identity(production_profile),
        "programs": [
            _program_index_entry(
                program,
                planned=planned_by_id[program.program_id],
                setup=setup_by_id[program.setup_id],
                tool=tool_by_id[program.tool_id],
            )
            for program in programs
        ],
    }


def _program_index_entry(
    program: ProductionMachineProgram,
    *,
    planned: ProductionProgram,
    setup: BoundSetup,
    tool: ProductionToolBinding,
) -> dict[str, Any]:
    path = f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}"
    return {
        "execution_order": program.run_order,
        "program_id": program.program_id,
        "path": path,
        "sha256": program.sha256,
        "size_bytes": len(program.content),
        "media_type": "text/x-gcode",
        "setup": {
            "id": setup.setup_id,
            "stock": {
                "id": setup.stock_id,
                "sheet_index": setup.sheet_index,
                "width_um": setup.stock_width_um,
                "height_um": setup.stock_height_um,
                "thickness_um": setup.stock_thickness_um,
                **_setup_material_binding(setup),
                "side": setup.side.value,
                "orientation": setup.orientation,
            },
            "wcs": setup.wcs,
            "machine_wcs_origin": {
                "x_um": setup.machine_wcs_origin.x_um,
                "y_um": setup.machine_wcs_origin.y_um,
                "z0_um": setup.machine_wcs_z0_um,
                "xy_rotation_mdeg": setup.machine_wcs_xy_rotation_mdeg,
            },
            "reference_surface": setup.reference_surface,
            "probe_method": setup.probe_method,
            "safe_z_um": setup.safe_z_um,
            "minimum_rapid_clearance_um": setup.minimum_rapid_clearance_um,
            "fixture": {
                "id": setup.fixture_id,
                "version": setup.fixture_version,
                "sha256": setup.fixture_sha256,
                "clearance_z_um": setup.fixture_clearance_z_um,
                "keep_out_policy": setup.keep_out_policy,
            },
            "spoilboard": (
                {
                    "id": setup.spoilboard_id,
                    "version": setup.spoilboard_version,
                    "sha256": setup.spoilboard_sha256,
                    "through_cut_allowance_um": setup.through_cut_allowance_um,
                }
                if setup.spoilboard_id is not None
                else None
            ),
        },
        "tool": {
            "id": tool.tool_id,
            "version": tool.tool_version,
            "source_tool_id": tool.source_tool_id,
            "source_tool_version": tool.source_tool_version,
            "source_tool_sha256": tool.source_tool_sha256,
            "controller_tool_number": tool.controller_tool_number,
            "length_offset_number": tool.length_offset_number,
            "expected_length_offset_um": {
                "x": tool.expected_length_offset_x_um,
                "y": tool.expected_length_offset_y_um,
                "z": tool.expected_length_offset_z_um,
            },
            "length_offset_semantics": "SIGNED_LINUXCNC_TOOL_TABLE_XYZ_VALUES",
            "tool_table_evidence": {
                "id": tool.tool_table_evidence_id,
                "version": tool.tool_table_evidence_version,
                "sha256": tool.tool_table_evidence_sha256,
            },
            "live_controller_h_offset_must_equal_expected": True,
            "effective_diameter_um": tool.effective_diameter_um,
            "drill_point_length_um": tool.drill_point_length_um,
            "cutting_length_um": tool.cutting_length_um,
            "measured_stickout_um": tool.measured_stickout_um,
            "minimum_holder_clearance_um": tool.minimum_holder_clearance_um,
            "assembly_collision_radius_um": tool.assembly_collision_radius_um,
        },
        "operation_ids": list(planned.operation_ids),
        "release_operation_ids": list(planned.release_operation_ids),
        "toolpath_binding": {
            "document_path": CAM_CANDIDATE_TOOLPATH_PATH,
            "document_sha256": program.source_toolpaths_sha256,
            "program_id": planned.program_id,
            "run_order": planned.run_order,
        },
        "production_machine_profile_sha256": program.production_machine_profile_sha256,
        "controller": {
            "id": program.controller,
            "version": program.controller_version,
        },
        "postprocessor": {
            "id": program.postprocessor_id,
            "version": program.postprocessor_version,
        },
    }


def _build_setup_instructions(
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
) -> dict[str, Any]:
    """Project immutable workshop facts into deterministic setup instructions."""

    context = document.execution_context
    planned_by_id = {program.program_id: program for program in document.programs}
    tool_by_id = {tool.tool_id: tool for tool in context.tool_bindings}
    recipe_by_id = {recipe.recipe_id: recipe for recipe in context.recipes}
    programs_by_setup: dict[str, list[ProductionMachineProgram]] = {
        setup.setup_id: [] for setup in context.setups
    }
    for program in programs:
        programs_by_setup[program.setup_id].append(program)

    ordered_setup_programs: list[tuple[BoundSetup, tuple[ProductionMachineProgram, ...]]] = []
    for setup in context.setups:
        setup_programs = tuple(programs_by_setup[setup.setup_id])
        if not setup_programs:
            raise ArtifactError(f"production setup has no executable program: {setup.setup_id}")
        ordered_setup_programs.append((setup, setup_programs))
    ordered_setup_programs.sort(key=lambda item: item[1][0].run_order)
    grouped_execution_order = tuple(
        program.run_order
        for _setup, setup_programs in ordered_setup_programs
        for program in setup_programs
    )
    if grouped_execution_order != tuple(range(1, len(programs) + 1)):
        raise ArtifactError("production programs revisit a setup after another setup has started")

    setup_rows: list[dict[str, Any]] = []
    for setup_order, (setup, setup_programs) in enumerate(
        ordered_setup_programs,
        start=1,
    ):
        used_tool_ids = tuple(dict.fromkeys(program.tool_id for program in setup_programs))
        setup_rows.append(
            {
                "setup_order": setup_order,
                "setup_id": setup.setup_id,
                "program_execution_orders": [program.run_order for program in setup_programs],
                "source_setup_sha256": setup.source_setup_sha256,
                "stock": {
                    "stock_id": setup.stock_id,
                    "sheet_index": setup.sheet_index,
                    "width_um": setup.stock_width_um,
                    "height_um": setup.stock_height_um,
                    "thickness_um": setup.stock_thickness_um,
                    **_setup_material_binding(setup),
                    "side": setup.side.value,
                    "orientation": setup.orientation,
                },
                "coordinate_registration": {
                    "source_to_wcs_xy_transform": setup.source_to_wcs_xy_transform,
                    "wcs": setup.wcs,
                    "machine_wcs_origin_semantics": ("RAW_LINUXCNC_G5X_OFFSET_FROM_MACHINE_ORIGIN"),
                    "programmed_coordinates_semantics": "CONTROLLED_POINT_WCS",
                    "g43_axis_endpoint_formula": ("MACHINE_WCS_PLUS_PROGRAMMED_PLUS_EXPECTED_H"),
                    "machine_wcs_origin": {
                        "x_um": setup.machine_wcs_origin.x_um,
                        "y_um": setup.machine_wcs_origin.y_um,
                        "z0_um": setup.machine_wcs_z0_um,
                        "xy_rotation_mdeg": setup.machine_wcs_xy_rotation_mdeg,
                    },
                    "reference_surface": setup.reference_surface,
                    "probe_method": setup.probe_method,
                    "safe_z_um": setup.safe_z_um,
                    "minimum_rapid_clearance_um": (setup.minimum_rapid_clearance_um),
                },
                "fixture": {
                    "id": setup.fixture_id,
                    "version": setup.fixture_version,
                    "sha256": setup.fixture_sha256,
                    "clearance_z_um": setup.fixture_clearance_z_um,
                    "keep_out_policy": setup.keep_out_policy,
                    "keep_out_zones": [
                        {
                            "x_um": zone.x_um,
                            "y_um": zone.y_um,
                            "width_um": zone.width_um,
                            "height_um": zone.height_um,
                        }
                        for zone in setup.keep_out_zones
                    ],
                    "spoilboard": (
                        {
                            "id": setup.spoilboard_id,
                            "version": setup.spoilboard_version,
                            "sha256": setup.spoilboard_sha256,
                            "through_cut_allowance_um": (setup.through_cut_allowance_um),
                        }
                        if setup.spoilboard_id is not None
                        else None
                    ),
                },
                "tools": [
                    {
                        "tool_id": tool_by_id[tool_id].tool_id,
                        "tool_version": tool_by_id[tool_id].tool_version,
                        "source_tool_id": tool_by_id[tool_id].source_tool_id,
                        "source_tool_version": tool_by_id[tool_id].source_tool_version,
                        "source_tool_sha256": tool_by_id[tool_id].source_tool_sha256,
                        "controller_tool_number": (tool_by_id[tool_id].controller_tool_number),
                        "length_offset_number": tool_by_id[tool_id].length_offset_number,
                        "expected_length_offset_um": {
                            "x": tool_by_id[tool_id].expected_length_offset_x_um,
                            "y": tool_by_id[tool_id].expected_length_offset_y_um,
                            "z": tool_by_id[tool_id].expected_length_offset_z_um,
                        },
                        "length_offset_semantics": ("SIGNED_LINUXCNC_TOOL_TABLE_XYZ_VALUES"),
                        "tool_table_evidence": {
                            "id": tool_by_id[tool_id].tool_table_evidence_id,
                            "version": tool_by_id[tool_id].tool_table_evidence_version,
                            "sha256": tool_by_id[tool_id].tool_table_evidence_sha256,
                        },
                        "live_controller_h_offset_must_equal_expected": True,
                        "effective_diameter_um": tool_by_id[tool_id].effective_diameter_um,
                        "drill_point_length_um": tool_by_id[tool_id].drill_point_length_um,
                        "cutting_length_um": tool_by_id[tool_id].cutting_length_um,
                        "measured_stickout_um": tool_by_id[tool_id].measured_stickout_um,
                        "minimum_holder_clearance_um": (
                            tool_by_id[tool_id].minimum_holder_clearance_um
                        ),
                        "assembly_collision_radius_um": (
                            tool_by_id[tool_id].assembly_collision_radius_um
                        ),
                        "geometry": tool_by_id[tool_id].geometry.value,
                        "spindle_direction": tool_by_id[tool_id].spindle_direction,
                    }
                    for tool_id in used_tool_ids
                ],
            }
        )

    program_rows: list[dict[str, Any]] = []
    for program in programs:
        planned = planned_by_id[program.program_id]
        tool = tool_by_id[program.tool_id]
        recipes = tuple(recipe_by_id[recipe_id] for recipe_id in planned.recipe_ids)
        program_rows.append(
            {
                "execution_order": program.run_order,
                "program_id": program.program_id,
                "path": f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}",
                "sha256": program.sha256,
                "setup_id": program.setup_id,
                "tool": {
                    "id": tool.tool_id,
                    "version": tool.tool_version,
                    "controller_tool_number": tool.controller_tool_number,
                    "length_offset_number": tool.length_offset_number,
                    "expected_length_offset_um": {
                        "x": tool.expected_length_offset_x_um,
                        "y": tool.expected_length_offset_y_um,
                        "z": tool.expected_length_offset_z_um,
                    },
                    "length_offset_semantics": ("SIGNED_LINUXCNC_TOOL_TABLE_XYZ_VALUES"),
                    "tool_table_evidence": {
                        "id": tool.tool_table_evidence_id,
                        "version": tool.tool_table_evidence_version,
                        "sha256": tool.tool_table_evidence_sha256,
                    },
                    "live_controller_h_offset_must_equal_expected": True,
                },
                "recipes": [
                    {
                        "id": recipe.recipe_id,
                        "version": recipe.version,
                        "operation_kind": recipe.operation_kind.value,
                        "spindle_rpm": recipe.spindle_rpm,
                        "feed_um_min": recipe.feed_um_min,
                        "plunge_um_min": recipe.plunge_um_min,
                        "stepdown_um": recipe.stepdown_um,
                        "stepover_ppm": recipe.stepover_ppm,
                        "peck_depth_um": recipe.peck_depth_um,
                        "approach_clearance_um": recipe.approach_clearance_um,
                        "through_overtravel_um": recipe.through_overtravel_um,
                        "tab_width_um": recipe.tab_width_um,
                        "tab_height_um": recipe.tab_height_um,
                        "accepted_tolerance_um": recipe.accepted_tolerance_um,
                        "process_accuracy_um": recipe.process_accuracy_um,
                        "entry_strategy": recipe.entry_strategy,
                        "diameter_tolerance_um": recipe.diameter_tolerance_um,
                        "countersink_top_diameter_um": (recipe.countersink_top_diameter_um),
                        "countersink_included_angle_mdeg": (recipe.countersink_included_angle_mdeg),
                    }
                    for recipe in recipes
                ],
                "operation_ids": list(planned.operation_ids),
                "release_operation_ids": list(planned.release_operation_ids),
                "sheet_release_state_after_program": (
                    "SHEET_RELEASED_NO_FURTHER_PROGRAMS"
                    if planned.release_operation_ids
                    else "SHEET_REMAINS_HELD"
                ),
            }
        )

    return {
        "schema_version": CAM_CANDIDATE_SETUP_INSTRUCTIONS_SCHEMA_VERSION,
        "status": CAM_CANDIDATE_STATUS,
        "mode": EXECUTABLE_CAM_CANDIDATE_MODE,
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "setup_count": len(context.setups),
        "program_count": len(programs),
        "units": {
            "linear": "integer_micrometres",
            "feed": "integer_micrometres_per_minute",
            "spindle": "revolutions_per_minute",
        },
        "machine": {
            "profile_id": context.machine_profile_id,
            "profile_version": context.machine_profile_version,
            "profile_sha256": document.machine_profile_fingerprint,
            "controller_id": context.controller_id,
            "controller_version": context.controller_version,
            "absolute_bounds_um": {
                "x_min": context.machine_x_min_um,
                "x_max": context.machine_x_max_um,
                "y_min": context.machine_y_min_um,
                "y_max": context.machine_y_max_um,
                "z_min": context.machine_z_min_um,
                "z_max": context.machine_z_max_um,
            },
        },
        "candidate_bindings": {
            "toolpaths_path": CAM_CANDIDATE_TOOLPATH_PATH,
            "toolpaths_sha256": document.fingerprint,
            "source_operations_path": CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
            "source_operations_sha256": document.operations_sha256,
            "production_profile_payload_sha256": production_profile.payload_sha256,
            "postprocessor_profile_sha256": production_machine_profile.config_sha256,
        },
        "materials": _physical_sheet_material_bindings(context),
        "expected_live_controller_state": {
            "observations_embedded": False,
            "observation_timing": "IMMEDIATELY_BEFORE_EACH_PROGRAM_START",
            "metric_xyz_identity_kinematics": {
                "policy": production_machine_profile.metric_xyz_identity_kinematics_policy,
                "evidence": {
                    "id": (production_machine_profile.metric_xyz_identity_kinematics_evidence_id),
                    "version": (
                        production_machine_profile.metric_xyz_identity_kinematics_evidence_version
                    ),
                    "sha256": (
                        production_machine_profile.metric_xyz_identity_kinematics_evidence_sha256
                    ),
                },
                "required_native_linear_units": "mm",
                "required_coordinates": "XYZ",
                "required_kinematics": "IDENTITY_TRIVKINS",
                "required_joint_count": 3,
                "required_joint_axis_mapping": ["0:X", "1:Y", "2:Z"],
                "additional_controlled_axes_permitted": False,
                "linear_units_mm_verified": production_machine_profile.linear_units_mm_verified,
                "coordinates_xyz_verified": production_machine_profile.coordinates_xyz_verified,
                "identity_trivkins_verified": (
                    production_machine_profile.identity_trivkins_verified
                ),
                "exactly_three_joints_verified": (
                    production_machine_profile.exactly_three_joints_verified
                ),
                "joint_0_x_1_y_2_z_verified": (
                    production_machine_profile.joint_0_x_1_y_2_z_verified
                ),
                "no_extra_controlled_axes_verified": (
                    production_machine_profile.no_extra_controlled_axes_verified
                ),
            },
            "home_axes": {
                "policy": production_machine_profile.homing_preflight_policy,
                "evidence": {
                    "id": production_machine_profile.homing_preflight_evidence_id,
                    "version": (production_machine_profile.homing_preflight_evidence_version),
                    "sha256": (production_machine_profile.homing_preflight_evidence_sha256),
                },
                "required_axes": ["X", "Y", "Z"],
                "candidate_program_performs_homing": False,
                "operator_verification_required": True,
                "all_xyz_homed_before_auto_verified": (
                    production_machine_profile.all_xyz_homed_before_auto_verified
                ),
                "no_force_homing_disabled_verified": (
                    production_machine_profile.no_force_homing_disabled_verified
                ),
            },
            "g53_tool_change_path": {
                "policy": production_machine_profile.g53_tool_change_path,
                "clearance_evidence": {
                    "id": (production_machine_profile.g53_tool_change_path_clearance_evidence_id),
                    "version": (
                        production_machine_profile.g53_tool_change_path_clearance_evidence_version
                    ),
                    "sha256": (
                        production_machine_profile.g53_tool_change_path_clearance_evidence_sha256
                    ),
                },
                "machine_coordinates_verified": (
                    production_machine_profile.g53_machine_coordinates_verified
                ),
                "complete_path_clearance_verified": (
                    production_machine_profile.g53_tool_change_path_clearance_verified
                ),
            },
            "initial_spindle_tool": {
                "candidate_observation_embedded": False,
                "required_state": "EMPTY_OR_EXACT_PROFILE_BOUND_TOOL_ASSEMBLY",
                "profile_bound_tool_ids": [tool.tool_id for tool in context.tool_bindings],
                "must_be_covered_by_g53_clearance_evidence": True,
                "unsafe_tool_number_override_permitted": False,
            },
            "tool_change_and_tool_table": {
                "policy": production_machine_profile.m6_tool_table_policy,
                "evidence": {
                    "id": production_machine_profile.m6_tool_table_evidence_id,
                    "version": production_machine_profile.m6_tool_table_evidence_version,
                    "sha256": production_machine_profile.m6_tool_table_evidence_sha256,
                },
                "m6_tool_change_verified": production_machine_profile.m6_tool_change_verified,
                "m6_preserves_axis_position": (
                    production_machine_profile.m6_preserves_axis_position
                ),
                "m6_preserves_bound_tool_table_verified": (
                    production_machine_profile.m6_preserves_bound_tool_table_verified
                ),
                "automatic_probe_or_remap_may_mutate_bound_h_row": False,
                "required_continuity": (
                    "EXACT_BOUND_T_AND_H_XYZ_TOOL_TABLE_ROW_FROM_PREFLIGHT_THROUGH_G43"
                ),
            },
            "g52_g92_offset_reset": {
                "policy": production_machine_profile.g52_g92_offset_reset_policy,
                "evidence": {
                    "id": production_machine_profile.g52_g92_offset_reset_evidence_id,
                    "version": (production_machine_profile.g52_g92_offset_reset_evidence_version),
                    "sha256": (production_machine_profile.g52_g92_offset_reset_evidence_sha256),
                },
                "g92_1_reset_verified": (
                    production_machine_profile.g92_1_clears_g52_g92_offsets_verified
                ),
            },
            "external_axis_offsets": {
                "policy": production_machine_profile.external_axis_offset_policy,
                "evidence": {
                    "id": production_machine_profile.external_axis_offset_evidence_id,
                    "version": (production_machine_profile.external_axis_offset_evidence_version),
                    "sha256": production_machine_profile.external_axis_offset_evidence_sha256,
                },
                "required_state": "P0_XYZ_EXTERNAL_OFFSETS_DISABLED",
                "external_xyz_offsets_disabled_verified": (
                    production_machine_profile.external_xyz_offsets_disabled_verified
                ),
            },
            "wcs_table": {
                "evidence": {
                    "id": production_machine_profile.wcs_offsets_evidence_id,
                    "version": production_machine_profile.wcs_offsets_evidence_version,
                    "sha256": production_machine_profile.wcs_offsets_evidence_sha256,
                },
                "offsets_verified": production_machine_profile.wcs_offsets_verified,
                "live_values_must_equal_expected": True,
                "live_values_must_remain_equal_expected_through_m6_and_wcs_selection": True,
                "m6_preservation": {
                    "policy": production_machine_profile.m6_wcs_table_policy,
                    "evidence": {
                        "id": production_machine_profile.m6_wcs_table_evidence_id,
                        "version": production_machine_profile.m6_wcs_table_evidence_version,
                        "sha256": production_machine_profile.m6_wcs_table_evidence_sha256,
                    },
                    "m6_preserves_bound_wcs_table_verified": (
                        production_machine_profile.m6_preserves_bound_wcs_table_verified
                    ),
                    "exact_raw_g5x_xyz_r_preservation_required": True,
                    "automatic_probe_or_remap_may_mutate_bound_g5x_row": False,
                    "required_continuity": (
                        "EXACT_RAW_G5X_XYZ_AND_R_FROM_PREFLIGHT_THROUGH_POST_M6_WCS_SELECTION"
                    ),
                },
                "expected_offsets": [
                    {
                        "wcs": offset.wcs,
                        "machine_x0_um": offset.machine_x0_um,
                        "machine_y0_um": offset.machine_y0_um,
                        "machine_z0_um": offset.machine_z0_um,
                        "machine_xy_rotation_mdeg": (offset.machine_xy_rotation_mdeg),
                    }
                    for offset in production_machine_profile.wcs_offsets
                ],
            },
            "spindle_and_overrides": {
                "policy": production_machine_profile.feed_spindle_override_policy,
                "evidence": {
                    "id": production_machine_profile.feed_spindle_override_evidence_id,
                    "version": (production_machine_profile.feed_spindle_override_evidence_version),
                    "sha256": (production_machine_profile.feed_spindle_override_evidence_sha256),
                },
                "required_program_states": ["G8", "G97", "M9", "M49", "M52 P0", "M53 P1"],
                "g8_radius_mode_verified": production_machine_profile.g8_radius_mode_verified,
                "g97_rpm_mode_verified": production_machine_profile.g97_rpm_mode_verified,
                "m9_coolant_off_verified": production_machine_profile.m9_coolant_off_verified,
                "m49_feed_and_spindle_overrides_disabled_verified": (
                    production_machine_profile.m49_feed_and_spindle_overrides_disabled_verified
                ),
                "m52_p0_adaptive_feed_disabled_verified": (
                    production_machine_profile.m52_p0_adaptive_feed_disabled_verified
                ),
                "m53_p1_feed_hold_enabled_verified": (
                    production_machine_profile.m53_p1_feed_hold_enabled_verified
                ),
                "spindle_at_speed": {
                    "policy": production_machine_profile.spindle_at_speed_policy,
                    "feedback_source": production_machine_profile.spindle_feedback_source,
                    "tolerance_ppm": (production_machine_profile.spindle_at_speed_tolerance_ppm),
                    "dwell_role": SPINDLE_DWELL_ROLE,
                    "g4_is_speed_proof": False,
                    "actual_rpm_must_be_nonzero": True,
                    "live_feedback_must_be_within_tolerance_before_feed": True,
                    "evidence": {
                        "id": production_machine_profile.spindle_at_speed_evidence_id,
                        "version": (production_machine_profile.spindle_at_speed_evidence_version),
                        "sha256": (production_machine_profile.spindle_at_speed_evidence_sha256),
                    },
                    "real_feedback_verified": (
                        production_machine_profile.real_spindle_feedback_verified
                    ),
                    "motion_interlock_verified": (
                        production_machine_profile.spindle_at_speed_motion_interlock_verified
                    ),
                    "vfd_fault_motion_inhibit_verified": (
                        production_machine_profile.vfd_fault_motion_inhibit_verified
                    ),
                    "vfd_fault_spindle_stop_verified": (
                        production_machine_profile.vfd_fault_spindle_stop_verified
                    ),
                    "continuous_cutting_feed_interlock": {
                        "policy": (
                            production_machine_profile.continuous_spindle_speed_interlock_policy
                        ),
                        "evidence": {
                            "id": (
                                production_machine_profile.continuous_spindle_speed_interlock_evidence_id
                            ),
                            "version": (
                                production_machine_profile.continuous_spindle_speed_interlock_evidence_version
                            ),
                            "sha256": (
                                production_machine_profile.continuous_spindle_speed_interlock_evidence_sha256
                            ),
                        },
                        "continuous_spindle_speed_feed_inhibit_verified": (
                            production_machine_profile.continuous_spindle_speed_feed_inhibit_verified
                        ),
                    },
                },
            },
            "program_execution": {
                "policy": production_machine_profile.program_restart_policy,
                "evidence": {
                    "id": production_machine_profile.program_restart_evidence_id,
                    "version": production_machine_profile.program_restart_evidence_version,
                    "sha256": production_machine_profile.program_restart_evidence_sha256,
                },
                "allowed_entry_point": "PROGRAM_START_ONLY",
                "run_from_line_disabled_verified": (
                    production_machine_profile.run_from_line_disabled_verified
                ),
                "full_restart_after_abort_required": (
                    production_machine_profile.full_restart_after_abort_required
                ),
            },
            "shop_services": {
                "candidate_observation_embedded": False,
                "operator_must_start_required_dust_extraction": True,
                "operator_must_confirm_bound_coolant_or_air_policy": True,
            },
            "physical_barriers": {
                "candidate_observation_embedded": False,
                "guards_and_access_barriers_must_be_effective": True,
                "emergency_stop_must_be_tested_and_reachable": True,
                "machine_envelope_and_safe_work_zone_must_be_clear": True,
            },
        },
        "required_preflight_checks": [
            "VERIFY_ALL_PACKAGE_CHECKSUMS",
            "VERIFY_NATIVE_MM_XYZ_IDENTITY_TRIVKINS_JOINTS_3_JOINT_0_X_1_Y_2_Z_NO_EXTRA_AXES",
            "VERIFY_NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO_AND_G53",
            "VERIFY_CURRENT_SPINDLE_TOOL_EMPTY_OR_PROFILE_BOUND",
            "VERIFY_M6_REMAP_AND_AUTO_PROBE_PRESERVE_EXACT_BOUND_T_AND_H_TOOL_TABLE_ROW",
            "VERIFY_P0_XYZ_EXTERNAL_AXIS_OFFSETS_DISABLED",
            "VERIFY_M6_REMAP_AND_AUTO_PROBE_PRESERVE_EXACT_RAW_G5X_XYZ_AND_R_UNTIL_WCS_SELECTION",
            "VERIFY_G52_G92_ZERO_OR_CANONICAL_PREAMBLE_RESET",
            "VERIFY_EXACT_MACHINE_CONTROLLER_AND_PROFILE",
            "VERIFY_SOURCE_MATERIAL_AND_EXACT_ACTUAL_MATERIAL_LOT_EVIDENCE",
            "VERIFY_STOCK_MATERIAL_DIMENSIONS_SIDE_AND_ORIENTATION",
            "VERIFY_FIXTURE_IDENTITY_CLEARANCE_AND_KEEP_OUTS",
            "VERIFY_EXACT_WCS_XYZ_AND_ZERO_XY_ROTATION",
            "PROBE_STOCK_TOP_Z0_WITH_BOUND_METHOD",
            "VERIFY_TN_TOOL_IDENTITY_AND_EXACT_HN_NUMERIC_TOOL_TABLE_OFFSET",
            "VERIFY_G8_RADIUS_MODE_AND_M49_FEED_SPINDLE_OVERRIDES_DISABLED",
            "VERIFY_PROGRAM_START_ONLY_RUN_FROM_LINE_DISABLED_FULL_RESTART_AFTER_ABORT",
            "VERIFY_G4_IS_MINIMUM_DWELL_NOT_SPINDLE_SPEED_PROOF",
            "VERIFY_ACTUAL_NONZERO_SPINDLE_RPM_WITHIN_PROFILE_PPM_BEFORE_FEED",
            "VERIFY_CONTINUOUS_ACTUAL_RPM_INTERLOCK_INHIBITS_CUTTING_FEED_OUTSIDE_TOLERANCE",
            "VERIFY_VFD_FAULT_INHIBITS_MOTION_AND_STOPS_SPINDLE",
            "START_REQUIRED_DUST_EXTRACTION_COOLANT_OR_AIR",
            "VERIFY_PHYSICAL_BARRIERS_GUARDS_ESTOP_AND_SAFE_WORK_ZONE",
            "RUN_CONTROLLER_SIMULATION_AND_WORKSHOP_AIR_CUT",
            "OBTAIN_WORKSHOP_ACCEPTANCE_BEFORE_MACHINE_START",
        ],
        "setups": setup_rows,
        "program_sequence": program_rows,
    }


def _build_cutting_program_report(
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
    source_operations: OperationsDocument,
) -> dict[str, Any]:
    independent = verify_production_toolpaths(document, source_operations)
    if independent.report.status != CuttingProgramStatus.PASS:
        raise ArtifactError("independent source-to-removal verification did not pass")
    planned_by_id = {program.program_id: program for program in document.programs}
    setup_by_id = {setup.setup_id: setup for setup in document.execution_context.setups}
    rows: list[dict[str, Any]] = []
    for program in programs:
        planned = planned_by_id[program.program_id]
        parsed = validate_production_program(
            program.content,
            document=document,
            program=planned,
            machine_profile=production_machine_profile,
        )
        rows.append(
            {
                "execution_order": program.run_order,
                "program_id": program.program_id,
                "path": f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}",
                "sha256": program.sha256,
                "source_toolpaths_sha256": program.source_toolpaths_sha256,
                "production_machine_profile_sha256": (program.production_machine_profile_sha256),
                "planned_move_count": len(planned.moves),
                "parsed_move_count": len(parsed.moves),
                "wcs": parsed.wcs,
                "controller_tool_number": parsed.controller_tool_number,
                "length_offset_number": parsed.length_offset_number,
                "spindle_rpm": parsed.spindle_rpm,
                "spindle_spinup_ms": parsed.spindle_spinup_ms,
                "material_binding": _setup_material_binding(setup_by_id[program.setup_id]),
                "result": "STRICT_ROUND_TRIP_PASSED",
            }
        )
    return {
        "schema_version": CAM_CANDIDATE_VALIDATION_REPORT_SCHEMA_VERSION,
        "status": CAM_CANDIDATE_STATUS,
        "mode": EXECUTABLE_CAM_CANDIDATE_MODE,
        "result": "PASS",
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "toolpaths_sha256": document.fingerprint,
        "operations_sha256": sha256_hex(source_operations.to_json()),
        "production_machine_profile_sha256": production_machine_profile.config_sha256,
        "production_profile": _loaded_production_profile_identity(production_profile),
        "materials": _physical_sheet_material_bindings(document.execution_context),
        "independent_source_to_removal": independent.report.as_dict(),
        "postprocessor_round_trip": {
            "result": "PASS",
            "parser_version": PRODUCTION_GCODE_PARSER_VERSION,
            "safety_validator_version": PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
            "scope": "STRICT_GCODE_TO_IMMUTABLE_TOOLPATH_ROUND_TRIP",
            "programs": rows,
        },
    }


def _build_manifest(
    base: _BaseDesignReviewBinding,
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
    artifacts: tuple[ArtifactFile, ...],
    producer_build: ProducerBuildIdentity,
) -> bytes:
    return _build_manifest_from_verified_provenance(
        base,
        document,
        programs,
        production_profile,
        production_machine_profile,
        artifacts,
        build_cam_software_provenance(
            producer_build,
            allow_test_only=production_profile.profile_class == TEST_ONLY_PROFILE,
        ),
    )


def _rebuild_manifest_for_verification(
    base: _BaseDesignReviewBinding,
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
    artifacts: tuple[ArtifactFile, ...],
    verified_software_provenance: Mapping[str, Any],
) -> bytes:
    """Rebuild a package with its already verified frozen implementation identity."""

    return _build_manifest_from_verified_provenance(
        base,
        document,
        programs,
        production_profile,
        production_machine_profile,
        artifacts,
        verified_software_provenance,
    )


def _build_manifest_from_verified_provenance(
    base: _BaseDesignReviewBinding,
    document: ProductionToolpathDocument,
    programs: tuple[ProductionMachineProgram, ...],
    production_profile: LoadedProductionMachineProfile,
    production_machine_profile: LinuxCNCProductionMachineProfile,
    artifacts: tuple[ArtifactFile, ...],
    verified_software_provenance: Mapping[str, Any],
) -> bytes:
    _require_unique_paths(artifacts)
    implementation_identity = parse_supported_cam_implementation_identity(
        verified_software_provenance.get("implementations")
    )
    entries = [_artifact_entry(artifact) for artifact in artifacts]
    context = document.execution_context
    machine_profile = {
        "id": context.machine_profile_id,
        "version": context.machine_profile_version,
        "fingerprint": document.machine_profile_fingerprint,
    }
    controller = {"id": context.controller_id, "version": context.controller_version}
    postprocessor = {
        "id": implementation_identity.postprocessor_id,
        "version": implementation_identity.postprocessor_version,
        "identity": (
            f"{implementation_identity.postprocessor_id}"
            f"@{implementation_identity.postprocessor_version}"
        ),
    }
    source_machine_profile = _trusted_source_machine_profile(base)
    toolpath_bytes = document.to_json()
    toolpath_identity = {
        "path": CAM_CANDIDATE_TOOLPATH_PATH,
        "schema_version": document.schema_version,
        "engine_version": document.engine_version,
        "sha256": sha256_hex(toolpath_bytes),
        "fingerprint": document.fingerprint,
        "size_bytes": len(toolpath_bytes),
        "operations_sha256": document.operations_sha256,
    }
    setup_instructions_bytes = canonical_json_bytes(
        _build_setup_instructions(
            document,
            programs,
            production_profile,
            production_machine_profile,
        )
    )
    setup_instructions_identity = {
        "path": CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
        "schema_version": CAM_CANDIDATE_SETUP_INSTRUCTIONS_SCHEMA_VERSION,
        "sha256": sha256_hex(setup_instructions_bytes),
        "size_bytes": len(setup_instructions_bytes),
    }
    candidate_context = {
        "schema_version": implementation_identity.candidate_manifest_schema_version,
        "builder_version": implementation_identity.candidate_package_builder_version,
        "status": CAM_CANDIDATE_STATUS,
        "mode": EXECUTABLE_CAM_CANDIDATE_MODE,
        "release_scope": "cam_candidate",
        "machine_use": "executable_cam_candidate",
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "base_design_review": base.as_dict(),
        "source_machine_profile": _source_machine_profile_identity(source_machine_profile),
        "machine_profile": machine_profile,
        "materials": _physical_sheet_material_bindings(context),
        "production_machine_profile": _production_machine_profile_identity(
            production_machine_profile
        ),
        "production_profile": _loaded_production_profile_identity(production_profile),
        "controller": controller,
        "postprocessor": postprocessor,
        "toolpaths": toolpath_identity,
        "setup_instructions": setup_instructions_identity,
        "software_provenance": dict(verified_software_provenance),
        "artifacts": entries,
    }
    if len(programs) != len(document.programs):
        raise ArtifactError("manifest cannot omit a production program")
    manifest = {
        **candidate_context,
        "candidate_context_hash": sha256_hex(canonical_json_bytes(candidate_context)),
        "checksum_scope": _CHECKSUM_SCOPE,
    }
    return canonical_json_bytes(manifest)


def _artifact_entry(artifact: ArtifactFile) -> dict[str, Any]:
    return {
        "path": artifact.path,
        "media_type": artifact.media_type,
        "role": artifact.role,
        "size_bytes": len(artifact.data),
        "sha256": sha256_hex(artifact.data),
    }


def _build_deterministic_zip(manifest: bytes, artifacts: tuple[ArtifactFile, ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path, data in (
            ("manifest.json", manifest),
            *((artifact.path, artifact.data) for artifact in artifacts),
        ):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    output = buffer.getvalue()
    if len(output) > MAX_CAM_CANDIDATE_PACKAGE_BYTES:
        raise ArtifactError("CAM candidate ZIP exceeds its canonical size limit")
    return output


def _validate_zip_envelope(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> None:
    if not infos or len(infos) > MAX_CAM_CANDIDATE_FILES:
        raise ArtifactError("CAM candidate ZIP has an invalid file count")
    if archive.comment:
        raise ArtifactError("CAM candidate ZIP comments are not canonical")
    total = 0
    names: list[str] = []
    for info in infos:
        _validate_candidate_path(info.filename)
        names.append(info.filename)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or info.create_system != 3
            or info.external_attr != 0o100644 << 16
            or info.date_time != (1980, 1, 1, 0, 0, 0)
            or info.compress_type != zipfile.ZIP_DEFLATED
            or info.extra
            or info.comment
        ):
            raise ArtifactError("CAM candidate ZIP contains a non-canonical entry")
        limit = (
            MAX_ARTIFACT_BYTES
            if info.filename == CAM_CANDIDATE_TOOLPATH_PATH
            else (
                MAX_CORE_DOCUMENT_BYTES if info.filename.endswith(".json") else MAX_ARTIFACT_BYTES
            )
        )
        if info.file_size <= 0 or info.file_size > limit:
            raise ArtifactError(f"CAM candidate ZIP entry has an invalid size: {info.filename}")
        total += info.file_size
        if total > MAX_CAM_CANDIDATE_UNCOMPRESSED_BYTES:
            raise ArtifactError("CAM candidate ZIP exceeds its uncompressed size limit")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / info.compress_size > MAX_CAM_CANDIDATE_COMPRESSION_RATIO
        ):
            raise ArtifactError(f"unsafe CAM candidate compression ratio: {info.filename}")
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ArtifactError("CAM candidate ZIP contains duplicate path aliases")


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> None:
    if frozenset(manifest) != _MANIFEST_KEYS:
        raise ArtifactError("CAM candidate manifest has an unexpected structure")
    software_provenance = manifest.get("software_provenance")
    try:
        if not isinstance(software_provenance, Mapping):
            raise CAMSoftwareProvenanceError("CAM software provenance is not an object")
        implementation_identity = parse_supported_cam_implementation_identity(
            software_provenance.get("implementations")
        )
    except CAMSoftwareProvenanceError as exc:
        raise ArtifactError("CAM candidate manifest implementation is unsupported") from exc
    required = {
        "schema_version": implementation_identity.candidate_manifest_schema_version,
        "builder_version": implementation_identity.candidate_package_builder_version,
        "status": CAM_CANDIDATE_STATUS,
        "mode": EXECUTABLE_CAM_CANDIDATE_MODE,
        "release_scope": "cam_candidate",
        "machine_use": "executable_cam_candidate",
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "checksum_scope": _CHECKSUM_SCOPE,
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise ArtifactError("CAM candidate manifest contains unsafe or unsupported claims")
    for key in (
        "base_design_review",
        "source_machine_profile",
        "machine_profile",
        "production_machine_profile",
        "controller",
        "postprocessor",
        "toolpaths",
        "setup_instructions",
        "software_provenance",
    ):
        if not isinstance(manifest.get(key), dict):
            raise ArtifactError(f"CAM candidate manifest {key} must be an object")
    hash_fields = _MANIFEST_KEYS - {"candidate_context_hash", "checksum_scope"}
    context = {key: manifest[key] for key in hash_fields}
    if manifest.get("candidate_context_hash") != sha256_hex(canonical_json_bytes(context)):
        raise ArtifactError("CAM candidate manifest context hash mismatch")


def _validate_artifact_entries(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ArtifactError("CAM candidate manifest artifacts must be a non-empty array")
    entries: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or frozenset(raw) != _ARTIFACT_KEYS:
            raise ArtifactError("CAM candidate manifest artifact entry is invalid")
        entry = {str(key): item for key, item in raw.items()}
        path = entry.get("path")
        _validate_candidate_path(path)
        if (
            not isinstance(entry.get("media_type"), str)
            or not entry["media_type"]
            or not isinstance(entry.get("role"), str)
            or not entry["role"]
            or type(entry.get("size_bytes")) is not int
            or entry["size_bytes"] <= 0
            or _HASH_RE.fullmatch(str(entry.get("sha256"))) is None
        ):
            raise ArtifactError("CAM candidate manifest artifact metadata is invalid")
        _validate_artifact_identity(entry)
        entries.append(entry)
    paths = [cast(str, entry["path"]) for entry in entries]
    if paths != sorted(paths):
        raise ArtifactError("CAM candidate manifest artifact paths are not canonical")
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ArtifactError("CAM candidate manifest contains duplicate path aliases")
    required = {
        CAM_CANDIDATE_TOOLPATH_PATH,
        CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        CAM_CANDIDATE_PROGRAM_INDEX_PATH,
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
        CAM_CANDIDATE_REPORT_PATH,
        CAM_CANDIDATE_BACKPLOT_PATH,
    }
    if not required <= set(paths) or not any(_PROGRAM_PATH_RE.fullmatch(path) for path in paths):
        raise ArtifactError("CAM candidate manifest inventory is incomplete")
    return tuple(entries)


def _validate_artifact_identity(entry: Mapping[str, Any]) -> None:
    path = str(entry["path"])
    expected = {
        CAM_CANDIDATE_TOOLPATH_PATH: ("application/json", "PRODUCTION_TOOLPATH_DOCUMENT"),
        CAM_CANDIDATE_PROGRAM_INDEX_PATH: ("application/json", "PRODUCTION_PROGRAM_INDEX"),
        CAM_CANDIDATE_REPORT_PATH: (
            "application/json",
            "CUTTING_PROGRAM_VALIDATION_REPORT",
        ),
        CAM_CANDIDATE_BACKPLOT_PATH: ("image/svg+xml", "CUTTING_BACKPLOT"),
        CAM_CANDIDATE_SOURCE_OPERATIONS_PATH: (
            "application/json",
            "SOURCE_MACHINE_NEUTRAL_OPERATIONS",
        ),
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH: (
            "application/json",
            "SOURCE_VALIDATION_MACHINE_PROFILE",
        ),
        CAM_CANDIDATE_MACHINE_PROFILE_PATH: (
            "application/json",
            "PRODUCTION_MACHINE_PROFILE_DOCUMENT",
        ),
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH: (
            "application/json",
            "LINUXCNC_POSTPROCESSOR_MACHINE_PROFILE",
        ),
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH: (
            "application/json",
            "PRODUCTION_SETUP_INSTRUCTIONS",
        ),
    }.get(path)
    if expected is None and _PROGRAM_PATH_RE.fullmatch(path):
        expected = ("text/x-gcode", "EXECUTABLE_CAM_CANDIDATE_PROGRAM")
    if expected is None or (entry["media_type"], entry["role"]) != expected:
        raise ArtifactError(f"CAM candidate artifact identity is unsupported: {path}")


def _programs_from_index(
    payload: bytes,
    *,
    document: ProductionToolpathDocument,
    production_machine_profile: LinuxCNCProductionMachineProfile,
    data_by_path: Mapping[str, bytes],
) -> tuple[ProductionMachineProgram, ...]:
    index = _canonical_json_object(payload, label="production program index")
    programs_value = index.get("programs")
    if not isinstance(programs_value, list):
        raise ArtifactError("production program index has no programs array")
    planned_by_id = {program.program_id: program for program in document.programs}
    output: list[ProductionMachineProgram] = []
    for raw in programs_value:
        if not isinstance(raw, dict):
            raise ArtifactError("production program index entry must be an object")
        try:
            path = _required_string(raw["path"], "program path")
            planned = planned_by_id[_required_string(raw["program_id"], "program ID")]
            controller = _exact_object(raw["controller"], {"id", "version"}, "controller")
            postprocessor = _exact_object(raw["postprocessor"], {"id", "version"}, "postprocessor")
            binding = _exact_object(
                raw["toolpath_binding"],
                {"document_path", "document_sha256", "program_id", "run_order"},
                "toolpath binding",
            )
            filename = path.removeprefix(CAM_CANDIDATE_PROGRAM_ROOT)
            program = ProductionMachineProgram(
                filename=filename,
                program_id=planned.program_id,
                run_order=_required_int(raw["execution_order"], "execution order"),
                setup_id=planned.setup_id,
                tool_id=planned.tool_id,
                controller=_required_string(controller["id"], "controller ID"),
                controller_version=_required_string(controller["version"], "controller version"),
                postprocessor_id=_required_string(postprocessor["id"], "postprocessor ID"),
                postprocessor_version=_required_string(
                    postprocessor["version"], "postprocessor version"
                ),
                source_toolpaths_sha256=_required_hash(
                    binding["document_sha256"], "source toolpaths hash"
                ),
                production_machine_profile_sha256=_required_hash(
                    raw["production_machine_profile_sha256"],
                    "production machine profile hash",
                ),
                content=data_by_path[path],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError("production program index cannot reconstruct a program") from exc
        output.append(program)
    return _validate_programs(document, output, production_machine_profile)


def _parse_operations_document(payload: bytes) -> OperationsDocument:
    raw = _canonical_json_object(payload, label="source operations document")
    expected = {
        "schema_version",
        "design_hash",
        "machine_profile_id",
        "machine_profile_version",
        "setups",
        "operations",
        "mode",
        "tool_catalog_version",
        "tool_catalog_fingerprint",
        "tools",
    }
    if set(raw) != expected:
        raise ArtifactError("source operations document has an unexpected structure")
    try:
        return OperationsDocument(
            schema_version=_required_string(raw["schema_version"], "operations schema"),
            design_hash=_required_hash(raw["design_hash"], "operations design hash"),
            machine_profile_id=_required_string(
                raw["machine_profile_id"],
                "operations machine profile ID",
            ),
            machine_profile_version=_required_string(
                raw["machine_profile_version"],
                "operations machine profile version",
            ),
            setups=tuple(
                _parse_source_setup(item) for item in _required_list(raw["setups"], "source setups")
            ),
            operations=tuple(
                _parse_source_operation(item)
                for item in _required_list(raw["operations"], "source operations")
            ),
            mode=_required_string(raw["mode"], "operations mode"),
            tool_catalog_version=_required_string(
                raw["tool_catalog_version"],
                "source tool catalog version",
            ),
            tool_catalog_fingerprint=_required_hash(
                raw["tool_catalog_fingerprint"],
                "source tool catalog fingerprint",
            ),
            tools=tuple(
                _parse_source_tool(item) for item in _required_list(raw["tools"], "source tools")
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError("source operations document is invalid") from exc


def _parse_source_setup(value: object) -> Setup:
    raw = _exact_object(
        value,
        {
            "setup_id",
            "stock_id",
            "material_id",
            "material_version",
            "sheet_index",
            "side",
            "wcs",
            "origin",
            "stock_width_um",
            "stock_height_um",
            "stock_thickness_um",
            "safe_z_um",
            "reference_surface",
            "orientation",
            "fixture",
            "keep_out_zones",
            "tool_ids",
            "probe_method",
            "operator_steps",
        },
        "source setup",
    )
    return Setup(
        setup_id=_required_string(raw["setup_id"], "source setup ID"),
        stock_id=_required_string(raw["stock_id"], "source stock ID"),
        material_id=_required_string(raw["material_id"], "source material ID"),
        material_version=_required_string(raw["material_version"], "source material version"),
        sheet_index=_required_int(raw["sheet_index"], "source sheet index"),
        side=Side(_required_string(raw["side"], "source side")),
        wcs=_required_string(raw["wcs"], "source WCS"),
        origin=_parse_point(raw["origin"]),
        stock_width_um=_required_int(raw["stock_width_um"], "source stock width"),
        stock_height_um=_required_int(raw["stock_height_um"], "source stock height"),
        stock_thickness_um=_required_int(
            raw["stock_thickness_um"],
            "source stock thickness",
        ),
        safe_z_um=_required_int(raw["safe_z_um"], "source safe Z"),
        reference_surface=_required_string(
            raw["reference_surface"],
            "source reference surface",
        ),
        orientation=_required_string(raw["orientation"], "source orientation"),
        fixture=_required_string(raw["fixture"], "source fixture"),
        keep_out_zones=tuple(
            _parse_rect(item) for item in _required_list(raw["keep_out_zones"], "source keep-outs")
        ),
        tool_ids=_string_tuple(raw["tool_ids"], "source tool IDs"),
        probe_method=_required_string(raw["probe_method"], "source probe method"),
        operator_steps=_string_tuple(raw["operator_steps"], "source operator steps"),
    )


def _parse_source_tool(value: object) -> ToolSpec:
    raw = _exact_object(
        value,
        {
            "tool_id",
            "name",
            "diameter_um",
            "cutting_length_um",
            "supported_operations",
            "spindle_rpm",
            "feed_um_min",
            "plunge_um_min",
            "measured_diameter_um",
            "runout_um",
            "version",
        },
        "source tool",
    )
    return ToolSpec(
        tool_id=_required_string(raw["tool_id"], "source tool ID"),
        name=_required_string(raw["name"], "source tool name"),
        diameter_um=_required_int(raw["diameter_um"], "source tool diameter"),
        cutting_length_um=_required_int(
            raw["cutting_length_um"],
            "source tool cutting length",
        ),
        supported_operations=tuple(
            OperationKind(_required_string(item, "source supported operation"))
            for item in _required_list(
                raw["supported_operations"],
                "source supported operations",
            )
        ),
        spindle_rpm=_required_int(raw["spindle_rpm"], "source spindle RPM"),
        feed_um_min=_required_int(raw["feed_um_min"], "source feed"),
        plunge_um_min=_required_int(raw["plunge_um_min"], "source plunge"),
        measured_diameter_um=_optional_int(raw["measured_diameter_um"]),
        runout_um=_required_int(raw["runout_um"], "source tool runout"),
        version=_required_string(raw["version"], "source tool version"),
    )


def _parse_source_operation(value: object) -> CAMOperation:
    raw = _exact_object(
        value,
        {
            "operation_id",
            "setup_id",
            "part_id",
            "instance_id",
            "feature_id",
            "kind",
            "side",
            "tool_id",
            "x_um",
            "y_um",
            "depth_um",
            "diameter_um",
            "width_um",
            "length_um",
            "cutter_envelope_x_um",
            "cutter_envelope_y_um",
            "cutter_envelope_width_um",
            "cutter_envelope_length_um",
            "stepdown_um",
            "stepover_ppm",
            "through",
            "source_rotation_90",
            "compensation",
            "holding_strategy",
            "corner_strategy",
            "corner_relief_radius_um",
            "open_end_reliefs",
            "tolerance_um",
            "fit_clearance_um",
        },
        "source operation",
    )
    return CAMOperation(
        operation_id=_required_string(raw["operation_id"], "source operation ID"),
        setup_id=_required_string(raw["setup_id"], "source operation setup ID"),
        part_id=_required_string(raw["part_id"], "source operation part ID"),
        instance_id=_required_string(raw["instance_id"], "source operation instance ID"),
        feature_id=_required_string(raw["feature_id"], "source operation feature ID"),
        kind=OperationKind(_required_string(raw["kind"], "source operation kind")),
        side=Side(_required_string(raw["side"], "source operation side")),
        tool_id=_required_string(raw["tool_id"], "source operation tool ID"),
        x_um=_required_int(raw["x_um"], "source operation X"),
        y_um=_required_int(raw["y_um"], "source operation Y"),
        depth_um=_required_int(raw["depth_um"], "source operation depth"),
        diameter_um=_optional_int(raw["diameter_um"]),
        width_um=_optional_int(raw["width_um"]),
        length_um=_optional_int(raw["length_um"]),
        cutter_envelope_x_um=_optional_int(raw["cutter_envelope_x_um"]),
        cutter_envelope_y_um=_optional_int(raw["cutter_envelope_y_um"]),
        cutter_envelope_width_um=_optional_int(raw["cutter_envelope_width_um"]),
        cutter_envelope_length_um=_optional_int(raw["cutter_envelope_length_um"]),
        stepdown_um=_optional_int(raw["stepdown_um"]),
        stepover_ppm=_optional_int(raw["stepover_ppm"]),
        through=_required_bool(raw["through"], "source operation through flag"),
        source_rotation_90=_required_bool(
            raw["source_rotation_90"],
            "source operation rotation flag",
        ),
        compensation=_optional_string(raw["compensation"], "source compensation"),
        holding_strategy=_optional_string(raw["holding_strategy"], "source holding strategy"),
        corner_strategy=_optional_string(raw["corner_strategy"], "source corner strategy"),
        corner_relief_radius_um=_optional_int(raw["corner_relief_radius_um"]),
        open_end_reliefs=_string_tuple(raw["open_end_reliefs"], "source open-end reliefs"),
        tolerance_um=_required_int(raw["tolerance_um"], "source tolerance"),
        fit_clearance_um=_required_int(raw["fit_clearance_um"], "source fit clearance"),
    )


def _parse_toolpath_document(payload: bytes) -> ProductionToolpathDocument:
    value = _exact_object(
        _canonical_json_object(payload, label="production toolpath document"),
        {
            "schema_version",
            "engine_version",
            "mode",
            "physical_cutting_authorized",
            "workshop_acceptance_required",
            "design_hash",
            "operations_sha256",
            "execution_context",
            "machine_profile_fingerprint",
            "tool_catalog_fingerprint",
            "recipe_catalog_fingerprint",
            "programs",
        },
        "production toolpath document",
    )
    try:
        context = _parse_execution_context(value["execution_context"])
        programs_raw = _required_list(value["programs"], "toolpath programs")
        programs = tuple(_parse_production_program(item) for item in programs_raw)
        return ProductionToolpathDocument(
            design_hash=_required_hash(value["design_hash"], "toolpath design hash"),
            operations_sha256=_required_hash(
                value["operations_sha256"], "toolpath operations hash"
            ),
            execution_context=context,
            machine_profile_fingerprint=_required_hash(
                value["machine_profile_fingerprint"], "machine profile fingerprint"
            ),
            tool_catalog_fingerprint=_required_hash(
                value["tool_catalog_fingerprint"], "tool catalog fingerprint"
            ),
            recipe_catalog_fingerprint=_required_hash(
                value["recipe_catalog_fingerprint"], "recipe catalog fingerprint"
            ),
            programs=programs,
            schema_version=_required_string(value["schema_version"], "toolpath schema"),
            engine_version=_required_string(value["engine_version"], "toolpath engine"),
            mode=_required_string(value["mode"], "toolpath mode"),
            physical_cutting_authorized=_required_bool(
                value["physical_cutting_authorized"], "physical authorization"
            ),
            workshop_acceptance_required=_required_bool(
                value["workshop_acceptance_required"], "workshop acceptance"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError("production toolpath document has an invalid contract") from exc


def _parse_production_machine_profile(payload: bytes) -> LinuxCNCProductionMachineProfile:
    _canonical_json_object(payload, label="LinuxCNC production machine profile")
    try:
        return LinuxCNCProductionMachineProfile.from_json(payload)
    except (TypeError, ValueError) as exc:
        raise ArtifactError("LinuxCNC production machine profile is invalid") from exc


def _parse_execution_context(value: object) -> ProductionExecutionContext:
    raw = _exact_object(
        value,
        {
            "source_machine_profile_id",
            "source_machine_profile_version",
            "source_machine_profile_fingerprint",
            "machine_profile_id",
            "machine_profile_version",
            "controller_id",
            "controller_version",
            "machine_x_min_um",
            "machine_x_max_um",
            "machine_y_min_um",
            "machine_y_max_um",
            "machine_z_min_um",
            "machine_z_max_um",
            "work_width_um",
            "work_height_um",
            "work_z_um",
            "min_spindle_rpm",
            "max_spindle_rpm",
            "max_feed_um_min",
            "max_plunge_um_min",
            "tool_catalog_version",
            "recipe_catalog_version",
            "setups",
            "tool_bindings",
            "recipes",
        },
        "execution context",
    )
    return ProductionExecutionContext(
        source_machine_profile_id=_required_string(
            raw["source_machine_profile_id"],
            "source machine profile ID",
        ),
        source_machine_profile_version=_required_string(
            raw["source_machine_profile_version"],
            "source machine profile version",
        ),
        source_machine_profile_fingerprint=_required_hash(
            raw["source_machine_profile_fingerprint"],
            "source machine profile fingerprint",
        ),
        machine_profile_id=_required_string(raw["machine_profile_id"], "machine profile ID"),
        machine_profile_version=_required_string(
            raw["machine_profile_version"], "machine profile version"
        ),
        controller_id=_required_string(raw["controller_id"], "controller ID"),
        controller_version=_required_string(raw["controller_version"], "controller version"),
        machine_x_min_um=_required_int(raw["machine_x_min_um"], "machine X minimum"),
        machine_x_max_um=_required_int(raw["machine_x_max_um"], "machine X maximum"),
        machine_y_min_um=_required_int(raw["machine_y_min_um"], "machine Y minimum"),
        machine_y_max_um=_required_int(raw["machine_y_max_um"], "machine Y maximum"),
        machine_z_min_um=_required_int(raw["machine_z_min_um"], "machine Z minimum"),
        machine_z_max_um=_required_int(raw["machine_z_max_um"], "machine Z maximum"),
        work_width_um=_required_int(raw["work_width_um"], "work width"),
        work_height_um=_required_int(raw["work_height_um"], "work height"),
        work_z_um=_required_int(raw["work_z_um"], "work Z"),
        min_spindle_rpm=_required_int(raw["min_spindle_rpm"], "min spindle RPM"),
        max_spindle_rpm=_required_int(raw["max_spindle_rpm"], "max spindle RPM"),
        max_feed_um_min=_required_int(raw["max_feed_um_min"], "max feed"),
        max_plunge_um_min=_required_int(raw["max_plunge_um_min"], "max plunge"),
        tool_catalog_version=_required_string(raw["tool_catalog_version"], "tool catalog version"),
        recipe_catalog_version=_required_string(
            raw["recipe_catalog_version"], "recipe catalog version"
        ),
        setups=tuple(_parse_bound_setup(item) for item in _required_list(raw["setups"], "setups")),
        tool_bindings=tuple(
            _parse_tool_binding(item)
            for item in _required_list(raw["tool_bindings"], "tool bindings")
        ),
        recipes=tuple(_parse_recipe(item) for item in _required_list(raw["recipes"], "recipes")),
    )


def _parse_bound_setup(value: object) -> BoundSetup:
    fields = {
        "setup_id",
        "stock_id",
        "source_material_id",
        "source_material_version",
        "material_id",
        "material_version",
        "material_evidence_id",
        "material_evidence_version",
        "material_evidence_sha256",
        "sheet_index",
        "side",
        "source_setup_sha256",
        "source_to_wcs_xy_transform",
        "wcs",
        "machine_wcs_origin",
        "machine_wcs_z0_um",
        "machine_wcs_xy_rotation_mdeg",
        "stock_width_um",
        "stock_height_um",
        "stock_thickness_um",
        "safe_z_um",
        "reference_surface",
        "orientation",
        "fixture_id",
        "fixture_version",
        "fixture_sha256",
        "fixture_clearance_z_um",
        "minimum_rapid_clearance_um",
        "keep_out_policy",
        "probe_method",
        "keep_out_zones",
        "spoilboard_id",
        "spoilboard_version",
        "spoilboard_sha256",
        "through_cut_allowance_um",
        "raw_allowance_um",
    }
    raw = _exact_object(value, fields, "bound setup")
    return BoundSetup(
        setup_id=_required_string(raw["setup_id"], "setup ID"),
        stock_id=_required_string(raw["stock_id"], "stock ID"),
        source_material_id=_required_string(raw["source_material_id"], "source material ID"),
        source_material_version=_required_string(
            raw["source_material_version"], "source material version"
        ),
        material_id=_required_string(raw["material_id"], "material ID"),
        material_version=_required_string(raw["material_version"], "material version"),
        material_evidence_id=_required_string(raw["material_evidence_id"], "material evidence ID"),
        material_evidence_version=_required_string(
            raw["material_evidence_version"], "material evidence version"
        ),
        material_evidence_sha256=_required_hash(
            raw["material_evidence_sha256"], "material evidence hash"
        ),
        sheet_index=_required_int(raw["sheet_index"], "sheet index"),
        side=Side(_required_string(raw["side"], "setup side")),
        source_setup_sha256=_required_hash(raw["source_setup_sha256"], "source setup hash"),
        source_to_wcs_xy_transform=_required_string(
            raw["source_to_wcs_xy_transform"],
            "source-to-WCS transform",
        ),
        wcs=_required_string(raw["wcs"], "setup WCS"),
        machine_wcs_origin=_parse_point(raw["machine_wcs_origin"]),
        machine_wcs_z0_um=_required_int(raw["machine_wcs_z0_um"], "machine WCS Z0"),
        machine_wcs_xy_rotation_mdeg=_required_int(
            raw["machine_wcs_xy_rotation_mdeg"],
            "machine WCS XY rotation",
        ),
        stock_width_um=_required_int(raw["stock_width_um"], "stock width"),
        stock_height_um=_required_int(raw["stock_height_um"], "stock height"),
        stock_thickness_um=_required_int(raw["stock_thickness_um"], "stock thickness"),
        safe_z_um=_required_int(raw["safe_z_um"], "safe Z"),
        reference_surface=_required_string(raw["reference_surface"], "reference surface"),
        orientation=_required_string(raw["orientation"], "orientation"),
        fixture_id=_required_string(raw["fixture_id"], "fixture ID"),
        fixture_version=_required_string(raw["fixture_version"], "fixture version"),
        fixture_sha256=_required_hash(raw["fixture_sha256"], "fixture hash"),
        fixture_clearance_z_um=_required_int(
            raw["fixture_clearance_z_um"],
            "fixture clearance Z",
        ),
        minimum_rapid_clearance_um=_required_int(
            raw["minimum_rapid_clearance_um"],
            "minimum rapid clearance",
        ),
        keep_out_policy=_required_string(raw["keep_out_policy"], "keep-out policy"),
        probe_method=_required_string(raw["probe_method"], "probe method"),
        keep_out_zones=tuple(
            _parse_rect(item) for item in _required_list(raw["keep_out_zones"], "keep-outs")
        ),
        spoilboard_id=_optional_string(raw["spoilboard_id"], "spoilboard ID"),
        spoilboard_version=_optional_string(raw["spoilboard_version"], "spoilboard version"),
        spoilboard_sha256=(
            None
            if raw["spoilboard_sha256"] is None
            else _required_hash(raw["spoilboard_sha256"], "spoilboard hash")
        ),
        through_cut_allowance_um=_required_int(
            raw["through_cut_allowance_um"], "through-cut allowance"
        ),
        raw_allowance_um=_required_int(raw["raw_allowance_um"], "raw allowance"),
    )


def _parse_tool_binding(value: object) -> ProductionToolBinding:
    raw = _exact_object(
        value,
        {
            "tool_id",
            "tool_version",
            "source_tool_id",
            "source_tool_version",
            "source_tool_sha256",
            "controller_tool_number",
            "length_offset_number",
            "expected_length_offset_x_um",
            "expected_length_offset_y_um",
            "expected_length_offset_z_um",
            "tool_table_evidence_id",
            "tool_table_evidence_version",
            "tool_table_evidence_sha256",
            "effective_diameter_um",
            "drill_point_length_um",
            "cutting_length_um",
            "measured_stickout_um",
            "minimum_holder_clearance_um",
            "assembly_collision_radius_um",
            "geometry",
            "center_cutting",
            "spindle_direction",
        },
        "tool binding",
    )
    return ProductionToolBinding(
        tool_id=_required_string(raw["tool_id"], "tool ID"),
        tool_version=_required_string(raw["tool_version"], "tool version"),
        source_tool_id=_required_string(raw["source_tool_id"], "source tool ID"),
        source_tool_version=_required_string(raw["source_tool_version"], "source tool version"),
        source_tool_sha256=_required_hash(raw["source_tool_sha256"], "source tool hash"),
        controller_tool_number=_required_int(
            raw["controller_tool_number"], "controller tool number"
        ),
        length_offset_number=_required_int(raw["length_offset_number"], "length offset"),
        expected_length_offset_x_um=_required_int(
            raw["expected_length_offset_x_um"],
            "expected tool-length X offset",
        ),
        expected_length_offset_y_um=_required_int(
            raw["expected_length_offset_y_um"],
            "expected tool-length Y offset",
        ),
        expected_length_offset_z_um=_required_int(
            raw["expected_length_offset_z_um"],
            "expected tool-length Z offset",
        ),
        tool_table_evidence_id=_required_string(
            raw["tool_table_evidence_id"],
            "tool-table evidence ID",
        ),
        tool_table_evidence_version=_required_string(
            raw["tool_table_evidence_version"],
            "tool-table evidence version",
        ),
        tool_table_evidence_sha256=_required_hash(
            raw["tool_table_evidence_sha256"],
            "tool-table evidence hash",
        ),
        effective_diameter_um=_required_int(raw["effective_diameter_um"], "tool diameter"),
        drill_point_length_um=_required_int(raw["drill_point_length_um"], "drill point length"),
        cutting_length_um=_required_int(raw["cutting_length_um"], "cutting length"),
        measured_stickout_um=_required_int(raw["measured_stickout_um"], "measured stickout"),
        minimum_holder_clearance_um=_required_int(
            raw["minimum_holder_clearance_um"],
            "minimum holder clearance",
        ),
        assembly_collision_radius_um=_required_int(
            raw["assembly_collision_radius_um"],
            "assembly collision radius",
        ),
        geometry=ProductionToolGeometry(_required_string(raw["geometry"], "tool geometry")),
        center_cutting=_required_bool(raw["center_cutting"], "center-cutting flag"),
        spindle_direction=_required_string(raw["spindle_direction"], "spindle direction"),
    )


def _parse_recipe(value: object) -> CuttingRecipe:
    fields = {
        "recipe_id",
        "version",
        "machine_profile_id",
        "machine_profile_version",
        "material_id",
        "material_version",
        "tool_id",
        "tool_version",
        "operation_kind",
        "spindle_rpm",
        "feed_um_min",
        "plunge_um_min",
        "stepdown_um",
        "stepover_ppm",
        "peck_depth_um",
        "approach_clearance_um",
        "through_overtravel_um",
        "tab_width_um",
        "tab_height_um",
        "process_accuracy_um",
        "accepted_tolerance_um",
        "entry_strategy",
        "diameter_tolerance_um",
        "countersink_top_diameter_um",
        "countersink_included_angle_mdeg",
    }
    raw = _exact_object(value, fields, "cutting recipe")
    return CuttingRecipe(
        recipe_id=_required_string(raw["recipe_id"], "recipe ID"),
        version=_required_string(raw["version"], "recipe version"),
        machine_profile_id=_required_string(raw["machine_profile_id"], "recipe machine ID"),
        machine_profile_version=_required_string(
            raw["machine_profile_version"], "recipe machine version"
        ),
        material_id=_required_string(raw["material_id"], "recipe material ID"),
        material_version=_required_string(raw["material_version"], "recipe material version"),
        tool_id=_required_string(raw["tool_id"], "recipe tool ID"),
        tool_version=_required_string(raw["tool_version"], "recipe tool version"),
        operation_kind=OperationKind(_required_string(raw["operation_kind"], "operation kind")),
        spindle_rpm=_required_int(raw["spindle_rpm"], "recipe spindle RPM"),
        feed_um_min=_required_int(raw["feed_um_min"], "recipe feed"),
        plunge_um_min=_required_int(raw["plunge_um_min"], "recipe plunge"),
        stepdown_um=_required_int(raw["stepdown_um"], "recipe stepdown"),
        stepover_ppm=_required_int(raw["stepover_ppm"], "recipe stepover"),
        peck_depth_um=_required_int(raw["peck_depth_um"], "recipe peck depth"),
        approach_clearance_um=_required_int(
            raw["approach_clearance_um"], "recipe approach clearance"
        ),
        through_overtravel_um=_required_int(
            raw["through_overtravel_um"], "recipe through overtravel"
        ),
        tab_width_um=_required_int(raw["tab_width_um"], "recipe tab width"),
        tab_height_um=_required_int(raw["tab_height_um"], "recipe tab height"),
        process_accuracy_um=_required_int(
            raw["process_accuracy_um"],
            "recipe process accuracy",
        ),
        accepted_tolerance_um=_required_int(
            raw["accepted_tolerance_um"],
            "recipe accepted tolerance",
        ),
        entry_strategy=_required_string(raw["entry_strategy"], "recipe entry strategy"),
        diameter_tolerance_um=_required_int(
            raw["diameter_tolerance_um"],
            "recipe diameter tolerance",
        ),
        countersink_top_diameter_um=_optional_int(raw["countersink_top_diameter_um"]),
        countersink_included_angle_mdeg=_optional_int(raw["countersink_included_angle_mdeg"]),
    )


def _parse_production_program(value: object) -> ProductionProgram:
    raw = _exact_object(
        value,
        {
            "program_id",
            "run_order",
            "setup_id",
            "tool_id",
            "tool_version",
            "recipe_ids",
            "operation_ids",
            "release_operation_ids",
            "moves",
        },
        "toolpath program",
    )
    return ProductionProgram(
        program_id=_required_string(raw["program_id"], "program ID"),
        run_order=_required_int(raw["run_order"], "run order"),
        setup_id=_required_string(raw["setup_id"], "program setup ID"),
        tool_id=_required_string(raw["tool_id"], "program tool ID"),
        tool_version=_required_string(raw["tool_version"], "program tool version"),
        recipe_ids=_string_tuple(raw["recipe_ids"], "program recipe IDs"),
        operation_ids=_string_tuple(raw["operation_ids"], "program operation IDs"),
        release_operation_ids=_string_tuple(
            raw["release_operation_ids"], "program release operation IDs"
        ),
        moves=tuple(_parse_move(item) for item in _required_list(raw["moves"], "program moves")),
    )


def _parse_move(value: object) -> ProductionMove:
    raw = _exact_object(
        value,
        {
            "sequence",
            "operation_id",
            "pass_index",
            "kind",
            "role",
            "x_um",
            "y_um",
            "z_um",
            "feed_um_min",
        },
        "production move",
    )
    return ProductionMove(
        sequence=_required_int(raw["sequence"], "move sequence"),
        operation_id=_required_string(raw["operation_id"], "move operation ID"),
        pass_index=_required_int(raw["pass_index"], "move pass index"),
        kind=ProductionMoveKind(_required_string(raw["kind"], "move kind")),
        role=ProductionMoveRole(_required_string(raw["role"], "move role")),
        x_um=_required_int(raw["x_um"], "move X"),
        y_um=_required_int(raw["y_um"], "move Y"),
        z_um=_required_int(raw["z_um"], "move Z"),
        feed_um_min=_optional_int(raw["feed_um_min"]),
    )


def _parse_point(value: object) -> Point2D:
    raw = _exact_object(value, {"x_um", "y_um"}, "point")
    return Point2D(_required_int(raw["x_um"], "point X"), _required_int(raw["y_um"], "point Y"))


def _parse_rect(value: object) -> Rect:
    raw = _exact_object(value, {"x_um", "y_um", "width_um", "height_um"}, "rectangle")
    return Rect(
        _required_int(raw["x_um"], "rectangle X"),
        _required_int(raw["y_um"], "rectangle Y"),
        _required_int(raw["width_um"], "rectangle width"),
        _required_int(raw["height_um"], "rectangle height"),
    )


def _canonical_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ArtifactError(f"{label} is not canonical UTF-8 JSON")

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        parsed = json.loads(payload.decode("utf-8"), parse_constant=reject_nonfinite)
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != payload:
            raise ValueError("JSON object is not canonical")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise ArtifactError(f"{label} is not canonical UTF-8 JSON") from exc
    return {str(key): value for key, value in parsed.items()}


def _validate_candidate_path(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
    ):
        raise ArtifactError("unsafe CAM candidate artifact path")


def _require_unique_paths(artifacts: tuple[ArtifactFile, ...]) -> None:
    paths = [artifact.path for artifact in artifacts]
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ArtifactError("CAM candidate artifacts contain duplicate path aliases")
    if paths != sorted(paths):
        raise ArtifactError("CAM candidate artifacts are not in canonical path order")


def _validate_backplot(payload: bytes) -> None:
    if type(payload) is not bytes or not payload or len(payload) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("cutting backplot must be non-empty bounded bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("cutting backplot must be UTF-8 SVG") from exc
    lowered = text.casefold()
    if (
        "<svg" not in lowered[:512]
        or "<script" in lowered
        or "javascript:" in lowered
        or "<!doctype" in lowered
        or "<!entity" in lowered
        or "<foreignobject" in lowered
    ):
        raise ArtifactError("cutting backplot contains unsupported active SVG content")


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ArtifactError(f"{label} has an unexpected structure")
    return {str(key): item for key, item in value.items()}


def _required_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ArtifactError(f"{label} must be an array")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArtifactError(f"{label} must be a canonical non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _required_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ArtifactError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _required_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ArtifactError(f"{label} must be an integer")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _required_int(value, "optional integer")


def _required_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ArtifactError(f"{label} must be a boolean")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(_required_string(item, label) for item in _required_list(value, label))


__all__ = [
    "CAM_CANDIDATE_BACKPLOT_PATH",
    "CAM_CANDIDATE_MACHINE_PROFILE_PATH",
    "CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION",
    "CAM_CANDIDATE_PACKAGE_BUILDER_VERSION",
    "CAM_CANDIDATE_POSTPROCESSOR_ID",
    "CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH",
    "CAM_CANDIDATE_POSTPROCESSOR_VERSION",
    "CAM_CANDIDATE_PROGRAM_INDEX_PATH",
    "CAM_CANDIDATE_PROGRAM_INDEX_SCHEMA_VERSION",
    "CAM_CANDIDATE_PROGRAM_ROOT",
    "CAM_CANDIDATE_REPORT_PATH",
    "CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH",
    "CAM_CANDIDATE_SETUP_INSTRUCTIONS_SCHEMA_VERSION",
    "CAM_CANDIDATE_STATUS",
    "CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH",
    "CAM_CANDIDATE_SOURCE_OPERATIONS_PATH",
    "CAM_CANDIDATE_TOOLPATH_PATH",
    "CAM_CANDIDATE_VALIDATION_REPORT_SCHEMA_VERSION",
    "CAMCandidateBundle",
    "build_cam_candidate_bundle",
    "read_operations_document_from_design_review_bundle",
    "read_and_verify_cam_candidate_package",
]
