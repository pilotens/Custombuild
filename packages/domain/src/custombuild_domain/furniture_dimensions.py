"""Reconcile site measurements with actual model dimensions without guessing trim."""

from __future__ import annotations

from typing import Any

from .furniture import FurnitureDesign

DIMENSION_REVIEW_VERSION = "furniture-dimensions-1.0.0"


def review_furniture_dimensions(design: FurnitureDesign) -> dict[str, Any]:
    actual = {key: getattr(design.intent, key) for key in ("width_um", "height_um", "depth_um")}
    installation = design.installation
    issues = []
    expected = None
    if installation is not None:
        expected = installation.carcass_dimensions()
        if expected is None:
            issues.append(
                {
                    "code": "INSTALLATION_ALLOWANCES_MISSING",
                    "message": "Ange utrymmet för list och montage på alla sex sidor. "
                    "Ett tomt mått är okänt; ange 0 där inget utrymme ska reserveras.",
                }
            )
        elif expected != actual:
            issues.append(
                {
                    "code": "INSTALLATION_DIMENSIONS_DIFFER",
                    "message": "Stommåtten stämmer inte med kundens yttermått minus reserverat "
                    "utrymme. Räkna om stommen före tillverkningsberedning.",
                }
            )
        if installation.width_includes_trim:
            issues.append(
                {
                    "code": "TRIM_DESIGN_REQUIRED",
                    "message": "Kundens längdmått inkluderar list. Listens tvärsnitt, längder "
                    "och mekaniska infästning behöver modelleras. Reserverat utrymme skapar "
                    "inga listdelar i kaplistan.",
                }
            )
    return {
        "version": DIMENSION_REVIEW_VERSION,
        "state": "requires_resolution"
        if issues
        else "reconciled"
        if installation
        else "not_specified",
        "carcass_dimensions_um": actual,
        "required_carcass_dimensions_um": expected,
        "installation": installation.model_dump(mode="json") if installation else None,
        "issues": issues,
        "production_qualified": False,
    }


def assert_production_dimensions(design: FurnitureDesign) -> None:
    review = review_furniture_dimensions(design)
    if review["issues"]:
        raise ValueError(" ".join(issue["message"] for issue in review["issues"]))
