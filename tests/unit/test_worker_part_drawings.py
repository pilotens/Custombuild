from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import custombuild_worker.part_drawings as drawings
import pytest
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    build_bookcase,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import FeatureKind, ManufacturingFeature, PartSpec, Side
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


def record_pdf(monkeypatch, design):
    """Observe real PDF drawing commands without requiring a second PDF library."""
    text = []
    circles = []

    class RecordingCanvas(Canvas):
        def drawString(self, x, y, value, *args, **kwargs):
            text.append((self.getPageNumber(), x, y, value))
            return super().drawString(x, y, value, *args, **kwargs)

        def circle(self, x, y, r, *args, **kwargs):
            circles.append((self.getPageNumber(), x, y, r))
            return super().circle(x, y, r, *args, **kwargs)

    monkeypatch.setattr(drawings, "Canvas", RecordingCanvas)
    return drawings.part_drawings_pdf(design), text, circles


def test_real_design_has_deterministic_pdf_with_every_part_and_both_faces(monkeypatch):
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="workshop-drawings",
            revision=3,
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    payload, text, _ = record_pdf(monkeypatch, design)
    assert payload.startswith(b"%PDF-")
    assert payload == drawings.part_drawings_pdf(design)
    for part in design.parts:
        for side in ("A", "B"):
            assert any(value == f"DXF: parts/{part.part_id}/{side}.dxf" for _, _, _, value in text)
    for _, x, y, value in text:
        assert x >= drawings.MARGIN
        assert 24 <= y <= 806
        assert x + stringWidth(value, "Helvetica", 7) <= drawings.PAGE_WIDTH - 20
    assert any(value == f"Revision 3 | Design {design.design_hash}" for _, _, _, value in text)
    assert any("Tolerans: EJ ANGIVEN" in value for _, _, _, value in text)
    assert any("Tolerans: +/- 0.05 mm" in value for _, _, _, value in text)


def test_fractional_b_face_hole_centres_are_not_mirrored_or_rounded(monkeypatch):
    feature = ManufacturingFeature(
        feature_id="three-holes",
        part_id="panel",
        kind=FeatureKind.DRILL_PATTERN,
        side=Side.B,
        x_um=33_375,
        y_um=77_625,
        depth_um=8_125,
        diameter_um=6_000,
        pattern_count=3,
        pitch_um=32_250,
        tolerance_um=75,
        fit_clearance_um=125,
    )
    part = PartSpec(
        part_id="panel",
        name="Panel",
        width_um=300_000,
        height_um=150_000,
        thickness_um=18_000,
        material_id="mdf",
        material_version="fixture",
        features=(feature,),
    )
    monkeypatch.setattr(drawings, "adapt_design_result", lambda _: SimpleNamespace(parts=(part,)))
    design = SimpleNamespace(spec=SimpleNamespace(revision=1), design_hash="d" * 64)
    _, text, circles = record_pdf(monkeypatch, design)
    side_b_text = "\n".join(value for page, _, _, value in text if page == 2)
    assert "U 33.375 / V 77.625 mm - första hålets centrum" in side_b_text
    assert "Diameter 6 | Djup 8.125 mm" in side_b_text
    assert "Tolerans: +/- 0.075 mm | Passningsspel: 0.125 mm" in side_b_text
    assert "33.375/77.625; 65.625/77.625; 97.875/77.625" in side_b_text
    # The drawn points must stay at their declared U coordinates on B, not 300-U.
    holes = [(x, y, r) for page, x, y, r in circles if page == 2 and r != 2]
    expected = [(116 + u * 1.35, 455.25 + 77.625 * 1.35, 4.05) for u in (33.375, 65.625, 97.875)]
    assert len(holes) == 3
    for actual, wanted in zip(holes, expected, strict=True):
        assert actual == pytest.approx(wanted)
    assert not any("three-holes" in value for page, _, _, value in text if page == 1)


def test_groove_depth_fit_and_reliefs_are_readable_without_rounding():
    feature = ManufacturingFeature(
        feature_id="groove",
        part_id="panel",
        kind=FeatureKind.GROOVE,
        side=Side.A,
        x_um=0,
        y_um=36_750,
        depth_um=6_125,
        width_um=300_000,
        length_um=18_250,
        radius_um=3_000,
        corner_strategy="dogbone-v2",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min", "u_max"),
        tolerance_um=50,
        fit_clearance_um=250,
    )
    lines = drawings.feature_note_lines(feature)
    assert "U 0 / V 36.75 mm - nedre vänstra hörnet" in lines
    assert "Bredd U 300 | Längd V 18.25 | Radie 3 | Djup 6.125 mm" in lines
    assert "Tolerans: +/- 0.05 mm | Passningsspel: 0.25 mm" in lines
    assert any("Avlastningsradie: 3 mm | Öppna ändar: u_min, u_max" in line for line in lines)
    assert any(
        "EJ ANGIVEN" in line
        for line in drawings.feature_note_lines(replace(feature, tolerance_um=0))
    )


def test_long_drill_schedule_is_paginated_without_losing_centres_or_identity():
    feature = ManufacturingFeature(
        feature_id="long-feature-" + "x" * 3_000,
        part_id="panel",
        kind=FeatureKind.DRILL_PATTERN,
        side=Side.A,
        x_um=20_125,
        y_um=30_875,
        depth_um=6_000,
        diameter_um=5_000,
        pattern_count=300,
        pitch_um=32_000,
    )
    pages = drawings._note_pages((feature,))
    assert len(pages) > 2
    assert all(page for page in pages)
    for page in pages:
        assert sum(len(note.lines) + 1 for note in page) <= drawings.NOTE_LINES_PER_PAGE
        assert all(
            stringWidth(line, "Helvetica", 8) <= drawings.NOTE_WIDTH
            for note in page
            for line in note.lines
        )
    actual = "".join(
        line
        for page in pages
        for note in page
        for line in note.lines
        if line != "F01  Fortsättning"
    ).replace(" ", "")
    assert feature.feature_id in actual
    for index in range(300):
        assert f"{20.125 + index * 32:.3f}/30.875" in actual
