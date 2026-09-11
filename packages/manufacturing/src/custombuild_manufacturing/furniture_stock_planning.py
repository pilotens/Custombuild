"""Material-bound blank/sheet checks; no nesting, hidden splices or CAM approval."""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import ManufacturingSelection
from custombuild_domain.furniture_engine import FurnitureResult

from .furniture_handoff import furniture_stock_requirements
from .model import MachineProfile


def _mm(value: int) -> str:
    return f"{value / 1_000:.3f}".rstrip("0").rstrip(".")


def _minimal_envelopes(candidates: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Pareto frontier: every part fits individually, never all on one sheet."""
    result: set[tuple[int, int]] = set()
    least_height: int | None = None
    for width, height in sorted(candidates):
        if least_height is None or height < least_height:
            result.add((width, height))
            least_height = height
    return result


def plan_furniture_stock(
    selection: ManufacturingSelection, result: FurnitureResult, machine: MachineProfile
) -> dict[str, Any]:
    requirements = furniture_stock_requirements(result)
    overrides = {stock.material_key: stock for stock in selection.material_stocks}
    used_keys: set[tuple[str, str, int]] = set()
    issues: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for requirement in requirements:
        key = (
            requirement["material_id"],
            requirement["material_version"],
            requirement["measured_thickness_um"],
        )
        used_keys.add(key)
        stock = overrides.get(key, selection)
        binding = dict(
            zip(("material_id", "material_version", "measured_thickness_um"), key, strict=True)
        )
        label = f"{key[0]} · {_mm(key[2])} mm"
        group_issues: list[dict[str, Any]] = []

        def issue(
            code: str,
            message: str,
            part_id: str | None = None,
            *,
            context: dict[str, Any] = binding,
            prefix: str = label,
            target: list[dict[str, Any]] = group_issues,
        ) -> None:
            entry = {**context, "code": code, "message": f"{prefix}: {message}"}
            if part_id is not None:
                entry["part_id"] = part_id
            target.append(entry)

        sw, sh, margin = stock.stock_width_um, stock.stock_height_um, stock.edge_margin_um
        uw, uh = sw - 2 * margin, sh - 2 * margin
        if sw > machine.work_width_um or sh > machine.work_height_um:
            issue(
                "STOCK_EXCEEDS_MACHINE",
                f"Råskivan {_mm(sw)} × {_mm(sh)} mm ryms inte inom maskinens arbetsområde "
                f"{_mm(machine.work_width_um)} × {_mm(machine.work_height_um)} mm (X × Y).",
            )
        if min(uw, uh) <= 0:
            issue(
                "STOCK_MARGIN_CONSUMES_SHEET", "Kantmarginalen lämnar inget användbart skivformat."
            )
        parts: list[dict[str, Any]] = []
        envelopes = {(0, 0)}
        for part in requirement["parts"]:
            grain = part["grain_direction"]
            rotations: tuple[bool, ...] = (False, True)
            if grain != "NONE":
                rotations = (
                    ()
                    if stock.stock_grain_axis is None
                    else (grain.lower() != stock.stock_grain_axis,)
                )
            rw, rh = part["raw_width_um"], part["raw_height_um"]
            orientations: list[dict[str, Any]] = []
            for rotate in rotations:
                width, height = (rh, rw) if rotate else (rw, rh)
                required_w, required_h = width + 2 * margin, height + 2 * margin
                orientations.append(
                    {
                        "rotation_deg": 90 if rotate else 0,
                        "required_stock_width_um": required_w,
                        "required_stock_height_um": required_h,
                        "width_shortfall_um": max(0, required_w - sw),
                        "height_shortfall_um": max(0, required_h - sh),
                        "fits_stock": required_w <= sw and required_h <= sh,
                        "fits_machine": required_w <= machine.work_width_um
                        and required_h <= machine.work_height_um,
                    }
                )
            fits = any(option["fits_stock"] for option in orientations)
            parts.append(
                {**part, "fits_stock": fits if rotations else None, "orientations": orientations}
            )
            envelopes = _minimal_envelopes(
                {
                    (
                        max(w, option["required_stock_width_um"]),
                        max(h, option["required_stock_height_um"]),
                    )
                    for w, h in envelopes
                    for option in orientations
                }
            )
            if not rotations:
                issue(
                    "GRAIN_AXIS_REQUIRED", "Råskivans fiberriktning måste anges.", part["part_id"]
                )
            elif not fits:
                issue(
                    "PART_EXCEEDS_STOCK",
                    f"{part['semantic_key']}: råmått {_mm(rw)} × {_mm(rh)} mm ryms inte "
                    f"på det användbara skivformatet {_mm(max(0, uw))} × {_mm(max(0, uh))} mm "
                    "med angiven fiberriktning. Välj annat format eller rita om fördelningen "
                    "i moduler med verifierade förband. Delen skarvas inte automatiskt.",
                    part["part_id"],
                )
        groups.append(
            {
                **binding,
                "selection_source": "material" if key in overrides else "default",
                "stock": {
                    "stock_width_um": sw,
                    "stock_height_um": sh,
                    "stock_grain_axis": stock.stock_grain_axis,
                    "edge_margin_um": margin,
                },
                "usable_width_um": max(0, uw),
                "usable_height_um": max(0, uh),
                "geometry_compatible": not group_issues,
                "parts": parts,
                "required_formats": [
                    {
                        "stock_width_um": w,
                        "stock_height_um": h,
                        "fits_machine": w <= machine.work_width_um and h <= machine.work_height_um,
                    }
                    for w, h in sorted(envelopes)
                ],
                "issues": group_issues,
            }
        )
        issues.extend(group_issues)
    for key in sorted(overrides.keys() - used_keys):
        issues.append(
            {
                "code": "MATERIAL_STOCK_NOT_USED",
                "material_id": key[0],
                "material_version": key[1],
                "measured_thickness_um": key[2],
                "message": f"Råformatet för {key[0]} · {key[1]} · {_mm(key[2])} mm hör inte "
                "till någon del i möbeln. Ta bort det eller välj material och tjocklek igen.",
            }
        )
    return {
        "stock_groups": groups,
        "issues": issues,
        "geometry_compatible": not issues,
        "machine_work_area_um": {
            "width_um": machine.work_width_um,
            "height_um": machine.work_height_um,
        },
        "format_scope": "Minimiformat inkluderar vald kantmarginal och låter varje del rymmas "
        "enskilt i angiven fiberriktning. Det är ingen nesting eller beställningskvantitet. "
        "Verktygsutrymme och uppspänning behöver verifieras separat.",
    }
