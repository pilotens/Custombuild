"""A distinct saved source for the shared immutable furniture review pipeline."""

from typing import Annotated, Literal

from pydantic import Field

from .models import FrozenModel
from .trestle_planning import TrestleRequest


class TrestleWorkspace(FrozenModel):
    schema_version: Literal["custombuild.trestle-workspace.v1"] = "custombuild.trestle-workspace.v1"
    project_id: Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")]
    revision: Annotated[int, Field(strict=True, ge=1)]
    request: TrestleRequest
