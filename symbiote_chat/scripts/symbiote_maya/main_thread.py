"""Maya's hop to its main thread; the rest of the tool plumbing is the core's (``symbiote_dcc.tool_support``).

The tool server calls a tool on its own thread; ``maya.cmds`` may only be used on the main thread, so each
tool hands its work over with ``MAIN.run``. A tool that changes the scene uses ``MAIN.checked``: the panel can
switch its tier off, or a confirmation dialog open, between the tool server's check and the moment the main
thread gets to the work (a call posted while a modal dialog is open runs inside it, measured).
"""

from __future__ import annotations

from typing import Any

import maya.utils
from symbiote_dcc.tool_support import MainThread

from symbiote_maya.ui import MAYA_UI


def _marshal(handler: Any, *args: Any) -> Any:
    # The source guard allows only a function here, and cannot see that ``handler`` is one;
    # MainThread.run (symbiote_dcc/tool_support.py) is the only caller and refuses a non-callable.
    if not callable(handler):
        raise TypeError(f"handler must be callable, got {type(handler).__name__}")
    return maya.utils.executeInMainThreadWithResult(handler, *args)


MAIN = MainThread(_marshal, MAYA_UI)
