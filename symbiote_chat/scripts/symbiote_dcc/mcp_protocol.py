"""MCP JSON-RPC handling, separate from sockets and from any DCC so it can be tested alone."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from symbiote_dcc import tool_arguments

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_VERSION = "0.1.0"
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_PARSE_ERROR = -32700
# A tool's tier is the plugin's own declaration of what it can do; the server decides what each allows.
TIER_META_KEY = "symbiote/tier"
TIERS = ("read", "import", "write", "run_code")
READ = "read"
RUN_CODE = "run_code"
_STUDIO_OFF = "it's off for this studio"
_STUDIO_UNKNOWN = "the panel couldn't get the studio's settings from the server, so it serves read tools only"
_STUDIO_PENDING = "the panel is still getting the studio's settings from the server; try again in a moment"
_SWITCHED_OFF = "you've switched it off in the panel"


@dataclass(frozen=True)
class AllowedTiers:
    """The tiers a tool server serves now, and which of the others the artist switched off.

    A tier outside ``tiers`` and ``switched_off`` is off for the studio (or the panel doesn't
    know yet). ``read`` is always served.
    """

    tiers: frozenset[str]
    switched_off: frozenset[str]
    # False until the panel has the studio's settings (or when it couldn't get them): a refused
    # tier then says so, rather than that the studio has it off.
    studio_known: bool = True
    # True while the panel's first fetch of the studio's settings is on its way.
    fetching: bool = False

    def __post_init__(self) -> None:
        unknown = sorted((self.tiers | self.switched_off) - set(TIERS))
        if unknown:
            raise ValueError(f"unknown tiers {unknown}")
        if READ not in self.tiers or READ in self.switched_off:
            raise ValueError("read tools are always served")
        if RUN_CODE in self.tiers:
            raise ValueError("run_code tools are never served")
        both = sorted(self.tiers & self.switched_off)
        if both:
            raise ValueError(f"tiers {both} can't be both served and switched off")

    def refusal(self, name: str, tier: str) -> str | None:
        """Why ``name`` (of ``tier``) is refused now, or ``None`` when it is served."""
        if tier in self.tiers:
            return None
        article = "an" if tier[:1] in "aeiou" else "a"
        if tier in self.switched_off:
            reason = _SWITCHED_OFF
        elif self.studio_known or tier == RUN_CODE:
            reason = _STUDIO_OFF
        elif self.fetching:
            reason = _STUDIO_PENDING
        else:
            reason = _STUDIO_UNKNOWN
        return f"{name} is {article} {tier} tool and {reason}"


READ_ONLY = AllowedTiers(frozenset({READ}), frozenset(), studio_known=False)
TierSource = Callable[[], AllowedTiers]
_current_call = threading.local()


@contextmanager
def tool_call(name: str, tier: str, allowed: TierSource) -> Iterator[None]:
    """Mark ``name`` as the tool call running on this thread, so it can re-check its tier."""
    previous = getattr(_current_call, "call", None)
    _current_call.call = (name, tier, allowed)
    try:
        yield
    finally:
        _current_call.call = previous


def tier_recheck() -> Callable[[], str | None]:
    """A check for the tool call running on this thread: its refusal now, or ``None``.

    A tool that waits on the artist (a confirmation dialog) takes this before it waits and
    runs it after they confirm, from any thread: a tier switched off while the dialog was
    open refuses the call before anything changes.
    """
    call = getattr(_current_call, "call", None)
    if call is None:
        raise RuntimeError("tier_recheck() works only inside a tool call")
    name, tier, allowed = call
    return lambda: allowed().refusal(name, tier)


class TierRefused(Exception):
    """A tool's tier is no longer served; the call changed nothing."""


_Result = TypeVar("_Result")


def refuse_if_switched_off(
    recheck: Callable[[], str | None], refused: Callable[[str], Exception] = TierRefused
) -> None:
    """Raise ``refused(reason)`` if the tool's tier is no longer served."""
    reason = recheck()
    if reason is not None:
        raise refused(reason)


def _after_recheck(
    recheck: Callable[[], str | None],
    refused: Callable[[str], Exception],
    handler: Callable[..., _Result],
    *args: Any,
) -> _Result:
    refuse_if_switched_off(recheck, refused)
    return handler(*args)


def checked_on_main_thread(
    marshal: Callable[..., Any],
    handler: Callable[..., _Result],
    *args: Any,
    refused: Callable[[str], Exception] = TierRefused,
) -> _Result:
    """Run ``handler(*args)`` through ``marshal`` (the DCC's hop to its main thread), tier re-checked first.

    Every DCC tool that changes the scene goes through this. The tool server checks the tier
    when the call arrives, but the work waits in the DCC's main-thread queue; the artist can
    switch the tier off in between, and then the work must not run. Call it on the tool server's
    thread, inside the tool call (``tier_recheck`` needs it). ``refused`` builds the adapter's
    own error from the reason.
    """
    recheck = tier_recheck()
    result: _Result = marshal(_after_recheck, recheck, refused, handler, *args)
    return result


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_listing(tools: Mapping[str, Mapping[str, Any]], allowed: AllowedTiers) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "_meta": {TIER_META_KEY: tool["tier"]},
        }
        for name, tool in tools.items()
        if tool["tier"] in allowed.tiers
    ]


def _call_tool(
    request_id: Any, params: dict[str, Any], tools: Mapping[str, Mapping[str, Any]], allowed: TierSource
) -> dict[str, Any]:
    name = params.get("name", "")
    tool = tools.get(name)
    if tool is None:
        return _error(request_id, _INVALID_PARAMS, f"Unknown tool: {params.get('name')!r}")
    refusal = allowed().refusal(name, tool["tier"])
    if refusal is not None:
        return _error(request_id, _INVALID_PARAMS, refusal)
    arguments = params.get("arguments", {})
    problem = tool_arguments.refusal(name, tool.get("inputSchema") or {}, arguments)
    if problem is not None:
        return _result(request_id, {"content": [{"type": "text", "text": problem}], "isError": True})
    try:
        with tool_call(name, tool["tier"], allowed):
            outcome = tool["handler"](**arguments)
    except Exception as exc:  # reported to the agent, which tells the artist
        text = f"{type(exc).__name__}: {exc}"
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
    return _result(request_id, {"content": [{"type": "text", "text": json.dumps(outcome)}]})


def handle_line(
    line: bytes, tools: Mapping[str, Mapping[str, Any]], server_name: str, allowed: TierSource
) -> dict[str, Any] | None:
    """Answer one request line. ``None`` means a notification, which gets no reply.

    ``allowed`` gives the tiers served now: ``tools/list`` omits the others and ``tools/call``
    refuses them, reading it afresh for each request.
    """
    try:
        request = json.loads(line)
    except ValueError:
        return _error(None, _PARSE_ERROR, "Request was not valid JSON")
    if not isinstance(request, dict):
        return _error(None, _PARSE_ERROR, "Request was not a JSON object")
    request_id = request.get("id")
    method = request.get("method", "")
    if request_id is None:
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": _SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": _tool_listing(tools, allowed())})
    if method == "tools/call":
        return _call_tool(request_id, request.get("params") or {}, tools, allowed)
    if method == "ping":
        return _result(request_id, {})
    return _error(request_id, _METHOD_NOT_FOUND, f"Method not found: {method}")
