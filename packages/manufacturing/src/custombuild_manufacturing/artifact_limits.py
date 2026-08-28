"""Shared fail-closed limits for persisted production artifacts.

The API, worker and package verifier must agree on the same byte ceilings.  Keep
these limits in the dependency-free manufacturing package so every boundary can
enforce them before allocating memory or trusting persisted size claims.
"""

from __future__ import annotations

from typing import Final

MAX_ARTIFACT_BYTES: Final = 32 * 1024 * 1024
# The API container has a 128 MiB /tmp tmpfs.  Reserve at most 96 MiB for
# verified spools, multipart bodies and retained review documents together so
# runtime temporary files retain a hard 32 MiB safety margin.
MAX_API_TRANSIENT_BYTES: Final = 96 * 1024 * 1024
MAX_HTTP_REQUEST_BYTES: Final = 21 * 1024 * 1024
MAX_PRODUCTION_BUNDLE_BYTES: Final = MAX_ARTIFACT_BYTES
MAX_CATALOG_SOURCE_BYTES: Final = 20 * 1024 * 1024
MAX_READINESS_STATUS_BYTES: Final = 64 * 1024
MAX_CORE_DOCUMENT_BYTES: Final = 3 * 1024 * 1024
# A production request can currently yield at most a few hundred retained setup
# and review artifacts.  Keep the persisted evidence inventory bounded well
# below the package reader's deliberately broader archive limits so the API can
# review the complete inventory without unbounded object-store work.
MAX_EVIDENCE_ARTIFACTS: Final = 512
MAX_EVIDENCE_TOTAL_BYTES: Final = 96 * 1024 * 1024

_READINESS_STATUS_KINDS: Final = frozenset(
    {
        "workshop_readiness",
        "design_review_package_status",
        "assembly_readiness",
        "cad_interchange_status",
    }
)
_CORE_DOCUMENT_KINDS: Final = frozenset(
    {
        "manifest",
        "dfm_report",
        "stock_selection",
        "generation_plan",
        "operations",
        "source_provenance",
    }
)


def artifact_size_limit(kind: str) -> int:
    """Return the canonical byte ceiling for a persisted artifact kind."""

    if kind in _READINESS_STATUS_KINDS:
        return MAX_READINESS_STATUS_BYTES
    if kind in _CORE_DOCUMENT_KINDS or kind.startswith("setup_sheet_"):
        return MAX_CORE_DOCUMENT_BYTES
    if kind == "catalog_source":
        return MAX_CATALOG_SOURCE_BYTES
    return MAX_ARTIFACT_BYTES


def valid_artifact_size(kind: str, size_bytes: object) -> bool:
    """Reject bool/coerced/zero/oversize size claims at trust boundaries."""

    return type(size_bytes) is int and size_bytes > 0 and size_bytes <= artifact_size_limit(kind)


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_API_TRANSIENT_BYTES",
    "MAX_CATALOG_SOURCE_BYTES",
    "MAX_CORE_DOCUMENT_BYTES",
    "MAX_EVIDENCE_ARTIFACTS",
    "MAX_EVIDENCE_TOTAL_BYTES",
    "MAX_PRODUCTION_BUNDLE_BYTES",
    "MAX_READINESS_STATUS_BYTES",
    "MAX_HTTP_REQUEST_BYTES",
    "artifact_size_limit",
    "valid_artifact_size",
]
