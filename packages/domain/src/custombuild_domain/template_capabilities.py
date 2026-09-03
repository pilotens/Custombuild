"""Immutable server-owned production capability registry for furniture templates.

The browser may choose a template identifier, but it can never assert that a
template is production supported.  Every frozen revision stores the exact
server snapshot and SHA-256 fingerprint returned by this module.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from .identity import content_hash
from .models import BOOKCASE_TEMPLATE_VERSION, FrozenModel, StableKey

TEMPLATE_CAPABILITY_REGISTRY_VERSION = "custombuild-template-capabilities-1.4.0"


class TemplateProductionLevel(StrEnum):
    SCREENED = "screened"
    CONCEPT = "concept"


class TemplateCapability(FrozenModel):
    template_id: StableKey
    template_version: StableKey
    production_level: TemplateProductionLevel
    archetype: Literal["bookcase", "wall_library"]
    allowed_furniture_types: tuple[Literal["bookcase", "wall_library"], ...]
    limitation: str | None = Field(default=None, max_length=500)

    @property
    def capability_fingerprint(self) -> str:
        return content_hash(self)

    def snapshot(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload["capability_fingerprint"] = self.capability_fingerprint
        return payload


_CAPABILITIES = (
    TemplateCapability(
        template_id="shelving",
        template_version=BOOKCASE_TEMPLATE_VERSION,
        production_level=TemplateProductionLevel.SCREENED,
        archetype="bookcase",
        allowed_furniture_types=("bookcase",),
    ),
    TemplateCapability(
        template_id="wall-library",
        template_version="1.0.0",
        production_level=TemplateProductionLevel.CONCEPT,
        archetype="wall_library",
        allowed_furniture_types=("wall_library",),
        limitation=(
            "Wall-library revisions have no versioned hinge catalogue, boring pattern, "
            "front clearance model or dry/mechanical retention system."
        ),
    ),
    TemplateCapability(
        template_id="sideboard",
        template_version="0.1.0",
        production_level=TemplateProductionLevel.CONCEPT,
        archetype="wall_library",
        allowed_furniture_types=("wall_library",),
        limitation=(
            "The sideboard uses wall-library carcass geometry but has no verified "
            "door, hinge, drawer or hardware system."
        ),
    ),
    TemplateCapability(
        template_id="room-divider",
        template_version="0.1.0",
        production_level=TemplateProductionLevel.CONCEPT,
        archetype="bookcase",
        allowed_furniture_types=("bookcase",),
        limitation=(
            "Freestanding stability, bidirectional use and anchoring are not modelled."
        ),
    ),
    TemplateCapability(
        template_id="hanging-shelf",
        template_version="0.1.0",
        production_level=TemplateProductionLevel.CONCEPT,
        archetype="bookcase",
        allowed_furniture_types=("bookcase",),
        limitation=(
            "Wall brackets, fixing points and substrate capacity are not modelled."
        ),
    ),
    TemplateCapability(
        template_id="cupboard",
        template_version="0.1.0",
        production_level=TemplateProductionLevel.CONCEPT,
        archetype="wall_library",
        allowed_furniture_types=("wall_library",),
        limitation=(
            "Tall doors, hinges, front clearances and hardware capacity are not verified."
        ),
    ),
)

TEMPLATE_CAPABILITIES = MappingProxyType(
    {capability.template_id: capability for capability in _CAPABILITIES}
)
TEMPLATE_CAPABILITY_REGISTRY_FINGERPRINT = content_hash(
    {
        "registry_version": TEMPLATE_CAPABILITY_REGISTRY_VERSION,
        "templates": [item.snapshot() for item in _CAPABILITIES],
        "authoritative_domain_template": f"bookcase@{BOOKCASE_TEMPLATE_VERSION}",
    }
)


class TemplateCapabilityError(ValueError):
    def __init__(self, code: str, message: str, solution: str, template_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.solution = solution
        self.template_id = template_id

    def as_detail(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "solution": self.solution,
            "template_id": self.template_id,
        }


def resolve_template_capability(template_id: str) -> TemplateCapability:
    try:
        return TEMPLATE_CAPABILITIES[template_id]
    except KeyError as exc:
        raise TemplateCapabilityError(
            "UNKNOWN_TEMPLATE",
            f"Template {template_id!r} is not present in the server capability registry.",
            "Choose a template returned by GET /v1/capabilities/templates.",
            template_id,
        ) from exc


def require_template_for_revision(
    template_id: str, furniture_type: str
) -> TemplateCapability:
    capability = resolve_template_capability(template_id)
    if capability.production_level is not TemplateProductionLevel.SCREENED:
        raise TemplateCapabilityError(
            "TEMPLATE_CAPABILITY_BLOCKED",
            capability.limitation or "The selected template is a concept model.",
            (
                "Continue designing as a concept or choose the screened shelving "
                "template before creating a production revision."
            ),
            template_id,
        )
    if furniture_type not in capability.allowed_furniture_types:
        raise TemplateCapabilityError(
            "TEMPLATE_FURNITURE_TYPE_MISMATCH",
            (
                f"Template {template_id!r} cannot represent furniture_type "
                f"{furniture_type!r}."
            ),
            "Restore the template defaults or select a compatible screened template.",
            template_id,
        )
    return capability


def template_capability_registry_payload() -> dict[str, object]:
    return {
        "registry_version": TEMPLATE_CAPABILITY_REGISTRY_VERSION,
        "registry_fingerprint": TEMPLATE_CAPABILITY_REGISTRY_FINGERPRINT,
        "templates": [item.snapshot() for item in _CAPABILITIES],
    }
