from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from custombuild_manufacturing import (
    canonical_json_bytes,
    dado_retention_evidence_missing,
)
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = float(A4[0]), float(A4[1])
MARGIN = 42
INK = HexColor("#17211c")
MUTED = HexColor("#68746d")
ACCENT = HexColor("#26754f")
WARNING = HexColor("#b36a1d")
BLOCK = HexColor("#b13a3a")
TWO_PERSON_WEIGHT_G = 20_000
TWO_PERSON_LENGTH_UM = 1_800_000
ASSEMBLY_PARTS_PER_PAGE = 10
ASSEMBLY_TOOL_CATALOG: dict[str, tuple[str, bool]] = {
    "hex-key-4mm": ("4 mm insexnyckel", True),
    "mallet": ("Gummiklubba", True),
    # The graph currently names this aid but does not define dimensions,
    # clamping capacity or an approved work instruction.  Keep it visible and
    # unresolved instead of silently treating the identifier as a real jig.
    "panel-positioning-jig": ("Panelpositioneringsjigg", False),
}


@dataclass(frozen=True, slots=True)
class ExplodedBox:
    part_id: str
    semantic_key: str
    moving: bool
    bounds_um: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class AssemblyGroupPlan:
    step_id: str
    moving_part_ids: tuple[str, ...]
    weight_g: int
    bounds_um: tuple[int, int, int, int, int, int]
    max_dimension_um: int
    minimum_people_floor: int
    requires_external_lift_plan: bool


@dataclass(frozen=True, slots=True)
class AssemblyManualPlan:
    groups: tuple[AssemblyGroupPlan, ...]
    tool_ids: tuple[str, ...]
    unresolved_tool_ids: tuple[str, ...]

    @property
    def requires_external_work_preparation(self) -> bool:
        return bool(self.unresolved_tool_ids) or any(
            group.requires_external_lift_plan for group in self.groups
        )


DOCUMENT_RENDERER_VERSION = "reportlab-production-documents-1.3.0"
ASSEMBLY_READINESS_SCHEMA_VERSION = "custombuild.assembly-readiness.v1"


def _canvas() -> tuple[io.BytesIO, Canvas]:
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
    canvas.setTitle("Custombuild production document")
    canvas.setAuthor("Custombuild deterministic document engine")
    return buffer, canvas


def _finish(buffer: io.BytesIO, canvas: Canvas) -> bytes:
    canvas.save()
    return buffer.getvalue()


def _header(canvas: Canvas, title: str, subtitle: str) -> float:
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(MARGIN, PAGE_HEIGHT - MARGIN, title)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(MARGIN, PAGE_HEIGHT - MARGIN - 16, subtitle)
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.5)
    canvas.line(MARGIN, PAGE_HEIGHT - MARGIN - 26, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - MARGIN - 26)
    return PAGE_HEIGHT - MARGIN - 46


def _footer(canvas: Canvas, text: str) -> None:
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 22, text)


def _part_dimensions(part: Any) -> tuple[float, float, float]:
    size = part.finished_size
    return size.width_um / 1000, size.depth_um / 1000, size.height_um / 1000


def bom_pdf(design: Any) -> bytes:
    buffer, canvas = _canvas()
    y = _header(canvas, "Material- och komponentlista", f"Designhash {design.design_hash}")
    canvas.setFont("Helvetica-Bold", 8)
    for x, title in (
        (MARGIN, "Del-ID"),
        (238, "Roll"),
        (338, "Mått mm"),
        (446, "Konservativ bruttovikt"),
    ):
        canvas.drawString(x, y, title)
    y -= 14
    canvas.setFont("Helvetica", 7.5)
    for part in sorted(design.parts, key=lambda item: item.semantic_key):
        if y < 55:
            _footer(canvas, "Screeningunderlag – ej produktcertifiering")
            canvas.showPage()
            y = _header(canvas, "Material- och komponentlista", "Fortsättning")
        width, depth, height = _part_dimensions(part)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN, y, part.part_id[:24])
        canvas.drawString(238, y, part.role.value)
        canvas.drawRightString(448, y, f"{width:g} × {depth:g} × {height:g}")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, y, f"{part.weight_g / 1000:.2f} kg")
        y -= 12
    y -= 6
    canvas.setStrokeColor(HexColor("#d6ddd8"))
    canvas.line(MARGIN, y, PAGE_WIDTH - MARGIN, y)
    y -= 15
    hardware = Counter(
        joint.hardware_sku
        for joint in design.joints
        for _ in range(joint.hardware_count)
        if joint.hardware_sku
    )
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Beslagsidentifierare och retentionskontrakt")
    canvas.setFont("Helvetica", 8)
    retention = getattr(getattr(design, "spec", None), "joint_retention", None)
    retention_resolved = retention is not None and not dado_retention_evidence_missing(design)
    for sku, quantity in sorted(hardware.items()):
        y -= 12
        if retention is not None and retention_resolved and sku == retention.hardware_sku:
            status = (
                f"{retention.system_id}@{retention.system_version} – STRUKTURELLT BUNDET, "
                "KÄLLAUTENTICITET EJ FASTSTÄLLD"
            )
        else:
            status = "EJ VERIFIERAD"
        canvas.drawString(MARGIN, y, f"{sku} – {status}")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, y, str(quantity))
    _footer(canvas, "Genererad deterministiskt från fryst DesignSpec")
    return _finish(buffer, canvas)


def hardware_csv(design: Any) -> bytes:
    """Return exact retention provenance or fail-closed hardware identifiers."""

    quantities: Counter[str] = Counter()
    joint_ids: dict[str, list[str]] = {}
    affected_part_ids: dict[str, set[str]] = {}
    joints_by_sku: dict[str, list[Any]] = {}
    for joint in design.joints:
        if not joint.hardware_sku or joint.hardware_count <= 0:
            continue
        quantities[joint.hardware_sku] += joint.hardware_count
        joint_ids.setdefault(joint.hardware_sku, []).append(joint.joint_id)
        joints_by_sku.setdefault(joint.hardware_sku, []).append(joint)
        affected_part_ids.setdefault(joint.hardware_sku, set()).update(
            member.part_id for member in joint.members
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "hardware_sku",
            "quantity",
            "source_joint_ids",
            "affected_part_ids",
            "catalog_system_id",
            "catalog_system_version",
            "catalog_entry_sha256",
            "catalog_authenticity_status",
            "evidence_id",
            "evidence_sha256",
            "installation_instruction_id",
            "installation_instruction_version",
            "installation_instruction_sha256",
            "applicable_materials",
            "joint_geometry_sha256",
            "minimum_applicable_thickness_um",
            "maximum_applicable_thickness_um",
            "rated_shear_design_load_n",
            "verified_shear_capacity_n",
            "rated_withdrawal_design_load_n",
            "verified_withdrawal_capacity_n",
            "safety_factor_permille",
            "selection_status",
            "required_action",
        )
    )
    for sku, quantity in sorted(quantities.items()):
        joints = joints_by_sku[sku]
        contracts = tuple(getattr(joint, "retention", None) for joint in joints)
        contract = (
            contracts[0]
            if contracts and all(item == contracts[0] for item in contracts)
            else None
        )
        contract_is_bound = (
            contract is not None
            and all(
                str(getattr(joint.joint_type, "value", joint.joint_type)).casefold()
                == "dado"
                for joint in joints
            )
            and not dado_retention_evidence_missing(design)
        )
        if contract is not None and contract_is_bound:
            load_cases = {item.mode.value: item for item in contract.load_cases}
            catalog_fields: tuple[Any, ...] = (
                contract.system_id,
                contract.system_version,
                contract.catalog_entry_sha256,
                "NOT_ESTABLISHED_BY_CURRENT_MVP",
                contract.evidence_id,
                contract.evidence_sha256,
                contract.installation_instruction_id,
                contract.installation_instruction_version,
                contract.installation_instruction_sha256,
                "|".join(
                    f"{item.material_id}@{item.material_version}"
                    for item in contract.applicable_materials
                ),
                contract.joint_geometry_sha256,
                contract.minimum_applicable_thickness_um,
                contract.maximum_applicable_thickness_um,
                load_cases["shear"].rated_design_load_n,
                load_cases["shear"].verified_capacity_n,
                load_cases["withdrawal"].rated_design_load_n,
                load_cases["withdrawal"].verified_capacity_n,
                contract.safety_factor_permille,
            )
            selection_status = "STRUCTURALLY_COMPLETE_RETENTION_APPLICATION"
            required_action = (
                "The structural application is checksum-bound, but the current MVP has no "
                "catalogue trust root and does not establish issuer authenticity. This design "
                "evidence does not authorize physical assembly or cutting."
            )
        else:
            catalog_fields = ("",) * 18
            selection_status = "UNVERIFIED_IDENTIFIER"
            required_action = (
                "Treat this value as an unverified identifier only. Select a versioned "
                "mechanical catalog item and verify capacity and boring pattern; "
                "adhesives are prohibited."
            )
        writer.writerow(
            (
                sku,
                quantity,
                "|".join(sorted(joint_ids[sku])),
                "|".join(sorted(affected_part_ids[sku])),
                *catalog_fields,
                selection_status,
                required_action,
            )
        )
    unresolved_front_ids = _unresolved_front_hardware_part_ids(design)
    if unresolved_front_ids:
        writer.writerow(
            (
                "",
                "",
                "",
                "|".join(unresolved_front_ids),
                *("",) * 18,
                "EXTERNAL_SELECTION_REQUIRED",
                (
                    "Select versioned hinges or other mechanical front hardware and verify "
                    "the exact boring pattern; fronts are not mountable until then."
                ),
            )
        )
    return output.getvalue().encode("utf-8")


def _unresolved_front_hardware_part_ids(design: Any) -> tuple[str, ...]:
    # No current cabinet-front contract binds hardware type, catalogue version,
    # boring pattern or mechanical verification. DADO retention contracts are
    # deliberately scoped to DADO joints and cannot make a front mountable.
    return tuple(
        sorted(
            part.part_id
            for part in design.parts
            if str(getattr(part.role, "value", part.role)) == "cabinet_front"
        )
    )


def assembly_manual_pdf(design: Any) -> bytes:
    buffer, canvas = _canvas()
    steps = design.assembly_graph.steps
    part_by_id = {part.part_id: part for part in design.parts}
    manual_plan = _assembly_manual_plan(design)
    y = _header(
        canvas,
        "START HÄR – monteringsgranskning",
        f"Designhash {design.design_hash}",
    )
    canvas.setFillColor(BLOCK)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(MARGIN, y, "EJ FRISLÄPPT SOM KUNDMANUAL ELLER FÖR FYSISK MONTERING")
    y -= 20
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica", 8)
    y = _draw_wrapped_text(
        canvas,
        "Dokumentet är ett designgranskningsunderlag. Ansvarig montör ska fastställa "
        "lyftplan, arbetsberedning, fixturer, infästningar och lokala säkerhetsåtgärder "
        "innan arbetet påbörjas.",
        MARGIN,
        y,
        max_chars=105,
    )
    y -= 5
    canvas.setFillColor(BLOCK)
    canvas.setFont("Helvetica-Bold", 8)
    y = _draw_wrapped_text(
        canvas,
        "LIMFRI POLICY: lim, fogmassa, epoxy och annan adhesiv låsning är förbjuden. "
        "Ett vanligt DADO-spår visar endast geometri och lokalt upplag. Fysisk montering "
        "kräver verifierad självlåsning eller en demonterbar mekanisk säkring.",
        MARGIN,
        y,
        max_chars=105,
    )
    y -= 5
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Sammanfattning")
    y -= 14
    canvas.setFont("Helvetica", 8)
    canvas.drawString(MARGIN, y, f"Delar: {len(design.parts)} · Monteringssteg: {len(steps)}")
    y -= 12
    canvas.drawString(
        MARGIN,
        y,
        f"Total konservativ bruttovikt: {design.total_weight_g / 1000:.2f} kg",
    )
    y -= 12
    heaviest = max(manual_plan.groups, key=lambda item: item.weight_g, default=None)
    if heaviest is not None:
        canvas.drawString(
            MARGIN,
            y,
            "Tyngsta rörliga grupp: "
            f"{heaviest.weight_g / 1000:.2f} kg · "
            f"maxmått {heaviest.max_dimension_um / 1000:g} mm",
        )
        y -= 12
    unresolved = (
        ", ".join(manual_plan.unresolved_tool_ids)
        if manual_plan.unresolved_tool_ids
        else "Inga i den frysta verktygslistan"
    )
    canvas.drawString(MARGIN, y, "Ej specificerade hjälpmedel: " + unresolved)
    y -= 18
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Verktyg och hjälpmedel från AssemblyGraph")
    y -= 14
    canvas.setFont("Helvetica", 8)
    for tool_id in manual_plan.tool_ids:
        label, specified = ASSEMBLY_TOOL_CATALOG.get(tool_id, (tool_id, False))
        status = "identifierat" if specified else "SPECIFIKATION KRÄVS"
        canvas.drawString(MARGIN, y, f"{tool_id}: {label} – {status}")
        y -= 12
    if not manual_plan.tool_ids:
        canvas.drawString(MARGIN, y, "Inga verktygs-ID:n finns i AssemblyGraph.")
    _footer(canvas, "Designgranskning – fysisk montering kräver separat arbetsberedning")

    if not steps:
        return _finish(buffer, canvas)

    group_by_step = {group.step_id: group for group in manual_plan.groups}
    for index, step in enumerate(steps or (None,), start=1):
        chunks = (
            ((),) if step is None else _assembly_part_chunks(step.part_ids, ASSEMBLY_PARTS_PER_PAGE)
        )
        for chunk_index, part_ids in enumerate(chunks, start=1):
            canvas.showPage()
            title = f"Monteringssteg {index} av {max(1, len(steps))}"
            continuation = f" · del {chunk_index} av {len(chunks)}" if len(chunks) > 1 else ""
            y = _header(
                canvas,
                title + continuation,
                f"Designhash {design.design_hash}",
            )
            if step is None:
                canvas.drawString(MARGIN, y, "Inga separata förbandssteg krävs.")
                _footer(canvas, "Följ alltid arbetsplatsens lyft-, kläm- och verktygsinstruktioner")
                continue
            chunk_parts = tuple(part_by_id[part_id] for part_id in part_ids)
            moving_part_ids = frozenset(step.moving_part_ids) & frozenset(part_ids)
            canvas.setFillColor(INK)
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawString(
                MARGIN,
                y,
                "Monteringsbana: "
                + " -> ".join(segment.value for segment in step.motion_path)
                + " (högerhänt X/Y/Z-system)",
            )
            y -= 18
            canvas.setFont("Helvetica", 8)
            canvas.drawString(MARGIN, y, f"Steg-ID: {step.step_id}")
            y -= 14
            canvas.setFont("Helvetica-Bold", 7.5)
            canvas.drawString(MARGIN, y, "Exakta part-ID:n")
            y -= 11
            y = _draw_part_id_table(
                canvas,
                chunk_parts,
                moving_part_ids,
                y,
            )
            canvas.setFont("Helvetica", 7)
            canvas.drawString(
                MARGIN,
                y,
                f"AssemblyGraph-förband i steget: {len(step.joint_ids)}; "
                "varje förband validerat exakt en gång.",
            )
            y -= 12
            group_plan = group_by_step[step.step_id]
            canvas.drawString(
                MARGIN,
                y,
                "Rörlig grupp: "
                f"{group_plan.weight_g / 1000:.2f} kg · "
                f"maxmått {group_plan.max_dimension_um / 1000:g} mm",
            )
            y -= 12
            people_text = (
                "minst två personer; slutligt antal och lyfthjälpmedel fastställs i lyftplan"
                if group_plan.requires_external_lift_plan
                else "en person enligt den konservativa MVP-gränsen; lokal riskbedömning krävs"
            )
            canvas.drawString(MARGIN, y, "Bemanning: " + people_text)
            y -= 12
            tools = ", ".join(step.tool_ids) if step.tool_ids else "Inga handverktyg angivna"
            canvas.drawString(MARGIN, y, "Verktyg: " + tools)
            y -= 12
            hardware_text = _assembly_step_hardware_text(design, step)
            canvas.drawString(MARGIN, y, "Beslag: " + hardware_text)
            y -= 12
            canvas.setFillColor(MUTED)
            canvas.setFont("Helvetica", 6.5)
            canvas.drawString(
                MARGIN,
                y,
                "IN = inkommande grupp, gemensamt förskjuten motsatt monteringsriktningen; "
                "FIX = befintlig delgrupp.",
            )
            y -= 8
            _draw_exploded_parts(
                canvas,
                chunk_parts,
                moving_part_ids,
                step.motion_path,
                top=y,
                height=245,
            )
            y -= 258
            canvas.setFillColor(ACCENT)
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawString(MARGIN, y, "Kontrollpunkt")
            canvas.setFillColor(INK)
            canvas.setFont("Helvetica", 8)
            y = _draw_wrapped_text(canvas, step.checkpoint, MARGIN, y - 13, max_chars=105)
            y -= 5
            canvas.setFillColor(BLOCK)
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawString(MARGIN, y, "Kritiska varningar")
            y -= 12
            canvas.setFillColor(INK)
            canvas.setFont("Helvetica", 7)
            y = _draw_wrapped_text(
                canvas,
                "Klämrisk i monteringsriktningen: stöd och säkra delarna, "
                "och håll händer borta från förbandslinjen.",
                MARGIN,
                y,
                max_chars=118,
                leading=10,
            )
            if group_plan.requires_external_lift_plan:
                _draw_wrapped_text(
                    canvas,
                    "LYFTPLAN KRÄVS för hela den rörliga gruppen enligt konservativ "
                    "MVP-tröskel (grupp ≥20 kg eller gruppmått ≥1800 mm). Enskilda "
                    "delvarningar ersätter inte gruppens arbetsberedning.",
                    MARGIN,
                    y,
                    max_chars=118,
                    leading=10,
                )
            _footer(canvas, "Följ alltid arbetsplatsens lyft-, kläm- och verktygsinstruktioner")
    return _finish(buffer, canvas)


def assembly_readiness_json(design: Any) -> bytes:
    """Return a machine-readable, fail-closed assembly review summary."""

    plan = _assembly_manual_plan(design)
    missing_requirements = [
        "named_assembly_safety_approver",
        "approved_local_work_preparation",
    ]
    if dado_retention_evidence_missing(design):
        missing_requirements.append("verified_adhesive_free_joint_retention")
    missing_requirements.extend(
        f"verified_cabinet_front_hardware:{part_id}"
        for part_id in _unresolved_front_hardware_part_ids(design)
    )
    missing_requirements.extend(
        f"approved_lift_plan:{group.step_id}"
        for group in plan.groups
        if group.requires_external_lift_plan
    )
    missing_requirements.extend(
        f"approved_tool_specification:{tool_id}" for tool_id in plan.unresolved_tool_ids
    )
    payload = {
        "schema_version": ASSEMBLY_READINESS_SCHEMA_VERSION,
        "design_hash": design.design_hash,
        "release_scope": "design_review",
        "customer_assembly_authorized": False,
        "physical_assembly_authorized": False,
        "requires_external_work_preparation": True,
        "missing_requirements": tuple(sorted(missing_requirements)),
        "groups": tuple(
            {
                "step_id": group.step_id,
                "moving_part_ids": group.moving_part_ids,
                "weight_g": group.weight_g,
                "bounds_um": group.bounds_um,
                "max_dimension_um": group.max_dimension_um,
                "minimum_people_floor": group.minimum_people_floor,
                "requires_external_lift_plan": group.requires_external_lift_plan,
            }
            for group in plan.groups
        ),
        "tools": tuple(
            {
                "tool_id": tool_id,
                "label": ASSEMBLY_TOOL_CATALOG.get(tool_id, (tool_id, False))[0],
                "specification_status": (
                    "IDENTIFIED"
                    if tool_id in ASSEMBLY_TOOL_CATALOG and ASSEMBLY_TOOL_CATALOG[tool_id][1]
                    else "EXTERNAL_SPECIFICATION_REQUIRED"
                ),
            }
            for tool_id in plan.tool_ids
        ),
    }
    return canonical_json_bytes(payload)


def _assembly_part_chunks(
    part_ids: tuple[str, ...],
    limit: int = ASSEMBLY_PARTS_PER_PAGE,
) -> tuple[tuple[str, ...], ...]:
    if limit <= 0:
        raise ValueError("assembly page part limit must be positive")
    if not part_ids:
        return ((),)
    return tuple(part_ids[offset : offset + limit] for offset in range(0, len(part_ids), limit))


def _draw_part_id_table(
    canvas: Canvas,
    parts: tuple[Any, ...],
    moving_part_ids: frozenset[str],
    y: float,
) -> float:
    row_height = 10
    column_width = (PAGE_WIDTH - 2 * MARGIN) / 2
    for index, part in enumerate(parts):
        column = index % 2
        row = index // 2
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(INK)
        canvas.drawString(
            MARGIN + column * column_width,
            y - row * row_height,
            f"{'IN ' if part.part_id in moving_part_ids else 'FIX '}"
            f"{part.semantic_key} · {part.part_id}",
        )
    rows = (len(parts) + 1) // 2
    return y - rows * row_height - 2


def _draw_wrapped_text(
    canvas: Canvas,
    value: str,
    x: float,
    y: float,
    *,
    max_chars: int,
    leading: float = 11,
) -> float:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    for line in lines:
        canvas.drawString(x, y, line)
        y -= leading
    return y


def _assembly_step_hardware(design: Any, step: Any) -> tuple[tuple[str, int], ...]:
    selected = set(step.joint_ids)
    quantities: Counter[str] = Counter()
    for joint in design.joints:
        if joint.joint_id in selected and joint.hardware_sku and joint.hardware_count:
            quantities[joint.hardware_sku] += joint.hardware_count
    return tuple(sorted(quantities.items()))


def _assembly_step_hardware_text(design: Any, step: Any) -> str:
    unresolved_fronts = set(_unresolved_front_hardware_part_ids(design)) & set(step.part_ids)
    if unresolved_fronts:
        return "FRONT EJ MONTERINGSBAR: verifierat mekaniskt beslag och borrbild saknas"
    selected_joint_ids = set(step.joint_ids)
    selected_joints = tuple(
        joint for joint in design.joints if joint.joint_id in selected_joint_ids
    )
    retention_contracts = tuple(
        joint.retention
        for joint in selected_joints
        if getattr(joint, "retention", None) is not None
    )
    if retention_contracts and not dado_retention_evidence_missing(design):
        identities = ", ".join(
            sorted(
                {
                    f"{contract.system_id}@{contract.system_version}"
                    for contract in retention_contracts
                }
            )
        )
        return (
            f"VERSIONSBUNDET RETENTIONSSYSTEM: {identities}; följ endast den "
            "checksum-bundna installationsinstruktionen. Källautenticitet fastställs inte "
            "av nuvarande MVP och fysisk montering är inte frisläppt av detta designbevis"
        )
    hardware = _assembly_step_hardware(design, step)
    if hardware:
        identifiers = ", ".join(f"{sku} × {quantity}" for sku, quantity in hardware)
        return (
            f"EJ VERIFIERAT BESLAG: {identifiers}; versionsbundet mekaniskt "
            "katalogbevis och borrbild saknas"
        )
    return (
        "Inga lösa beslag. Spårgeometrin visar lokalt upplag men är inte verifierad "
        "som permanent låsning"
    )


def _two_person_lift_parts(parts: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(
        part.part_id
        for part in parts
        if part.weight_g >= TWO_PERSON_WEIGHT_G
        or max(
            part.finished_size.width_um,
            part.finished_size.depth_um,
            part.finished_size.height_um,
        )
        >= TWO_PERSON_LENGTH_UM
    )


def _assembly_group_plan(part_by_id: dict[str, Any], step: Any) -> AssemblyGroupPlan:
    moving_ids = tuple(sorted(step.moving_part_ids))
    if not moving_ids:
        raise ValueError("assembly step has no moving group")
    try:
        parts = tuple(part_by_id[part_id] for part_id in moving_ids)
    except KeyError as exc:
        raise ValueError("assembly step references a missing moving part") from exc
    x0 = min(part.placement.x_um for part in parts)
    y0 = min(part.placement.y_um for part in parts)
    z0 = min(part.placement.z_um for part in parts)
    x1 = max(part.placement.x_um + part.finished_size.width_um for part in parts)
    y1 = max(part.placement.y_um + part.finished_size.depth_um for part in parts)
    z1 = max(part.placement.z_um + part.finished_size.height_um for part in parts)
    max_dimension_um = max(x1 - x0, y1 - y0, z1 - z0)
    weight_g = sum(part.weight_g for part in parts)
    requires_lift_plan = weight_g >= TWO_PERSON_WEIGHT_G or max_dimension_um >= TWO_PERSON_LENGTH_UM
    return AssemblyGroupPlan(
        step_id=step.step_id,
        moving_part_ids=moving_ids,
        weight_g=weight_g,
        bounds_um=(x0, y0, z0, x1, y1, z1),
        max_dimension_um=max_dimension_um,
        minimum_people_floor=2 if requires_lift_plan else 1,
        requires_external_lift_plan=requires_lift_plan,
    )


def _assembly_manual_plan(design: Any) -> AssemblyManualPlan:
    part_by_id = {part.part_id: part for part in design.parts}
    groups = tuple(_assembly_group_plan(part_by_id, step) for step in design.assembly_graph.steps)
    tool_ids = tuple(
        sorted({tool_id for step in design.assembly_graph.steps for tool_id in step.tool_ids})
    )
    unresolved = tuple(
        tool_id
        for tool_id in tool_ids
        if tool_id not in ASSEMBLY_TOOL_CATALOG or not ASSEMBLY_TOOL_CATALOG[tool_id][1]
    )
    return AssemblyManualPlan(
        groups=groups,
        tool_ids=tool_ids,
        unresolved_tool_ids=unresolved,
    )


def _direction_vector(direction: Any) -> tuple[int, int, int]:
    return {
        "+x": (1, 0, 0),
        "-x": (-1, 0, 0),
        "+y": (0, 1, 0),
        "-y": (0, -1, 0),
        "+z": (0, 0, 1),
        "-z": (0, 0, -1),
    }[str(getattr(direction, "value", direction))]


def _exploded_boxes(
    parts: Iterable[Any],
    moving_part_ids: frozenset[str],
    motion_path: tuple[Any, ...],
) -> tuple[ExplodedBox, ...]:
    values = tuple(parts)
    if not values:
        return ()
    available_ids = {part.part_id for part in values}
    if not moving_part_ids <= available_ids:
        raise ValueError("exploded moving group must be a subset of the rendered step parts")
    if not motion_path:
        raise ValueError("exploded motion path must contain at least one segment")
    motion_vectors = tuple(_direction_vector(direction) for direction in motion_path)
    largest_dimension = max(
        dimension
        for part in values
        for dimension in (
            part.finished_size.width_um,
            part.finished_size.depth_um,
            part.finished_size.height_um,
        )
    )
    gap_um = max(80_000, largest_dimension // 12)
    boxes: list[ExplodedBox] = []
    for part in values:
        moving = part.part_id in moving_part_ids
        # Assembly direction describes motion into the final position.  The
        # exploded start position is therefore one common offset in the exact
        # opposite direction, preserving incoming subassembly relationships.
        offset_x = -gap_um * sum(vector[0] for vector in motion_vectors) if moving else 0
        offset_y = -gap_um * sum(vector[1] for vector in motion_vectors) if moving else 0
        offset_z = -gap_um * sum(vector[2] for vector in motion_vectors) if moving else 0
        x0 = part.placement.x_um + offset_x
        y0 = part.placement.y_um + offset_y
        z0 = part.placement.z_um + offset_z
        boxes.append(
            ExplodedBox(
                part_id=part.part_id,
                semantic_key=part.semantic_key,
                moving=moving,
                bounds_um=(
                    x0,
                    y0,
                    z0,
                    x0 + part.finished_size.width_um,
                    y0 + part.finished_size.depth_um,
                    z0 + part.finished_size.height_um,
                ),
            )
        )
    return tuple(boxes)


def _project_point(x_um: int, y_um: int, z_um: int) -> tuple[float, float]:
    # Deterministic dimetric projection of the authoritative axis-aligned boxes.
    return (x_um - 0.58 * y_um, z_um + 0.24 * x_um + 0.18 * y_um)


def _box_points(box: ExplodedBox) -> tuple[tuple[float, float], ...]:
    x0, y0, z0, x1, y1, z1 = box.bounds_um
    return tuple(
        _project_point(x, y, z)
        for x, y, z in (
            (x0, y0, z0),
            (x1, y0, z0),
            (x1, y1, z0),
            (x0, y1, z0),
            (x0, y0, z1),
            (x1, y0, z1),
            (x1, y1, z1),
            (x0, y1, z1),
        )
    )


def _draw_exploded_parts(
    canvas: Canvas,
    parts: Iterable[Any],
    moving_part_ids: frozenset[str],
    motion_path: tuple[Any, ...],
    *,
    top: float,
    height: float,
) -> None:
    boxes = _exploded_boxes(parts, moving_part_ids, motion_path)
    if not boxes:
        return
    projected = tuple((box, _box_points(box)) for box in boxes)
    min_x = min(point[0] for _, points in projected for point in points)
    max_x = max(point[0] for _, points in projected for point in points)
    min_y = min(point[1] for _, points in projected for point in points)
    max_y = max(point[1] for _, points in projected for point in points)
    available_width = PAGE_WIDTH - 2 * MARGIN - 20
    scale = min(
        available_width / max(1, max_x - min_x),
        (height - 18) / max(1, max_y - min_y),
    )
    left = MARGIN + 10
    bottom = top - height

    def page_point(point: tuple[float, float]) -> tuple[float, float]:
        return (
            left + (point[0] - min_x) * scale,
            bottom + 8 + (point[1] - min_y) * scale,
        )

    faces = (
        ((0, 1, 5, 4), HexColor("#dcece4")),
        ((1, 2, 6, 5), HexColor("#b9d7c7")),
        ((4, 5, 6, 7), HexColor("#edf6f1")),
    )
    canvas.saveState()
    canvas.setLineWidth(0.55)
    for box, points in sorted(
        projected,
        key=lambda item: sum(point[1] for point in item[1]) / len(item[1]),
    ):
        mapped = tuple(page_point(point) for point in points)
        for indices, fill in faces:
            path = canvas.beginPath()
            first_x, first_y = mapped[indices[0]]
            path.moveTo(first_x, first_y)
            for point_index in indices[1:]:
                point_x, point_y = mapped[point_index]
                path.lineTo(point_x, point_y)
            path.close()
            canvas.setFillColor(fill)
            canvas.setStrokeColor(ACCENT)
            canvas.drawPath(path, stroke=1, fill=1)
        label_x = sum(point[0] for point in mapped) / len(mapped)
        label_y = sum(point[1] for point in mapped) / len(mapped)
        canvas.setFillColor(BLOCK if box.moving else INK)
        canvas.setFont("Helvetica-Bold", 5.5)
        prefix = "IN " if box.moving else ""
        canvas.drawCentredString(label_x, label_y, prefix + box.semantic_key)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 6)
    canvas.drawRightString(
        PAGE_WIDTH - MARGIN,
        bottom,
        "Monteringsbana "
        + " -> ".join(str(getattr(direction, "value", direction)) for direction in motion_path),
    )
    canvas.restoreState()


def labels_pdf(design: Any) -> bytes:
    buffer, canvas = _canvas()
    label_width = (PAGE_WIDTH - 2 * MARGIN - 12) / 2
    label_height = 112
    for index, part in enumerate(sorted(design.parts, key=lambda item: item.part_id)):
        slot = index % 8
        if slot == 0 and index:
            _footer(canvas, "Etiketter – verifiera del-ID före bearbetning och montering")
            canvas.showPage()
        column = slot % 2
        row = slot // 2
        x = MARGIN + column * (label_width + 12)
        y = PAGE_HEIGHT - MARGIN - (row + 1) * label_height
        canvas.setStrokeColor(HexColor("#aab5ae"))
        canvas.rect(x, y, label_width, label_height - 8)
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(x + 8, y + label_height - 25, part.semantic_key)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(x + 8, y + label_height - 39, part.part_id)
        width, depth, height = _part_dimensions(part)
        canvas.drawString(x + 8, y + label_height - 52, f"{width:g} × {depth:g} × {height:g} mm")
        canvas.drawString(
            x + 8,
            y + label_height - 65,
            f"{part.material_id}@{part.material_version} · design {design.design_hash[:12]}",
        )
        widget = qr.QrCodeWidget(_label_qr_payload(design.design_hash, part.part_id))
        bounds = widget.getBounds()
        drawing = Drawing(
            54,
            54,
            transform=[54 / (bounds[2] - bounds[0]), 0, 0, 54 / (bounds[3] - bounds[1]), 0, 0],
        )
        drawing.add(widget)
        renderPDF.draw(drawing, canvas, x + label_width - 64, y + 9)
    _footer(canvas, "Etiketter – verifiera del-ID före bearbetning och montering")
    return _finish(buffer, canvas)


def _label_qr_payload(design_hash: str, part_id: str) -> str:
    """Bind a physical label to the immutable design and exact instance."""

    return f"custombuild:part:{design_hash}:{part_id}:001"


def _rule_threshold_label(evaluation: Any) -> str:
    rule_id = str(evaluation.rule_id).upper()
    if rule_id == "CB-TIP-001":
        return "minimikrav"
    if rule_id == "CB-JOINT-001":
        return "verifierad lokal kapacitet"
    if any(token in rule_id for token in ("DEFLECTION", "BENDING", "STABILITY")):
        return "högsta tillåtna"
    if any(token in rule_id for token in ("HARDWARE", "SUPPORT")):
        return "kravvärde"
    return "gränsvärde"


def validation_report_pdf(rule_report: Any, dfm_report: Any | None = None) -> bytes:
    buffer, canvas = _canvas()
    y = _header(
        canvas,
        "Valideringsrapport",
        "Deterministisk screening och DFM – inte produktcertifiering",
    )
    canvas.setFont("Helvetica-Bold", 11)
    status = str(getattr(rule_report.overall_status, "value", rule_report.overall_status))
    canvas.setFillColor(_status_color(status))
    canvas.drawString(MARGIN, y, f"Konstruktionsstatus: {status}")
    y -= 22
    for evaluation in rule_report.evaluations:
        if y < 90:
            canvas.showPage()
            y = _header(canvas, "Valideringsrapport", "Fortsättning")
        item_status = str(getattr(evaluation.status, "value", evaluation.status))
        canvas.setFillColor(_status_color(item_status))
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(MARGIN, y, f"{evaluation.rule_id} · {item_status} · {evaluation.title}")
        y -= 12
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(
            MARGIN + 8,
            y,
            f"Beräknat {evaluation.calculated_value} {evaluation.unit}; "
            f"{_rule_threshold_label(evaluation)} {evaluation.allowed_value}",
        )
        y -= 18
    if dfm_report is not None:
        canvas.setFont("Helvetica-Bold", 10)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN, y, "DFM")
        y -= 14
        for issue in getattr(dfm_report, "issues", ()):
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(
                _status_color(str(getattr(issue.severity, "value", issue.severity)))
            )
            canvas.drawString(MARGIN, y, f"{issue.code}: {issue.message}"[:120])
            y -= 11
    _footer(canvas, "BLOCK måste åtgärdas innan frisläppning")
    return _finish(buffer, canvas)


def qa_protocol_pdf(design: Any) -> bytes:
    buffer, canvas = _canvas()
    y = _header(canvas, "QA- och mätprotokoll", f"Designhash {design.design_hash}")
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(
        MARGIN,
        y,
        "Tomma fält är avsiktliga. Ingen del eller mätning är förhandsgodkänd.",
    )
    y -= 18
    canvas.setFont("Helvetica", 8)
    for part in sorted(design.parts, key=lambda item: item.semantic_key):
        if y < 80:
            _footer(canvas, "Fyll i mätvärde, resultat, signatur och datum efter kontroll")
            canvas.showPage()
            y = _header(canvas, "QA- och mätprotokoll", "Fortsättning")
        width, depth, height = _part_dimensions(part)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN, y, part.part_id[:26])
        canvas.drawString(250, y, f"{width:g} × {depth:g} × {height:g} mm")
        y -= 11
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN + 14, y, "Mätt B/D/H: ______ / ______ / ______ mm")
        canvas.drawString(300, y, "Resultat: __________")
        canvas.drawString(420, y, "Sign./datum: ______________")
        y -= 17
    _footer(canvas, "Fyll i mätvärde, resultat, signatur och datum efter kontroll")
    return _finish(buffer, canvas)


def _status_color(status: str) -> Any:
    return (
        BLOCK if status.upper() == "BLOCK" else WARNING if status.upper() == "WARNING" else ACCENT
    )
