"""Maya's view tools: capture the viewport the artist sees, and frame an object in it.

Neither changes a node the artist made. Framing moves a startup camera (``persp``), which the scene file saves,
so it counts as a scene write; it never moves any other camera and never changes the selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maya.cmds as cmds
from symbiote_dcc import capture, capture_image, consent, tool_support
from symbiote_dcc.tool_support import ToolError

from symbiote_maya.main_thread import MAIN
from symbiote_maya.names import resolve_node
from symbiote_maya.scene_writes import WRITES
from symbiote_maya.ui import DCC_NAME, MAYA_UI

# playblast numbers its image ``<stem>.####.png`` (measured: ``.0000.png``); the stem sits beside the target.
_STEM_SUFFIX = "-playblast"
_FRAME_DIGITS = "####"


@dataclass(frozen=True)
class _View:
    """The model panel the artist is looking at, and the camera it looks through."""

    panel: str
    camera: str


def _find_viewer() -> capture.Viewer | None:
    """The active model panel, named "<label> (<camera>)"; ``None`` when there is no 3D viewport to use."""
    try:
        # Names the viewport even while the Script Editor has focus (measured, Maya 2027 GUI).
        editor = cmds.playblast(activeEditor=True)
    except RuntimeError:
        return None  # "Unable to find active 3d viewport" (measured in mayapy): there is none to capture
    panel = cmds.modelEditor(editor, query=True, panel=True) if editor else None
    if not panel:
        return None  # the active editor is not a model editor
    camera = cmds.modelPanel(panel, query=True, camera=True)
    label = cmds.panel(panel, query=True, label=True)
    return capture.Viewer(f"{label} ({camera})", _View(str(panel), str(camera)))


def _numbered(stem: Path) -> list[Path]:
    return sorted(stem.parent.glob(f"{stem.name}.????.png"))


def _render(view: _View, target: Path) -> None:
    stem = target.with_name(target.stem + _STEM_SUFFIX)
    for stale in _numbered(stem):  # an earlier capture at another frame left its own number
        stale.unlink()
    pattern = cmds.playblast(
        editorPanelName=view.panel,
        filename=str(stem),
        format="image",
        compression="png",
        frame=[cmds.currentTime(query=True)],
        viewer=False,
        showOrnaments=False,
        offScreen=True,
        widthHeight=capture.DEFAULT_RESOLUTION,
        percent=100,
        forceOverwrite=True,
    )
    if _FRAME_DIGITS not in str(pattern):
        raise ToolError(f"Maya's playblast named its image {pattern}, without the {_FRAME_DIGITS} frame number.")
    written = _numbered(stem)
    if len(written) != 1:
        raise ToolError(f"Maya's playblast wrote {len(written)} images for one frame (it named {pattern}).")
    written[0].replace(target)


def _metadata(view: _View) -> dict[str, Any]:
    return {"frame": cmds.currentTime(query=True), "viewport": view.panel, "camera": view.camera}


def _capture_viewport(include_image: bool, deadline_seconds: int, deadline_unix: float) -> dict[str, Any]:
    return capture.capture_tool(
        MAYA_UI,
        DCC_NAME,
        find_viewer=_find_viewer,
        render=_render,
        metadata=_metadata,
        to_rgb_png=capture_image.drop_alpha,  # playblast leaves alpha 0 over the background (measured)
        include_image=include_image,
        deadline_seconds=deadline_seconds,
        deadline_unix=deadline_unix,
    )


def capture_viewport(
    include_image: bool = False, deadline_seconds: int = 0, deadline_unix: float = 0.0
) -> dict[str, Any]:
    """Capture the viewport; a picture that leaves the machine needs the artist's yes."""
    with capture.one_capture_at_a_time(MAYA_UI, "capture"):
        return MAIN.run(_capture_viewport, include_image, deadline_seconds, deadline_unix)


def is_startup_camera(camera: str) -> bool:
    """Whether ``camera`` (a transform or its shape) is one of Maya's startup cameras, by query, not by name."""
    shapes = cmds.listRelatives(camera, shapes=True, type="camera", fullPath=True) or [camera]
    return bool(cmds.camera(shapes[0], query=True, startupCamera=True))


def frame_through(camera: str, node: str) -> None:
    """Move startup camera ``camera`` so ``node`` fills the view; anything else is refused before it moves."""
    if len(cmds.ls(camera, long=True) or []) > 1:  # modelPanel -q -camera can answer a shared short name
        raise ToolError(
            f"You're looking through {camera}, a name more than one camera has, so framing can't tell which camera "
            "would move. Rename one of them, or look through persp, top, front or side."
        )
    if not is_startup_camera(camera):
        raise ToolError(f"You're looking through {camera}; framing would move that camera.")
    # Counted and marked before the move: a viewFit that fails part way can only make a save or capture stricter.
    WRITES.bump()
    consent.POLICY.require_ask_after_reframe(node)
    cmds.viewFit(camera, node, animate=False)


def _frame_object(name: str) -> dict[str, Any]:
    # Again here: a frame queued before an import, delete or save dialog opened is serviced inside it (gui-3).
    tool_support.refuse_while_a_confirmation_is_open(MAYA_UI)
    node = resolve_node(name)
    if not cmds.ls(node, type="dagNode", long=True):
        raise ToolError(f"{name} is not an object in the viewport (a DAG node), so there is nothing to frame.")
    viewer = _find_viewer()
    if viewer is None:
        raise ToolError(f"{MAYA_UI.display_name} has no 3D viewport open to frame.")
    frame_through(viewer.handle.camera, node)
    return {"framed": True, "node": node, "viewer": viewer.name}


def frame_object(name: str) -> dict[str, Any]:
    """Move the visible viewport's startup camera to look at a node; no node or selection changes."""
    with capture.one_capture_at_a_time(MAYA_UI, "frame"):
        tool_support.refuse_while_a_confirmation_is_open(MAYA_UI)
        return MAIN.run(_frame_object, name)
