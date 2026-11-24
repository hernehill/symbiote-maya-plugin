"""What every DCC's tools share: the error they report, refusals, dialog locks and the main-thread hop.

The tool server calls a tool on its own thread; the DCC's scene API may only be used on its main
thread. ``MainThread`` wraps a DCC's hop to it (Houdini's ``hdefereval``, Maya's
``executeInMainThreadWithResult``) and adds the two checks every scene-changing tool needs: the
tier is re-checked and no confirmation dialog may be open when the work finally runs.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from symbiote_dcc import capture, mcp_protocol
from symbiote_dcc.adapter import DialogHost

_Result = TypeVar("_Result")

# One confirmation dialog at a time: an import or a delete waiting on the artist.
CONFIRMATION_OPEN = threading.Lock()

# Live check: after sizex=rand(1) was refused, the model created a plain box anyway. The skill
# already said not to; the refusal is what the model is reading when it decides, so it says so too.
REFUSED_CREATE_NEXT = (
    " Tell the artist what was refused. Don't create the node without those values, with other values"
    " or somewhere else unless the artist asks you to."
)


class ToolError(Exception):
    """A failure the calling agent should read and report."""


def refusal_text(what: str, reasons: Iterable[str]) -> str:
    """One message naming every refused part, so the chat sees all of them at once."""
    listed = "; ".join(reasons)
    return f"{what} was refused, and nothing changed: {listed}."


def _dialog_open_error(host: DialogHost) -> ToolError:
    return ToolError(f"A Symbiote confirmation is already open in {host.display_name}; answer it first.")


@contextmanager
def one_dialog_at_a_time(host: DialogHost) -> Iterator[None]:
    """Hold the confirmation lock for a dialog's whole life, or refuse if another is open."""
    if not CONFIRMATION_OPEN.acquire(blocking=False):
        raise _dialog_open_error(host)
    try:
        yield
    finally:
        CONFIRMATION_OPEN.release()


def refuse_while_a_dialog_is_open(host: DialogHost) -> None:
    """Refuse a scene change while a confirmation or capture dialog is on screen.

    Reads the locks without taking them, so it is safe on any thread and for any tool.
    """
    refuse_while_a_confirmation_is_open(host)
    if capture.CAPTURE_OPEN.locked():
        raise _dialog_open_error(host)


def refuse_while_a_confirmation_is_open(host: DialogHost) -> None:
    """Refuse while an import, delete or save confirmation is on screen; the capture lock is not read.

    For a tool that holds the capture lock itself (a frame, inside ``one_capture_at_a_time``), where
    ``refuse_while_a_dialog_is_open`` would see its own lock. Safe on any thread.
    """
    if CONFIRMATION_OPEN.locked():
        raise _dialog_open_error(host)


def _guarded(host: DialogHost, owns_dialog: bool, handler: Callable[..., _Result], *args: Any) -> _Result:
    """Runs on the main thread: a call queued before a dialog opened can be serviced inside it."""
    if not owns_dialog:
        refuse_while_a_dialog_is_open(host)
    return handler(*args)


def _require_callable(handler: object) -> None:
    # A string handed to a DCC's hop may be executed as code; refuse before it gets there.
    if not callable(handler):
        raise TypeError(f"handler must be callable, got {type(handler).__name__}")


class MainThread:
    """A DCC's hop to its main thread, with the tier and dialog checks for scene-changing work."""

    def __init__(self, marshal: Callable[..., Any], host: DialogHost) -> None:
        self._marshal = marshal
        self._host = host

    def run(self, handler: Callable[..., _Result], *args: Any) -> _Result:
        """Run ``handler`` on the main thread and return what it returned."""
        _require_callable(handler)
        result: _Result = self._marshal(handler, *args)
        return result

    def checked(self, handler: Callable[..., _Result], *args: Any, owns_dialog: bool = False) -> _Result:
        """``run`` for a tool that changes the scene: tier and open dialogs are checked first.

        Call it on the tool server's thread, inside the tool call. The tool that holds the dialog
        lock (import, delete) passes ``owns_dialog=True`` for its own work.
        """
        _require_callable(handler)
        result: _Result = mcp_protocol.checked_on_main_thread(
            self.run, _guarded, self._host, owns_dialog, handler, *args, refused=ToolError
        )
        return result

    def refuse_if_switched_off(self, recheck: Callable[[], str | None]) -> None:
        """Raise the tier's refusal as a ``ToolError`` if the tool's tier is no longer served."""
        mcp_protocol.refuse_if_switched_off(recheck, ToolError)


def checked_values(values: object, noun: str, max_count: int) -> dict[str, Any]:
    """``values`` as a dict of at most ``max_count`` entries named by strings; ``noun`` is singular (``parm``)."""
    if not isinstance(values, Mapping):
        raise ToolError(f"{noun}s must be an object of {noun} names and values.")
    if not all(isinstance(name, str) for name in values):
        raise ToolError(f"{noun}s names must be strings.")
    if len(values) > max_count:
        raise ToolError(f"Set at most {max_count} {noun}s in one call.")
    return dict(values)


def object_schema(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    """A JSON Schema object for a tool's arguments; unknown arguments are always rejected."""
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema
