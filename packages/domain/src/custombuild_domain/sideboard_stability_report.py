"""Evidence for the bounded static screen of an unanchored, closed S1 carcass."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import FrozenModel

Finite = Annotated[float, Field(allow_inf_nan=False)]


class BaseTestRecord(FrozenModel):
    assembly_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(min_length=1, max_length=160, pattern=r"\S")
    batch: str = Field(min_length=1, max_length=120, pattern=r"\S")
    case: Literal["S1-closed-combined-v1"]
    total_vertical_n: Finite = Field(strict=True, gt=0, le=100000)
    horizontal_n: Finite = Field(strict=True, ge=0, le=10000)


class TippingEdge(FrozenModel):
    edge: Literal["vänster", "höger", "fram", "bak"]
    empty_reserve_nm: Finite
    fully_loaded_reserve_nm: Finite
    minimum_reserve_nm: Finite


class ClosedStability(FrozenModel):
    wood_mass_kg: Finite = Field(gt=0)
    horizontal_force_n: Finite = Field(ge=0)
    top_payload_kg: Finite = Field(ge=0)
    push_moment_nm: Finite = Field(ge=0)
    edges: tuple[TippingEdge, ...] = Field(min_length=4, max_length=4)
    positive_reserve: bool = Field(strict=True)
    total_vertical_n: Finite = Field(gt=0)
    base_test_comparison: Literal[
        "missing", "wrong_assembly", "wrong_batch", "outside_recorded_case", "within_recorded_case"
    ]
    basis: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def consistent(self) -> "ClosedStability":
        if len({r.edge for r in self.edges}) != 4:
            raise ValueError("Fyra olika tippkanter krävs.")
        if any(
            r.minimum_reserve_nm > min(r.empty_reserve_nm, r.fully_loaded_reserve_nm) + 1e-8
            for r in self.edges
        ):
            raise ValueError("Minsta reserv överstiger ett av lastfallen.")
        if self.positive_reserve != all(r.minimum_reserve_nm > 0 for r in self.edges):
            raise ValueError("Tippresultatet motsäger kantreserverna.")
        return self
