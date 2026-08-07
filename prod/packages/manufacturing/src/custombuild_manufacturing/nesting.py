"""Reproducible bottom-left nesting for rectangular sheet parts."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import NestingError
from .model import (
    DFMIssue,
    NestingLayout,
    PartInstance,
    PartSpec,
    Placement,
    Severity,
    StockSheet,
    coerce_part_instances,
)

NESTING_ALGORITHM_VERSION = "deterministic-bottom-left-v1"


class DeterministicNester:
    """A deterministic, conservative 2D nesting implementation.

    It intentionally favours repeatability and inspectability over globally
    optimal packing.  Candidate locations are generated from the usable origin
    and every placed/blocked rectangle edge, then selected by sheet, Y, X and
    orientation.  The same inputs therefore always produce the same layout.
    """

    algorithm_version = NESTING_ALGORITHM_VERSION

    def nest(
        self,
        parts: Iterable[PartSpec] | Iterable[PartInstance],
        stock: StockSheet,
    ) -> NestingLayout:
        instances = coerce_part_instances(parts)
        if not instances:
            return NestingLayout(stock, (), (), 0, 0, self.algorithm_version)

        ordered = sorted(
            instances,
            key=lambda item: (
                -(item.part.blank_width_um * item.part.blank_height_um),
                -max(item.part.blank_width_um, item.part.blank_height_um),
                -min(item.part.blank_width_um, item.part.blank_height_um),
                item.part.part_id,
                item.instance_id,
            ),
        )

        per_sheet: list[list[Placement]] = [[] for _ in range(stock.quantity)]
        placements: list[Placement] = []
        unplaced: list[str] = []

        for instance in ordered:
            if not _compatible(instance.part, stock):
                unplaced.append(instance.instance_id)
                continue

            choice: tuple[tuple[int, int, int, int, bool], Placement] | None = None
            for sheet_index in range(stock.quantity):
                occupied = per_sheet[sheet_index]
                candidates = _candidate_origins(stock, occupied)
                for rotated, width_um, height_um in _orientations(instance.part, stock):
                    for x_um, y_um in candidates:
                        candidate = Placement(
                            instance.instance_id,
                            instance.part.part_id,
                            stock.stock_id,
                            sheet_index,
                            x_um,
                            y_um,
                            width_um,
                            height_um,
                            rotated,
                        )
                        if not _fits(candidate, occupied, stock):
                            continue
                        score = (
                            sheet_index,
                            y_um,
                            x_um,
                            candidate.rect.top_um,
                            rotated,
                        )
                        if choice is None or score < choice[0]:
                            choice = (score, candidate)
                    # Candidates are ordered, but evaluate both orientations to
                    # keep the tie-break independent of iteration order.

            if choice is None:
                unplaced.append(instance.instance_id)
                continue

            placement = choice[1]
            per_sheet[placement.sheet_index].append(placement)
            placements.append(placement)

        placements.sort(key=lambda item: (item.sheet_index, item.y_um, item.x_um, item.instance_id))
        used_sheets = 1 + max((item.sheet_index for item in placements), default=-1)
        used_area = stock.width_um * stock.height_um * used_sheets
        part_area = sum(item.width_um * item.height_um for item in placements)
        utilization_ppm = 0 if used_area == 0 else (part_area * 1_000_000) // used_area
        layout = NestingLayout(
            stock=stock,
            placements=tuple(placements),
            unplaced_instance_ids=tuple(sorted(unplaced)),
            used_sheet_count=used_sheets,
            utilization_ppm=utilization_ppm,
            algorithm=self.algorithm_version,
        )

        internal_issues = validate_layout(layout, instances)
        blocking = [issue for issue in internal_issues if issue.severity == Severity.BLOCK]
        # Unplaced parts are a valid result, not an internal algorithm failure.
        unexpected = [issue for issue in blocking if issue.code != "NESTING_UNPLACED"]
        if unexpected:
            codes = ", ".join(issue.code for issue in unexpected)
            raise NestingError(f"nester produced an invalid layout: {codes}")
        return layout


def _compatible(part: PartSpec, stock: StockSheet) -> bool:
    return (
        part.material_id == stock.material_id
        and part.material_version == stock.material_version
        and part.thickness_um == stock.thickness_um
    )


def _normalise_grain(value: str) -> str:
    upper = str(value).upper()
    if upper in {"NONE", "NO_GRAIN", "ANY", "UNSPECIFIED"}:
        return "NONE"
    if upper in {"X", "LENGTH", "LONG", "U"}:
        return "X"
    if upper in {"Y", "WIDTH", "CROSS", "V"}:
        return "Y"
    return upper


def _grain_allows(part: PartSpec, stock: StockSheet, rotated: bool) -> bool:
    part_grain = _normalise_grain(part.grain_direction)
    stock_grain = _normalise_grain(stock.grain_direction)
    if part_grain == "NONE" or stock_grain == "NONE":
        return True
    effective = "Y" if part_grain == "X" and rotated else part_grain
    if part_grain == "Y" and rotated:
        effective = "X"
    return effective == stock_grain


def _orientations(part: PartSpec, stock: StockSheet) -> tuple[tuple[bool, int, int], ...]:
    candidates = [(False, part.blank_width_um, part.blank_height_um)]
    if part.allow_rotation and stock.allow_rotation and part.blank_width_um != part.blank_height_um:
        candidates.append((True, part.blank_height_um, part.blank_width_um))
    return tuple(
        orientation for orientation in candidates if _grain_allows(part, stock, orientation[0])
    )


def _candidate_origins(
    stock: StockSheet,
    placements: list[Placement],
) -> tuple[tuple[int, int], ...]:
    usable = stock.usable_rect
    obstacles = [placement.rect for placement in placements]
    obstacles.extend(stock.defect_zones)
    obstacles.extend(stock.clamp_zones)

    xs = {usable.x_um}
    ys = {usable.y_um}
    for obstacle in obstacles:
        xs.add(obstacle.right_um + stock.kerf_um)
        ys.add(obstacle.top_um + stock.kerf_um)
    return tuple(
        sorted(
            (
                (x_um, y_um)
                for x_um in xs
                for y_um in ys
                if x_um < usable.right_um and y_um < usable.top_um
            ),
            key=lambda point: (point[1], point[0]),
        )
    )


def _fits(candidate: Placement, occupied: list[Placement], stock: StockSheet) -> bool:
    rect = candidate.rect
    if not stock.usable_rect.contains(rect):
        return False
    if any(rect.intersects(zone, clearance_um=stock.kerf_um) for zone in stock.defect_zones):
        return False
    if any(rect.intersects(zone, clearance_um=stock.kerf_um) for zone in stock.clamp_zones):
        return False
    return not any(rect.intersects(other.rect, clearance_um=stock.kerf_um) for other in occupied)


def validate_layout(
    layout: NestingLayout,
    instances: Iterable[PartInstance],
) -> tuple[DFMIssue, ...]:
    """Validate generated or manually adjusted nesting layouts."""

    stock = layout.stock
    by_id = {instance.instance_id: instance for instance in instances}
    issues: list[DFMIssue] = []
    seen: set[str] = set()

    if layout.used_sheet_count > stock.quantity:
        issues.append(
            DFMIssue(
                "NESTING_SHEET_COUNT",
                Severity.BLOCK,
                "Layout uses more sheets than the selected stock quantity.",
                inputs={"used": layout.used_sheet_count, "available": stock.quantity},
            )
        )

    for placement in layout.placements:
        instance = by_id.get(placement.instance_id)
        if instance is None:
            issues.append(
                DFMIssue(
                    "NESTING_UNKNOWN_INSTANCE",
                    Severity.BLOCK,
                    "Placement references an unknown part instance.",
                    part_id=placement.part_id,
                    inputs={"instance_id": placement.instance_id},
                )
            )
            continue
        if placement.instance_id in seen:
            issues.append(
                DFMIssue(
                    "NESTING_DUPLICATE_INSTANCE",
                    Severity.BLOCK,
                    "A part instance is placed more than once.",
                    part_id=placement.part_id,
                    inputs={"instance_id": placement.instance_id},
                )
            )
        seen.add(placement.instance_id)

        expected = (
            (instance.part.blank_height_um, instance.part.blank_width_um)
            if placement.rotated_90
            else (instance.part.blank_width_um, instance.part.blank_height_um)
        )
        if (placement.width_um, placement.height_um) != expected:
            issues.append(
                DFMIssue(
                    "NESTING_DIMENSION_MISMATCH",
                    Severity.BLOCK,
                    "Placed dimensions do not match the source part and rotation.",
                    part_id=placement.part_id,
                    inputs={
                        "expected_um": expected,
                        "actual_um": (placement.width_um, placement.height_um),
                    },
                )
            )
        if not _grain_allows(instance.part, stock, placement.rotated_90):
            issues.append(
                DFMIssue(
                    "NESTING_GRAIN_MISMATCH",
                    Severity.BLOCK,
                    "The placement violates the required grain direction.",
                    part_id=placement.part_id,
                    inputs={"rotated_90": placement.rotated_90},
                    suggestion="Rotate the part or select compatible stock grain.",
                )
            )
        if placement.sheet_index < 0 or placement.sheet_index >= stock.quantity:
            issues.append(
                DFMIssue(
                    "NESTING_INVALID_SHEET",
                    Severity.BLOCK,
                    "Placement references a sheet outside the selected stock quantity.",
                    part_id=placement.part_id,
                )
            )
        if not stock.usable_rect.contains(placement.rect):
            issues.append(
                DFMIssue(
                    "NESTING_OUT_OF_BOUNDS",
                    Severity.BLOCK,
                    "A placed part is outside the trimmed usable stock area.",
                    part_id=placement.part_id,
                    inputs={"placement": placement.rect},
                )
            )
        for zone_kind, zones in (("DEFECT", stock.defect_zones), ("CLAMP", stock.clamp_zones)):
            if any(placement.rect.intersects(zone, clearance_um=stock.kerf_um) for zone in zones):
                issues.append(
                    DFMIssue(
                        f"NESTING_{zone_kind}_COLLISION",
                        Severity.BLOCK,
                        f"A part intersects a {zone_kind.lower()} or its machining clearance.",
                        part_id=placement.part_id,
                    )
                )

    by_sheet: dict[int, list[Placement]] = {}
    for placement in layout.placements:
        by_sheet.setdefault(placement.sheet_index, []).append(placement)
    for sheet_placements in by_sheet.values():
        ordered = sorted(sheet_placements, key=lambda item: item.instance_id)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if first.rect.intersects(second.rect, clearance_um=stock.kerf_um):
                    issues.append(
                        DFMIssue(
                            "NESTING_OVERLAP",
                            Severity.BLOCK,
                            "Nested parts overlap or violate kerf clearance.",
                            part_id=first.part_id,
                            inputs={
                                "first_instance": first.instance_id,
                                "second_instance": second.instance_id,
                                "kerf_um": stock.kerf_um,
                            },
                        )
                    )

    unplaced = set(layout.unplaced_instance_ids)
    expected_ids = set(by_id)
    missing = expected_ids - seen
    if missing != unplaced:
        issues.append(
            DFMIssue(
                "NESTING_ACCOUNTING",
                Severity.BLOCK,
                "Placed and unplaced instance accounting is inconsistent.",
                inputs={"missing": sorted(missing), "declared_unplaced": sorted(unplaced)},
            )
        )
    for instance_id in sorted(unplaced):
        instance = by_id.get(instance_id)
        issues.append(
            DFMIssue(
                "NESTING_UNPLACED",
                Severity.BLOCK,
                "Part could not be placed on the selected stock.",
                part_id=instance.part.part_id if instance else None,
                inputs={"instance_id": instance_id},
                suggestion="Add stock, change raw format, or revise the part dimensions.",
            )
        )
    return tuple(
        sorted(issues, key=lambda item: (item.code, item.part_id or "", item.feature_id or ""))
    )
