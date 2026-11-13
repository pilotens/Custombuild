from custombuild_domain import BackPanelType, build_bookcase
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.rear_coverage import ReferenceConstruction, reference_discrepancies
from custombuild_domain.reference_posts import ReferencePost
from custombuild_manufacturing.part_inspection import inspect_part

from tests.unit.test_domain_engine import make_spec


def test_short_back_on_one_bay_is_reported_as_partial_instead_of_full():
    original = build_bookcase(
        make_spec(vertical_divider_count=1, back_panel=BackPanelType.INSET_GROOVE)
    )
    back = next(p for p in original.parts if p.semantic_key == "back-b0")
    shelf = next(p for p in original.parts if p.semantic_key == "shelf-r0-b0")
    # A regression fixture keeps only the upper 200 mm of the first rear field.
    height = 200_000
    shortened = back.model_copy(
        update={
            "placement": back.placement.model_copy(
                update={
                    "z_um": back.placement.z_um + back.finished_size.height_um - height,
                }
            ),
            "finished_size": back.finished_size.model_copy(update={"height_um": height}),
        }
    )
    broken = original.model_copy(
        update={
            "parts": tuple(shortened if p.part_id == back.part_id else p for p in original.parts)
        }
    )
    requirements = ReferenceConstruction(
        uprights="panels", back="all_openings", shelves="per_bay", material="birch-plywood"
    )
    review = inspect_part(broken, shelf, requirements)
    assert [(o.bay, o.state) for o in review.rear_openings] == [(1, "partial"), (2, "covered")]
    partial = next(o for o in review.reference_objects if o.key == "back-upper-1")
    assert partial.state == "different" and partial.compared_parts == (back.part_id,)
    assert (
        next(o for o in review.reference_objects if o.key == "back-upper-2").state == "represented"
    )
    restored = inspect_part(original, shelf, requirements)
    assert all(o.state == "covered" for o in restored.rear_openings)
    assert restored.sheet_sha256 != review.sheet_sha256


def test_drawer_side_links_its_own_three_runner_fixings_and_distinguishes_the_other_drawer():
    workspace = FurnitureWorkspace.model_validate(
        {
            "design": {
                "intent": {"family": "chest_of_drawers", "drawer_count": 2},
                "hardware": {"catalog_id": "hettich-ka4532-9114275"},
            }
        }
    )
    model = build_furniture(workspace.design)
    side = next(p for p in model.parts if p.semantic_key == "drawer-1-left")
    sheet = inspect_part(model, side)
    runner = next(h for h in sheet.hardware if h.sku == "wood-screw-4x14-runner")
    assert runner.quantity == len(set(runner.feature_ids)) == 3
    hole_ids = {h.feature_id for drawing in sheet.drawings for h in drawing.holes}
    assert set(runner.feature_ids) <= hole_ids
    assert sheet.runner is not None and sheet.runner.catalog_id == "hettich-ka4532-9114275"
    assert sheet.removal.affected_moving_groups == ("drawer-1",)
    assert not sheet.physical_cutting_authorized


def test_wall_register_names_the_open_base_mismatched_posts_and_unretained_surface():
    result = build_bookcase(
        make_spec(
            vertical_divider_count=1,
            base_cabinet_count=2,
            base_cabinet_height_um=650_000,
            base_cabinet_depth_um=320_000,
            back_panel=BackPanelType.INSET_GROOVE,
        )
    )
    left = next(p for p in result.parts if p.semantic_key == "left-side")
    round_post = ReferencePost(
        key="ref-post-1",
        name="Ändstöd",
        shape="round",
        width_um=18_000,
        depth_um=18_000,
        height_um=300_000,
        x_um=0,
        y_um=-18_000,
        z_um=150_000,
    )
    req = ReferenceConstruction(
        uprights="separate_posts",
        shelves="continuous",
        back="all_openings",
        material="solid-walnut",
        unresolved_detail="Gångjärn okända.",
        posts=(round_post,),
    )
    sheet = inspect_part(result, left, req)
    records = {o.key: o for o in sheet.reference_objects}
    assert records["ref-post-1"].touching == (left.part_id,)
    assert not records["ref-post-1"].overlapping
    assert records["carcass-material"].state == "different"
    assert records["support-left-side"].state == "different"
    assert records["shelf-r0"].state == "different" and len(records["shelf-r0"].compared_parts) == 2
    assert records["back-base-1"].state == records["back-base-2"].state == "different"
    assert records["back-upper-1"].state == records["back-upper-2"].state == "represented"
    assert records["unmodeled-detail"].state == "different"
    reasons = reference_discrepancies(req, result)
    assert len(reasons) == sum(o.state != "represented" for o in records.values())
    matched = ReferenceConstruction(
        uprights="panels", shelves="per_bay", back="upper_only", material="birch-plywood"
    )
    assert reference_discrepancies(matched, result) == []
