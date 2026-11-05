"""Revision-bound S1 planning calculations; physical qualification is separate."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .materials import screening_birch_plywood_18
from .measured_assembly import MeasuredAssembly
from .models import FrozenModel
from .sideboard_motion import HingePath, MotionResult, door_motion
from .sideboard_stability_report import BaseTestRecord

PositiveThickness = Annotated[float, Field(strict=True, ge=1, le=60, allow_inf_nan=False)]


class PanelMeasurements(FrozenModel):
    batch: str = Field(min_length=1, max_length=120, pattern=r"\S")
    back_mm: PositiveThickness
    shelf_divider_mm: PositiveThickness
    front_mm: PositiveThickness
    carcass_mm: PositiveThickness


class ShelfMaterialData(FrozenModel):
    source: str = Field(min_length=1, max_length=160, pattern=r"\S")
    density_kg_m3: float = Field(strict=True, ge=100, le=1800, allow_inf_nan=False)
    service_modulus_mpa: float = Field(strict=True, ge=1, le=50000, allow_inf_nan=False)
    allowable_bending_mpa: float = Field(strict=True, gt=0, le=200, allow_inf_nan=False)


class SideboardReviewInputs(FrozenModel):
    base_test: BaseTestRecord | None = None
    shelf_material: ShelfMaterialData | None = None
    shelf_payload_kg: float = Field(default=20, strict=True, ge=0, le=200, allow_inf_nan=False)
    top_payload_kg: float = Field(default=10, strict=True, ge=0, le=200, allow_inf_nan=False)
    horizontal_force_n: float = Field(default=50, strict=True, ge=0, le=1000, allow_inf_nan=False)
    edge_error_mm: float = Field(default=0.2, strict=True, ge=0, le=3, allow_inf_nan=False)
    bore_depth_error_mm: float = Field(default=0.3, strict=True, ge=0, le=3, allow_inf_nan=False)
    measurements: PanelMeasurements | None = None
    hinge_paths: tuple[HingePath, ...] = Field(default=(), max_length=2)
    pin_working_load_n: float | None = Field(default=None, strict=True, gt=0, le=10000)
    pin_source: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")

    @model_validator(mode="after")
    def matched_inputs(self) -> "SideboardReviewInputs":
        if len({p.article for p in self.hinge_paths}) != len(self.hinge_paths):
            raise ValueError("Ange endast en rörelsebana per artikel.")
        if (self.pin_working_load_n is None) != (self.pin_source is None):
            raise ValueError("Hyllbärarens tillåtna brukslast och källa behöver anges tillsammans.")
        return self


class ShelfScreen(FrozenModel):
    part_id: str
    span_mm: float
    deflection_mm: float
    limit_mm: float
    bending_stress_mpa: float
    bending_within_declared_limit: bool | None
    support_demand_n: float
    deflection_within_limit: bool
    pin_within_declared_load: bool | None


class HingeGap(FrozenModel):
    part_id: str
    article: str
    available_mm: float
    required_mm: float | None
    state: Literal["within_table", "insufficient", "outside_table"]


class SideboardAssessment(FrozenModel):
    shelf_screens: tuple[ShelfScreen, ...]
    hinge_gaps: tuple[HingeGap, ...]
    door_motion: tuple[MotionResult, ...]
    batch_state: Literal["missing", "matches_nominal", "revise_dimensions"]
    minimum_divider_web_mm: float
    minimum_front_skin_mm: float
    findings: tuple[str, ...]
    material_profile: str
    total_shelf_payload_kg: float
    physical_cutting_authorized: Literal[False] = False


def assess_sideboard(
    assembly: MeasuredAssembly, inputs: SideboardReviewInputs, dimensions_confirmed: bool
) -> SideboardAssessment:
    material = screening_birch_plywood_18()
    measured = inputs.measurements
    thick = measured.shelf_divider_mm if measured else 18
    front = measured.front_mm if measured else 19
    batch: Literal["missing", "matches_nominal", "revise_dimensions"] = "missing"
    if measured:
        # Geometry is built at nominal thickness. Do not accept an arbitrary shop
        # deviation as matching that geometry; compare at the model's 1 µm precision.
        pairs = [(measured.back_mm, 6), (thick, 18), (front, 19), (measured.carcass_mm, 24)]
        batch = (
            "matches_nominal"
            if all(round(a * 1000) == b * 1000 for a, b in pairs)
            else "revise_dimensions"
        )
    issues = []
    if not dimensions_confirmed:
        issues.append("Bekräfta yttermått och luckindelning för denna revision.")
    if batch != "matches_nominal":
        issues.append(
            "Mät skivbatchens fyra tjocklekar."
            if not measured
            else "Skivbatchen avviker från ritningens tjocklekar. Byt batch eller revidera."
        )
    shelves = []
    e = material.elastic_modulus_mpa * (1 - material.property_uncertainty_permille / 1000)
    creep = 1 + material.creep_factor_permille / 1000
    effective_e = e / creep
    density = material.density_kg_m3
    supplied = inputs.shelf_material
    if supplied:
        effective_e = supplied.service_modulus_mpa
    else:
        issues.append(
            "Hyllberäkningen använder indikativa plywooddata. Ange materialets bruksvärden."
        )
    for p in assembly.parts:
        if not p.id.startswith("SHELF-"):
            continue
        span, depth, _ = (v / 1000 for v in p.size_um)
        own_kg = span * depth * thick * (supplied.density_kg_m3 if supplied else density) / 1e9
        force = (own_kg + inputs.shelf_payload_kg) * 9.81
        delta = 5 * force * span**3 / (32 * effective_e * depth * thick**3)
        stress = 3 * force * span / (4 * depth * thick**2)
        limit = min(3, span / 200)
        # Assume one pin takes the entire reaction at its end of this uniformly loaded shelf.
        demand = force / 2
        shelves.append(
            ShelfScreen(
                part_id=p.id,
                span_mm=span,
                deflection_mm=delta,
                limit_mm=limit,
                bending_stress_mpa=stress,
                bending_within_declared_limit=(
                    stress <= supplied.allowable_bending_mpa if supplied else None
                ),
                support_demand_n=demand,
                deflection_within_limit=delta <= limit,
                pin_within_declared_load=(
                    demand <= inputs.pin_working_load_n
                    if inputs.pin_working_load_n is not None
                    else None
                ),
            )
        )
    if any(not s.deflection_within_limit for s in shelves):
        issues.append("Vald last överskrider gränsen för beräknad hyllnedböjning.")
    if any(s.bending_within_declared_limit is False for s in shelves):
        issues.append("Minst ett hyllplan överskrider materialets angivna böjspänning.")
    if any(s.pin_within_declared_load is not True for s in shelves):
        issues.append(
            "Minst en hyllbärare belastas över angiven brukslast."
            if inputs.pin_working_load_n is not None
            else "Ange artikelns tillåtna brukslast per hyllbärare."
        )
    web = thick - 2 * (8 + inputs.bore_depth_error_mm)
    skin = front - (15 + inputs.bore_depth_error_mm)
    if min(web, skin) <= 0:
        issues.append("Borrtoleransen kan ge genombrott i mellanvägg eller lucka.")
    gaps = []
    available = 3 - 2 * inputs.edge_error_mm
    for i in range(1, 6):
        article = "70T3750.TL" if i == 1 else "70T3650.TL"
        # Blum catalogue 2024/25 p.77, R1, MD0, TB7/TB6. Round thickness
        # upward within this supported interval; include +2 mm adjustment allowance.
        required = None
        for fd, inset, half, allowance in [
            (18, 0.8, 0.8, 0.4),
            (19, 0.9, 0.9, 0.4),
            (20, 1.1, 1.2, 0.5),
        ]:
            if 18 <= front <= fd:
                required = (inset if i == 1 else half) + allowance
                break
        gaps.append(
            HingeGap(
                part_id=f"DOOR-{i}",
                article=article,
                available_mm=available,
                required_mm=required,
                state=(
                    "outside_table"
                    if required is None
                    else "within_table"
                    if available >= required
                    else "insufficient"
                ),
            )
        )
    if any(g.state != "within_table" for g in gaps):
        issues.append("Luckans minsta springa ligger utanför den valda katalogkontrollen.")
    motion = door_motion(assembly, inputs.hinge_paths, 2 * inputs.edge_error_mm)
    if any(m.state != "clear_in_model" for m in motion):
        issues.append("En eller flera luckor saknar en fri beräknad öppningsbana.")
    issues.extend(
        [
            "Verifiera verkliga beslagkroppar, mekanism och passning med valda beslag.",
            "Kvalificera hela stommens last, benförband och tippsäkring.",
        ]
    )
    return SideboardAssessment(
        shelf_screens=tuple(shelves),
        hinge_gaps=tuple(gaps),
        door_motion=motion,
        batch_state=batch,
        minimum_divider_web_mm=web,
        minimum_front_skin_mm=skin,
        findings=tuple(issues),
        material_profile=(
            supplied.source if supplied else material.material_id + "/" + material.version
        ),
        total_shelf_payload_kg=len(shelves) * inputs.shelf_payload_kg,
    )
