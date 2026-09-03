from app.api import _artifact_filename


def test_download_filenames_describe_design_review_scope() -> None:
    assert _artifact_filename("production_bundle", 7) == "custombuild-design-review-rev-7.zip"
    assert _artifact_filename("manifest", 7) == "custombuild-design-review-rev-7-manifest.json"
    assert _artifact_filename("stock_selection", 7) == "custombuild-stock-selection-rev-7.json"
    assert _artifact_filename("generation_plan", 7) == "custombuild-generation-plan-rev-7.json"
    assert _artifact_filename("manufacturing_intent", 7) == (
        "custombuild-manufacturing-intent-rev-7.json"
    )
    assert _artifact_filename("supplier_handoff", 7) == (
        "custombuild-cnc-shop-handoff-rev-7.json"
    )
    assert _artifact_filename("unknown", 7) is None
