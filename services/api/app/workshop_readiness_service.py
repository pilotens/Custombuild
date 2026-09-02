from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DesignVersion, GenerationJob, JobStatus, Release

WorkshopPreparationBlockerCode = Literal[
    "EXECUTABLE_MACHINE_PROGRAM_MISSING",
    "WORKSHOP_EXECUTABLE_PACKAGE_MISSING",
    "WORKSHOP_GENERATION_JOB_NOT_READY",
]

EXECUTABLE_MACHINE_PROGRAM_MISSING: Final[WorkshopPreparationBlockerCode] = (
    "EXECUTABLE_MACHINE_PROGRAM_MISSING"
)
WORKSHOP_EXECUTABLE_PACKAGE_MISSING: Final[WorkshopPreparationBlockerCode] = (
    "WORKSHOP_EXECUTABLE_PACKAGE_MISSING"
)
WORKSHOP_GENERATION_JOB_NOT_READY: Final[WorkshopPreparationBlockerCode] = (
    "WORKSHOP_GENERATION_JOB_NOT_READY"
)


@dataclass(frozen=True, slots=True)
class WorkshopPreparationBlocker(Exception):
    """One stable, actionable reason a workshop run cannot be prepared."""

    code: WorkshopPreparationBlockerCode
    message: str
    solution: str

    def as_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "solution": self.solution,
            "workshop_status": "BLOCKED",
            "release_review_eligible": False,
            "cutting_blocker_codes": (self.code,),
            "physical_cutting_authorized": False,
        }


def _block_generation_not_ready() -> NoReturn:
    raise WorkshopPreparationBlocker(
        code=WORKSHOP_GENERATION_JOB_NOT_READY,
        message=(
            "The selected generation job is not a successful result for this exact "
            "tenant design revision."
        ),
        solution="Complete a new server-owned generation for this exact revision.",
    )


def _block_executable_program_missing() -> NoReturn:
    raise WorkshopPreparationBlocker(
        code=EXECUTABLE_MACHINE_PROGRAM_MISSING,
        message=(
            "The selected generation contains no server-identified executable machine "
            "program. Current machine programs are validation-only."
        ),
        solution=(
            "Generate an executable program through a future machine-, tool-, material- "
            "and setup-bound production workflow."
        ),
    )


def _block_executable_package_missing() -> NoReturn:
    raise WorkshopPreparationBlocker(
        code=WORKSHOP_EXECUTABLE_PACKAGE_MISSING,
        message=(
            "No immutable executable workshop package is bound to the selected release. "
            "A design-review release cannot start a physical workshop run."
        ),
        solution=(
            "Create a server-verified executable release package before preparing a "
            "workshop run."
        ),
    )


def _claims_executable_program(result: Mapping[str, Any]) -> bool:
    """Recognize only explicit executable claims; ambiguous values fail closed.

    This predicate is intentionally insufficient for preparation. Even an exact
    claim must still be backed by a persisted executable package contract, which
    the current release model does not provide.
    """

    return (
        result.get("machine_program_mode") == "EXECUTABLE"
        and result.get("production_machine_program") is True
    )


def require_workshop_preparation_source(
    session: Session,
    *,
    organization_id: str,
    version: DesignVersion,
    generation_job_id: str,
) -> NoReturn:
    """Resolve server-owned production state and reject today's unsafe boundary.

    The request supplies only an opaque job identity. Every production fact is
    re-derived from tenant-scoped database rows. The function deliberately has
    no success path until an immutable executable-package model exists; it must
    never turn mutable JSON claims into physical-cutting authority.
    """

    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == generation_job_id,
            GenerationJob.organization_id == organization_id,
            GenerationJob.design_version_id == version.id,
        )
    )
    if (
        job is None
        or job.status is not JobStatus.succeeded
        or not isinstance(job.result_json, dict)
    ):
        _block_generation_not_ready()

    release = session.scalar(
        select(Release).where(
            Release.organization_id == organization_id,
            Release.design_version_id == version.id,
        )
    )
    if (
        release is None
        or release.generation_job_id != job.id
        or release.production_context_hash != job.production_context_hash
        or release.manifest_sha256 != job.result_json.get("manifest_sha256")
        or release.generation_result_json != job.result_json
    ):
        _block_executable_package_missing()

    if not _claims_executable_program(job.result_json):
        _block_executable_program_missing()

    # The current Release schema stores only a design-review bundle and has no
    # executable program inventory, exact setup identity or workshop policy.
    # Never infer those physical facts from result JSON or artifact filenames.
    _block_executable_package_missing()
