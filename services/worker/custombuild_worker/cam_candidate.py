"""Worker-side orchestration for one opt-in executable CAM candidate.

This module is deliberately separate from the validation-only production
pipeline.  It consumes that pipeline's already-built, verified review bundle,
then creates a checksum-bound sidecar.  No function here can authorize a
physical machine start.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from custombuild_cam import generate_production_toolpaths
from custombuild_cam.production_verification import (
    CuttingProgramStatus,
    cutting_backplot_svg,
    verify_production_toolpaths,
)
from custombuild_manufacturing import (
    ArtifactError,
    ArtifactFile,
    CAMStageStatus,
    ProductionBlockedError,
)
from custombuild_manufacturing.cam_candidate_package import (
    CAM_CANDIDATE_BACKPLOT_PATH,
    CAM_CANDIDATE_MACHINE_PROFILE_PATH,
    CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
    CAM_CANDIDATE_PROGRAM_INDEX_PATH,
    CAM_CANDIDATE_PROGRAM_ROOT,
    CAM_CANDIDATE_REPORT_PATH,
    CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
    CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
    CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
    CAM_CANDIDATE_STATUS,
    CAM_CANDIDATE_TOOLPATH_PATH,
    CAMCandidateBundle,
    build_cam_candidate_bundle,
)
from custombuild_manufacturing.cam_software_provenance import (
    CAMSoftwareProvenanceError,
    cam_software_provenance_sha256,
    producer_build_identity_from_engine_context,
)
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex
from custombuild_manufacturing.pipeline import ProductionBundle
from custombuild_manufacturing.production_context import ProductionEngineContext
from custombuild_manufacturing.production_machine_profile import (
    TEST_ONLY_PROFILE,
    LoadedProductionMachineProfile,
    production_machine_profile_job_binding,
)
from custombuild_postprocessors import LinuxCNCProductionPostprocessor

CAM_CANDIDATE_RESULT_SCHEMA_VERSION = "custombuild.cam-candidate-result.v2"
CAM_CANDIDATE_BUNDLE_STORAGE_PATH = "cam-candidate/cam-candidate.zip"


@dataclass(frozen=True, slots=True)
class CandidateEvidenceArtifact:
    kind: str
    artifact: ArtifactFile


@dataclass(frozen=True, slots=True)
class WorkerCAMCandidate:
    bundle: CAMCandidateBundle
    evidence: tuple[CandidateEvidenceArtifact, ...]
    result_claims: dict[str, Any]


def build_worker_cam_candidate(
    base: ProductionBundle,
    profile: LoadedProductionMachineProfile,
    production_engine_context: ProductionEngineContext | Mapping[str, Any] | None,
) -> WorkerCAMCandidate:
    """Build and independently verify a cutting sidecar or fail closed."""

    source = base.operations
    status = base.review_status
    if (
        source is None
        or status.cam_status is not CAMStageStatus.VALIDATION_GENERATED
        or status.operations_included is not True
        or status.nesting_included is not True
        or status.setup_sheets_included is not True
        or status.validation_backplot_included is not True
        or status.validation_program_included is not True
        or status.blocker_codes
        or base.manifest.get("release_scope") != "design_review"
        or base.manifest.get("machine_use") != "validation_only"
        or base.manifest.get("physical_cutting_authorized") is not False
    ):
        raise ProductionBlockedError(
            "cutting candidate requires an unblocked, complete validation CAM review package"
        )
    engine_context = (
        production_engine_context.as_dict()
        if isinstance(production_engine_context, ProductionEngineContext)
        else production_engine_context
    )
    if not isinstance(engine_context, Mapping):
        raise ProductionBlockedError(
            "cutting candidate requires the independently resolved production engine context"
        )
    try:
        if canonical_json_bytes(base.manifest.get("production_engine_context")) != (
            canonical_json_bytes(engine_context)
        ):
            raise ProductionBlockedError(
                "cutting candidate producer context differs from the review bundle"
            )
        producer_build = producer_build_identity_from_engine_context(
            engine_context,
            allow_test_only=profile.profile_class == TEST_ONLY_PROFILE,
        )
    except CAMSoftwareProvenanceError as exc:
        raise ProductionBlockedError(
            "cutting candidate producer build identity is invalid"
        ) from exc
    try:
        toolpaths = generate_production_toolpaths(source, profile.execution_context)
        independent = verify_production_toolpaths(toolpaths, source)
        if independent.report.status is not CuttingProgramStatus.PASS:
            codes = ", ".join(sorted({issue.code for issue in independent.report.issues}))
            raise ProductionBlockedError(
                f"independent cutting-toolpath verification failed: {codes}"
            )
        backplot = cutting_backplot_svg(toolpaths, source)
        postprocessor = LinuxCNCProductionPostprocessor(profile.postprocessor_profile)
        programs = postprocessor.generate(toolpaths)
        candidate = build_cam_candidate_bundle(
            base.zip_bytes,
            toolpaths=toolpaths,
            programs=programs,
            production_profile=profile,
            cutting_backplot_svg=backplot,
            producer_build_identity=producer_build,
        )
    except ProductionBlockedError:
        raise
    except (ArtifactError, TypeError, ValueError) as exc:
        raise ProductionBlockedError(f"cutting candidate generation failed: {exc}") from exc

    evidence = _public_evidence(candidate)
    manifest_bytes = canonical_json_bytes(candidate.manifest)
    software_provenance = candidate.manifest["software_provenance"]
    software_provenance_sha256 = cam_software_provenance_sha256(
        software_provenance,
        allow_test_only=profile.profile_class == TEST_ONLY_PROFILE,
    )
    result_claims = {
        "schema_version": CAM_CANDIDATE_RESULT_SCHEMA_VERSION,
        "status": CAM_CANDIDATE_STATUS,
        "mode": candidate.manifest["mode"],
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "base_design_review_bundle_sha256": sha256_hex(base.zip_bytes),
        "bundle_sha256": sha256_hex(candidate.zip_bytes),
        "bundle_size_bytes": len(candidate.zip_bytes),
        "manifest_sha256": sha256_hex(manifest_bytes),
        "candidate_context_hash": candidate.manifest["candidate_context_hash"],
        "software_provenance": software_provenance,
        "software_provenance_sha256": software_provenance_sha256,
        "production_profile_job_binding": production_machine_profile_job_binding(profile),
        "production_profile_payload_sha256": profile.payload_sha256,
        "execution_context_sha256": profile.execution_context.fingerprint,
        "production_machine_profile_sha256": profile.document_sha256,
        "postprocessor_machine_profile_sha256": profile.postprocessor_profile.config_sha256,
        "toolpaths_sha256": candidate.toolpaths.fingerprint,
        "program_count": len(candidate.programs),
        "postprocessor": {
            "id": candidate.manifest["postprocessor"]["id"],
            "version": candidate.manifest["postprocessor"]["version"],
        },
    }
    return WorkerCAMCandidate(
        bundle=candidate,
        evidence=evidence,
        result_claims=result_claims,
    )


def _public_evidence(candidate: CAMCandidateBundle) -> tuple[CandidateEvidenceArtifact, ...]:
    by_path = {artifact.path: artifact for artifact in candidate.artifacts}
    if len(by_path) != len(candidate.artifacts):
        raise ProductionBlockedError("cutting candidate contains duplicate artifact paths")

    required_singletons = {
        CAM_CANDIDATE_TOOLPATH_PATH: "cutting_toolpaths",
        CAM_CANDIDATE_PROGRAM_INDEX_PATH: "machine_program_index",
        CAM_CANDIDATE_REPORT_PATH: "cutting_program_validation_report",
        CAM_CANDIDATE_BACKPLOT_PATH: "cutting_backplot",
        CAM_CANDIDATE_MACHINE_PROFILE_PATH: "production_machine_profile",
    }
    missing = sorted(set(required_singletons) - set(by_path))
    if missing:
        raise ProductionBlockedError(
            "cutting candidate is missing required public evidence: " + ", ".join(missing)
        )

    values: list[CandidateEvidenceArtifact] = [
        CandidateEvidenceArtifact(
            "cam_candidate_bundle",
            ArtifactFile(
                CAM_CANDIDATE_BUNDLE_STORAGE_PATH,
                candidate.zip_bytes,
                "application/zip",
                "EXECUTABLE_CAM_CANDIDATE_BUNDLE",
            ),
        )
    ]
    values.extend(
        CandidateEvidenceArtifact(kind, by_path[path]) for path, kind in required_singletons.items()
    )

    program_paths = {
        f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}" for program in candidate.programs
    }
    actual_program_paths = {
        artifact.path
        for artifact in candidate.artifacts
        if artifact.role == "EXECUTABLE_CAM_CANDIDATE_PROGRAM"
    }
    if actual_program_paths != program_paths:
        raise ProductionBlockedError(
            "cutting candidate program artifacts differ from its program inventory"
        )
    for expected_order, program in enumerate(candidate.programs, start=1):
        if program.run_order != expected_order or expected_order > 999:
            raise ProductionBlockedError(
                "cutting candidate program order must be dense and fit three digits"
            )
        path = f"{CAM_CANDIDATE_PROGRAM_ROOT}{program.filename}"
        artifact = by_path[path]
        if (
            artifact.data != program.content
            or artifact.media_type != "text/x-gcode"
            or artifact.role != "EXECUTABLE_CAM_CANDIDATE_PROGRAM"
        ):
            raise ProductionBlockedError(
                "cutting candidate program artifact is detached from its program"
            )
        values.append(CandidateEvidenceArtifact(f"machine_program_{expected_order:03d}", artifact))

    allowed_paths = {
        CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
        *required_singletons,
        *program_paths,
    }
    if set(by_path) != allowed_paths:
        raise ProductionBlockedError("cutting candidate contains an unknown public artifact")
    kinds = tuple(value.kind for value in values)
    if len(kinds) != len(set(kinds)):
        raise ProductionBlockedError("cutting candidate public evidence kinds are not unique")
    return tuple(values)


__all__ = [
    "CAM_CANDIDATE_BUNDLE_STORAGE_PATH",
    "CAM_CANDIDATE_RESULT_SCHEMA_VERSION",
    "CandidateEvidenceArtifact",
    "WorkerCAMCandidate",
    "build_worker_cam_candidate",
]
