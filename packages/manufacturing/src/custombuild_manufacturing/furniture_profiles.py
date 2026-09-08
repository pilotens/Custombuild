"""Profile change consequences over immutable, machine-independent designs."""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import FurnitureWorkspace, ProfileChange
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from custombuild_domain.identity import content_hash
from custombuild_rules.furniture import evaluate_furniture

from .adapters import adapt_design_result
from .furniture_handoff import furniture_workshop_handoff
from .model import canonical_data
from .profiles import linuxcnc_reference_router_1325, linuxcnc_reference_router_5125

PROFILE_PLANNING_VERSION = "furniture-profile-planning-1.0.0"
_MACHINES = (linuxcnc_reference_router_1325(), linuxcnc_reference_router_5125())


def _millimetre_text(value_um: int) -> str:
    return f"{value_um / 1_000:.3f}".rstrip("0").rstrip(".")


def furniture_machine_catalog() -> list[dict[str, Any]]:
    return [
        {
            "profile_id": m.profile_id,
            "version": m.version,
            "name": m.name,
            "fingerprint": content_hash(canonical_data(m)),
            "work_width_um": m.work_width_um,
            "work_height_um": m.work_height_um,
            "production_qualified": False,
        }
        for m in _MACHINES
    ]


def _manufacturing(workspace: FurnitureWorkspace, result: FurnitureResult) -> dict[str, Any]:
    selection = workspace.manufacturing
    if selection is None:
        return {
            "state": "not_selected",
            "geometry_compatible": None,
            "fingerprint": None,
            "issues": [],
            "production_qualified": False,
        }
    machine = next((m for m in _MACHINES if m.profile_id == selection.machine_profile_id), None)
    if machine is None or selection.machine_profile_version != machine.version:
        raise ValueError("unknown machine profile or retired machine version")
    issues: list[dict[str, str]] = []
    sw, sh = selection.stock_width_um, selection.stock_height_um
    if sw > machine.work_width_um or sh > machine.work_height_um:
        issues.append(
            {
                "code": "STOCK_EXCEEDS_MACHINE",
                "message": "Vald råskiva ryms inte inom maskinens arbetsområde.",
            }
        )
    usable_width = sw - 2 * selection.edge_margin_um
    usable_height = sh - 2 * selection.edge_margin_um
    if min(usable_width, usable_height) <= 0:
        issues.append(
            {
                "code": "STOCK_MARGIN_CONSUMES_SHEET",
                "message": "Kantmarginalen lämnar inget användbart skivformat.",
            }
        )
    names = {part.part_id: part.semantic_key for part in result.parts}
    for part in adapt_design_result(result).parts:
        directional = part.grain_direction != "NONE"
        if directional and selection.stock_grain_axis is None:
            issues.append(
                {
                    "code": "GRAIN_AXIS_REQUIRED",
                    "part_id": part.part_id,
                    "message": "Råskivans fiberriktning måste anges.",
                }
            )
            continue
        rotations = (
            (False, True)
            if not directional
            else (part.grain_direction.lower() != selection.stock_grain_axis,)
        )
        raw_width, raw_height = (
            part.raw_width_um or part.width_um,
            part.raw_height_um or part.height_um,
        )
        fits = any(
            (raw_height if rotate else raw_width) <= usable_width
            and (raw_width if rotate else raw_height) <= usable_height
            for rotate in rotations
        )
        if not fits:
            issues.append(
                {
                    "code": "PART_EXCEEDS_STOCK",
                    "part_id": part.part_id,
                    "message": f"{names[part.part_id]}: råmått {_millimetre_text(raw_width)} × "
                    f"{_millimetre_text(raw_height)} mm ryms inte på det användbara skivformatet "
                    f"{_millimetre_text(max(0, usable_width))} × "
                    f"{_millimetre_text(max(0, usable_height))} mm "
                    "med angiven fiberriktning. Välj annat format eller rita om fördelningen "
                    "i moduler med verifierade förband. Delen skarvas inte automatiskt.",
                }
            )
    binding = {
        "selection": selection.model_dump(mode="json"),
        "machine": canonical_data(machine),
        "planning_version": PROFILE_PLANNING_VERSION,
    }
    return {
        "state": "requires_change" if issues else "not_qualified",
        "geometry_compatible": not issues,
        "fingerprint": content_hash(binding),
        "issues": issues,
        "production_qualified": False,
        "detail": "Referensprofil för planering. Nesting, verktygsåtkomst, uppspänning, "
        "postprocessor och skärande CAM måste verifieras för den verkliga maskinen.",
    }


def _dependencies(
    workspace: FurnitureWorkspace, result: FurnitureResult, manufacturing: dict[str, Any]
) -> dict[str, str]:
    design = workspace.design
    materials = [design.material]
    if design.intent.family != "table":
        materials.append(design.back_material)
    return {
        "intent": result.intent_hash,
        "material": content_hash(
            [{"selection": m, "profile": resolve_material(m)} for m in materials]
        ),
        "hardware": content_hash(result.hardware_profile),
        "geometry": content_hash(
            [p.model_dump(mode="json", exclude={"revision"}) for p in result.parts]
        ),
        "manufacturing": content_hash(manufacturing["fingerprint"]),
    }


def preview_furniture(workspace: FurnitureWorkspace) -> dict[str, Any]:
    result = build_furniture(workspace.design)
    manufacturing = _manufacturing(workspace, result)
    return {
        "schema_version": "custombuild.furniture-preview.v1",
        "workspace": workspace.model_dump(mode="json"),
        "design": result.model_dump(mode="json"),
        "dependencies": _dependencies(workspace, result, manufacturing),
        "rules": evaluate_furniture(result),
        "manufacturing": manufacturing,
        "workshop_handoff": furniture_workshop_handoff(result),
        "production_qualified": False,
        "physical_cutting_authorized": False,
    }


def compare_furniture_profiles(change: ProfileChange) -> dict[str, Any]:
    before = preview_furniture(change.current)
    try:
        after = preview_furniture(change.proposed)
    except ValueError as exc:
        return {
            "state": "requires_change",
            "message": str(exc),
            "proposed": None,
            "original_design_hash": before["design"]["design_hash"],
            "changed_dependencies": [],
            "invalidated_reviews": [],
            "can_apply": False,
            "production_qualified": False,
        }
    changed = sorted(
        key for key, value in before["dependencies"].items() if value != after["dependencies"][key]
    )
    invalidated: set[str] = set()
    if set(changed) & {"intent", "material", "hardware", "geometry"}:
        invalidated.update(("construction", "material_batch", "joint_hardware"))
    if changed:
        invalidated.update(("nesting", "setups", "toolpaths", "postprocessor", "cam", "workshop"))
    if "manufacturing" in changed:
        invalidated.update(("nesting", "setups", "toolpaths", "postprocessor", "cam", "workshop"))
    old_parts = {p["part_id"]: p for p in before["design"]["parts"]}
    new_parts = {p["part_id"]: p for p in after["design"]["parts"]}
    parts_changed = sorted(
        key
        for key in old_parts.keys() | new_parts.keys()
        if old_parts.get(key) != new_parts.get(key)
    )
    return {
        "state": after["manufacturing"]["state"]
        if change.proposed.manufacturing
        else "not_qualified",
        "original_design_hash": before["design"]["design_hash"],
        "proposed": after,
        "changed_dependencies": changed,
        "changed_part_ids": parts_changed,
        "invalidated_reviews": sorted(invalidated),
        "intent_preserved": before["design"]["intent_hash"] == after["design"]["intent_hash"],
        "can_apply": True,
        "production_qualified": False,
        "message": "Förslaget kan sparas som en ny designrevision. Tillverkningen är inte godkänd.",
    }
