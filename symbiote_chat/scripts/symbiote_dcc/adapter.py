"""What a DCC supplies to the shared panel: its name, its dialog host and its tools."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from symbiote_dcc.mcp_protocol import READ, TIERS

_NAME_PATTERN = re.compile(r"^[a-z0-9]+$")


@dataclass(frozen=True)
class DialogHost:
    display_name: str
    main_window: Callable[[], Any]
    stylesheet: Callable[[], str]


@dataclass(frozen=True)
class DccAdapter:
    name: str
    ui: DialogHost
    tools: Mapping[str, Mapping[str, Any]]
    version_label: str
    # The tool whose result answers the server's state query (#2167): the scene summary.
    state_tool: str
    # The DCC's own version ("2027"), for the panel's User-Agent; "" when it can't say.
    host_version: Callable[[], str] = lambda: ""

    def __post_init__(self) -> None:
        # The name builds file names and an agent name, so it must not carry a path.
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"DCC name must match [a-z0-9]+, got {self.name!r}")
        state = self.tools.get(self.state_tool)
        if state is None:
            raise ValueError(f"state tool {self.state_tool!r} is not one of the DCC's tools")
        # The CLI calls the state tool with {}; a required argument would fail every query.
        if state.get("inputSchema", {}).get("required"):
            raise ValueError(f"state tool {self.state_tool!r} must take no required arguments")
        # A bad plugin fails when the panel starts, not when the server reads its tools.
        for tool_name, tool in self.tools.items():
            tier = tool.get("tier")
            if tier is None:
                raise ValueError(f"tool {tool_name!r} declares no tier")
            if tier not in TIERS:
                raise ValueError(f"tool {tool_name!r} declares unknown tier {tier!r}")
        # The CLI calls the state tool to answer state queries, which the server doesn't gate
        # by tier; the CLI refuses a non-read one too, and so does the panel.
        if state["tier"] != READ:
            raise ValueError(f"the state tool {self.state_tool!r} must be a read tool (it's {state['tier']})")
