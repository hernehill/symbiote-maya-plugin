"""Which Maya is running, and whether Symbiote measured its tools on that (year, platform, update).

A year's census holds across platforms only where it was measured there, so a gate asks for the pair, and then for an
update at or above the one measured (a later update of a measured pair is allowed; an earlier one is not).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping

import maya.cmds as cmds

from symbiote_maya.targets import PLATFORMS, TARGETS

__all__ = ["measured_refusal", "platform_name", "running_api", "running_platform", "running_version", "update_name"]

_YEAR = re.compile(r"\d{4}")
_PLATFORM_NAMES = {"mac": "macOS", "linux": "Linux"}


def running_version() -> str:
    """The running Maya's year: "2027" from ``about(version=True)`` ("2027", or "2027.1" and the like).

    Text without a leading four-digit year (a preview build) comes back whole, so a gate refuses it by name.
    """
    text = str(cmds.about(version=True))
    found = _YEAR.match(text)
    return found.group(0) if found else text


def running_api() -> object:
    """``about(apiVersion=True)``: ``YYYYUUPP`` (20250303 is Maya 2025.3). Not converted, so a gate sees what Maya said."""
    api: object = cmds.about(apiVersion=True)
    return api


def running_platform() -> str:
    """The platform name the gates use ("mac", "linux"); an unknown platform is ``sys.platform`` itself."""
    return PLATFORMS.get(sys.platform, sys.platform)


def update_name(api: int) -> str:
    """ "2025.3" for 20250303: the year and the update number."""
    return f"{api // 10000}.{api // 100 % 100}"


def platform_name(platform: str) -> str:
    """The name an artist reads for a platform key: "macOS" for "mac", "Linux" for "linux"."""
    return _PLATFORM_NAMES.get(platform, platform)


def measured_refusal(what: str, measured: Mapping[tuple[str, str], int], *, year: str | None = None) -> str | None:
    """Why ``what`` stays off in this Maya, or ``None`` when it was measured here (or on an earlier update of it).

    ``measured`` maps (year, platform) to the API version it was measured at; a refusal names only the measured
    pairs that are also ``TARGETS``. The year is checked for four digits
    first, then the pair against the table, and only then is the API compared with the year. A caller that already
    read the year passes it as ``year``, so the gate and its other lookups share one reading.
    """
    year = running_version() if year is None else year
    platform = running_platform()
    if not _YEAR.fullmatch(year) or (year, platform) not in measured:
        # Only the pairs this release loads in: a measured pair that isn't a target yet is no place to go.
        pairs = ", ".join(f"Maya {y} on {platform_name(p)}" for y, p in sorted(set(measured) & set(TARGETS)))
        return (
            f"this is Maya {year} on {platform_name(platform)}, and Symbiote's {what} have only been measured in "
            f"{pairs}, so they stay off here until it is measured"
        )
    api = running_api()
    if not isinstance(api, int) or isinstance(api, bool) or api // 10000 != int(year):
        return f"Maya reports version {year!r} with API {api!r}, which don't agree, so Symbiote's {what} stay off"
    floor = measured[(year, platform)]
    if api < floor:
        return (
            f"this is Maya {update_name(api)} on {platform_name(platform)}, and Symbiote's {what} were measured in "
            f"Maya {update_name(floor)}; update to Maya {update_name(floor)} or later"
        )
    return None
