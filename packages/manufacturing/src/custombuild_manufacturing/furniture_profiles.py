"""Profile change consequences over immutable, machine-independent designs."""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import FurnitureWorkspace, ProfileChange
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from custombuild_domain.identity import content_hash
from custombuild_rules.furniture import evaluate_furniture

from .furniture_handoff import furniture_workshop_handoff
from .furniture_stock_planning import plan_furniture_stock
from .furniture_trial import furniture_trial_readiness
from .model import canonical_data
from .profiles import linuxcnc_reference_router_1325, linuxcnc_reference_router_5125

PROFILE_PLANNING_VERSION = "furniture-profile-planning-1.1.0"
_MACHINES = (linuxcnc_reference_router_1325(), linuxcnc_reference_router_5125())


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
    plan = plan_furniture_stock(selection, result, machine)
    binding = {
        "selection": selection.model_dump(mode="json"),
        "machine": canonical_data(machine),
        "planning_version": PROFILE_PLANNING_VERSION,
    }
    return {
        "state": "requires_change" if plan["issues"] else "not_qualified",
        **plan,
        "fingerprint": content_hash(binding),
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
    rules = evaluate_furniture(result)
    handoff = {**furniture_workshop_handoff(result), "stock_plan": manufacturing}
    trial_readiness = furniture_trial_readiness(workspace, result, rules, manufacturing, handoff)
    return {
        "schema_version": "custombuild.furniture-preview.v1",
        "workspace": workspace.model_dump(mode="json"),
        "design": result.model_dump(mode="json"),
        "dependencies": _dependencies(workspace, result, manufacturing),
        "rules": rules,
        "manufacturing": manufacturing,
        "workshop_handoff": {**handoff, "trial_readiness": trial_readiness},
        "trial_readiness": trial_readiness,
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
