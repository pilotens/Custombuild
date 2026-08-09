from __future__ import annotations

from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

PAGE_W, PAGE_H = A4
MARGIN = 36
INK = HexColor('#16231c')
MUTED = HexColor('#5f6d65')
ACCENT = HexColor('#257552')
PANEL = HexColor('#eef3ef')


def _name(part: Any) -> str:
    key = str(part.semantic_key).lower()
    if 'left' in key and 'side' in key:
        return 'Vänster gavel'
    if 'right' in key and 'side' in key:
        return 'Höger gavel'
    if 'shelf' in key:
        return 'Hyllplan'
    if 'divider' in key:
        return 'Vertikal avdelare'
    if 'bottom' in key:
        return 'Botten'
    if 'top' in key:
        return 'Topp'
    if 'back' in key:
        return 'Bakstycke'
    if 'plinth' in key:
        return 'Sockel'
    return str(part.semantic_key).replace('_', ' ').title()


def _motion_text(motion: tuple[str, ...]) -> str:
    labels = {
        '+x': 'från vänster mot höger', '-x': 'från höger mot vänster',
        '+y': 'framifrån mot baksidan', '-y': 'bakifrån mot framsidan',
        '+z': 'nedifrån och upp', '-z': 'uppifrån och ned',
    }
    values = [labels[item.lower()] for item in motion if item.lower() in labels]
    return ', sedan '.join(values) if values else 'följ pilens riktning i bilden'


def _draw_part(canvas: Canvas, part: Any, cx: float, cy: float, scale: float, highlighted: bool) -> None:
    size = part.finished_size
    w = max(6, size.width_um / 1000 * scale)
    h = max(6, size.height_um / 1000 * scale)
    canvas.setFillColor(HexColor('#bfe8d2') if highlighted else HexColor('#e4e9e5'))
    canvas.setStrokeColor(ACCENT if highlighted else MUTED)
    canvas.rect(cx - w / 2, cy - h / 2, w, h, stroke=1, fill=1)


def simple_assembly_manual_pdf(design: Any) -> bytes:
    """Generate a consumer-facing, IKEA-like manual from the authoritative AssemblyGraph."""
    from io import BytesIO

    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4, pageCompression=1, invariant=1)
    by_id = {part.part_id: part for part in design.parts}
    joints_by_id = {joint.joint_id: joint for joint in design.joints}

    canvas.setFillColor(INK)
    canvas.setFont('Helvetica-Bold', 22)
    canvas.drawString(MARGIN, PAGE_H - 62, 'Monteringsmanual')
    canvas.setFont('Helvetica', 9)
    canvas.setFillColor(MUTED)
    p = design.spec.parameters
    canvas.drawString(MARGIN, PAGE_H - 82, f'{p.width_um/1000:g} × {p.depth_um/1000:g} × {p.height_um/1000:g} mm · revision {design.spec.revision}')
    canvas.setFont('Helvetica-Bold', 11)
    canvas.setFillColor(INK)
    canvas.drawString(MARGIN, PAGE_H - 118, 'Innan du börjar')
    canvas.setFont('Helvetica', 9)
    intro = [
        '• Sortera delarna efter etiketten/part-ID.',
        '• Kontrollera att alla beslag i BOM finns med.',
        '• Montera på ett rent, plant underlag och skydda synliga ytor.',
        '• Tvinga aldrig ihop en fog. Kontrollera orientering och hålbild först.',
    ]
    y = PAGE_H - 138
    for line in intro:
        canvas.drawString(MARGIN + 6, y, line)
        y -= 16
    canvas.setFillColor(PANEL)
    canvas.roundRect(MARGIN, 90, PAGE_W - 2 * MARGIN, 230, 12, stroke=0, fill=1)
    canvas.setFillColor(INK)
    canvas.setFont('Helvetica-Bold', 12)
    canvas.drawString(MARGIN + 18, 292, 'Delöversikt')
    canvas.setFont('Helvetica', 7.5)
    y = 272
    for part in sorted(design.parts, key=lambda item: item.semantic_key)[:18]:
        canvas.drawString(MARGIN + 18, y, f'{_name(part)}  ·  {part.part_id}')
        y -= 11
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(PAGE_W - MARGIN, 28, f'Designhash {design.design_hash}')
    canvas.showPage()

    completed: set[str] = set()
    for index, step in enumerate(design.assembly_graph.steps, start=1):
        incoming = tuple(step.moving_part_ids)
        if not incoming:
            incoming = tuple(part_id for part_id in step.part_ids if part_id not in completed)
        canvas.setFillColor(INK)
        canvas.setFont('Helvetica-Bold', 20)
        canvas.drawString(MARGIN, PAGE_H - 54, f'Steg {index}')
        canvas.setFont('Helvetica-Bold', 11)
        canvas.drawString(MARGIN, PAGE_H - 82, 'Lägg till')
        canvas.setFont('Helvetica', 9)
        y = PAGE_H - 100
        for part_id in incoming:
            part = by_id.get(part_id)
            label = _name(part) if part is not None else 'Del'
            canvas.drawString(MARGIN + 8, y, f'{label}  ·  {part_id}')
            y -= 14
        canvas.setFont('Helvetica-Bold', 10)
        canvas.drawString(MARGIN, y - 6, 'Gör så här')
        canvas.setFont('Helvetica', 9)
        motion = tuple(
            item.value if hasattr(item, 'value') else str(item) for item in step.motion_path
        )
        canvas.drawString(
            MARGIN + 8, y - 24,
            f'För in den markerade delen {_motion_text(motion)}.',
        )

        canvas.setFillColor(PANEL)
        canvas.roundRect(MARGIN, 205, PAGE_W - 2 * MARGIN, 330, 12, stroke=0, fill=1)
        visible_ids = tuple(dict.fromkeys((*completed, *step.part_ids)))
        visible_parts = [by_id[pid] for pid in visible_ids if pid in by_id]
        max_w = max((part.finished_size.width_um / 1000 for part in visible_parts), default=1)
        max_h = max((part.finished_size.height_um / 1000 for part in visible_parts), default=1)
        scale = min(400 / max_w, 260 / max_h)
        for order, part in enumerate(visible_parts):
            px = MARGIN + 80 + (order % 5) * 88
            py = 420 - (order // 5) * 82
            _draw_part(canvas, part, px, py, scale * 0.16, part.part_id in incoming)
            canvas.setFillColor(INK)
            canvas.setFont('Helvetica', 5.8)
            canvas.drawCentredString(px, py - 28, _name(part)[:22])

        hardware = tuple(
            f'{joint.hardware_count} × {joint.hardware_sku}'
            for joint_id in step.joint_ids
            if (joint := joints_by_id.get(joint_id)) is not None
            and joint.hardware_sku
            and joint.hardware_count > 0
        )
        canvas.setFillColor(INK)
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawString(MARGIN, 176, 'Beslag/verktyg')
        canvas.setFont('Helvetica', 8)
        canvas.drawString(MARGIN + 8, 162, ', '.join(hardware) if hardware else 'Inga extra beslag i detta steg.')
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawString(MARGIN, 134, 'Kontroll')
        canvas.setFont('Helvetica', 8)
        checkpoint = str(getattr(step, 'checkpoint', '') or 'Kontrollera att delen sitter helt i sitt läge och att fogarna ligger an.')
        canvas.drawString(MARGIN + 8, 120, checkpoint[:110])
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(PAGE_W - MARGIN, 28, f'Steg {index} · part-ID är den fysiska referensen')
        canvas.showPage()
        completed.update(step.part_ids)

    canvas.save()
    return buffer.getvalue()
