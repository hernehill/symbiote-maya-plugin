"""Which ``symbiote`` CLI versions can run a microagent for this panel."""

from __future__ import annotations

import re
from dataclasses import dataclass

# `symbiote version` prints a `features:` line, and the panel needs every one of these:
# the tool server asks for a token (#2165), and the server needs each tool's tier label
# (dcc-pr3). Features, not a version number: a CLI built from main reports the last
# release's number, and every CLI too old for the panel lacks one of them. busy-refusal:
# a CLI that queues a second command lets a dialog answer a call the server gave up on, so
# the panel needs one that refuses it instead.
REQUIRED_FEATURES = ("tool-server-token", "tool-tiers", "busy-refusal")
# Optional (#2187): a CLI that lists it exits when the DCC does, given --exit-with-parent.
PARENT_WATCH_FEATURE = "exit-with-parent"
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.search(output)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def label(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def features(output: str) -> frozenset[str]:
    """The words on the ``features:`` line of ``symbiote version``; empty when there is none."""
    for line in output.splitlines():
        name, separator, listed = line.partition(":")
        if separator and name.strip() == "features":
            return frozenset(listed.replace(",", " ").split())
    return frozenset()


def missing_features(output: str) -> list[str]:
    """The required features the ``features:`` line of ``symbiote version`` does not list."""
    listed = features(output)
    return [feature for feature in REQUIRED_FEATURES if feature not in listed]


@dataclass(frozen=True)
class CliCheck:
    """What one run of ``symbiote version`` says: why the CLI is unusable, and what it can do."""

    problem: str | None
    features: frozenset[str]

    def has(self, feature: str) -> bool:
        return feature in self.features
