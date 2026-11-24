"""Maya's dialog host: the parent window dialogs and the panel use."""

from __future__ import annotations

from typing import Any

import shiboken6
from maya.OpenMayaUI import MQtUtil
from PySide6.QtWidgets import QWidget
from symbiote_dcc.adapter import DialogHost

# The adapter's name: settings, runtime dir (~/.symbiote/maya/) and agent name all use it.
DCC_NAME = "maya"


def main_window() -> Any:
    pointer = MQtUtil.mainWindow()
    if not pointer:
        raise RuntimeError("Maya has no main window to parent the dialog on.")
    return shiboken6.wrapInstance(int(pointer), QWidget)


# Maya styles through the palette (measured): no stylesheet.
MAYA_UI = DialogHost("Maya", main_window, lambda: "")
