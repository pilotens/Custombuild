"""High-level, deterministic domain-to-production-package orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from custombuild_cad import CADDependencyUnavailable, CADExportError, CadQueryAdapter
from custombuild_cam import backplot_svg
from custombuild_postprocessors import LinuxCNCValidationPostprocessor

from .adapters import adapt_design_result
from .dfm import DFMValidator
from .errors import ProductionBlockedError
from .model import (
    DFMIssue,
    DFMReport,
    MachineProfile,
    NestingLayout,
    OperationsDocument,
    PartSpec,
    Severity,
    StockSheet,
    canonical_json_bytes,
)
from .nesting import DeterministicNester
from .operations import generate_operations_document
from .package import (
    ArtifactFile,
    ManifestContext,
    build_deterministic_zip,
    default_artifacts,
    read_and_verify_package,
)
from .profiles import tool_catalog_fingerprint

PRODUCTION_PIPELINE_VERSION = "production-pipeline-1.0.0"


@dataclass(frozen=True, slots=True)
class ProductionBundle:
    zip_bytes: bytes
    manifest: dict[str, Any]
    artifacts: tuple[ArtifactFile, ...]
    layouts: tuple[NestingLayout, ...]
    dfm_report: DFMReport
    operations: OperationsDocument


def build_production_bundle(
    design_result: Any,
    *,
    stock: StockSheet | Iterable[StockSheet],
    machine: MachineProfile,
    context: ManifestContext,
    include_step: bool = True,
    include_validation_program: bool = True,
    production_release: bool = False,
    additional_artifacts: Iterable[ArtifactFile] = (),
) -> ProductionBundle:
    """Build a complete reproducible bundle or fail before returning partial data.

    ``include_step=True`` always means genuine server-side STEP and GLB. If
    CadQuery/OpenCascade is absent or either export fails, generation is blocked;
    placeholder geometry is never inserted.
    """

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

    grouped_parts, selection_issues = _assign_parts_to_stock(adapted.parts, stocks)
    layouts: list[NestingLayout] = []
    report_issues = list(selection_issues)
    documents: list[OperationsDocument] = []
    for selected_stock, selected_parts in grouped_parts:
        current_layout = DeterministicNester().nest(selected_parts, selected_stock)
        layouts.append(current_layout)
        current_report = DFMValidator().validate(selected_parts, current_layout, machine)
        report_issues.extend(current_report.issues)
        if not current_report.blocking_issues:
            documents.append(
                generate_operations_document(
                    design_hash=adapted.design_hash,
                    parts=selected_parts,
                    layout=current_layout,
                    machine=machine,
                    validate=False,
                )
            )
    report = DFMReport(tuple(report_issues))
    if report.blocking_issues:
        codes = ", ".join(sorted({issue.code for issue in report.blocking_issues}))
        raise ProductionBlockedError(f"production bundle blocked by DFM: {codes}", report=report)
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
        schema_version="custombuild.operations.v1",
        design_hash=adapted.design_hash,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        setups=tuple(setup for document in documents for setup in document.setups),
        operations=tuple(operation for document in documents for operation in document.operations),
        mode="VALIDATION",
        tool_catalog_version=machine.tool_library_version,
        tool_catalog_fingerprint=tool_catalog_fingerprint(selected_tools),
        tools=selected_tools,
    )

    additional: list[ArtifactFile] = [
        ArtifactFile(
            "validation/dfm-report.json",
            canonical_json_bytes(report),
            "application/json",
            "DFM_VALIDATION_REPORT",
        ),
        ArtifactFile(
            "cam/validation-backplot.svg",
            backplot_svg(operations),
            "image/svg+xml",
            "VALIDATION_BACKPLOT",
        ),
    ]
    additional.extend(additional_artifacts)
    cad_status = "NOT_REQUESTED"
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

    postprocessor = LinuxCNCValidationPostprocessor()
    if include_validation_program:
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

    derived_material_versions = tuple(
        sorted({f"{part.material_id}@{part.material_version}" for part in adapted.parts})
    )
    frozen_context = replace(
        context,
        engine_version=adapted.engine_version,
        template_version=adapted.template_version,
        material_versions=derived_material_versions,
        postprocessor_version=postprocessor.version if include_validation_program else "none",
        cad_status=cad_status,
    )
    artifacts = default_artifacts(
        parts=adapted.parts,
        layout=tuple(layouts),
        operations=operations,
        additional=additional,
    )
    payload = build_deterministic_zip(
        frozen_context,
        artifacts,
        production_release=production_release,
    )
    manifest = read_and_verify_package(payload)
    return ProductionBundle(payload, manifest, artifacts, tuple(layouts), report, operations)


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
            and nester.nest((part,), item).is_complete
        ]
        if not compatible:
            issues.append(
                DFMIssue(
                    "STOCK_PROFILE_MISSING",
                    Severity.BLOCK,
                    "No selected stock profile matches the part material, thickness and size.",
                    part_id=part.part_id,
                    inputs={
                        "material_id": part.material_id,
                        "material_version": part.material_version,
                        "thickness_um": part.thickness_um,
                        "blank_um": (part.blank_width_um, part.blank_height_um),
                    },
                    suggestion="Select matching stock with sufficient quantity and usable area.",
                )
            )
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
