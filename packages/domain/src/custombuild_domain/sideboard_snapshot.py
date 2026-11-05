"""A distinct source schema; never reinterpret an existing studio sideboard draft."""

from typing import Literal

from pydantic import Field

from .models import FrozenModel
from .sideboard_construction import SideboardRequest


class SideboardWorkspace(FrozenModel):
    schema_version: Literal["custombuild.sideboard-workspace.v1"] = (
        "custombuild.sideboard-workspace.v1"
    )
    project_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    revision: int = Field(strict=True, ge=1)
    request: SideboardRequest
