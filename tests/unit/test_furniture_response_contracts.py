from copy import deepcopy

import pytest
from app.furniture_schemas import (
    FurnitureCatalogRead,
    FurniturePreviewRead,
    FurnitureProfileChangeRead,
    FurnitureShelfSuggestionRead,
)
from app.main import app
from custombuild_domain.furniture import ManufacturingSelection, ProfileChange
from custombuild_domain.furniture_catalog import furniture_catalog
from custombuild_manufacturing.furniture_profiles import (
    compare_furniture_profiles,
    furniture_machine_catalog,
    preview_furniture,
)
from custombuild_manufacturing.furniture_suggestions import suggest_shelf_bays
from pydantic import ValidationError

from tests.unit.test_furniture_families import workspace


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
@pytest.mark.parametrize("machine", [False, True])
def test_preview_response_preserves_complete_domain_payload(family, machine):
    draft = workspace(family)
    if machine:
        draft = draft.model_copy(
            update={
                "manufacturing": ManufacturingSelection(
                    machine_profile_id="custombuild-router-1325-linuxcnc",
                )
            }
        )
    payload = preview_furniture(draft)
    assert (
        FurniturePreviewRead.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        == payload
    )


def test_catalog_response_preserves_full_profiles():
    payload = {**furniture_catalog(), "machines": furniture_machine_catalog()}
    assert FurnitureCatalogRead.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
def test_suggestion_response_preserves_complete_screening(family):
    payload = suggest_shelf_bays(workspace(family))
    assert (
        FurnitureShelfSuggestionRead.model_validate(payload).model_dump(
            mode="json", exclude_unset=True
        )
        == payload
    )


@pytest.mark.parametrize(
    "section,field",
    [
        (None, "physical_cutting_authorized"),
        (None, "production_qualified"),
        ("manufacturing", "production_qualified"),
        ("workshop_handoff", "physical_cutting_authorized"),
    ],
)
def test_response_schema_rejects_accidental_production_authority(section, field):
    payload = deepcopy(preview_furniture(workspace()))
    target = payload if section is None else payload[section]
    target[field] = True
    with pytest.raises(ValidationError):
        FurniturePreviewRead.model_validate(payload)


def test_profile_response_preserves_optional_consequences():
    draft = workspace()
    payload = compare_furniture_profiles(ProfileChange(current=draft, proposed=draft))
    assert (
        FurnitureProfileChangeRead.model_validate(payload).model_dump(
            mode="json", exclude_unset=True
        )
        == payload
    )


@pytest.mark.parametrize(
    "path,method,schema",
    [
        ("/catalog", "get", "FurnitureCatalogRead"),
        ("/preview", "post", "FurniturePreviewRead"),
        ("/profile-change", "post", "FurnitureProfileChangeRead"),
        ("/shelf-bay-suggestion", "post", "FurnitureShelfSuggestionRead"),
        ("/projects/{project_id}/draft", "get", "FurnitureDraftRead"),
        ("/projects/{project_id}/draft", "put", "FurnitureDraftRead"),
        ("/projects/{project_id}/history", "get", "FurnitureHistoryRead"),
    ],
)
def test_openapi_exposes_named_furniture_response_contracts(path, method, schema):
    response = app.openapi()["paths"][f"/v1/furniture{path}"][method]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{schema}"
    }
