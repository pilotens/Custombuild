"""Dry-fit trial budget. Clearances are relative to installed part datums, in µm."""

from collections import defaultdict
from dataclasses import dataclass, replace

from .enums import FaceName, FeatureKind, JointType, OpenEndRelief, PartRole
from .models import (
    TRIAL_EDGE_TOLERANCE_UM,
    TRIAL_FACE_TOLERANCE_UM,
    TRIAL_FRONT_STOP_CLEARANCE_UM,
    TRIAL_GROOVE_BOTTOM_CLEARANCE_UM,
    Joint,
    ManufacturingFeature,
    PartInstance,
)

_AXIS = {
    FaceName.LEFT: 0,
    FaceName.RIGHT: 0,
    FaceName.FRONT: 1,
    FaceName.BACK: 1,
    FaceName.TOP: 2,
    FaceName.BOTTOM: 2,
}


@dataclass(frozen=True)
class GrooveFit:
    joint_id: str
    groove_id: str
    host_id: str
    inserted_part_id: str
    normal_axis: str
    entry_face_world_um: int
    inserted_end_world_um: int
    cut_depth_um: int
    nominal_engagement_um: int
    minimum_engagement_um: int
    nominal_bottom_gap_um: int
    minimum_bottom_gap_um: int
    minimum_total_width_clearance_um: int
    minimum_remaining_web_um: int
    maximum_bottom_gap_um: int
    minimum_floating_capture_um: int | None = None
    minimum_front_stop_gap_um: int | None = None


def _physical_face(p: PartInstance, face: FaceName) -> FaceName:
    return {FaceName.A: p.a_side, FaceName.B: p.b_side}.get(face, face)


def _footprint(f: ManufacturingFeature, face: FaceName) -> tuple[int, int, int, int]:
    origin = (f.origin.x_um, f.origin.y_um, f.origin.z_um)
    u, v = (value for axis, value in enumerate(origin) if axis != _AXIS[face])
    extent = f.dimensions
    if f.kind == FeatureKind.DRILL:
        radius = ((extent.diameter_um or 0) + f.tolerance_um + 1) // 2 + f.tolerance_um
        return u - radius, v - radius, u + radius, v + radius
    if f.kind != FeatureKind.GROOVE or extent.width_um is None or extent.length_um is None:
        raise ValueError("Provprofilen kräver en verifierad utbredning för varje bearbetning.")
    pad = (extent.radius_um or 0) + 3 * f.tolerance_um  # position, width and relief radius
    return u - pad, v - pad, u + extent.width_um + pad, v + extent.length_um + pad


def _remaining_web(p: PartInstance) -> dict[str, int]:
    """Bound the remaining stock, including opposing pilots and groove corner relief."""
    cuts = []
    for f in p.features:
        face = _physical_face(p, f.face)
        if face not in {p.a_side, p.b_side}:
            raise ValueError("Spårprovet kräver modellerade bearbetningar på delens bredsidor.")
        cuts.append((f, face, _footprint(f, face)))
    result = {}
    for f, face, (u, v, u2, v2) in cuts:
        other = max(
            (
                (g.dimensions.depth_um or 0) + g.tolerance_um
                for g, other_face, (x, y, x2, y2) in cuts
                if other_face != face and min(x2, u2) >= max(x, u) and min(y2, v2) >= max(y, v)
            ),
            default=0,
        )
        web = (
            p.actual_thickness_um
            - p.stock_tolerance_um
            - (f.dimensions.depth_um or 0)
            - f.tolerance_um
            - other
        )
        if web <= 0:
            raise ValueError(
                f"{p.semantic_key}: spår eller pilothål kan bryta igenom. "
                "Flytta bearbetningen eller ändra indelningen."
            )
        result[f.feature_id] = web
    return result


def trial_groove_fits(
    parts: tuple[PartInstance, ...], joints: tuple[Joint, ...]
) -> tuple[GrooveFit, ...]:
    by_id = {p.part_id: p for p in parts}
    if any(p.profile_tolerance_um <= 0 or p.stock_tolerance_um <= 0 for p in parts):
        raise ValueError("Provdelarna kräver angiven kontur- och tjocklekstolerans.")
    bores = {f.feature_id: f for p in parts for f in p.features}
    webs = {fid: web for p in parts for fid, web in _remaining_web(p).items()}
    fits: list[GrooveFit] = []
    floating_pairs: dict[tuple[str, int], list[int]] = defaultdict(list)
    end_budget = TRIAL_EDGE_TOLERANCE_UM + TRIAL_FACE_TOLERANCE_UM
    for j in joints:
        if j.joint_type != JointType.DADO:
            raise ValueError("Vinkelprovets passningsmodell omfattar endast spårfogar.")
        cut, inserted = j.members
        if len(cut.feature_ids) != 1 or inserted.feature_ids:
            raise ValueError("Provfogen kräver ett värdspår och en obruten motkant.")
        host, mate = by_id[cut.part_id], by_id[inserted.part_id]
        f = bores[cut.feature_ids[0]]
        if f.kind != FeatureKind.GROOVE:
            raise ValueError("Provfogens värddel saknar spår.")
        face = _physical_face(host, f.face)
        axis = _AXIS[face]
        coord = "xyz"[axis] + "_um"
        dim = ("width_um", "depth_um", "height_um")[axis]
        lo = getattr(host.placement, coord)
        start = getattr(mate.placement, coord)
        overlap = min(
            lo + getattr(host.finished_size, dim), start + getattr(mate.finished_size, dim)
        ) - max(lo, start)
        depth = f.dimensions.depth_um or 0
        gap = depth - overlap
        minimum_gap = gap - f.tolerance_um - end_budget
        normal_to_mate = _AXIS[mate.a_side]
        u_axis, v_axis = (i for i in range(3) if i != axis)
        spans = {u_axis: f.dimensions.width_um, v_axis: f.dimensions.length_um}
        stop_gap = None
        if mate.role in {PartRole.SHELF, PartRole.DIVIDER}:
            opening = OpenEndRelief.U_MIN if u_axis == 1 else OpenEndRelief.V_MIN
            cut_end = host.placement.y_um + f.origin.y_um + (spans.get(1) or 0)
            mate_end = mate.placement.y_um + mate.finished_size.depth_um
            gap_y = cut_end - mate_end
            stop_gap = (
                gap_y - 2 * f.tolerance_um - mate.profile_tolerance_um - TRIAL_FACE_TOLERANCE_UM
            )
            if (
                opening not in f.open_end_reliefs
                or f.origin.y_um != 0
                or host.placement.y_um != mate.placement.y_um
                or gap_y != TRIAL_FRONT_STOP_CLEARANCE_UM
                or gap_y != f.front_stop_clearance_um
                or stop_gap <= 0
            ):
                raise ValueError(
                    "Det frontmatade spåret saknar öppen ingång eller säkerställd bakre stoppmån."
                )
        elif f.front_stop_clearance_um:
            raise ValueError("Stoppmån i Y kräver en hylla eller avdelare som skjuts in framifrån.")
        slot_width = spans.get(normal_to_mate) or 0
        transverse_gap = slot_width - mate.actual_thickness_um
        width_clearance = transverse_gap - f.tolerance_um - mate.stock_tolerance_um
        from_low = face in {FaceName.LEFT, FaceName.FRONT, FaceName.BOTTOM}
        entry = lo if from_low else lo + getattr(host.finished_size, dim)
        end = start + getattr(mate.finished_size, dim) if from_low else start
        if (
            gap != TRIAL_GROOVE_BOTTOM_CLEARANCE_UM
            or gap != f.groove_bottom_clearance_um
            or transverse_gap != f.fit_clearance_um
            or abs(end - entry) != overlap
            or minimum_gap <= 0
            or width_clearance <= 0
            or overlap <= end_budget
        ):
            raise ValueError(
                f"{host.semantic_key}/{mate.semantic_key}: provfog saknar "
                "säkerställd spårbottenmån eller tillräckligt instick vid toleransgränsen."
            )
        if mate.role == PartRole.BACK:
            floating_pairs[mate.part_id, axis].append(len(fits))
        fits.append(
            GrooveFit(
                joint_id=j.joint_id,
                groove_id=f.feature_id,
                host_id=host.part_id,
                inserted_part_id=mate.part_id,
                normal_axis="xyz"[axis],
                entry_face_world_um=entry,
                inserted_end_world_um=end,
                cut_depth_um=depth,
                nominal_engagement_um=overlap,
                minimum_engagement_um=overlap - end_budget,
                nominal_bottom_gap_um=gap,
                minimum_bottom_gap_um=minimum_gap,
                minimum_total_width_clearance_um=width_clearance,
                minimum_remaining_web_um=webs[f.feature_id],
                maximum_bottom_gap_um=gap + end_budget + f.tolerance_um,
                minimum_front_stop_gap_um=stop_gap,
            )
        )
    # An inset plywood field can settle against either groove bottom. The edge
    # opposite the direction of travel must stay captured even at maximum play.
    for pair in floating_pairs.values():
        if len(pair) != 2:
            raise ValueError("Ryggfältet kräver två motstående spår per infångningsaxel.")
        for i, opposite in zip(pair, reversed(pair), strict=True):
            remaining = fits[i].minimum_engagement_um - fits[opposite].maximum_bottom_gap_um
            if remaining <= 0:
                raise ValueError("Ryggkanten kan lämna motstående spår vid full förskjutning.")
            fits[i] = replace(fits[i], minimum_floating_capture_um=remaining)
    return tuple(fits)
