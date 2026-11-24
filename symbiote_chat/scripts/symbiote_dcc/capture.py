"""The viewport capture tool every DCC shares: consent, the picture's file, and what comes back.

A DCC supplies how to find the viewer the artist can see and how to render it; the order around
them (find, ask, render, measure, describe) is here, so every DCC asks the same way.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symbiote_dcc import consent, tool_support
from symbiote_dcc.adapter import DialogHost
from symbiote_dcc.settings import config_dir

# Held while a capture confirmation is on screen; the dialog-refusing tools read it.
CAPTURE_OPEN = threading.Lock()

DEFAULT_RESOLUTION = (1280, 720)
# Matches the server's MAX_DECODED_BYTES: the reply carries the image twice in base64,
# so anything larger overruns the 16 MiB WebSocket limit and drops the connection.
MAX_CAPTURE_BYTES = 6_000_000

_LOCKED = {
    "capture": "A capture confirmation is already open in {display_name}.",
    "frame": "A capture confirmation is open in {display_name}, so the view cannot move now.",
}

CAPTURE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_image": {
            "type": "boolean",
            "description": "Add the PNG as base64. Only for callers that decode it.",
        },
        "deadline_seconds": {
            "type": "integer",
            "description": "Seconds the caller will wait; set by the Symbiote server.",
        },
        "deadline_unix": {
            "type": "number",
            "description": "Unix time the caller stops waiting; set by the Symbiote server.",
        },
    },
    "additionalProperties": False,
}


class CaptureTooLarge(ValueError):
    """The picture is over the size that can be sent back."""


@dataclass(frozen=True)
class Viewer:
    """The viewer a capture would take: ``name`` is what the consent dialog shows, ``handle`` the DCC's own."""

    name: str
    handle: Any


def describe(display_name: str) -> str:
    """The capture tool's description, naming the DCC."""
    return (
        f"Capture the artist's current {display_name} 3D viewport at the current frame to a PNG on "
        "their workstation. Returns its path, frame and camera. The result does not contain "
        "the picture unless include_image is true, in which case the artist is asked first "
        "and {captured: false, reason} comes back if they decline."
    )


@contextmanager
def one_capture_at_a_time(host: DialogHost, what: str) -> Iterator[None]:
    """Hold the capture lock around a capture or a frame, or refuse while a capture dialog is open.

    Take it on the tool server's thread, before the main-thread hop: a second call is refused
    there, without queueing behind the dialog. ``what`` is ``"capture"`` or ``"frame"``.
    """
    if what not in _LOCKED:
        raise ValueError(f"what must be one of {sorted(_LOCKED)}, got {what!r}")
    if not CAPTURE_OPEN.acquire(blocking=False):
        raise tool_support.ToolError(_LOCKED[what].format(display_name=host.display_name))
    try:
        yield
    finally:
        CAPTURE_OPEN.release()


def capture_payload(path: Path, *, include_image: bool, limit_bytes: int) -> dict[str, Any]:
    """Describe a capture, adding its pixels only when the caller asks.

    Off by default: a tool result is text, so a direct call would otherwise put
    hundreds of kilobytes of base64 into the conversation.
    """
    if not path.is_file():
        raise ValueError(f"The viewport wrote no image at {path}")
    size = path.stat().st_size
    if size > limit_bytes:
        raise CaptureTooLarge(f"The capture is {size} bytes, over the {limit_bytes} byte limit")
    payload: dict[str, Any] = {"path": str(path), "bytes": size, "mime_type": "image/png"}
    if include_image:
        return {**payload, "image_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
    return payload


def capture_tool(
    host: DialogHost,
    dcc_name: str,
    *,
    find_viewer: Callable[[], Viewer | None],
    render: Callable[[Any, Path], None],
    metadata: Callable[[Any], dict[str, Any]],
    to_rgb_png: Callable[[Path], None] | None,
    include_image: bool,
    deadline_seconds: int,
    deadline_unix: float,
) -> dict[str, Any]:
    """Capture the visible viewer; a picture that leaves the machine needs the artist's yes.

    Call it inside the DCC's main-thread hop, holding ``one_capture_at_a_time``. ``render``
    writes a PNG of the viewer's handle at ``DEFAULT_RESOLUTION`` to the path it is given;
    ``to_rgb_png`` (or ``None``) then rewrites that file in place; ``metadata`` names the
    frame, viewport and camera. A declined picture comes back as ``{captured: False, reason}``,
    and a direct call's failure as a ``ToolError``.
    """
    viewer = find_viewer()
    if viewer is None:
        if include_image:
            return consent.declined("no_viewport")
        raise tool_support.ToolError(f"{host.display_name} has no 3D viewport open to capture.")
    if include_image:
        reason = consent.decline_reason(host, deadline_seconds, deadline_unix, viewer.name)
        if reason is not None:
            return consent.declined(reason)
    target = config_dir() / dcc_name / "viewport-latest.png"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.unlink(missing_ok=True)
    render(viewer.handle, target)
    try:
        payload = _measured(target, to_rgb_png, include_image)
    except CaptureTooLarge as exc:
        if include_image:
            return consent.declined("too_large")
        raise tool_support.ToolError(str(exc)) from exc
    except ValueError as exc:
        raise tool_support.ToolError(str(exc)) from exc
    return {**payload, **metadata(viewer.handle), "resolution": list(DEFAULT_RESOLUTION)}


def _measured(target: Path, to_rgb_png: Callable[[Path], None] | None, include_image: bool) -> dict[str, Any]:
    if to_rgb_png is not None and target.is_file():  # a missing file is capture_payload's to name
        to_rgb_png(target)
    return capture_payload(target, include_image=include_image, limit_bytes=MAX_CAPTURE_BYTES)
