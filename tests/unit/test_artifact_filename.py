from app.api import _artifact_filename


def test_download_filenames_describe_design_review_scope() -> None:
    assert (
        _artifact_filename("production_bundle", 7)
        == "custombuild-design-review-rev-7.zip"
    )
    assert (
        _artifact_filename("manifest", 7)
        == "custombuild-design-review-rev-7-manifest.json"
    )
    assert _artifact_filename("unknown", 7) is None
