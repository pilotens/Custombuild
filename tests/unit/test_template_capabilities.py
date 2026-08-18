from __future__ import annotations

import pytest
from custombuild_domain import (
    TEMPLATE_CAPABILITY_REGISTRY_FINGERPRINT,
    TemplateCapabilityError,
    require_template_for_revision,
    resolve_template_capability,
    template_capability_registry_payload,
)


def test_registry_has_stable_server_owned_fingerprints() -> None:
    payload = template_capability_registry_payload()
    assert payload["registry_fingerprint"] == TEMPLATE_CAPABILITY_REGISTRY_FINGERPRINT
    assert len(str(payload["registry_fingerprint"])) == 64
    templates = {item["template_id"]: item for item in payload["templates"]}
    assert templates["shelving"]["production_level"] == "screened"
    assert templates["wall-library"]["production_level"] == "screened"
    assert templates["cupboard"]["production_level"] == "concept"
    assert all(len(str(item["capability_fingerprint"])) == 64 for item in templates.values())


def test_only_screened_matching_templates_can_create_revisions() -> None:
    capability = require_template_for_revision("shelving", "bookcase")
    assert capability.capability_fingerprint == resolve_template_capability(
        "shelving"
    ).capability_fingerprint
    with pytest.raises(TemplateCapabilityError) as concept:
        require_template_for_revision("sideboard", "wall_library")
    assert concept.value.code == "TEMPLATE_CAPABILITY_BLOCKED"
    with pytest.raises(TemplateCapabilityError) as mismatch:
        require_template_for_revision("wall-library", "bookcase")
    assert mismatch.value.code == "TEMPLATE_FURNITURE_TYPE_MISMATCH"
    with pytest.raises(TemplateCapabilityError) as unknown:
        resolve_template_capability("client-invented")
    assert unknown.value.code == "UNKNOWN_TEMPLATE"
