"""Native studio work orders, including the five concept-only template boundaries."""

from uuid import uuid4

from .catalog import STUDIO
from .support import check_carcass, response_json

SHOP = {
    "machine_profile_id": "custombuild-router-1325-linuxcnc",
    "stock_width_mm": 2440,
    "stock_height_mm": 1220,
    "stock_count": 10,
    "back_stock_width_mm": 2440,
    "back_stock_height_mm": 1220,
    "back_stock_count": 6,
}
INTENT = {
    "schema_version": "custombuild.workspace-intent.v1",
    "bay_sizing_mode": "count",
    "target_bay_width_mm": 300,
    "symmetry_locked": False,
    "production_context": SHOP,
    "part_overrides": {},
    "removed_part_ids": [],
}


def make_spec(template, inputs):
    product = STUDIO[template]
    spec = {
        "width_mm": product.width,
        "depth_mm": product.depth,
        "height_mm": product.height,
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "load_per_shelf_kg": 10,
        "back_panel": product.back,
        "plinth": product.plinth,
        "plinth_height_mm": 80 if product.plinth else 0,
        "joint_system": "dado",
        "reinforcement_mode": "manual",
        "wall_anchor_required": False,
        "furniture_type": "wall_library" if product.base else "bookcase",
        **inputs,
    }
    if product.base:
        spec.update(
            base_cabinet_count=inputs["divider_count"] + 1,
            base_cabinet_height_mm=product.base,
            base_cabinet_depth_mm=product.depth,
        )
    return spec


def inspect_studio(template, draft, spec, e):
    result = draft["result_json"]
    assert draft["design_hash"] == result["design_hash"]
    assert draft["updated_at"].endswith("Z")
    product = STUDIO[template]
    capability = result["template_capability"]
    e.equal(
        "template_capability",
        [draft["template_id"], capability["production_level"]],
        [template, "screened" if template == "shelving" else "concept"],
    )
    bays = spec["divider_count"] + 1
    check_carcass(
        result["parts"],
        width=spec["width_mm"] * 1000,
        height=product.height * 1000,
        depth=product.depth * 1000,
        rows=spec["shelf_count"],
        bays=bays,
        backs=bays if product.back else 0,
        plinth=product.plinth,
        evidence=e,
        name="name",
        size="size_um",
        loc="placement_um",
        role="kind",
        ratios=spec.get("bay_width_ratios"),
        centres=spec.get("shelf_height_ratios"),
        floor=(spec["plinth_height_mm"] + (product.base or 18)) * 1000,
    )
    e.equal(
        "cabinet_fronts",
        sum(i["kind"] == "cabinet_front" for i in result["parts"]),
        bays if product.base else 0,
    )
    assert len({i["part_id"] for i in result["parts"]}) == len(result["parts"])


def exercise_studio(client, case, e):
    template = case.product.removeprefix("studio-")
    spec = make_spec(template, case.order.inputs)
    project = response_json(client.post("/v1/projects", json={"name": f"{case.id}-{uuid4()}"}), 201)
    path = f"/v1/projects/{project['id']}"
    order = {"template_id": template, "spec": spec, "workspace_spec": INTENT}
    draft = response_json(client.put(f"{path}/draft", json={**order, "expected_draft_revision": 0}))
    assert response_json(client.get(f"{path}/draft")) == draft
    inspect_studio(template, draft, spec, e)
    if case.level == "very-hard":
        spec = {**spec, "width_mm": spec["width_mm"] + 7}
        latest = response_json(
            client.put(
                f"{path}/draft",
                json={**order, "spec": spec, "expected_draft_revision": draft["draft_revision"]},
            )
        )
        assert latest["design_hash"] != draft["design_hash"]
        conflict = response_json(
            client.put(
                f"{path}/draft", json={**order, "expected_draft_revision": draft["draft_revision"]}
            ),
            409,
        )
        e.equal("stale_write", conflict["detail"]["code"], "DRAFT_REVISION_CONFLICT")
        assert response_json(client.get(f"{path}/draft")) == latest
        inspect_studio(template, latest, spec, e)
        if template == "shelving":
            payload = {
                "template_id": template,
                "spec": spec,
                "production_context": SHOP,
                "expected_current_revision": 0,
                "expected_design_hash": draft["design_hash"],
            }
            stale = response_json(client.post(f"{path}/versions", json=payload), 409)
            assert "EXPECTED_DESIGN_HASH_MISMATCH" in stale["detail"]
            version = response_json(
                client.post(
                    f"{path}/versions",
                    json={**payload, "expected_design_hash": latest["design_hash"]},
                ),
                201,
            )
            e.equal("frozen_revision", version["design_hash"], latest["design_hash"])
    if case.level != "impossible":
        return
    if template == "shelving":
        bad = {**spec, "shelf_count": 20, "height_mm": 400}
        response = response_json(
            client.put(
                f"{path}/draft",
                json={**order, "spec": bad, "expected_draft_revision": draft["draft_revision"]},
            ),
            422,
        )
        e.note("rejection", response["detail"])
        assert response["detail"]["code"] == "DESIGN_INPUT_INVALID"
        assert "opening" in str(response["detail"]["errors"]).lower()
    else:
        response = response_json(
            client.post(
                f"{path}/versions",
                json={
                    "template_id": template,
                    "spec": spec,
                    "production_context": SHOP,
                    "expected_current_revision": 0,
                    "expected_design_hash": draft["design_hash"],
                },
            ),
            409,
        )
        reason = response["detail"]
        e.equal(
            "rejected_template",
            [reason["code"], reason["template_id"]],
            ["TEMPLATE_CAPABILITY_BLOCKED", template],
        )
        expected_terms = {
            "wall-library": "hinge",
            "sideboard": "drawer",
            "room-divider": "stability",
            "hanging-shelf": "bracket",
            "cupboard": "clearance",
        }
        e.note("restriction", reason["message"])
        assert expected_terms[template] in reason["message"].lower() and reason["solution"]
    assert response_json(client.get(f"{path}/versions")) == []
    assert response_json(client.get(f"{path}/draft")) == draft
