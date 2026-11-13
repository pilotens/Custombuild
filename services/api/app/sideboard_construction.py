"""Parameterized S1 construction and the existing durable drawing export queue."""

from custombuild_domain.sideboard_construction import (
    SideboardConstruction,
    SideboardRequest,
    construct_sideboard,
)
from fastapi import APIRouter, Depends

from .api import PrincipalDep, _validation_error
from .auth import Capability, require_capability
from .furniture_api import GeneratorDep, SessionDep
from .trestle_exports import SideboardExportRequest, TrestleExportAccepted, enqueue_construction

router = APIRouter(prefix="/v1/constructions/sideboard")


@router.post("", response_model=SideboardConstruction)
def preview_sideboard(request: SideboardRequest, principal: PrincipalDep) -> SideboardConstruction:
    try:
        return construct_sideboard(request)
    except ValueError as exc:
        raise _validation_error(exc) from exc


@router.post(
    "/exports",
    response_model=TrestleExportAccepted,
    status_code=202,
    dependencies=[Depends(require_capability(Capability.DESIGN))],
)
def export_sideboard(
    payload: SideboardExportRequest, principal: GeneratorDep, session: SessionDep
) -> TrestleExportAccepted:
    return enqueue_construction(payload, principal, session)
