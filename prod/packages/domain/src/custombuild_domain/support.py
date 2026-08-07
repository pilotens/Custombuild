"""Versioned end-to-end support claims for production-affecting joint systems."""

from __future__ import annotations

from types import MappingProxyType

from .enums import JointType

BOOKCASE_JOINT_SUPPORT_VERSION = "bookcase-joints-1.0.0"
WEIGHT_BASIS = "conservative-finished-blank-before-machining-v1"

# ``supported`` means the complete domain -> geometry -> DFM -> CAM -> assembly
# chain is implemented for the stated use.  A blocked entry may still have
# experimental domain helpers; those helpers are not a production support claim.
BOOKCASE_JOINT_SUPPORT_MATRIX = MappingProxyType(
    {
        JointType.DOWEL: MappingProxyType(
            {
                "status": "blocked",
                "scope": "primary-carcass",
                "reason": "Matching edge-boring setups and assembly access are not verified.",
            }
        ),
        JointType.CONFIRMAT: MappingProxyType(
            {
                "status": "blocked",
                "scope": "primary-carcass",
                "reason": "Edge drilling requires a documented setup unavailable in the MVP.",
            }
        ),
        JointType.CAM_DOWEL: MappingProxyType(
            {
                "status": "blocked",
                "scope": "primary-carcass",
                "reason": "The supplier-specific boring pattern is not versioned and verified.",
            }
        ),
        JointType.SHELF_PIN: MappingProxyType(
            {
                "status": "conditional",
                "scope": "adjustable-shelves-only",
                "reason": "Supported only when shelf_mount=adjustable; not a carcass joint.",
            }
        ),
        JointType.RABBET: MappingProxyType(
            {
                "status": "blocked",
                "scope": "primary-carcass",
                "reason": "Mating geometry and fastening capacity are not production-verified.",
            }
        ),
        JointType.DADO: MappingProxyType(
            {
                "status": "supported",
                "scope": "bookcase-production-mvp",
                "reason": "Deterministic mating geometry, DFM, CAM and assembly sequence exist.",
            }
        ),
        JointType.TENON: MappingProxyType(
            {
                "status": "blocked",
                "scope": "primary-carcass",
                "reason": "Authoritative CAD and toolpath generation are not implemented.",
            }
        ),
    }
)

SUPPORTED_BOOKCASE_PRIMARY_JOINTS = frozenset({JointType.DADO})


def joint_support_payload() -> dict[str, object]:
    """Return a stable JSON-ready representation for UI/API capability displays."""

    return {
        "version": BOOKCASE_JOINT_SUPPORT_VERSION,
        "joints": {joint.value: dict(BOOKCASE_JOINT_SUPPORT_MATRIX[joint]) for joint in JointType},
    }
