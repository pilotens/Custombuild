"""Standing assembly at the installed datum, using the same strokes as the CAD check."""

from typing import Literal

from .bookcase_motion import Bounds, assembly_strokes
from .furniture import InstallationSpace, Wall
from .models import DesignResult, FrozenModel
from .units import um_to_mm

WALL_NAMES = {"left": "vänster vägg", "right": "höger vägg", "rear": "bakväggen"}


class WallTravel(FrozenModel):
    wall: Wall
    step_number: int | None
    required_reservation_um: int | None
    reserved_um: int | None
    margin_um: int | None


class StandingSiteReview(FrozenModel):
    minimum_height_from_base_um: int
    minimum_ceiling_height_um: int | None
    measured_ceiling_height_um: int | None
    minimum_front_space_um: int
    measured_front_space_um: int | None
    walls: tuple[WallTravel, ...]
    state: Literal["blocked", "requires_measurement", "nominal_clear"]
    issues: tuple[dict[str, str], ...]


def _wall_excursion(wall: Wall, bounds: Bounds, width: int, depth: int) -> int:
    lo, hi = bounds
    # Limit the projection to the wall alongside the carcass. Geometry outside
    # the declared installation needs a separate site measurement.
    if wall == "rear":
        return max(0, hi[1] - depth) if min(hi[0], width) > max(lo[0], 0) else 0
    if min(hi[1], depth) <= max(lo[1], 0):
        return 0
    return max(0, -lo[0] if wall == "left" else hi[0] - width)


def review_standing_site(
    design: DesignResult, site: InstallationSpace | None
) -> StandingSiteReview:
    trim = site.trim_profile if site else None
    trimmed = trim.walls if trim and trim.use == "existing_room_trim" else ()
    bare = site.additional_bounded_walls if site else None
    walls = tuple(dict.fromkeys((*trimmed, *(bare or ()))))
    floor = site.bottom_allowance_um if site else None
    params = design.spec.parameters
    peak = 0
    front = 0
    # Record the first step with the largest required width on each wall.
    reservations: dict[Wall, tuple[int, int | None]] = {wall: (0, None) for wall in walls}
    for stroke in assembly_strokes(design):
        peak = max(peak, stroke.travel.world_max_um[2])
        front = max(front, -stroke.travel.world_min_um[1])
        for wall in walls:
            for bounds in stroke.moving_bounds.values():
                excursion = _wall_excursion(wall, bounds, params.width_um, params.depth_um)
                if not excursion:
                    continue
                # Above the skirting only the bare wall constrains the stroke.
                # Unknown heights are reported separately, never assumed clear.
                in_trim = (
                    wall in trimmed
                    and trim
                    and trim.height_um is not None
                    and floor is not None
                    and (bounds[0][2] + floor < trim.height_um and bounds[1][2] + floor > 0)
                )
                projection = trim.width_um if in_trim and trim else 0
                needed = excursion + (projection or 0)
                if needed > reservations[wall][0]:
                    reservations[wall] = (needed, stroke.travel.step_number)

    if site is None:
        return StandingSiteReview(
            minimum_height_from_base_um=peak,
            minimum_ceiling_height_um=None,
            measured_ceiling_height_um=None,
            minimum_front_space_um=front,
            measured_front_space_um=None,
            walls=(),
            state="requires_measurement",
            issues=(
                {
                    "code": "STANDING_SITE_UNMEASURED",
                    "message": "Monteringsplatsen är inte angiven. Öppna Kundens yttermått och "
                    "listutrymme och ange väggläge, golvavstånd, fri takhöjd och plats "
                    "framför stommen.",
                },
            ),
        )

    issues = []
    if (bare is None or site.remaining_walls_unmeasured) and set(walls) != set(WALL_NAMES):
        missing = ", ".join(WALL_NAMES[wall] for wall in WALL_NAMES if wall not in walls)
        issues.append(
            {
                "code": "STANDING_WALL_LAYOUT_UNMEASURED",
                "message": f"Kontrollera väggläget vid {missing}. Ange var en vägg begränsar "
                "införingen vid kundens yttermått och bekräfta vilka övriga sidor som är fria.",
            }
        )
    conflicts = False
    reports = []
    for wall, (required, step) in reservations.items():
        if (
            wall in trimmed
            and trim
            and trim.width_um is not None
            and trim.height_um is not None
            and floor is not None
            and floor < trim.height_um
            and trim.width_um > required
        ):
            required, step = trim.width_um, None
        reserved = getattr(site, f"{wall}_allowance_um")
        known = wall not in trimmed or (
            trim is not None
            and trim.width_um is not None
            and trim.height_um is not None
            and floor is not None
        )
        margin = reserved - required if known and reserved is not None else None
        reports.append(
            WallTravel(
                wall=wall,
                step_number=step,
                required_reservation_um=required if known else None,
                reserved_um=reserved,
                margin_um=margin,
            )
        )
        if step is None and margin is not None and margin < 0:
            conflicts = True
            issues.append(
                {
                    "code": f"STANDING_{wall.upper()}_CLEARANCE",
                    "message": f"Stommen går in i listen vid {WALL_NAMES[wall]}. "
                    f"Reservera minst {um_to_mm(required)} mm och räkna om stommen.",
                }
            )
        if step is not None and margin is not None and margin <= 0:
            conflicts = True
            issues.append(
                {
                    "code": f"STANDING_{wall.upper()}_CLEARANCE",
                    "message": f"Monteringssteg {step} vid {WALL_NAMES[wall]} behöver "
                    f"{um_to_mm(required)} mm nominellt från väggen. Reserverat "
                    f"{um_to_mm(reserved)} mm. Reservera mer än {um_to_mm(required)} mm inklusive "
                    "montagespel och räkna om stommen från kundmåtten.",
                }
            )
        if reserved is None:
            issues.append(
                {
                    "code": f"STANDING_{wall.upper()}_UNMEASURED",
                    "message": f"Ange reserverat avstånd från stommen till {WALL_NAMES[wall]} "
                    "och räkna om stommen.",
                }
            )
    ceiling = site.measured_ceiling_height_um
    required_height = None if floor is None else floor + peak
    if ceiling is None or required_height is None:
        issues.append(
            {
                "code": "STANDING_HEIGHT_UNMEASURED",
                "message": f"Stående montering behöver {um_to_mm(peak)} mm över stommens "
                "botten. Ange uppmätt fri takhöjd och avstånd från golv till stomme. "
                "Lägg till plats för händer och lyfthjälp.",
            }
        )
    elif ceiling <= required_height:
        conflicts = True
        issues.append(
            {
                "code": "STANDING_CEILING_CLEARANCE",
                "message": f"Trädelarna behöver {um_to_mm(required_height)} mm fri takhöjd. "
                f"Uppmätt {um_to_mm(ceiling)} mm. Öka reservationen ovanför stommen, räkna om "
                "stommen och kontrollera även arbetsutrymmet.",
            }
        )
    measured_front = site.measured_front_space_um
    if front > 0 and measured_front is None:
        issues.append(
            {
                "code": "STANDING_FRONT_UNMEASURED",
                "message": f"Mät fri plats framför stommens front. Trädelarnas införing behöver "
                f"{um_to_mm(front)} mm. Lägg till arbetsutrymme för händer och fixturer.",
            }
        )
    elif front > 0 and measured_front is not None and measured_front <= front:
        conflicts = True
        issues.append(
            {
                "code": "STANDING_FRONT_CLEARANCE",
                "message": f"Fri plats framför stommen är {um_to_mm(measured_front)} mm. "
                f"Införingen behöver mer än {um_to_mm(front)} mm inklusive montagespel. "
                "Frigör utrymme framför möbeln eller minska djupet och kontrollera revisionen.",
            }
        )
    incomplete = bool(
        walls
        and trim
        and (
            trim.width_um is None
            or trim.height_um is None
            or floor is None
            or any(w.reserved_um is None for w in reports)
        )
    ) or bool(
        trim and (trim.use == "unassigned" or (trim.use == "existing_room_trim" and not trimmed))
    )
    expected = site.carcass_dimensions()
    incomplete |= expected != {k: getattr(params, k) for k in ("width_um", "height_um", "depth_um")}
    return StandingSiteReview(
        minimum_front_space_um=front,
        measured_front_space_um=measured_front,
        minimum_height_from_base_um=peak,
        minimum_ceiling_height_um=required_height,
        measured_ceiling_height_um=ceiling,
        walls=tuple(reports),
        issues=tuple(issues),
        state="blocked"
        if conflicts
        else "requires_measurement"
        if incomplete or issues
        else "nominal_clear",
    )
