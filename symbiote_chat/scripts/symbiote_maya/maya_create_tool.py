"""maya_create_object: a primitive, a camera or a light, with every value checked before anything is created.

A create makes up to three nodes: a primitive's creator (polyCube1), the transform and the shape (a mesh, a
camera or a light). Each attribute is matched against the creator's census, then the transform's, then the
shape's; a name two of them hold is refused unless it is qualified (``creator.x``, ``transform.x``, ``shape.x``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import maya.cmds as cmds
from symbiote_dcc.tool_support import ToolError, refuse_while_a_dialog_is_open

from symbiote_maya import maya_write_limits as limits
from symbiote_maya.main_thread import MAIN
from symbiote_maya.maya_denylist import write_denial
from symbiote_maya.maya_write_limits import LIGHT_TYPES
from symbiote_maya.maya_write_support import (
    checked_attributes,
    create_refused,
    exact_transform_refusal,
    maya_value,
    ready_refusal,
    set_values,
    undoable,
)
from symbiote_maya.names import new_name, resolve_node
from symbiote_maya.ui import MAYA_UI

CREATOR, TRANSFORM, SHAPE = "creator", "transform", "shape"
_SHOWS_SMOOTH = "displaySmoothMesh"
_SMOOTH_LEVEL = "smoothLevel"
_CAMERAS = frozenset({"camera"})
# Maya's own first names (measured: polyCube() makes pCube1); "#" is the first free number.
_PRIMITIVE_STEMS = {
    "polyCube": "pCube#",
    "polySphere": "pSphere#",
    "polyCylinder": "pCylinder#",
    "polyCone": "pCone#",
    "polyPlane": "pPlane#",
    "polyTorus": "pTorus#",
}


@dataclass(frozen=True)
class _Plan:
    """What a create makes, by role, and the values for each role's node."""

    kind: str
    types: Mapping[str, str]  # role -> node type
    values: Mapping[str, Mapping[str, Any]]  # role -> {attribute: value}


def _roles(kind: str) -> dict[str, str]:
    if kind in limits.PRIMITIVE_TYPES:
        return {CREATOR: kind, TRANSFORM: "transform", SHAPE: "mesh"}
    return {TRANSFORM: "transform", SHAPE: kind}


def _role_of(key: str, kind: str, types: Mapping[str, str]) -> tuple[str | None, str, str | None]:
    """The role ``key`` names and its attribute, or why it names none."""
    role, dot, attr = key.partition(".")
    if dot:
        if role not in (CREATOR, TRANSFORM, SHAPE):
            return None, key, f"{key} isn't an attribute name; qualify one only as creator.x, transform.x or shape.x"
        if role not in types:
            return None, attr, f"a {kind} has no creator; name transform.{attr} or shape.{attr}"
        return role, attr, None
    holders = [r for r, t in types.items() if _attr_of(t, key)]
    if len(holders) > 1:
        named = " or ".join(f"{r}.{key}" for r in holders)
        return None, key, f"{key} is on the {' and the '.join(holders)}; name which: {named}"
    if not holders:
        return None, key, _unheld(key, kind, types)
    return holders[0], key, None


def _attr_of(node_type: str, attr: str) -> bool:
    return attr in limits.settable_attributes(node_type)


def _unheld(attr: str, kind: str, types: Mapping[str, str]) -> str:
    denied = limits.attribute_refusal(types[SHAPE], attr, None) if write_denial(attr) else None
    if denied is not None:
        return denied
    made = ", ".join(types.values())
    return (
        f"{attr} isn't an attribute Symbiote may set on any node a {kind} create makes ({made}). Use the "
        "attribute's full name as Maya lists it (translateX, not tx)"
    )


def _plan(kind: str, attributes: Mapping[str, Any]) -> tuple[_Plan, list[str]]:
    types = _roles(kind)
    values: dict[str, dict[str, Any]] = {role: {} for role in types}
    reasons: list[str] = []
    for key, value in attributes.items():
        role, attr, reason = _role_of(key, kind, types)
        if role is None:
            reasons.append(reason or key)
        elif attr in values[role]:
            reasons.append(f"{attr} is given twice for the {role}")
        else:
            values[role][attr] = value
    return _Plan(kind, types, values), reasons


def _smooth_level(mesh_values: Mapping[str, Any]) -> int | None:
    """The level the new mesh smooths at, when it shows smoothing or sets a level: the create's own, or Maya's."""
    if _SMOOTH_LEVEL in mesh_values:
        return int(mesh_values[_SMOOTH_LEVEL])
    shows = mesh_values.get(_SHOWS_SMOOTH, 0)
    if maya_value("mesh", _SHOWS_SMOOTH, shows) == 0:
        return None
    return int(limits.settable_attributes("mesh")[_SMOOTH_LEVEL]["default"])


def _value_reasons(plan: _Plan, faces: bool, under: str | None = None) -> list[str]:
    reasons = [
        reason
        for role, node_type in plan.types.items()
        for attr, value in plan.values[role].items()
        if (reason := limits.attribute_refusal(node_type, attr, value)) is not None
    ]
    if reasons:
        return reasons  # the face count needs every value of the right kind
    ghosts = [limits.planned_ghost(plan.types[role], plan.values[role]) for role in (TRANSFORM, SHAPE)]
    reasons = [reason for reason, _copies in ghosts if reason]
    if reasons or not faces or CREATOR not in plan.types:
        return reasons
    # Every level's ghosts add up: the new transform's, its shape's, and the parent's and those above it.
    copies = 1 + sum(extra for _reason, extra in ghosts) + (limits.ghost_copies_of(under) - 1 if under else 0)
    refused = limits.create_face_refusal(plan.kind, plan.values[CREATOR], _smooth_level(plan.values[SHAPE]), copies)
    return [refused] if refused else []


def _parent(parent: object) -> str | None:
    if parent in ("", None):
        return None
    node = resolve_node(parent)
    refused = limits.edit_refusal(node) or exact_transform_refusal(node)
    if refused:
        raise create_refused(f"Creating under {node}", [refused])
    return node


def _make_primitive(kind: str, name: str) -> list[str]:
    label = name or _PRIMITIVE_STEMS[kind]
    if kind == "polyCube":
        made = cmds.polyCube(name=label)
    elif kind == "polySphere":
        made = cmds.polySphere(name=label)
    elif kind == "polyCylinder":
        made = cmds.polyCylinder(name=label)
    elif kind == "polyCone":
        made = cmds.polyCone(name=label)
    elif kind == "polyPlane":
        made = cmds.polyPlane(name=label)
    else:
        made = cmds.polyTorus(name=label)
    return [str(each) for each in made]


def _make_light(kind: str, name: str) -> str:
    """The light's transform. Measured: shadingNode names the shape, and Maya names its transform without "Shape"."""
    shape = f"{name}Shape" if name else f"{kind}Shape#"
    if kind not in LIGHT_TYPES:
        raise ToolError(f"{kind} isn't a light Symbiote creates.")
    return str(cmds.shadingNode(kind, asLight=True, name=shape))


def _make_camera(name: str) -> str:
    """The camera's transform. shadingNode returns the shape, and Maya names its transform after it without
    "Shape", with Maya's next number when that name is taken (measured: heroShape -> hero1, as polyCube does).

    Measured in mayapy 2027: asUtility=True also connects the shape's message into
    defaultRenderUtilityList1.utilities (Hypershade's Utilities tab), which cmds.camera and the Cameras menu don't.
    It is bookkeeping only, removed with the camera; the ledger records it.
    """
    # The constant is checked against the frozenset right before shadingNode: the source guard requires that check
    # for every shadingNode type, and this is how a fixed type meets it.
    kind = "camera"
    if kind not in _CAMERAS:
        raise ToolError(f"{kind} isn't a camera type.")
    shape = str(cmds.shadingNode(kind, asUtility=True, name=f"{name}Shape" if name else "cameraShape#"))
    cmds.setAttr(f"{shape}.renderable", False)  # as camera() and Create > Cameras make it (the census default)
    return str((cmds.listRelatives(shape, parent=True, fullPath=True) or [""])[0])


def _make(kind: str, name: str) -> dict[str, str]:
    """Create the nodes and return each role's full path."""
    if kind in limits.PRIMITIVE_TYPES:
        transform, creator = _make_primitive(kind, name)
        made = {CREATOR: creator, TRANSFORM: transform}
    elif kind in limits.LIGHT_TYPES:
        made = {TRANSFORM: _make_light(kind, name)}
    else:
        made = {TRANSFORM: _make_camera(name)}
    made[TRANSFORM] = str(cmds.ls(made[TRANSFORM], long=True)[0])
    made[SHAPE] = str((cmds.listRelatives(made[TRANSFORM], shapes=True, fullPath=True) or [""])[0])
    return made


def _build(plan: _Plan, name: str, parent: str | None) -> dict[str, str]:
    made = _make(plan.kind, name)
    if parent is not None:  # Maya renames it when the parent already has a child of that name (measured)
        moved = cmds.parent(made[TRANSFORM], parent)
        made[TRANSFORM] = f"{parent}|{str(moved[0]).rsplit('|', 1)[-1]}"
        made[SHAPE] = str((cmds.listRelatives(made[TRANSFORM], shapes=True, fullPath=True) or [""])[0])
    for role, node_type in plan.types.items():
        set_values(made[role], node_type, plan.values[role])
    return made


def _create_object(kind: object, name: object, attributes: object, parent: object) -> dict[str, Any]:
    if not isinstance(kind, str) or kind not in limits.CREATABLE_TYPES:
        allowed = ", ".join(sorted(limits.CREATABLE_TYPES))
        raise create_refused("Creating that object", [f"type must be one of {allowed}"])
    what = f"Creating a {kind}"
    refused = ready_refusal()
    if refused:
        raise create_refused(what, [refused])
    node_name = new_name(name)
    values = checked_attributes(attributes)
    under = _parent(parent)
    plan, reasons = _plan(kind, values)
    reasons += _value_reasons(plan, faces=not reasons, under=under)
    if reasons:
        raise create_refused(what, reasons)
    made = undoable(f"Symbiote: maya_create_object {node_name or kind}", lambda: _build(plan, node_name, under))
    return {"ok": True, "created": made[TRANSFORM], "type": kind, "nodes": made, "set": sorted(values)}


def create_object(type: object, name: object = "", attributes: object = None, parent: object = "") -> dict[str, Any]:
    """Create one primitive, camera or light with its values, or nothing if any part is refused."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_create_object, type, name, {} if attributes is None else attributes, parent)
