"""maya_delete_object and maya_save_scene: changes the artist confirms first, in a dialog that defaults to Cancel.

``flows.confirmed_change`` keeps the order: check, dialog, tier again, revalidate, change. Revalidation compares
what the dialog showed with the scene now, because a call serviced while the dialog was open, or the artist,
can change it: a delete compares the node's UUID and the set of UUIDs below it; a save compares Symbiote's
write count, Maya's modified flag and the scene's file name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import maya.cmds as cmds
from symbiote_dcc.flows import ChangeDialog, confirmed_change
from symbiote_dcc.mcp_protocol import tier_recheck
from symbiote_dcc.tool_support import ToolError

from symbiote_maya import maya_write_limits as limits
from symbiote_maya.main_thread import MAIN
from symbiote_maya.maya_write_support import edit_refusals, ready_refusal, refused, undoable
from symbiote_maya.names import resolve_node
from symbiote_maya.scene_writes import WRITES
from symbiote_maya.ui import MAYA_UI

MAX_LISTED = 20
_MAX_REASONS = 20


def _uuids(nodes: list[str]) -> frozenset[str]:
    """Each node's UUID: ls with the nodes named (a bare ls(uuid=True) lists names, measured)."""
    return frozenset(str(u) for u in cmds.ls(nodes, uuid=True) or []) if nodes else frozenset()


# maya_delete_object


@dataclass(frozen=True)
class _Deleting:
    path: str
    uuid: frozenset[str]
    below: tuple[str, ...]
    below_uuids: frozenset[str]


def _delete_subject(name: object) -> _Deleting:
    node = resolve_node(name)
    what = f"Deleting {node}"
    reason = ready_refusal()
    if reason:
        raise refused(what, [reason])
    if not cmds.ls(node, type="dagNode", long=True):
        raise refused(what, [f"{node} is not an object in the outliner (a DAG node); only those are deleted"])
    below = tuple(str(n) for n in cmds.listRelatives(node, allDescendents=True, fullPath=True) or [])
    reasons = edit_refusals([node, *below])
    if reasons:
        more = len(reasons) - _MAX_REASONS
        raise refused(what, reasons[:_MAX_REASONS] + ([f"and {more} more"] if more > 0 else []))
    return _Deleting(node, _uuids([node]), tuple(sorted(below)), _uuids(list(below)))


def _below_note(subject: _Deleting) -> str:
    count = len(subject.below)
    if count == 0:
        return ""
    shown = "\n".join(subject.below[:MAX_LISTED])
    more = f"\n...and {count - MAX_LISTED} more" if count > MAX_LISTED else ""
    return f"\n\nand the {count} {'node' if count == 1 else 'nodes'} below it:\n{shown}{more}"


def _delete_dialog(subject: _Deleting) -> ChangeDialog:
    return ChangeDialog(
        title="Symbiote: delete from your scene?",
        # Measured: delete also removes history only this object uses (its creator, ShapeOrig, deformers and
        # their handles); a creator another object shares stays.
        text=(
            "Symbiote wants to delete this from your Maya scene, and Maya also deletes the history only it uses "
            "(its creator and deformers). Ctrl-Z brings it all back."
        ),
        details=f"{subject.path}{_below_note(subject)}",
        accept_label="Delete",
        what=subject.path,
        verb_past="deleted",
        cancelled={"ok": True, "deleted": False, "reason": "The artist cancelled the delete."},
    )


def _still_the_same(subject: _Deleting) -> bool:
    """Whether the node and everything below it are what the dialog showed. Undo switched off (or a Maya without
    a census) while the dialog was open is refused with its own reason: the node didn't change."""
    reason = ready_refusal()
    if reason:
        raise refused(f"Deleting {subject.path}", [reason])
    try:
        now = _delete_subject(subject.path)  # every check again: it may be locked or replaced by now
    except ToolError:
        return False
    return now.uuid == subject.uuid and now.below_uuids == subject.below_uuids


def _delete_only_what_was_shown(subject: _Deleting) -> None:
    """Delete the node, then refuse inside the undo entry if any other object went with it.

    Measured in mayapy 2027: deleting a mesh also deletes history only it uses, and for an exclusive deformer that
    includes the handle's shape, a DAG node outside the subtree the dialog showed (and the handle, with anything
    under it, when nothing else holds it). Long names identify the objects: a delete renames nothing.
    """
    before = set(cmds.ls(dag=True, long=True) or [])
    cmds.delete(subject.path)
    gone = before - set(cmds.ls(dag=True, long=True) or [])
    extra = sorted(gone - {subject.path} - set(subject.below))
    if extra:
        shown = ", ".join(extra[:MAX_LISTED]) + (
            f" and {len(extra) - MAX_LISTED} more" if len(extra) > MAX_LISTED else ""
        )
        raise refused(
            f"Deleting {subject.path}",
            [
                f"Maya would also have deleted {shown}, which the dialog didn't show (history only this object "
                "uses, such as a deformer's handle); the artist can delete it in Maya if they mean to"
            ],
        )


def _delete(subject: _Deleting) -> dict[str, Any]:
    undoable(f"Symbiote: maya_delete_object {subject.path}", lambda: _delete_only_what_was_shown(subject))
    return {"ok": True, "deleted": True, "node": subject.path, "nodes_below": len(subject.below)}


def delete_object(name: object) -> dict[str, Any]:
    """Delete one node and everything below it, once the artist confirms."""
    return confirmed_change(
        MAYA_UI,
        MAIN,
        tier_recheck(),  # taken here, on the tool server's thread, before the dialog
        check=lambda: _delete_subject(name),
        describe=_delete_dialog,
        revalidate=_still_the_same,
        perform=_delete,
    )


# maya_save_scene


@dataclass(frozen=True)
class _Saving:
    path: str
    writes: int
    modified: bool


def _scene_now() -> _Saving:
    path = str(cmds.file(query=True, sceneName=True) or "")
    return _Saving(path, WRITES.value, bool(cmds.file(query=True, modified=True)))


def _save_subject() -> _Saving:
    reason = limits.version_refusal()  # not undo_refusal: a save isn't undoable (ruling R2)
    if reason:
        raise refused("Saving the scene", [reason])
    subject = _scene_now()
    if not subject.path:
        raise refused(
            "Saving the scene",
            ["it has never been saved, so it has no file to save over; save it once yourself (File > Save Scene As)"],
        )
    return subject


def _save_dialog(subject: _Saving) -> ChangeDialog:
    return ChangeDialog(
        title="Symbiote: save your scene?",
        text="Symbiote wants to save your scene over this file. Saving can't be undone.",
        details=subject.path,
        accept_label="Save",
        what="The scene",
        verb_past="saved",
        cancelled={"ok": True, "saved": False, "reason": "The artist cancelled the save."},
    )


def _save(subject: _Saving) -> dict[str, Any]:
    try:
        cmds.file(save=True)
    except RuntimeError as error:  # reported as a failed save, never swallowed (M6)
        raise ToolError(f"Saving the scene failed: Maya couldn't save {subject.path}: {error}.") from error
    return {"ok": True, "saved": True, "path": subject.path}


def save_scene() -> dict[str, Any]:
    """Save the scene over its own file, once the artist confirms; it is never saved anywhere else."""
    return confirmed_change(
        MAYA_UI,
        MAIN,
        tier_recheck(),
        check=_save_subject,
        describe=_save_dialog,
        revalidate=lambda subject: _scene_now() == subject,
        perform=_save,
    )
