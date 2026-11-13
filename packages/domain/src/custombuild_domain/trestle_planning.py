"""Explicit design inputs and reproducible sizing for the bolted trestle family.

Species means support a deflection estimate, not a certified load rating. A
selected construction is a new design; it is not recovered hidden photo detail.
"""

from __future__ import annotations

from math import ceil
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import FrozenModel


class TrestleRequest(FrozenModel):
    length_mm: Annotated[int, Field(strict=True, ge=1800, le=2600)] = 2200
    width_mm: Annotated[int, Field(strict=True, ge=850, le=1100)] = 950
    height_mm: Annotated[int, Field(strict=True, ge=700, le=800)] = 750
    material: Literal["american-black-walnut"] = "american-black-walnut"
    central_load_n: Annotated[int, Field(strict=True, ge=100, le=1500)] = 300
    moisture_min_pct: Annotated[int, Field(strict=True, ge=6, le=13)] = 6
    moisture_max_pct: Annotated[int, Field(strict=True, ge=7, le=14)] = 12
    dimensions_confirmed: bool = False

    @model_validator(mode="after")
    def moisture_interval(self) -> TrestleRequest:
        if self.moisture_min_pct >= self.moisture_max_pct:
            raise ValueError("Fuktkvotens minvärde måste understiga maxvärdet.")
        return self


class TrestleSizing(FrozenModel):
    top_thickness_mm: int
    span_mm: int
    strip_width_mm: Literal[300] = 300
    elastic_modulus_mpa: float
    predicted_deflection_mm: float
    deflection_target_mm: float
    bending_stress_mpa: float
    width_change_mm: float
    mounting_travel_each_way_mm: float
    mounting_slot_length_mm: int
    load_rating_verified: Literal[False] = False
    assumptions: tuple[str, ...]
    sources: tuple[str, ...]


def size_trestle(request: TrestleRequest) -> TrestleSizing:
    span = request.length_mm - 710
    elastic = 11_584 * 0.6
    target = span / 300
    for thickness in (32, 36, 40, 45, 50, 55, 60, 65, 70):
        inertia = 300 * thickness**3 / 12
        own_weight_n_mm = 609 * 9.81 * (300 * thickness) / 1e9
        deflection = 1.5 * (
            request.central_load_n * span**3 / (48 * elastic * inertia)
            + 5 * own_weight_n_mm * span**4 / (384 * elastic * inertia)
        )
        stress = (
            (request.central_load_n * span / 4 + own_weight_n_mm * span**2 / 8)
            * thickness
            / (2 * inertia)
        )
        if deflection <= target:
            break
    else:
        raise ValueError("Last och spännvidd kräver en annan konstruktion än denna bordfamilj.")
    # USDA table 13-5: black walnut tangential dimensional change per MC %-point.
    # Use the complete dry/wet interval in EACH direction from a centred mounting:
    # this also covers assembly at either endpoint, plus 1 mm positioning allowance.
    delta_mc = request.moisture_max_pct - request.moisture_min_pct
    travel = 150 * 0.00274 * delta_mc + 1
    return TrestleSizing(
        top_thickness_mm=thickness,
        span_mm=span,
        elastic_modulus_mpa=elastic,
        predicted_deflection_mm=deflection,
        deflection_target_mm=target,
        bending_stress_mpa=stress,
        width_change_mm=request.width_mm * 0.00274 * delta_mc,
        mounting_travel_each_way_mm=travel,
        mounting_slot_length_mm=ceil(9 + 2 * travel),
        assumptions=(
            "Amerikansk svartvalnöt, Juglans nigra, fibrer längs respektive del.",
            "En fritt upplagd 300 mm bred skivremsa, centrisk punktlast och egentyngd. "
            "Längsbalkens stöd tillgodoräknas inte. Kantlast ingår inte.",
            "Artmedel E = 11584 MPa reduceras med 40 %. Nedböjningen multipliceras "
            "med 1,5. Dessa är redovisade screeningantaganden, inte materialklassning.",
            "Mål för uppskattad nedböjning L/300. Böjspänning redovisas utan "
            "godkännande mot en påhittad tillåten spänning.",
            "Fuktrörelse enligt USDA tabell 13-5, tangentiellt 0,00274 per procentenhet. "
            "Centrum fixeras; fyra slitsar medger rörelse tvärs fibrerna.",
            "Längsrörelse antas till 0,0001 per procentenhet. Samtidig rörelse längs "
            "och tvärs fibrerna samt 1 mm positionsmån kontrolleras mot slits och brickficka.",
        ),
        sources=(
            "https://www.americanhardwood.org/en/american-hardwood/american-walnut",
            "https://research.fs.usda.gov/download/treesearch/62261.pdf",
            "https://research.fs.usda.gov/download/treesearch/62251.pdf",
        ),
    )
