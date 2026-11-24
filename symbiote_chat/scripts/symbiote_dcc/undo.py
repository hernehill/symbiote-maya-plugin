"""One undo entry per tool call, and a failure that undoes only that call's own entry.

Every call gets its own label (``"<label> #<n>"``), so a failed call can tell its own entry from
an identical earlier call's: two "Symbiote: create cube" calls in a row are two different entries.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar

from symbiote_dcc.tool_support import ToolError

_Result = TypeVar("_Result")

_numbers = itertools.count(1)
_numbers_lock = threading.Lock()


class UndoApi(Protocol):
    """A DCC's undo queue, as ``undoable`` uses it."""

    def group(self, label: str) -> AbstractContextManager[object]:
        """Everything changed inside is one undo entry named ``label``."""

    def top_label(self) -> str:
        """The label of the entry Ctrl-Z would undo next, or ``""`` when there is none to undo."""

    def undo_last(self) -> None:
        """Undo the top entry."""


def _unique(label: str) -> str:
    with _numbers_lock:
        number = next(_numbers)
    return f"{label} #{number}"


def undoable(label: str, work: Callable[[], _Result], api: UndoApi) -> _Result:
    """Run ``work`` in one undo group; if it fails partway, undo what it changed and re-raise.

    The group is undone only when it is the top entry, so a failure before anything changed
    never undoes the artist's own last change, nor an earlier call with the same label.
    """
    unique = _unique(label)
    try:
        with api.group(unique):
            return work()
    except Exception as failure:
        if api.top_label() == unique:
            try:
                api.undo_last()
            except Exception as undo_failure:
                raise ToolError(f"{failure}; undoing the partial change also failed: {undo_failure}") from failure
        raise
