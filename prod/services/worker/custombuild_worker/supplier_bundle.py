from __future__ import annotations

from typing import Any

from custombuild_manufacturing import ArtifactFile
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.package import safe_component

from .simple_assembly import simple_assembly_manual_pdf
from .supplier_documents import (
    assembly_overview_pdf,
    part_drawing_pdf,
    part_feature_csv,
    supplier_readme_pdf,
)


def supplier_artifacts(design_result: Any) -> tuple[ArtifactFile, ...]:
    """Return the machine-neutral handoff required by an external CNC supplier."""
    adapted = adapt_design_result(design_result)
    artifacts: list[ArtifactFile] = [
        ArtifactFile(
            "00-read-me-first.pdf",
            supplier_readme_pdf(design_result, adapted.parts),
            "application/pdf",
            "SUPPLIER_README",
        ),
        ArtifactFile(
            "assembly/assembly-overview.pdf",
            assembly_overview_pdf(design_result),
            "application/pdf",
            "ASSEMBLY_OVERVIEW",
        ),
        ArtifactFile(
            "assembly/simple-assembly-manual.pdf",
            simple_assembly_manual_pdf(design_result),
            "application/pdf",
            "CONSUMER_ASSEMBLY_MANUAL",
        ),
    ]
    for part in sorted(adapted.parts, key=lambda item: item.part_id):
        component = safe_component(part.part_id)
        artifacts.extend(
            (
                ArtifactFile(
                    f"detail-drawings/{component}.pdf",
                    part_drawing_pdf(part),
                    "application/pdf",
                    "PART_DETAIL_DRAWING",
                ),
                ArtifactFile(
                    f"part-data/{component}-features.csv",
                    part_feature_csv(part),
                    "text/csv",
                    "PART_FEATURE_TABLE",
                ),
            )
        )
    return tuple(artifacts)
