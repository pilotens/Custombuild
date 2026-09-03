import pytest
from app.api import _artifact_filename, _release_artifact_filename
from fastapi import HTTPException

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def test_download_filenames_describe_design_review_scope() -> None:
    prefix = f"custombuild-project-{PROJECT_ID}-"
    assert _artifact_filename("production_bundle", 7, PROJECT_ID, "application/zip") == (
        f"{prefix}design-review-rev-7.zip"
    )
    assert _artifact_filename("manifest", 7, PROJECT_ID, "application/json") == (
        f"{prefix}design-review-manifest-rev-7.json"
    )
    assert _artifact_filename("stock_selection", 7, PROJECT_ID, "application/json") == (
        f"{prefix}stock-selection-rev-7.json"
    )
    assert _artifact_filename("generation_plan", 7, PROJECT_ID, "application/json") == (
        f"{prefix}generation-plan-rev-7.json"
    )
    assert _artifact_filename("manufacturing_intent", 7, PROJECT_ID, "application/json") == (
        f"{prefix}manufacturing-intent-rev-7.json"
    )
    assert _artifact_filename("supplier_handoff", 7, PROJECT_ID, "application/json") == (
        f"{prefix}cnc-shop-handoff-rev-7.json"
    )


def test_download_filenames_are_project_unique_and_keep_media_extensions() -> None:
    first = _artifact_filename("design_glb", 7, PROJECT_ID, "model/gltf-binary")
    second = _artifact_filename("design_glb", 7, OTHER_PROJECT_ID, "model/gltf-binary")

    assert first == f"custombuild-project-{PROJECT_ID}-design-rev-7.glb"
    assert second == f"custombuild-project-{OTHER_PROJECT_ID}-design-rev-7.glb"
    assert first != second
    assert _artifact_filename("design_fcstd", 7, PROJECT_ID, "application/vnd.freecad").endswith(
        "-design-rev-7.FCStd"
    )
    assert _artifact_filename("setup_sheet_012", 7, PROJECT_ID, "image/svg+xml").endswith(
        "-setup-sheet-012-rev-7.svg"
    )


def test_release_download_filename_keeps_release_and_project_identity() -> None:
    first = _release_artifact_filename(
        PROJECT_ID,
        "ARCHIVE-R1",
        7,
        "production_bundle",
        "application/zip",
    )
    second = _release_artifact_filename(
        OTHER_PROJECT_ID,
        "ARCHIVE-R1",
        7,
        "production_bundle",
        "application/zip",
    )

    assert first == (
        f"custombuild-project-{PROJECT_ID}-release-ARCHIVE-R1-design-review-rev-7.zip"
    )
    assert second != first
    assert _release_artifact_filename(
        PROJECT_ID,
        "ARCHIVE-R1",
        7,
        "manifest",
        "application/json",
    ).endswith("-release-ARCHIVE-R1-design-review-manifest-rev-7.json")


def test_release_download_filename_bounds_long_valid_release_number() -> None:
    filename = _release_artifact_filename(
        PROJECT_ID,
        "A" * 40,
        2_147_483_647,
        "design_review_package_status",
        "application/json",
    )

    assert len(filename) <= 128
    assert filename.startswith(f"custombuild-project-{PROJECT_ID}-release-AAAAA-")
    assert filename.endswith("-design-review-package-status-rev-2147483647.json")


@pytest.mark.parametrize(
    ("project_id", "kind", "content_type"),
    (
        (PROJECT_ID, "unknown", "application/json"),
        (PROJECT_ID, "manifest", "text/html"),
        ("00000000-0000-0000-0000-000000000000", "manifest", "application/json"),
    ),
)
def test_release_download_filename_fails_closed(
    project_id: str,
    kind: str,
    content_type: str,
) -> None:
    with pytest.raises(HTTPException) as captured:
        _release_artifact_filename(
            project_id,
            "ARCHIVE-R1",
            7,
            kind,
            content_type,
        )
    assert captured.value.status_code == 409


@pytest.mark.parametrize(
    ("kind", "content_type", "expected_suffix"),
    (
        ("dfm_report", "application/json", "dfm-report-rev-7.json"),
        (
            "design_review_package_status",
            "application/json",
            "design-review-package-status-rev-7.json",
        ),
        ("operations", "application/json", "machine-neutral-operations-rev-7.json"),
        ("validation_backplot", "image/svg+xml", "validation-backplot-rev-7.svg"),
        ("cad_interchange_status", "application/json", "cad-interchange-status-rev-7.json"),
        ("source_provenance", "application/json", "source-provenance-rev-7.json"),
        ("workshop_readiness", "application/json", "workshop-readiness-rev-7.json"),
        ("assembly_readiness", "application/json", "assembly-readiness-rev-7.json"),
    ),
)
def test_download_filename_supports_every_persisted_review_kind(
    kind: str,
    content_type: str,
    expected_suffix: str,
) -> None:
    assert _artifact_filename(kind, 7, PROJECT_ID, content_type) == (
        f"custombuild-project-{PROJECT_ID}-{expected_suffix}"
    )


@pytest.mark.parametrize(
    ("kind", "revision", "project_id", "content_type"),
    (
        ("unknown", 7, PROJECT_ID, "application/json"),
        ("../manifest", 7, PROJECT_ID, "application/json"),
        ("manifest", 7, "../project", "application/json"),
        ("manifest", 7, "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "application/json"),
        ("manifest", 7, "00000000-0000-0000-0000-000000000000", "application/json"),
        ("manifest", 7, "aaaaaaaa-aaaa-6aaa-8aaa-aaaaaaaaaaaa", "application/json"),
        ("manifest", 7, "aaaaaaaa-aaaa-4aaa-7aaa-aaaaaaaaaaaa", "application/json"),
        (
            "manifest",
            7,
            "11111111-1111-4111-8111-111111111111\r\nX-Test: injected",
            "application/json",
        ),
        ("manifest", 0, PROJECT_ID, "application/json"),
        ("manifest", True, PROJECT_ID, "application/json"),
        ("manifest", 7, PROJECT_ID, "text/html"),
        ("setup_sheet_12", 7, PROJECT_ID, "image/svg+xml"),
        ("setup_sheet_001", 7, PROJECT_ID, "application/json"),
    ),
)
def test_download_filename_rejects_untrusted_or_inconsistent_identity(
    kind: str,
    revision: int,
    project_id: str,
    content_type: str,
) -> None:
    with pytest.raises(ValueError, match="artifact"):
        _artifact_filename(kind, revision, project_id, content_type)
