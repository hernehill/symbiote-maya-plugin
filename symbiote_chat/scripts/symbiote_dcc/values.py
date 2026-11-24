"""Checks on the values a write tool is asked to set, shared by every DCC.

Plain Python numbers only, so ``True`` is not a number and NaN or infinity is not finite:
a DCC would accept them and store something the artist never meant.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def finite_number(value: object) -> bool:
    """A real, finite int or float; not a bool, NaN, infinity or an int too big for a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def is_strict_bool(value: object) -> bool:
    """A bool, and not the int 0 or 1."""
    return isinstance(value, bool)


def enum_choice(value: object, fields: Sequence[str]) -> int | None:
    """The index in ``fields`` that ``value`` (a field name or an index) selects, or None."""
    if isinstance(value, str):
        return fields.index(value) if value in fields else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(fields):
        return value
    return None


def vector(value: object, length: int) -> tuple[float, ...] | None:
    """``value`` as ``length`` floats if it is a list or tuple of that many finite numbers, else None."""
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    if not all(finite_number(item) for item in value):
        return None
    return tuple(float(item) for item in value)
