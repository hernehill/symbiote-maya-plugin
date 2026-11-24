"""maya_import_asset: bring an Alembic or OBJ file into the scene once the artist confirms, then audit it.

Nothing on disk is touched before the artist answers the dialog (``flows.confirmed_import`` keeps that order).
After Confirm, one main-thread call imports the file under a new group, audits every node it brought
(``import_audit``) and, if anything is refused, deletes them all: no artist action can come in between.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import maya.cmds as cmds
from symbiote_dcc import flows, paths
from symbiote_dcc.mcp_protocol import tier_recheck
from symbiote_dcc.tool_support import ToolError

from symbiote_maya import import_audit, import_defaults, import_residue
from symbiote_maya.main_thread import MAIN
from symbiote_maya.maya_versions import measured_refusal
from symbiote_maya.names import new_name
from symbiote_maya.scene_writes import WRITES
from symbiote_maya.ui import MAYA_UI

ABC, OBJ = ".abc", ".obj"
_SCENE = "a Maya scene can carry script nodes"
_FBX = (
    "FBX import isn't supported: by default it changes nodes already in your scene, writes embedded media to "
    "disk, and brings image planes, audio and caches"
)
_USD = "USD import isn't supported: a USD file can load Maya scenes (.ma, .mb) through MayaReference prims"
REFUSED_FORMATS = {
    ".ma": _SCENE,
    ".mb": _SCENE,
    ".fbx": _FBX,
    ".usd": _USD,
    ".usda": _USD,
    ".usdc": _USD,
    ".usdz": _USD,
}
# AbcImport adds the Alembic command (loaded at start in GUI Maya, measured). OBJ needs no plug-in: mayapy imports
# an OBJ with objExport unloaded (measured).
_ALEMBIC_PLUGIN = "AbcImport"
OBJ_UNSAVED = "OBJ import clears Maya's undo history; save your scene first, then ask again"
_MAX_REASONS = 20
_WINDOWS = os.name == "nt"


def import_version_refusal() -> str | None:
    """Why imports stay off in this Maya: its (year, platform) pair has no measured import defaults, or it is an
    update older than the measured one. ``None`` when the audit's pinned defaults were measured here."""
    return measured_refusal("imports", import_defaults.API_MEASURED)


def _pick_loader(path: str) -> str:
    """The file's extension, ``.abc`` or ``.obj``; anything else is refused by name, before the dialog."""
    extension = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if extension in REFUSED_FORMATS:
        raise ToolError(f"Symbiote won't import {name}: {REFUSED_FORMATS[extension]}. It imports .abc and .obj files.")
    if extension not in (ABC, OBJ):
        raise ToolError(f"Cannot import {name}: supported types are .abc, .obj")
    if extension == ABC and not cmds.pluginInfo(_ALEMBIC_PLUGIN, query=True, loaded=True):
        raise ToolError(
            f"Maya's Alembic plug-in ({_ALEMBIC_PLUGIN}) isn't loaded, so Symbiote can't import {name}. Load it in "
            "Windows > Settings/Preferences > Plug-in Manager, then ask again."
        )
    if extension == OBJ:
        _refuse_obj_while_unsaved()
    return extension


def _refuse_obj_while_unsaved() -> None:
    if cmds.file(query=True, modified=True):
        raise ToolError(OBJ_UNSAVED)


def _group_name(requested: str, path: str) -> str:
    """The name the new group gets: the first one no node has, so the dialog names the real group."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", os.path.splitext(os.path.basename(path))[0])[:48]
    base = requested or (stem if re.match(r"[A-Za-z_]", stem) else f"_{stem}") + "_import"
    name, number = base, 0
    while cmds.objExists(name):
        number += 1
        name = f"{base}{number}"
    return name


def undo_note(group: str, extension: str) -> str:
    clears = ", and it clears Maya's undo history" if extension == OBJ else ""
    return f"Ctrl-Z can't remove this import; delete the group {group} to remove it{clears}."


def _dialog(path: str, group: str) -> flows.ImportDialog:
    extension = os.path.splitext(path)[1].lower()
    details = f"{path}\n\nGroup: {group}\n{undo_note(group, extension)}"
    return flows.ImportDialog("Symbiote: import asset?", "Import this file into Maya?", details, "Import")


def _pre_import(path: str) -> None:
    """After Confirm, before anything is touched: an OBJ needs a saved scene and its own folder's materials."""
    if path.lower().endswith(OBJ):
        _refuse_obj_while_unsaved()
        check_obj_materials(path)


def check_obj_materials(obj_path: str) -> None:
    """Refuse an OBJ whose ``mtllib`` lines could make Maya read a file outside the OBJ's folder, or a texture.

    Maya honours a leading tab, several names on one line and a backslash continuation (measured), so any line
    ending in a backslash is refused, the keyword is matched in any case, and every name must be a plain file
    name that, followed through its links, stays in the folder. Each material file there is read the same way:
    Maya makes a file node from every texture it names, with the path as written (a share, or outside the
    folder; measured), so a textured material is refused before the import.
    """
    folder = _resolved(os.path.dirname(obj_path))
    for where, tokens in _lines(obj_path):
        if tokens[0].lower() == "mtllib":
            for material in tokens[1:]:
                resolved = _check_material(obj_path, folder, where, material)
                if os.path.isfile(resolved):
                    _check_textures(resolved)


def _lines(path: str) -> Iterator[tuple[str, list[str]]]:
    """("<file> line N", its whitespace-separated tokens) of each line that has any; a continued line refuses."""
    name = os.path.basename(path)
    with open(path, "rb") as handle:
        text = handle.read().decode("latin-1")
    for number, raw in enumerate(re.split(r"\r\n|\r|\n", text), 1):
        line = raw.strip()
        if line.endswith("\\"):
            raise ToolError(
                f"{name} line {number} ends in a backslash, which Maya joins to the next line, so nothing was imported."
            )
        if line.split():
            yield f"{name} line {number}", line.split()


# The MTL directives whose argument is a texture file: every map_* (map_Ka, map_Kd, map_Ks, map_Ns, map_d,
# map_aat, map_bump and the PBR map_Pr, map_Pm, map_Ps, map_Ke) and these.
TEXTURE_DIRECTIVES = frozenset({"bump", "disp", "decal", "refl", "norm"})


def _check_textures(mtl_path: str) -> None:
    for where, tokens in _lines(mtl_path):
        keyword = tokens[0].lower()
        if keyword.startswith("map_") or keyword in TEXTURE_DIRECTIVES:
            raise ToolError(
                f"{where} names a texture ({tokens[0]}); Symbiote doesn't import textured OBJ materials yet, "
                "so nothing was imported."
            )


def _check_material(obj_path: str, folder: str, where: str, material: str) -> str:
    """The material file ``material`` names, resolved; refused unless it is a plain name staying in the folder."""
    plain = material not in ("", ".") and ".." not in material and not re.search(r"[/\\:]", material)
    if not plain or paths.has_hidden_character(material):
        raise ToolError(
            f"{where} names the material file {material!r}, which is not in the OBJ's folder, so nothing was imported."
        )
    resolved = _resolved(os.path.join(os.path.dirname(obj_path), material))
    if not resolved.startswith(folder.rstrip(os.sep) + os.sep):
        raise ToolError(
            f"{where} names {material}, which links to {resolved}, outside the OBJ's folder, so nothing was imported."
        )
    return resolved


def _resolved(path: str) -> str:
    try:
        return paths.resolve_checked(
            path or os.curdir,
            windows=_WINDOWS,
            display_name=MAYA_UI.display_name,
            expression_chars=import_audit.EXPRESSION_CHARACTERS,
            expression_reason=import_audit.EXPRESSION_REASON,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@contextmanager
def _undo_suspended() -> Iterator[None]:
    """Nothing inside goes on the undo queue, so Ctrl-Z can't bring refused nodes back (measured: re-review N1).

    Undo is turned off around the whole import, never around the delete alone: that crashed Maya.
    """
    was_on = bool(cmds.undoInfo(query=True, state=True))
    if was_on:
        cmds.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        if was_on:
            cmds.undoInfo(stateWithoutFlush=True)


def _do_import(path: str, extension: str, group: str) -> dict[str, Any]:
    # Every scene command of the import, the delete of a refused one included, runs with undo off, so none of it
    # is on the queue: Ctrl-Z after a refusal walks the artist's own history and never brings a refused node back
    # (measured in mayapy, undo until empty and redo). The history is kept; an OBJ import clears it itself.
    with _undo_suspended():
        count = _land(path, extension, group)
    WRITES.bump()
    return {"imported": True, "group": f"|{group}", "nodes": count, "path": path, "undo": undo_note(group, extension)}


def _land(path: str, extension: str, group: str) -> int:
    """Import, audit, and keep the new nodes or delete them all. Returns how many nodes the import made."""
    before, groups = _uuids(), import_residue.snapshot()
    try:
        _load(path, extension, group)
        new = _names(_new_uuids(before))
        changes = import_residue.changes(groups, frozenset(new))
        reasons = import_residue.clash(changes) or [
            *(_group_reasons(new, group, path) or import_audit.audit(new, path)),
            *import_residue.reasons(changes),
        ]
    except ToolError as exc:
        raise _removed(before, groups, group, [str(exc)], path) from exc
    except Exception as exc:  # the audit failed: what the import brought is unchecked, so it goes
        raise _removed(
            before, groups, group, [f"Symbiote couldn't finish checking it ({type(exc).__name__}: {exc})"], path
        ) from exc
    if reasons:
        raise _removed(before, groups, group, reasons, path)
    left = import_residue.take_back(changes)  # the face-set name AbcImport wrote on Maya's default group
    if left:
        raise _removed(before, groups, group, [f"Symbiote couldn't {', '.join(left)}"], path)
    return len(new)


def _load(path: str, extension: str, group: str) -> None:
    try:
        if extension == ABC:
            made = cmds.group(empty=True, name=group)
            if made != group:
                raise ToolError(f"another node took the name {group} after you confirmed")
            cmds.AbcImport(path, mode="import", reparent=group)
        else:
            cmds.file(
                path, i=True, type="OBJ", groupReference=True, groupName=group, returnNewNodes=True, ignoreVersion=True
            )
    except RuntimeError as exc:
        raise ToolError(f"Maya couldn't read it: {exc}") from exc


def _uuids() -> frozenset[str]:
    """Every node's UUID. ls(uuid=True) alone lists names (measured), and a name an import makes ambiguous
    (the artist's |hero once the file brings another hero) reads as new; UUIDs come only from listed objects."""
    names = cmds.ls(long=True) or []
    return frozenset(cmds.ls(names, uuid=True) or []) if names else frozenset()


def _new_uuids(before: frozenset[str]) -> list[str]:
    return sorted(_uuids() - before)


def _names(uuids: list[str]) -> list[str]:
    """The long names of the nodes with these UUIDs that still exist; none for none (``ls([])`` lists every node)."""
    return [str(name) for name in cmds.ls(uuids, long=True) or []] if uuids else []


def _group_reasons(new: list[str], group: str, path: str) -> list[str]:
    name = os.path.basename(path)
    if not new or new == [f"|{group}"]:
        return [f"Maya found nothing to import in {name}"]
    if f"|{group}" not in new:
        return [f"Maya didn't put {name} under the group {group} you confirmed"]
    # DAG nodes have long names starting with |; every one must be the group or under it.
    outside = [
        node for node in new if node.startswith("|") and node != f"|{group}" and not node.startswith(f"|{group}|")
    ]
    return [f"{node} landed outside the group {group}" for node in outside]


def _removed(
    before: frozenset[str], groups: import_residue.Snapshot, group: str, reasons: list[str], path: str
) -> ToolError:
    """Delete every new node that still exists, by UUID, and take back what the import added to existing shading
    groups; the refusal to raise names anything that survived."""
    survivors = _names(_new_uuids(before))
    # Measured before the delete: once the new nodes go, their wiring into existing groups goes with them.
    residue = import_residue.changes(groups, frozenset(survivors))
    failure = ""
    if survivors:
        try:
            cmds.delete(survivors)
        except RuntimeError as exc:
            failure = f" ({exc})"
    nodes_left = _names(_new_uuids(before))
    left = ([f"remove {', '.join(nodes_left[:_MAX_REASONS])}{failure}"] if nodes_left else []) + (
        import_residue.take_back(residue)
    )
    more = len(reasons) - _MAX_REASONS
    shown = "; ".join(reasons[:_MAX_REASONS]) + (f"; and {more} more" if more > 0 else "")
    text = (
        f"Symbiote removed what {os.path.basename(path)} brought into the scene, because {shown}. It was never on "
        "the undo queue, so Ctrl-Z can't bring it back."
    )
    if left:
        WRITES.bump()  # something it brought is still in the scene
        text += f" Symbiote couldn't {'; '.join(left)}; delete the group {group}."
    return ToolError(text)


def import_asset(path: object, group_name: object = "") -> dict[str, Any]:
    """Import one .abc or .obj file once the artist confirms; nothing on disk is touched before they do.

    A Maya whose import defaults weren't measured is refused first: before the tier recheck (deliberately; the tool
    call is already gated, and the version isn't secret), before the path is looked at, and before any dialog.
    """
    version_refused = MAIN.run(import_version_refusal)  # maya.cmds only on the main thread
    if version_refused:
        raise ToolError(f"Symbiote can't import in this Maya: {version_refused}.")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ToolError("path must be the absolute path of an .abc or .obj file on this workstation.")
    requested = new_name(group_name)
    shown: dict[str, str] = {}

    def describe(confirmed: str) -> flows.ImportDialog:
        shown["group"] = _group_name(requested, confirmed)
        return _dialog(confirmed, shown["group"])

    return flows.confirmed_import(
        MAYA_UI,
        MAIN,
        path,
        tier_recheck(),  # taken here, on the tool server's thread, before the dialog
        windows=_WINDOWS,
        expression_chars=import_audit.EXPRESSION_CHARACTERS,
        expression_reason=import_audit.EXPRESSION_REASON,
        pick_loader=_pick_loader,
        describe=describe,
        pre_import=_pre_import,
        do_import=lambda confirmed, extension: _do_import(confirmed, extension, shown["group"]),
    )
