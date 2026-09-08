"""Reconcile site measurements with actual model dimensions without guessing trim."""

from __future__ import annotations

from typing import Any

from .furniture import FurnitureDesign

DIMENSION_REVIEW_VERSION = "furniture-dimensions-1.1.0"


def review_furniture_dimensions(design: FurnitureDesign) -> dict[str, Any]:
    actual = {key: getattr(design.intent, key) for key in ("width_um", "height_um", "depth_um")}
    installation = design.installation
    issues = []
    expected = None
    clearances = []
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
        trim = installation.trim_profile
        if trim is not None and (trim.height_um is None or trim.width_um is None):
            issues.append(
                {
                    "code": "TRIM_DIMENSIONS_REQUIRED",
                    "message": "Ange listens uppmätta höjd och bredd/utstick. "
                    "Tomma värden ersätts inte av antagna profilmått.",
                }
            )
        if trim is not None and trim.use == "existing_room_trim":
            if not trim.walls:
                issues.append(
                    {
                        "code": "ROOM_TRIM_LOCATION_REQUIRED",
                        "message": "Ange vilka väggar som har befintlig list. "
                        "Profilens mått bestämmer inte placeringen.",
                    }
                )
            for wall in trim.walls:
                allowance = getattr(installation, f"{wall}_allowance_um")
                clearances.append(
                    {
                        "wall": wall,
                        "required_clearance_um": trim.width_um,
                        "reserved_clearance_um": allowance,
                        "height_um": trim.height_um,
                    }
                )
                if trim.width_um is not None and (allowance is None or allowance < trim.width_um):
                    wall_name = {
                        "left": "vänster vägg",
                        "right": "höger vägg",
                        "rear": "bakväggen",
                    }[wall]
                    issues.append(
                        {
                            "code": "ROOM_TRIM_CLEARANCE_REQUIRED",
                            "message": f"Befintlig list vid {wall_name} kräver minst "
                            f"{trim.width_um / 1_000:g} mm frigång. Ange utrymmet och räkna "
                            "om stommen. Ingen urfräsning eller borttagning av list antas.",
                        }
                    )
        elif trim is not None and trim.use == "unassigned":
            issues.append(
                {
                    "code": "TRIM_USE_REQUIRED",
                    "message": "Listprofilens mått är sparade. Ange om den hör till rummet "
                    "eller ska tillverkas som en del av möbeln.",
                }
            )
        if (trim is not None and trim.use == "furniture_trim") or (
            installation.width_includes_trim and trim is None
        ):
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
        "room_trim_clearances": clearances,
        "installation": installation.model_dump(mode="json") if installation else None,
        "issues": issues,
        "production_qualified": False,
    }


def assert_production_dimensions(design: FurnitureDesign) -> None:
    review = review_furniture_dimensions(design)
    if review["issues"]:
        raise ValueError(" ".join(issue["message"] for issue in review["issues"]))
