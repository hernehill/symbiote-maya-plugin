"""Whether Symbiote Chat can run in this Maya, and the menu that says why when it can't.

userSetup asks before it imports ``dock``: on a host whose Python can't load ``ssl``, importing the panel would
fail deep inside the core. This module imports nothing that needs ``ssl``, so it can always say why. Its menu runs
no text: the **Why?** item's command is a callable, and the reason reaches the dialog only as data.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable
from typing import Any

import maya.cmds as cmds
from symbiote_dcc import host_ssl

# The same menu dock.install_menu builds (a test pins them together); dock can't be imported here.
MENU = "symbioteMenu"
MAIN_WINDOW = "MayaWindow"
UNAVAILABLE_LABEL = "Symbiote Chat isn't available here"
WHY_LABEL = "Why?"


def unsupported_reason(platform: str = sys.platform, is_file: Callable[[str], bool] = os.path.isfile) -> str | None:
    """Why Symbiote Chat can't start on this host, or None: ``ssl`` first, then, on Linux, the RHEL CA store."""
    reason = host_ssl.ssl_refusal()
    if reason is not None:
        return reason
    if platform.startswith("linux") and not is_file(host_ssl.RHEL_CA_STORE):
        return host_ssl.missing_store_reason()
    return None


def install_refusal_menu(reason: str) -> None:
    """The Symbiote menu, with a disabled item saying the chat isn't available and a **Why?** that shows ``reason``."""
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU, menu=True)
    cmds.menu(MENU, label="Symbiote", parent=MAIN_WINDOW, tearOff=False)
    cmds.menuItem(label=UNAVAILABLE_LABEL, enable=False, parent=MENU)
    cmds.menuItem(label=WHY_LABEL, command=functools.partial(_show_reason, reason), parent=MENU)
    cmds.warning(reason)


def _show_reason(reason: str, *_unused: Any) -> None:
    """Maya passes the item's checked state, which this ignores."""
    cmds.confirmDialog(title="Symbiote Chat", message=reason, button=["OK"])
