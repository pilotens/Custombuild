from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest
from custombuild_cam.production_model import ProductionMove, ProductionMoveKind, ProductionMoveRole
from custombuild_cam.production_verification import (
    CuttingProgramStatus,
    verify_production_toolpaths,
)
from custombuild_cam.toolpaths import generate_production_toolpaths
from custombuild_manufacturing.model import OperationKind

from tests.unit.test_cam_production_toolpaths import _context, _document, _operations


def _distance_squared(
    point: tuple[Fraction, Fraction],
    start: tuple[int, int],
    end: tuple[int, int],
) -> Fraction:
    """Independent exact distance to a finite tool-centre segment."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2
    projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
    fraction = max(Fraction(0), min(Fraction(1), projection))
    return (point[0] - start[0] - fraction * dx) ** 2 + (point[1] - start[1] - fraction * dy) ** 2


@pytest.mark.parametrize("kind", (OperationKind.GROOVE, OperationKind.POCKET))
@pytest.mark.parametrize(
    ("width", "length"),
    ((20_000, 80_000), (80_000, 20_000), (6_001, 40_000), (40_000, 6_001), (6_001, 6_001)),
)
@pytest.mark.parametrize("stepover_ppm", (125_000, 400_000, 850_000))
def test_actual_cutter_sweeps_cover_straight_walls_at_every_depth(
    kind: OperationKind, width: int, length: int, stepover_ppm: int
) -> None:
    operation = replace(
        _operations()[2 if kind == OperationKind.GROOVE else 3],
        width_um=width,
        length_um=length,
        cutter_envelope_width_um=width,
        cutter_envelope_length_um=length,
        depth_um=6_000,
    )
    source = _document(
        operations=tuple(
            operation if item.operation_id == operation.operation_id else item
            for item in _operations()
        )
    )
    context = _context()
    context = replace(
        context,
        recipes=tuple(replace(recipe, stepover_ppm=stepover_ppm) for recipe in context.recipes),
    )
    candidate = generate_production_toolpaths(source, context)
    report = verify_production_toolpaths(candidate, source).report
    assert report.status == CuttingProgramStatus.PASS, report.issues

    radius = 3_000
    x, y = operation.x_um, operation.y_um
    points = []
    for index in range(21):
        fraction = Fraction(index, 20)
        wall_x = x + radius + fraction * (width - 2 * radius)
        wall_y = y + radius + fraction * (length - 2 * radius)
        points.extend(
            (
                (wall_x, Fraction(y)),
                (wall_x, Fraction(y + length)),
                (Fraction(x), wall_y),
                (Fraction(x + width), wall_y),
            )
        )
    for level in (-5_000, -6_000):
        segments = [
            ((start.x_um, start.y_um), (end.x_um, end.y_um))
            for program in candidate.programs
            for start, end in zip(program.moves, program.moves[1:], strict=False)
            if start.kind == end.kind == ProductionMoveKind.LINEAR
            and start.z_um == end.z_um == level
            and start.pass_index == end.pass_index
            and start.operation_id == end.operation_id == operation.operation_id
        ]
        assert segments
        for point in points:
            assert min(_distance_squared(point, start, end) for start, end in segments) <= radius**2


@pytest.mark.parametrize("removed_passes", ((1,), (2,), (1, 2)))
def test_verifier_blocks_uncut_wall_scallops_even_when_all_raster_lanes_remain(
    removed_passes: tuple[int, ...],
) -> None:
    source = _document()
    candidate = generate_production_toolpaths(source, _context())
    programs = []
    for program in candidate.programs:
        moves: list[ProductionMove] = []
        for move in program.moves:
            if (
                move.role == ProductionMoveRole.RETRACT
                and move.pass_index in removed_passes
                and move.operation_id == _operations()[2].operation_id
            ):
                del moves[-4:]
            moves.append(move)
        programs.append(
            replace(
                program, moves=tuple(replace(move, sequence=i) for i, move in enumerate(moves, 1))
            )
        )
    mutant = replace(candidate, programs=tuple(programs))
    assert mutant.fingerprint != candidate.fingerprint
    report = verify_production_toolpaths(mutant, source).report
    assert report.status == CuttingProgramStatus.BLOCK
    assert "MATERIAL_REMOVAL_BOUNDARY_GAP" in {issue.code for issue in report.issues}
    # The previous verifier only checked interior lane spacing and accepted
    # this exact geometry. The new rejection must come from wall coverage.
    assert "MATERIAL_REMOVAL_COVERAGE_INVALID" not in {issue.code for issue in report.issues}
    for pass_index in removed_passes:
        assert any(
            issue.code == "MATERIAL_REMOVAL_BOUNDARY_GAP"
            and f"depth pass {pass_index}" in issue.message
            for issue in report.issues
        )


def test_continuous_split_segments_are_accepted_without_generator_path_identity() -> None:
    source = _document()
    candidate = generate_production_toolpaths(source, _context())
    programs = []
    for program in candidate.programs:
        moves: list[ProductionMove] = []
        for index, move in enumerate(program.moves):
            previous = program.moves[index - 1] if index else None
            if (
                previous is not None
                and previous.kind == move.kind == ProductionMoveKind.LINEAR
                and previous.z_um == move.z_um
                and previous.pass_index == move.pass_index
                and previous.operation_id == move.operation_id == _operations()[2].operation_id
            ):
                midpoint = ((previous.x_um + move.x_um) // 2, (previous.y_um + move.y_um) // 2)
                if midpoint not in {(previous.x_um, previous.y_um), (move.x_um, move.y_um)}:
                    moves.append(replace(move, x_um=midpoint[0], y_um=midpoint[1]))
            moves.append(move)
        programs.append(
            replace(
                program, moves=tuple(replace(move, sequence=i) for i, move in enumerate(moves, 1))
            )
        )
    report = verify_production_toolpaths(
        replace(candidate, programs=tuple(programs)), source
    ).report
    assert report.status == CuttingProgramStatus.PASS, report.issues
