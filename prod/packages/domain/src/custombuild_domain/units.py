"""Exact unit conversion at the domain/CAD boundary.

All persisted geometry is represented as integer micrometres.  Floating point
values are deliberately not accepted by the domain models.
"""

from __future__ import annotations

from decimal import Decimal

UM_PER_MM = 1_000
UM_PER_M = 1_000_000


def mm(value: int | str | Decimal) -> int:
    """Convert an exact millimetre value to integer micrometres.

    Decimal/string input is supported for fixtures and catalogues.  A value
    that cannot be represented as a whole micrometre is rejected rather than
    rounded silently.
    """

    converted = Decimal(value) * UM_PER_MM
    integral = converted.to_integral_value()
    if converted != integral:
        raise ValueError(f"{value!r} mm is not representable as whole micrometres")
    return int(integral)


def to_mm(value_um: int) -> Decimal:
    return Decimal(value_um) / UM_PER_MM


def metres(value_um: int) -> Decimal:
    return Decimal(value_um) / UM_PER_M
