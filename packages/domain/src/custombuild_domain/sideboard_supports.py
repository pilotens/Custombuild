"""Dimensions in mm and uniformly distributed load for the S1 shelf supports."""

from dataclasses import dataclass

from .measured_assembly import MeasuredAssembly


@dataclass(frozen=True)
class SupportProfile:
    article: str
    insert: float
    projection: float
    bore: float
    collar_diameter: float
    collar_width: float
    specification: str


LEGACY = SupportProfile(
    "SHELF-PIN-5x12",
    8,
    4,
    8,
    0,
    0,
    "Cylindrisk hyllbärare Ø5; 8 mm i blindhålet och 4 mm utstick. Ej lastklassad artikel.",
)
HAFELE = SupportProfile(
    "282.43.905",
    8,
    8.5,
    8.5,
    7,
    0.5,
    "Häfele 282.43.905, Ø5 × 16,5 mm. Tapp 8 mm, krage Ø7 × 0,5 mm och "
    "8 mm bäryta utanför kragen. Borra Ø5 × 8,5 mm från angiven sida.",
)
CATALOGUE = "https://files.hafele.co.uk/CatalogFiles/www/15KD212.PDF"
DATASHEET = "https://yektaiq.com/sites/default/files/products-datasheets/haefele_28243905_print.pdf"
# UK lists 80 kg for four evenly loaded supports. The US sheet lists 75 lb.
# Apply the smaller figure to the complete set, including the shelf's own mass.
SET_LIMIT_KG = min(80, 75 * 0.45359237)


def divider_web_mm(assembly: MeasuredAssembly, thickness: float, depth_error: float) -> float:
    """Least remaining thickness on a pin bore axis, accounting for overlapping bores."""
    remaining = []
    for part in assembly.parts:
        if not part.id.startswith("DIV-"):
            continue
        holes = [o for o in part.operations if o.id.startswith("PIN-")]
        for a in holes:
            opposite_depth = max(
                (
                    b.depth_um / 1000 + depth_error
                    for b in holes
                    if b.face != a.face
                    and (a.u_um - b.u_um) ** 2 + (a.v_um - b.v_um) ** 2
                    < ((a.width_um + b.width_um) / 2) ** 2
                ),
                default=0.0,
            )
            remaining.append(thickness - a.depth_um / 1000 - depth_error - opposite_depth)
    return min(remaining, default=thickness)
