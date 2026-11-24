"""The docked Symbiote chat pane and the Symbiote menu.

Maya docks a pane in a workspace control named after the widget, so the pane ``symbioteChat`` lives in
``symbioteChatWorkspaceControl``. The control's ``uiScript`` rebuilds the pane when Maya restarts with the
pane in its layout (measured: it re-runs after a full relaunch).

Closing the pane must shut its panel down, as closing Houdini's pane tab does. Maya reaches
``dockCloseEventTriggered`` only for a pane the mixin's ``show()`` built (``mayaMixin.workspaceControlClosed``
dispatches to the controls ``show()`` registered); a pane ``restore()`` rebuilt was never shown, so the control's
``closeCommand`` (``CLOSE_SCRIPT``) closes it. A pane ``show()`` built gets both; ``closed`` makes them one shutdown.
"""

from __future__ import annotations

from typing import Any

import maya.cmds as cmds
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from maya.OpenMayaUI import MQtUtil
from PySide6.QtWidgets import QVBoxLayout, QWidget
from symbiote_dcc.panel import SymbioteChatPanel

from symbiote_maya.adapter import MAYA

OBJECT_NAME = "symbioteChat"
CONTROL = OBJECT_NAME + "WorkspaceControl"
MENU = "symbioteMenu"
MAIN_WINDOW = "MayaWindow"
# A constant, not built from anything: Maya runs it as Python when it restores the layout.
RESTORE_SCRIPT = "import symbiote_maya.dock as d; d.restore()"
# The same kind of constant: Maya runs it when the artist closes the pane.
CLOSE_SCRIPT = "import symbiote_maya.dock as d; d.on_close()"


class ChatDock(MayaQWidgetDockableMixin, QWidget):
    """The pane: the shared chat panel, shut down once when Maya closes the dock."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent=parent)
        self.setObjectName(OBJECT_NAME)
        self.setWindowTitle("Symbiote Chat")
        self.panel = SymbioteChatPanel(MAYA)
        self.closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel)

    def dockCloseEventTriggered(self) -> None:
        if not self.closed:
            self.closed = True
            self.panel.shutdown()


class _Live:
    """The one pane that is open, if any."""

    dock: ChatDock | None = None


def live_dock() -> ChatDock | None:
    dock = _Live.dock
    return dock if dock is not None and not dock.closed else None


def open_chat(*_unused: Any) -> None:
    """Show the pane: restore the docked control when its pane is live, else dock a new pane.

    A control can outlive its pane (whether Maya deletes it on close isn't measured, and a layout-restored pane's
    retain flag comes from the saved workspace), so a control around a pane that was shut down is replaced.
    """
    if cmds.workspaceControl(CONTROL, exists=True):
        if live_dock() is not None:
            cmds.workspaceControl(CONTROL, e=True, restore=True)
            return
        cmds.deleteUI(CONTROL)
    dock = ChatDock()
    _Live.dock = dock
    dock.show(dockable=True, retain=False, uiScript=RESTORE_SCRIPT, closeCallback=CLOSE_SCRIPT)


def _control_pointer(name: str) -> int:
    """Qt's pointer to the control ``name``; findControl answers None for a control Maya doesn't have."""
    pointer = MQtUtil.findControl(name)
    if pointer is None:
        raise RuntimeError(f"Maya has no control named {name!r} to put the Symbiote pane into.")
    return int(pointer)


def restore() -> None:
    """Maya's layout restore (``RESTORE_SCRIPT``): put a pane into the control Maya made, unless one is open."""
    if live_dock() is not None:
        return
    dock = ChatDock()
    _Live.dock = dock
    MQtUtil.addWidgetToMayaLayout(_control_pointer(dock.objectName()), _control_pointer(CONTROL))
    # Set here, not trusted to the saved layout: the mixin never registered this pane, so this is its only close.
    cmds.workspaceControl(CONTROL, e=True, closeCommand=CLOSE_SCRIPT)


def on_close() -> None:
    """Maya's close of the pane (``CLOSE_SCRIPT``): shut the open pane's panel down, once."""
    dock = live_dock()
    if dock is not None:
        dock.dockCloseEventTriggered()


def forget_layout(*_unused: Any) -> None:
    """Drop the saved pane from the layout, so Maya doesn't try to rebuild it once the plugin is gone."""
    cmds.workspaceControlState(CONTROL, remove=True)


def install_menu() -> None:
    """Add the top-level Symbiote menu, replacing an earlier one. A batch or farm Maya has no menus."""
    if cmds.about(batch=True):
        return
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU, menu=True)
    cmds.menu(MENU, label="Symbiote", parent=MAIN_WINDOW, tearOff=False)
    cmds.menuItem(label="Symbiote Chat", command=open_chat, parent=MENU)
    cmds.menuItem(divider=True, parent=MENU)
    cmds.menuItem(label="Remove Symbiote Chat from the layout", command=forget_layout, parent=MENU)
