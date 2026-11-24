"""The Maya adapter: names, tools and dialog host handed to the shared panel."""

from __future__ import annotations

import maya.cmds as cmds
from symbiote_dcc.adapter import DccAdapter

from symbiote_maya import version_label
from symbiote_maya.tools import TOOLS
from symbiote_maya.ui import DCC_NAME, MAYA_UI

MAYA = DccAdapter(
    DCC_NAME,
    MAYA_UI,
    TOOLS,
    version_label(),
    "maya_scene_info",
    host_version=lambda: cmds.about(version=True),
)
