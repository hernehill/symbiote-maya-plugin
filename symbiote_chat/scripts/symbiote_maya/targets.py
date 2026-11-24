"""The (Maya year, platform) pairs Symbiote Chat for Maya is measured on, and the platform names it uses.

Pure stdlib, so the measurement tools, ``install.py`` and ``build_release.py`` can load it by path under any Python.
The release ``.mod`` carries one strict line per pair: a line without ``MAYAVERSION:`` or ``PLATFORM:`` would load the
module in every Maya, measured or not, so ``mod_pairs`` rejects it.
"""

from __future__ import annotations

import re

# ``sys.platform`` -> the platform name in the gates and the ``.mod`` (Maya's ``PLATFORM:`` token).
PLATFORMS = {"darwin": "mac", "linux": "linux"}

TARGETS: tuple[tuple[str, str], ...] = (("2027", "mac"),)

# ASCII: a Unicode digit or space must not pass for one.
MOD_LINE = re.compile(r"^\+ MAYAVERSION:(\d{4}) PLATFORM:(mac|linux) symbiote_chat (\S+) \./symbiote_chat$", re.ASCII)


def mod_template() -> str:
    """The release ``.mod``: one module-relative line per target, ``{version}`` left for the build to fill."""
    return "".join(
        f"+ MAYAVERSION:{year} PLATFORM:{platform} symbiote_chat {{version}} ./symbiote_chat\n"
        for year, platform in TARGETS
    )


def mod_pairs(text: str) -> list[tuple[str, str, str]]:
    """(year, platform, version) for each line of a release ``.mod``; ``ValueError`` names any other non-empty line.

    Split on ``\n`` only, so a stray ``\r`` (or any other line break) stays in its line and fails the match.
    """
    pairs = []
    for line in text.split("\n"):
        if not line:
            continue
        found = MOD_LINE.fullmatch(line)
        if found is None:
            raise ValueError(f"not a strict symbiote_chat .mod line (MAYAVERSION and PLATFORM both set): {line!r}")
        year, platform, version = found.groups()
        pairs.append((year, platform, version))
    return pairs
