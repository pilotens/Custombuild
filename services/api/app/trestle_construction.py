"""Bounded, deterministic construction without a CAD dependency in the API image."""

from custombuild_domain.trestle_construction import TrestleConstruction, construct_trestle
from custombuild_domain.trestle_planning import TrestleRequest
from fastapi import APIRouter

from .api import PrincipalDep, _validation_error

router = APIRouter(prefix="/v1/constructions")


@router.post("/trestle-table", response_model=TrestleConstruction)
def trestle_construction(request: TrestleRequest, principal: PrincipalDep) -> TrestleConstruction:
    try:
        return construct_trestle(request)
    except ValueError as exc:
        raise _validation_error(exc) from exc
