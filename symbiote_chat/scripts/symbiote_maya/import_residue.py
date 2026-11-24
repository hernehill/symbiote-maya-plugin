"""What an import adds to nodes that were already in the scene, and taking it back off.

AbcImport puts an Alembic face set into an existing shading group of the same name and adds a string,
``AbcFacesetName``, to that group (measured: ``initialShadingGroup`` for faces left on Maya's default shader, or
an artist's ``matASG``). Shading groups are the only existing nodes an import was measured changing, so they are
what is watched: the attributes added, the string values overwritten (AbcImport rewrites an AbcFacesetName that is
already there), and new nodes wired in. Maya's default group may take the face set; the attribute is then removed.
An artist's group may not: Symbiote never changes the artist's nodes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

import maya.cmds as cmds

FACE_SET_ATTRIBUTE = "AbcFacesetName"
FACE_SET_CLASH = (
    "this Alembic's face sets are named like shading groups already in your scene ({names}); Symbiote won't change "
    "your shading groups. Rename them or import into a fresh scene"  # the refusal adds the full stop
)

# A shading group's UUID -> {user-defined attribute: (kind, value)}. The kind keeps an unset string (it reads None,
# measured) apart from an attribute whose value isn't compared: "string", "number" or "other" (value None).
Value = tuple[str, Any]
Snapshot = Mapping[str, Mapping[str, Value]]
_NUMBER_KINDS = frozenset(
    {"bool", "byte", "char", "short", "long", "enum", "float", "double", "doubleLinear", "doubleAngle"}
)


class Change(NamedTuple):
    """What the import did to one existing shading group: attributes it added, string values it overwrote (with
    the value before), and whether a new node was wired into it."""

    added: tuple[str, ...]
    overwritten: Mapping[str, Value]
    wired: bool


def snapshot() -> Snapshot:
    """Every shading group's user-defined attributes and their string values, by UUID, taken before the import."""
    found = {}
    for name in cmds.ls(type="shadingEngine", long=True) or []:
        found[str(cmds.ls(name, uuid=True)[0])] = _values(str(name))
    return found


def _values(name: str) -> dict[str, Value]:
    values: dict[str, Value] = {}
    for attribute in cmds.listAttr(name, userDefined=True) or []:
        plug = f"{name}.{attribute}"
        kind = cmds.attributeQuery(attribute, node=name, attributeType=True)
        if kind == "typed" and cmds.getAttr(plug, type=True) == "string":
            values[attribute] = ("string", cmds.getAttr(plug) or "")  # unset (None) and "" both hold no text
        elif kind in _NUMBER_KINDS:
            values[attribute] = ("number", cmds.getAttr(plug))
        else:
            values[attribute] = ("other", None)
    return values


def changes(before: Snapshot, new_nodes: frozenset[str]) -> dict[str, Change]:
    """What the import did to each shading group that was there before, by the group's name."""
    found: dict[str, Change] = {}
    for uuid, old in before.items():
        name = str((cmds.ls(uuid) or [""])[0])
        if not name:
            continue
        now = _values(name)
        added = tuple(sorted(set(now) - set(old)))
        overwritten = {a: old[a] for a in old if a in now and now[a] != old[a]}
        wired = _wired_from(name, new_nodes)
        if added or overwritten or wired:
            found[name] = Change(added, overwritten, wired)
    return found


def _wired_from(name: str, new_nodes: frozenset[str]) -> bool:
    """Whether a node the import made drives this group (a face set's groupId or object group, measured)."""
    sources = cmds.listConnections(name, source=True, destination=False) or []
    return any(str((cmds.ls(source, long=True) or [source])[0]) in new_nodes for source in sources)


def clash(found: Mapping[str, Change]) -> list[str]:
    """The one reason, when the import reused an artist's shading group: it is refused for that alone."""
    artists = sorted(name for name in found if not cmds.ls(name, defaultNodes=True))
    return [FACE_SET_CLASH.format(names=", ".join(artists))] if artists else []


def reasons(found: Mapping[str, Change]) -> list[str]:
    """Why the changes to Maya's default shading groups refuse the import: they may take only a new face set of
    their own name (the measured join of faces left on the default shader)."""
    refused = []
    for name, change in sorted(found.items()):
        changed = [*change.added, *change.overwritten]
        own_name = change.added == (FACE_SET_ATTRIBUTE,) and not change.overwritten
        if changed and (not own_name or cmds.getAttr(f"{name}.{FACE_SET_ATTRIBUTE}") != name.rsplit("|", 1)[-1]):
            refused.append(f"the file changed {name}, which was already in your scene ({', '.join(changed)})")
    return refused


def take_back(found: Mapping[str, Change]) -> list[str]:
    """Remove what the import added to existing groups and restore what it overwrote; what couldn't be, named."""
    left = []
    for name, change in found.items():
        for attribute in change.added:
            try:
                cmds.deleteAttr(f"{name}.{attribute}")
            except RuntimeError:
                left.append(f"remove {name}.{attribute}")
        for attribute, (kind, value) in change.overwritten.items():
            try:
                _restore(f"{name}.{attribute}", kind, value)
            except RuntimeError:
                left.append(f"restore {name}.{attribute}")
    return left


def _restore(plug: str, kind: str, value: Any) -> None:
    """Set ``plug`` back. An unset string can't be unset again; "" reads back as "" and holds no text (measured)."""
    if kind == "string":
        cmds.setAttr(plug, value, type="string")
    elif kind == "number":
        cmds.setAttr(plug, value)
    else:
        raise RuntimeError(f"{plug} is not a kind Symbiote can set back")
