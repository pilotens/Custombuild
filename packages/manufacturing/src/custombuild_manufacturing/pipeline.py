"""High-level, deterministic domain-to-production-package orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from custombuild_cad import (
    FREECAD_BRIDGE_VERSION,
    FREECAD_PROJECT_CONTRACT_VERSION,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    FreeCADBridgeError,
    FreeCADProjectBridge,
)
from custombuild_cam import backplot_svg
from custombuild_postprocessors import LinuxCNCValidationPostprocessor

from .adapters import adapt_design_result
from .dfm import (
    DFM_ENGINE_VERSION,
    JOINT_SYSTEM_UNSUPPORTED_CODE,
    DFMValidator,
    stock_profile_missing_issue,
    unsupported_joint_system_issues,
)
from .errors import ProductionBlockedError
from .grain import (
    DFM_GRAIN_BLOCKER_CODE,
    stock_grain_binding_issues,
    stock_grain_binding_required,
)
from .model import (
    DFMIssue,
    DFMReport,
    MachineProfile,
    NestingLayout,
    OperationsDocument,
    PartSpec,
    Rect,
    Severity,
    StockSheet,
    canonical_json_bytes,
    sha256_hex,
)
from .nesting import DeterministicNester
from .operations import (
    MIN_VALIDATION_CONTOUR_KERF_UM,
    OPERATIONS_SCHEMA_VERSION,
    TwoSidedRegistration,
    generate_operations_document,
    registration_pin_keep_out_rectangles,
)
from .package import (
    GENERATION_PLAN_PIPELINE_VERSION,
    ArtifactFile,
    ManifestContext,
    build_deterministic_zip,
    caller_additional_artifact_violation,
    default_artifacts,
    design_review_artifacts,
    generation_plan_artifact,
    read_and_verify_package,
    stock_selection_artifact,
    supplier_handoff_manifest_context,
)
from .profiles import tool_catalog_fingerprint
from .quality import (
    SUPPLIER_HANDOFF_PATH,
    SUPPLIER_HANDOFF_ROLE,
    supplier_handoff_json,
)
from .readiness import WorkshopReadinessReport, build_workshop_readiness_report
from .review_status import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    DesignReviewPackageStatus,
    back_panel_retention_evidence_missing,
    blocked_design_review_package_status,
    dado_retention_evidence_missing,
    generated_design_review_package_status,
)

PRODUCTION_PIPELINE_VERSION = GENERATION_PLAN_PIPELINE_VERSION
FROZEN_DESIGN_SPEC_SCHEMA_VERSION = "custombuild.frozen-design-spec.v1"
DESIGN_RESULT_SUMMARY_SCHEMA_VERSION = "custombuild.design-result-summary.v1"


def _known_retention_decision_codes(design_result: Any) -> tuple[str, ...]:
    """Return every unresolved retention decision, independent of stage precedence."""

    codes: list[str] = []
    if dado_retention_evidence_missing(design_result):
        codes.append(DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE)
    if back_panel_retention_evidence_missing(design_result):
        codes.append(BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE)
    return tuple(sorted(codes))


def _retention_blocker_code(design_result: Any) -> str | None:
    """Resolve the active retention prerequisite in stable, fail-closed order."""

    codes = _known_retention_decision_codes(design_result)
    if DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE in codes:
        return DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    if BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE in codes:
        return BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    return None


@dataclass(frozen=True, slots=True)
class ProductionBundle:
    zip_bytes: bytes
    manifest: dict[str, Any]
    artifacts: tuple[ArtifactFile, ...]
    layouts: tuple[NestingLayout, ...]
    dfm_report: DFMReport
    operations: OperationsDocument | None
    workshop_readiness: WorkshopReadinessReport
    review_status: DesignReviewPackageStatus


def _run_dfm_screen(
    *,
    grouped_parts: Iterable[tuple[StockSheet, tuple[PartSpec, ...]]],
    selection_issues: Iterable[DFMIssue],
    machine: MachineProfile,
) -> tuple[
    DFMReport,
    tuple[tuple[StockSheet, tuple[PartSpec, ...], NestingLayout], ...],
]:
    """Run the complete applicable DFM screen before any later-stage blocker wins.

    Retention evidence controls whether semantic CAM may be generated; it must
    never suppress geometry, tooling, nesting, keep-out, or feature-collision
    checks for the supplier review package that remains available.
    """

    report_issues = list(selection_issues)
    validated_groups: list[tuple[StockSheet, tuple[PartSpec, ...], NestingLayout]] = []
    validator = DFMValidator()
    for selected_stock, selected_parts in grouped_parts:
        current_layout = DeterministicNester().nest(selected_parts, selected_stock)
        validated_groups.append((selected_stock, selected_parts, current_layout))
        current_report = validator.validate(selected_parts, current_layout, machine)
        report_issues.extend(current_report.issues)
    return (
        DFMReport(tuple(report_issues), engine_version=validator.engine_version),
        tuple(validated_groups),
    )


def build_production_bundle(
    design_result: Any,
    *,
    stock: StockSheet | Iterable[StockSheet],
    machine: MachineProfile,
    context: ManifestContext,
    include_step: bool = True,
    include_freecad_project: bool = False,
    include_validation_program: bool = True,
    production_release: bool = False,
    allow_blocked_cam: bool = False,
    two_sided_registration_by_stock: Mapping[str, Mapping[int, TwoSidedRegistration]] | None = None,
    additional_artifacts: Iterable[ArtifactFile] = (),
    freecad_bridge: FreeCADProjectBridge | None = None,
) -> ProductionBundle:
    """Build a complete reproducible bundle or fail before returning partial data.

    ``include_step=True`` always means genuine server-side STEP and GLB. If
    CadQuery/OpenCascade is absent or either export fails, generation is blocked;
    placeholder geometry is never inserted.

    Two-sided layouts require an explicit registration plan at
    ``two_sided_registration_by_stock[stock_id][sheet_index]``. The pipeline
    deliberately does not infer WCS, pins, fixtures or registration coordinates.
    ``allow_blocked_cam`` may only convert an exact missing-stock profile, an
    unbound directional stock axis, unresolved dry/mechanical DADO retention,
    or a missing registration plan into a checksum-bound design-review package.
    It never invents evidence or emits partial nesting/CAM artifacts. Every
    other joint-system claim is rejected before feature kinds can be lowered to
    operation aliases, including RABBET-to-GROOVE lowering.
    """

    if include_freecad_project and not include_step:
        raise ProductionBlockedError(
            "include_freecad_project=true requires include_step=true authoritative geometry"
        )
    if type(allow_blocked_cam) is not bool:
        raise TypeError("allow_blocked_cam must be a boolean")
    additional_artifact_values = tuple(additional_artifacts)
    forbidden_additions = tuple(
        reason
        for artifact in additional_artifact_values
        if (
            reason := caller_additional_artifact_violation(
                artifact.path,
                artifact.role,
                artifact.media_type,
            )
        )
        is not None
    )
    if forbidden_additions:
        raise ProductionBlockedError(
            "caller-supplied manufacturing or non-review artifact is outside the safe "
            "review-document allowlist: "
            + "; ".join(forbidden_additions)
        )

    adapted = adapt_design_result(design_result)
    if context.design_hash != adapted.design_hash:
        raise ProductionBlockedError("manifest context design_hash does not match DesignResult")
    if (
        context.machine_profile_id != machine.profile_id
        or context.machine_profile_version != machine.version
    ):
        raise ProductionBlockedError(
            "manifest context machine profile does not match selected machine"
        )

    stocks = (stock,) if isinstance(stock, StockSheet) else tuple(stock)
    if not stocks:
        raise ProductionBlockedError("at least one stock profile is required")
    if len({item.stock_id for item in stocks}) != len(stocks):
        raise ProductionBlockedError("stock_id values must be unique within a generation request")
    if any(item.kerf_um < MIN_VALIDATION_CONTOUR_KERF_UM for item in stocks):
        raise ProductionBlockedError(
            "validation stock kerf is smaller than the supported 6000 um contour-tool envelope"
        )
    registration_values = two_sided_registration_by_stock or {}
    if not isinstance(registration_values, Mapping):
        raise ProductionBlockedError("two-sided registrations must be a mapping")
    stock_by_id = {item.stock_id: item for item in stocks}
    keep_outs_by_stock: dict[str, list[Rect]] = {}
    for stock_id, registrations_by_sheet in registration_values.items():
        selected_stock = stock_by_id.get(stock_id)
        if selected_stock is None or not isinstance(registrations_by_sheet, Mapping):
            raise ProductionBlockedError("two-sided registration references unknown stock")
        for sheet_index, registration in registrations_by_sheet.items():
            if (
                type(sheet_index) is not int
                or not 0 <= sheet_index < selected_stock.quantity
                or not isinstance(registration, TwoSidedRegistration)
            ):
                raise ProductionBlockedError("two-sided registration sheet identity is invalid")
            footprints = registration_pin_keep_out_rectangles(registration)
            sheet_bounds = Rect(0, 0, selected_stock.width_um, selected_stock.height_um)
            for footprint in footprints:
                if not sheet_bounds.contains(footprint):
                    raise ProductionBlockedError(
                        "two-sided registration pin footprint lies outside stock"
                    )
                if any(footprint.intersects(zone) for zone in selected_stock.defect_zones):
                    raise ProductionBlockedError(
                        "two-sided registration pin footprint intersects a defect zone"
                    )
                if any(footprint.intersects(zone) for zone in selected_stock.clamp_zones):
                    raise ProductionBlockedError(
                        "two-sided registration pin footprint intersects a fixture keep-out"
                    )
            keep_outs_by_stock.setdefault(stock_id, []).extend(footprints)

    def zone_key(zone: Rect) -> tuple[int, int, int, int]:
        return (zone.y_um, zone.x_um, zone.height_um, zone.width_um)

    stocks = tuple(
        replace(
            item,
            clamp_zones=tuple(
                sorted(
                    set((*item.clamp_zones, *keep_outs_by_stock.get(item.stock_id, ()))),
                    key=zone_key,
                )
            ),
        )
        for item in stocks
    )

    grouped_parts, selection_issues = _assign_parts_to_stock(adapted.parts, stocks)
    selection_blocking_issues = tuple(
        issue for issue in selection_issues if issue.severity is Severity.BLOCK
    )
    selection_grain_warnings = (
        stock_grain_binding_issues(adapted.parts, None, severity=Severity.WARNING)
        if selection_blocking_issues
        else ()
    )
    selection_report = DFMReport(
        (*selection_issues, *selection_grain_warnings),
        engine_version=DFM_ENGINE_VERSION,
    )
    selection_blocker_codes = tuple(
        sorted({issue.code for issue in selection_report.blocking_issues})
    )
    stock_profile_blocked = bool(
        selection_report.blocking_issues
        and allow_blocked_cam
        and include_step
        and selection_blocker_codes == ("STOCK_PROFILE_MISSING",)
    )
    if selection_report.blocking_issues and not stock_profile_blocked:
        codes = ", ".join(selection_blocker_codes)
        raise ProductionBlockedError(
            f"production bundle blocked by DFM: {codes}", report=selection_report
        )
    grain_issues = (
        ()
        if selection_report.blocking_issues
        else tuple(
            issue
            for selected_stock, selected_parts in grouped_parts
            for issue in stock_grain_binding_issues(selected_parts, selected_stock)
        )
    )
    grain_report = DFMReport(grain_issues, engine_version=DFM_ENGINE_VERSION)
    grain_blocker_codes = tuple(sorted({issue.code for issue in grain_report.blocking_issues}))
    grain_profile_blocked = bool(
        grain_report.blocking_issues
        and allow_blocked_cam
        and include_step
        and grain_blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
    )
    if grain_report.blocking_issues and not grain_profile_blocked:
        codes = ", ".join(grain_blocker_codes)
        raise ProductionBlockedError(
            f"production bundle blocked by DFM: {codes}", report=grain_report
        )

    known_retention_decision_codes = _known_retention_decision_codes(design_result)
    retention_blocker_code = _retention_blocker_code(design_result)
    dado_retention_blocked = (
        retention_blocker_code == DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    )
    back_retention_missing = (
        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
        in known_retention_decision_codes
    )
    back_retention_blocked = (
        not stock_profile_blocked
        and not grain_profile_blocked
        and retention_blocker_code
        == BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    )
    if not stock_profile_blocked and not grain_profile_blocked:
        joint_support_issues = unsupported_joint_system_issues(
            design_result,
            defer_surface_back_to_retention=back_retention_missing,
        )
        if joint_support_issues:
            report = DFMReport(
                tuple((*selection_issues, *grain_issues, *joint_support_issues)),
                engine_version=DFM_ENGINE_VERSION,
            )
            joint_types = ", ".join(
                str(issue.inputs["joint_type"]) for issue in joint_support_issues
            )
            raise ProductionBlockedError(
                "production bundle blocked before CAM by unsupported joint systems: "
                f"{JOINT_SYSTEM_UNSUPPORTED_CODE} ({joint_types})",
                report=report,
            )
    layouts: list[NestingLayout] = []
    operations: OperationsDocument | None = None
    if stock_profile_blocked:
        report = selection_report
        review_status = blocked_design_review_package_status(selection_blocker_codes)
    elif grain_profile_blocked:
        report = grain_report
        review_status = blocked_design_review_package_status(grain_blocker_codes)
    elif dado_retention_blocked or back_retention_blocked:
        report, _validated_groups = _run_dfm_screen(
            grouped_parts=grouped_parts,
            selection_issues=(*selection_issues, *grain_issues),
            machine=machine,
        )
        if report.blocking_issues:
            codes = ", ".join(sorted({issue.code for issue in report.blocking_issues}))
            raise ProductionBlockedError(
                f"production bundle blocked by DFM: {codes}", report=report
            )
        assert retention_blocker_code is not None
        if not allow_blocked_cam:
            raise ProductionBlockedError(
                "production bundle blocked by unresolved joint retention: "
                f"{retention_blocker_code}",
                report=report,
            )
        review_status = blocked_design_review_package_status(
            (retention_blocker_code,)
        )
    else:
        report, validated_groups = _run_dfm_screen(
            grouped_parts=grouped_parts,
            selection_issues=(*selection_issues, *grain_issues),
            machine=machine,
        )
        layouts.extend(layout for _, _, layout in validated_groups)
        if report.blocking_issues:
            codes = ", ".join(sorted({issue.code for issue in report.blocking_issues}))
            raise ProductionBlockedError(
                f"production bundle blocked by DFM: {codes}", report=report
            )

        try:
            documents = [
                generate_operations_document(
                    design_hash=adapted.design_hash,
                    parts=selected_parts,
                    layout=current_layout,
                    machine=machine,
                    validate=False,
                    two_sided_registration_by_sheet=(two_sided_registration_by_stock or {}).get(
                        selected_stock.stock_id
                    ),
                )
                for selected_stock, selected_parts, current_layout in validated_groups
            ]
        except ProductionBlockedError as exc:
            blocker_codes = _blocking_issue_codes(exc)
            if not allow_blocked_cam or blocker_codes != ("TWO_SIDED_REGISTRATION_MISSING",):
                raise
            if not include_step:
                raise ProductionBlockedError(
                    "a CAM-blocked design-review package requires include_step=true "
                    "authoritative CAD"
                ) from exc
            review_status = blocked_design_review_package_status(blocker_codes)
        else:
            selected_tool_ids = {
                operation.tool_id for document in documents for operation in document.operations
            }
            selected_tools = tuple(
                sorted(
                    (tool for tool in machine.tools if tool.tool_id in selected_tool_ids),
                    key=lambda item: item.tool_id,
                )
            )
            operations = OperationsDocument(
                schema_version=OPERATIONS_SCHEMA_VERSION,
                design_hash=adapted.design_hash,
                machine_profile_id=machine.profile_id,
                machine_profile_version=machine.version,
                setups=tuple(setup for document in documents for setup in document.setups),
                operations=tuple(
                    operation for document in documents for operation in document.operations
                ),
                mode="VALIDATION",
                tool_catalog_version=machine.tool_library_version,
                tool_catalog_fingerprint=tool_catalog_fingerprint(selected_tools),
                tools=selected_tools,
            )
            review_status = generated_design_review_package_status(
                validation_program_included=include_validation_program
            )

    if not include_step:
        raise ProductionBlockedError(
            "schema-v5 production packages require authoritative STEP/GLB and a canonical "
            "design-review status; statusless generation is disabled"
        )

    additional: list[ArtifactFile] = [
        *_design_identity_artifacts(design_result, adapted),
        stock_selection_artifact(
            stocks,
            grouped_parts,
            unmatched_part_ids=(
                issue.part_id for issue in selection_issues if issue.part_id is not None
            ),
        ),
        generation_plan_artifact(
            machine=machine,
            stocks=stocks,
            two_sided_registration_by_stock=two_sided_registration_by_stock,
            validation_program_requested=include_validation_program,
        ),
        ArtifactFile(
            "validation/dfm-report.json",
            canonical_json_bytes(report),
            "application/json",
            "DFM_VALIDATION_REPORT",
        ),
    ]
    if operations is not None:
        additional.append(
            ArtifactFile(
                "cam/validation-backplot.svg",
                backplot_svg(operations),
                "image/svg+xml",
                "VALIDATION_BACKPLOT",
            )
        )
    additional.extend(additional_artifact_values)
    cad_status = "NOT_REQUESTED"
    freecad_status: dict[str, Any] = {
        "status": "OPTIONAL_NOT_REQUESTED",
        "requested": False,
        "runtime_probe_performed": False,
        "bridge_version": FREECAD_BRIDGE_VERSION,
        "contract_version": FREECAD_PROJECT_CONTRACT_VERSION,
        "authoritative_geometry": False,
        "authoritative_source": "model/design.step",
        "mode": "optional_downstream_interchange",
        "machine_authorization": False,
    }
    if include_step:
        try:
            cad = CadQueryAdapter().export_design(design_result)
        except (CADDependencyUnavailable, CADExportError) as exc:
            raise ProductionBlockedError(
                f"include_step=true but authoritative CAD generation is unavailable: {exc}"
            ) from exc
        additional.extend(
            (
                ArtifactFile("model/design.step", cad.step, "model/step", "AUTHORITATIVE_STEP"),
                ArtifactFile("model/design.glb", cad.glb, "model/gltf-binary", "WEB_PREVIEW_GLB"),
            )
        )
        cad_status = "GENERATED"
        if include_freecad_project:
            bridge = freecad_bridge or FreeCADProjectBridge()
            try:
                freecad = bridge.convert_authoritative_step(
                    cad.step,
                    adapted.design_hash,
                    metadata={
                        "project_id": context.project_id,
                        "revision": context.revision,
                        "template_version": adapted.template_version,
                    },
                )
            except FreeCADBridgeError as exc:
                raise ProductionBlockedError(
                    f"include_freecad_project=true but headless FreeCAD export failed: {exc}"
                ) from exc
            additional.append(
                ArtifactFile(
                    "model/design.fcstd",
                    freecad.fcstd,
                    "application/vnd.freecad",
                    "NON_AUTHORITATIVE_FREECAD_PROJECT",
                )
            )
            freecad_status = {
                **freecad_status,
                "status": "GENERATED",
                "requested": True,
                "runtime_probe_performed": True,
                "runtime_version": freecad.runtime_version,
                "source_step_sha256": freecad.source_step_sha256,
            }

    additional.append(
        ArtifactFile(
            "validation/cad-interchange-status.json",
            canonical_json_bytes(freecad_status),
            "application/json",
            "CAD_INTERCHANGE_STATUS",
        )
    )

    postprocessor = LinuxCNCValidationPostprocessor()
    validation_program_generated = operations is not None and include_validation_program
    if operations is not None and include_validation_program:
        programs = postprocessor.generate(operations)
        additional.extend(
            ArtifactFile(
                f"machine-validation/{program.filename}",
                program.content,
                "text/x-gcode",
                "NON_CUTTING_VALIDATION_PROGRAM",
            )
            for program in programs
        )

    if include_step:
        additional.append(
            ArtifactFile(
                DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
                canonical_json_bytes(review_status.as_dict()),
                "application/json",
                DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
            )
        )

    workshop_readiness = build_workshop_readiness_report(
        authoritative_cad=cad_status == "GENERATED",
        dfm_passed=not report.blocking_issues,
        operation_count=len(operations.operations) if operations is not None else 0,
        setup_count=len(operations.setups) if operations is not None else 0,
        validation_backplot=operations is not None,
        validation_program=validation_program_generated,
        edge_band_selection_required=any(
            detail.procurement_status != "CATALOG_IDENTIFIED"
            for part in adapted.parts
            for detail in part.edge_band_details
        ),
        material_grain_binding_required=stock_grain_binding_required(adapted.parts),
        external_evidence=context.external_evidence,
    )
    additional.append(
        ArtifactFile(
            "validation/workshop-readiness.json",
            canonical_json_bytes(workshop_readiness.as_dict()),
            "application/json",
            "WORKSHOP_READINESS_REPORT",
        )
    )

    derived_material_versions = tuple(
        sorted({f"{part.material_id}@{part.material_version}" for part in adapted.parts})
    )
    frozen_context = replace(
        context,
        engine_version=adapted.engine_version,
        template_version=adapted.template_version,
        material_versions=derived_material_versions,
        # The manifest records the selected, frozen postprocessor identity even
        # when CAM is blocked. Whether output exists is a separate status claim.
        postprocessor_version=postprocessor.version,
        cad_status=cad_status,
    )
    if operations is None:
        artifacts = design_review_artifacts(
            parts=adapted.parts,
            project_id=frozen_context.project_id,
            revision=frozen_context.revision,
            design_hash=frozen_context.design_hash,
            additional=additional,
        )
    else:
        artifacts = default_artifacts(
            parts=adapted.parts,
            layout=tuple(layouts),
            operations=operations,
            project_id=frozen_context.project_id,
            revision=frozen_context.revision,
            design_hash=frozen_context.design_hash,
            additional=additional,
        )
    supplier_handoff = ArtifactFile(
        SUPPLIER_HANDOFF_PATH,
        supplier_handoff_json(
            project_id=frozen_context.project_id,
            revision=frozen_context.revision,
            design_hash=frozen_context.design_hash,
            machine=machine,
            stocks=stocks,
            operations=operations,
            cam_status=review_status.cam_status.value,
            blocker_codes=review_status.blocker_codes,
            cam_required_action=review_status.required_action,
            design_review_ready=workshop_readiness.design_review_ready,
            manifest_context_projection=supplier_handoff_manifest_context(frozen_context),
            payload_inventory_entries=(
                {
                    "path": artifact.path,
                    "media_type": artifact.media_type,
                    "role": artifact.role,
                    "size_bytes": len(artifact.data),
                    "sha256": sha256_hex(artifact.data),
                }
                for artifact in artifacts
            ),
            known_unresolved_decision_codes=known_retention_decision_codes,
            dfm_warning_issues=(
                issue for issue in report.issues if issue.severity is Severity.WARNING
            ),
        ),
        "application/json",
        SUPPLIER_HANDOFF_ROLE,
    )
    artifacts = tuple(sorted((*artifacts, supplier_handoff), key=lambda item: item.path))
    payload = build_deterministic_zip(
        frozen_context,
        artifacts,
        production_release=production_release,
    )
    manifest = read_and_verify_package(payload)
    return ProductionBundle(
        payload,
        manifest,
        artifacts,
        tuple(layouts),
        report,
        operations,
        workshop_readiness,
        review_status,
    )


def _design_identity_artifacts(
    design_result: Any,
    adapted: Any,
) -> tuple[ArtifactFile, ArtifactFile]:
    """Freeze the exact parametric input beside a compact derived-result index.

    These two artifacts are intentionally generated before CAD/CAM.  The
    manifest already hashes every artifact, so the DesignSpec, the derived
    design hash and all downstream files become one offline-verifiable unit.
    """

    spec = getattr(design_result, "spec", None)
    if spec is None:
        raise ProductionBlockedError("production bundle requires the authoritative DesignSpec")
    spec_bytes = canonical_json_bytes(
        {
            "schema_version": FROZEN_DESIGN_SPEC_SCHEMA_VERSION,
            "spec": spec,
        }
    )
    part_ids = tuple(sorted(str(part.part_id) for part in getattr(design_result, "parts", ())))
    joint_ids = tuple(sorted(str(joint.joint_id) for joint in getattr(design_result, "joints", ())))
    assembly_graph = getattr(design_result, "assembly_graph", None)
    steps = tuple(getattr(assembly_graph, "steps", ()))
    summary = {
        "schema_version": DESIGN_RESULT_SUMMARY_SCHEMA_VERSION,
        "design_hash": adapted.design_hash,
        "design_spec": {
            "path": "design/design-spec.json",
            "sha256": sha256_hex(spec_bytes),
            "schema_version": FROZEN_DESIGN_SPEC_SCHEMA_VERSION,
        },
        "engine_version": adapted.engine_version,
        "domain_template_version": adapted.template_version,
        "part_count": len(part_ids),
        "part_ids": part_ids,
        "joint_count": len(joint_ids),
        "joint_ids": joint_ids,
        "assembly_step_count": len(steps),
        "total_weight_g": int(getattr(design_result, "total_weight_g", 0)),
    }
    return (
        ArtifactFile(
            "design/design-spec.json",
            spec_bytes,
            "application/json",
            "FROZEN_DESIGN_SPEC",
        ),
        ArtifactFile(
            "design/result-summary.json",
            canonical_json_bytes(summary),
            "application/json",
            "DESIGN_RESULT_SUMMARY",
        ),
    )


def _assign_parts_to_stock(
    parts: tuple[PartSpec, ...],
    stocks: tuple[StockSheet, ...],
) -> tuple[tuple[tuple[StockSheet, tuple[PartSpec, ...]], ...], tuple[DFMIssue, ...]]:
    grouped: dict[str, list[PartSpec]] = {}
    stock_by_id = {item.stock_id: item for item in stocks}
    issues: list[DFMIssue] = []
    nester = DeterministicNester()
    for part in sorted(parts, key=lambda item: item.part_id):
        compatible = [
            item
            for item in stocks
            if item.material_id == part.material_id
            and item.material_version == part.material_version
            and item.thickness_um == part.thickness_um
            # Stock selection answers only whether material, thickness and one
            # exemplar's geometry match. Grain is a separate, higher-precedence
            # manufacturing gate evaluated before any real layout is retained.
            and nester.nest(
                (replace(part, quantity=1, grain_direction="NONE"),), item
            ).is_complete
        ]
        if not compatible:
            issues.append(stock_profile_missing_issue(part))
            continue
        selected = min(
            compatible,
            key=lambda item: (item.width_um * item.height_um, item.stock_id),
        )
        grouped.setdefault(selected.stock_id, []).append(part)
    result = tuple(
        (stock_by_id[stock_id], tuple(grouped[stock_id])) for stock_id in sorted(grouped)
    )
    return result, tuple(issues)


def _blocking_issue_codes(exc: ProductionBlockedError) -> tuple[str, ...]:
    report = exc.report
    if not isinstance(report, DFMReport):
        return ()
    return tuple(sorted({issue.code for issue in report.blocking_issues}))
