from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.schemas import BookcasePreviewInput
from custombuild_domain import BookcaseParameters

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "packages" / "contracts" / "design-constraints.v1.json"
OPENAPI_PATH = ROOT / "packages" / "contracts" / "openapi.json"
TEMPLATE_PATH = (
    ROOT
    / "packages"
    / "template-sdk"
    / "src"
    / "custombuild_templates"
    / "data"
    / "bookcase.v1.json"
)
FRONTEND_ENVELOPE_PATH = ROOT / "apps" / "web" / "lib" / "workspace-design-envelope.ts"

API_FIELDS = (
    "width_mm",
    "height_mm",
    "depth_mm",
    "shelf_count",
    "divider_count",
    "load_per_shelf_kg",
    "base_cabinet_count",
    "base_cabinet_height_mm",
    "base_cabinet_depth_mm",
)

TEMPLATE_FIELDS = {
    "width_mm": ("width_um", 1_000),
    "height_mm": ("height_um", 1_000),
    "depth_mm": ("depth_um", 1_000),
    "shelf_count": ("shelf_count", 1),
    "divider_count": ("vertical_divider_count", 1),
    "base_cabinet_count": ("base_cabinet_count", 1),
    "base_cabinet_height_mm": ("base_cabinet_height_um", 1_000),
    "base_cabinet_depth_mm": ("base_cabinet_depth_um", 1_000),
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _contract() -> dict[str, Any]:
    return _read_json(CONTRACT_PATH)


def _canonical_fingerprint(value: dict[str, Any]) -> str:
    payload = copy.deepcopy(value)
    payload.pop("fingerprint")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounds(value: dict[str, Any]) -> tuple[int | float, int | float]:
    return value["minimum"], value["maximum"]


def _frontend_call_bounds(
    source: str,
    *,
    function: str,
    expression: str,
    path: str,
) -> tuple[int, int]:
    pattern = re.compile(
        rf"{function}\(\s*{re.escape(expression)}\s*,\s*\"{re.escape(path)}\"\s*,"
        r"\s*([0-9_]+)\s*,\s*([0-9_]+)\s*,\s*code\s*,?\s*\)",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"missing strict frontend bound for {path}"
    return tuple(int(item.replace("_", "")) for item in match.groups())  # type: ignore[return-value]


def test_contract_is_versioned_fingerprinted_and_fail_closed() -> None:
    contract = _contract()

    assert contract["schema_version"] == "custombuild.design-constraints.v1"
    assert contract["contract_version"] == "1.0.0"
    assert contract["fingerprint"]["algorithm"] == "sha256"
    assert contract["fingerprint"]["value"] == _canonical_fingerprint(contract)
    assert contract["safety"]["physical_cutting_authorized"] is False
    assert contract["safety"]["design_review_required"] is True


def test_derived_and_family_invariants_are_explicit() -> None:
    contract = _contract()

    assert contract["derived_fields"]["bay_count"] == {
        "type": "integer",
        "expression": "divider_count + 1",
        "minimum": 1,
        "maximum": 17,
    }
    assert "base_cabinet_depth_mm == 0" in contract["family_invariants"]["bookcase"]
    assert "base_cabinet_depth_mm == depth_mm" in contract["family_invariants"]["wall_library"]
    assert contract["representations"]["template_manifest"]["load_per_shelf"] == {
        "api_field": "load_per_shelf_kg",
        "template_field": "shelf_load_n",
        "template_minimum": 0,
        "template_maximum": 5000,
        "note": (
            "The template's 0..5000 N field is the current approximate runtime "
            "representation of the 0..500 kg API envelope. It is not a certified load "
            "class, a certified conversion, or manufacturing evidence."
        ),
    }


def test_api_model_and_committed_openapi_match_the_contract() -> None:
    contract = _contract()
    api_properties = BookcasePreviewInput.model_json_schema()["properties"]
    openapi = _read_json(OPENAPI_PATH)
    openapi_properties = openapi["components"]["schemas"]["BookcasePreviewInput"]["properties"]

    for field in API_FIELDS:
        expected = _bounds(contract["envelope"][field])
        assert _bounds(api_properties[field]) == expected
        assert _bounds(openapi_properties[field]) == expected

    assert api_properties["bay_width_ratios"]["maxItems"] == 17
    assert api_properties["shelf_height_ratios"]["maxItems"] == 40
    assert openapi_properties["bay_width_ratios"]["maxItems"] == 17
    assert openapi_properties["shelf_height_ratios"]["maxItems"] == 40


def test_template_manifest_matches_the_contract_representations() -> None:
    contract = _contract()
    parameters = {
        parameter["key"]: parameter for parameter in _read_json(TEMPLATE_PATH)["parameters"]
    }

    for contract_field, (template_field, scale) in TEMPLATE_FIELDS.items():
        minimum, maximum = _bounds(contract["envelope"][contract_field])
        assert _bounds(parameters[template_field]) == (minimum * scale, maximum * scale)

    load_representation = contract["representations"]["template_manifest"]["load_per_shelf"]
    assert _bounds(parameters[load_representation["template_field"]]) == (
        load_representation["template_minimum"],
        load_representation["template_maximum"],
    )
    assert "not a certified load class" in load_representation["note"]


def test_domain_model_matches_the_contract_representations() -> None:
    contract = _contract()
    properties = BookcaseParameters.model_json_schema()["properties"]

    for contract_field, (domain_field, scale) in TEMPLATE_FIELDS.items():
        minimum, maximum = _bounds(contract["envelope"][contract_field])
        assert _bounds(properties[domain_field]) == (minimum * scale, maximum * scale)

    load_representation = contract["representations"]["template_manifest"]["load_per_shelf"]
    assert _bounds(properties["shelf_load_n"]) == (
        load_representation["template_minimum"],
        load_representation["template_maximum"],
    )


def test_frontend_strict_hydration_matches_the_contract() -> None:
    contract = _contract()
    source = FRONTEND_ENVELOPE_PATH.read_text(encoding="utf-8")
    checks = {
        "width_mm": ("finiteNumber", "root.width_mm", "spec_json.width_mm"),
        "height_mm": ("finiteNumber", "root.height_mm", "spec_json.height_mm"),
        "depth_mm": ("finiteNumber", "root.depth_mm", "spec_json.depth_mm"),
        "shelf_count": ("integer", "root.shelf_count", "spec_json.shelf_count"),
        "divider_count": ("integer", "root.divider_count", "spec_json.divider_count"),
        "load_per_shelf_kg": (
            "finiteNumber",
            "root.load_per_shelf_kg",
            "spec_json.load_per_shelf_kg",
        ),
        "base_cabinet_count": (
            "integer",
            "root.base_cabinet_count",
            "spec_json.base_cabinet_count",
        ),
        "base_cabinet_height_mm": (
            "finiteNumber",
            "root.base_cabinet_height_mm",
            "spec_json.base_cabinet_height_mm",
        ),
        "base_cabinet_depth_mm": (
            "finiteNumber",
            "root.base_cabinet_depth_mm",
            "spec_json.base_cabinet_depth_mm",
        ),
    }

    for field, (function, expression, path) in checks.items():
        assert _frontend_call_bounds(
            source,
            function=function,
            expression=expression,
            path=path,
        ) == _bounds(contract["envelope"][field])

    normalized = re.sub(r"\s+", " ", source)
    assert "spec.base_cabinet_depth_mm !== spec.depth_mm" in normalized
    assert "spec.base_cabinet_count !== 0" in normalized
    assert "spec.base_cabinet_height_mm !== 0" in normalized
    assert "spec.base_cabinet_depth_mm !== 0" in normalized
    assert (
        "spec.base_cabinet_height_mm >= spec.height_mm - spec.measured_thickness_mm - 200"
        in normalized
    )
