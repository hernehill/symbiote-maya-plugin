"""Maya's direct write tools: set attributes, set a transform, select, assign a material, group or parent.

Each checks every part of the call on Maya's main thread before it changes anything, and makes its change as one
undo entry (``maya_write_support``). Creating is in ``maya_create_tool``; delete and save, which the artist
confirms first, are in ``maya_confirmed_writes``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import maya.cmds as cmds
from symbiote_dcc import values
from symbiote_dcc.tool_support import ToolError, refuse_while_a_dialog_is_open

from symbiote_maya import maya_write_limits as limits
from symbiote_maya.main_thread import MAIN
from symbiote_maya.maya_write_limits import MATERIAL_TYPES
from symbiote_maya.maya_write_support import (
    checked_attributes,
    checked_names,
    edit_refusals,
    exact_transform_refusal,
    node_type,
    ready_refusal,
    refused,
    reshaped_meshes,
    set_values,
    undoable,
    value_refusals,
)
from symbiote_maya.names import new_name, resolve_node
from symbiote_maya.ui import MAYA_UI

MAX_SELECTED = 200
MAX_TARGETS = 200
SELECT_MODES = ("replace", "add", "remove")
SPACES = ("object", "world")
_CREATE_KEYS = frozenset({"type", "name", "attributes"})


def _ready(what: str) -> None:
    reason = ready_refusal()
    if reason:
        raise refused(what, [reason])


# maya_set_attribute


def _set_attribute(name: object, attributes: object) -> dict[str, Any]:
    node = resolve_node(name)
    what = f"Setting attributes on {node}"
    _ready(what)
    given = checked_attributes(attributes)
    if not given:
        raise ToolError("Name at least one attribute to set.")
    kind = node_type(node)
    reasons = edit_refusals([node, *reshaped_meshes(node, kind)]) or value_refusals(node, kind, given)
    if reasons:
        raise refused(what, reasons)
    undoable(f"Symbiote: maya_set_attribute {node}", lambda: set_values(node, kind, given))
    return {"ok": True, "node": node, "set": sorted(given)}


def set_attribute(name: object, attributes: object) -> dict[str, Any]:
    """Set census attributes on one node, all of them or none."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_set_attribute, name, attributes)


# maya_set_transform


def _transform_values(translate: object, rotate: object, scale: object) -> dict[str, Any]:
    given = {k: v for k, v in (("translate", translate), ("rotate", rotate), ("scale", scale)) if v is not None}
    if not given:
        raise ToolError("Give at least one of translate, rotate or scale, each three numbers.")
    for attr, value in given.items():
        if values.vector(value, 3) is None:
            raise ToolError(f"{attr} takes three finite numbers.")
    return given


def _channels(node: str, attr: str) -> tuple[float, ...]:
    return tuple(float(v) for v in cmds.getAttr(f"{node}.{attr}")[0])  # a double3 reads as [(x, y, z)]


def _combined(attr: str, current: tuple[float, ...], offset: Any) -> list[float]:
    """A relative change's result: translate and rotate add the offset, scale multiplies by it."""
    if attr == "scale":
        return [held * factor for held, factor in zip(current, offset)]
    return [held + step for held, step in zip(current, offset)]


def _channel_values(node: str, given: Mapping[str, Any], world: bool, relative: bool) -> dict[str, Any]:
    """The values the call sets on the channels themselves: all three in object space, scale alone in world space.

    Object space means the channels (what the channel box shows). xform's objectSpace translate goes along the
    node's own rotated and scaled axes instead (measured in mayapy 2027), so object space never uses xform.
    """
    channels = {attr: value for attr, value in given.items() if not world or attr == "scale"}
    if not relative:
        return channels
    return {attr: _combined(attr, _channels(node, attr), offset) for attr, offset in channels.items()}


def _locked_channel_refusals(node: str, given: Mapping[str, Any]) -> list[str]:
    """Every channel the call would change that Maya won't let it: locked, its parent locked, or driven.

    Measured in mayapy 2027: setAttr raises on such a channel, while world-space xform skips it and reports
    nothing, so the node would not move and the tool would say it had. Both spaces refuse it here first, alike.
    """
    plugs = [f"{attr}{axis}" for attr in given for axis in "XYZ"]
    return [
        f"{plug} is locked or driven by a connection, so it can't be changed"
        for plug in plugs
        if not cmds.getAttr(f"{node}.{plug}", settable=True)
    ]


def _not_finite(attr: str, value: list[float]) -> str | None:
    return None if values.vector(value, 3) is not None else f"{attr} would not be three finite numbers after the change"


def _stored_refusals(node: str, values_held: Mapping[str, Any]) -> list[str]:
    """Why channel values may not be held: not finite, or refused by the census and VALUE_CAPS."""
    reasons = [r for r in (_not_finite(attr, list(value)) for attr, value in values_held.items()) if r]
    return reasons or value_refusals(node, "transform", values_held)


def _value_refusals(node: str, given: Mapping[str, Any], channels: Mapping[str, Any], relative: bool) -> list[str]:
    """Why the values may not be set, checked before any change against what the channels will hold.

    ``channels`` are the values the call sets itself (for a relative change, the result, never the offset). A
    world-space translate or rotate is Maya's to work out, so ``_checked_world_move`` checks the values Maya
    stored, after the move and inside its undo entry: every census check and VALUE_CAPS entry reaches them there.
    """
    reasons = _stored_refusals(node, channels)
    if "rotate" in given and "rotate" not in channels:
        reasons += [r for r in [limits.angle_unit_refusal("rotate")] if r]
    if relative and "translate" in given and "translate" not in channels:  # refused early, before any change
        current = cmds.xform(node, query=True, translation=True, worldSpace=True)
        reasons += [
            r for r in [_not_finite("translate", _combined("translate", tuple(current), given["translate"]))] if r
        ]
    return reasons


def _world_move(node: str, given: Mapping[str, Any], relative: bool) -> None:
    """translate and rotate in world space: Maya works out the local values. Every flag is a constant (source guard).

    Measured in mayapy 2027: a relative world translate moves by a world-axis offset, and a relative world rotate
    turns the node about the world axes, its position kept.
    """
    translate, rotate = given.get("translate"), given.get("rotate")
    if translate is not None and relative:
        cmds.xform(node, translation=list(translate), worldSpace=True, relative=True)
    elif translate is not None:
        cmds.xform(node, translation=list(translate), worldSpace=True)
    if rotate is not None and relative:
        cmds.xform(node, rotation=list(rotate), worldSpace=True, relative=True)
    elif rotate is not None:
        cmds.xform(node, rotation=list(rotate), worldSpace=True)


def _checked_world_move(node: str, given: Mapping[str, Any], relative: bool) -> None:
    """The world move, then the local values Maya stored for it, held to every check the channels are.

    Maya stores world x parent-inverse, so a finite world value can store a huge local one. Measured in mayapy
    2027: under a parent scaled 1e-300 or 0, Maya clamps the inverse, and world y 2 stores translateY 2e12. A
    refusal here raises inside the undo entry, so ``undoable`` takes the whole call back.
    """
    _world_move(node, given, relative)
    stored = {attr: list(_channels(node, attr)) for attr in ("translate", "rotate") if attr in given}
    reasons = _stored_refusals(node, stored)
    if reasons:
        raise refused(f"Setting the transform of {node}", reasons)


def _move(node: str, given: Mapping[str, Any], channels: Mapping[str, Any], world: bool, relative: bool) -> None:
    """World space moves translate and rotate through xform; the rest sets the channels (plan review L9)."""
    if world:
        _checked_world_move(node, given, relative)
    set_values(node, "transform", channels)


def _set_transform(
    name: object, translate: object, rotate: object, scale: object, space: object, relative: object
) -> dict[str, Any]:
    node = resolve_node(name)
    what = f"Setting the transform of {node}"
    _ready(what)
    if space not in SPACES:
        raise ToolError(f"space is one of {', '.join(SPACES)}.")
    if not isinstance(relative, bool):
        raise ToolError("relative takes true or false (a boolean): true moves by the values, false moves to them.")
    given = _transform_values(translate, rotate, scale)
    world = space == "world"
    reasons = edit_refusals([node]) or [r for r in [exact_transform_refusal(node)] if r]
    reasons = reasons or _locked_channel_refusals(node, given)
    if reasons:
        raise refused(what, reasons)
    channels = _channel_values(node, given, world, relative)
    reasons = _value_refusals(node, given, channels, relative)
    if reasons:
        raise refused(what, reasons)
    undoable(f"Symbiote: maya_set_transform {node}", lambda: _move(node, given, channels, world, relative))
    return {"ok": True, "node": node, "set": sorted(given), "space": space, "relative": relative}


def set_transform(
    name: object,
    translate: object = None,
    rotate: object = None,
    scale: object = None,
    space: object = "object",
    relative: object = False,
) -> dict[str, Any]:
    """Move, rotate or scale one transform, to the values or by them, in one undo entry."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_set_transform, name, translate, rotate, scale, space, relative)


# maya_select


def _select(names: object, mode: object) -> dict[str, Any]:
    what = "Changing the selection"
    _ready(what)
    if mode not in SELECT_MODES:
        raise ToolError(f"mode is one of {', '.join(SELECT_MODES)}.")
    # Ruling R1: a selection changes no node, so any node may be selected (no edit_refusal), by exact name only.
    nodes = checked_names(names, "names", MAX_SELECTED)

    def select() -> None:
        if mode == "replace":
            cmds.select(nodes, replace=True)
        elif mode == "add":
            cmds.select(nodes, add=True)
        else:
            cmds.select(nodes, deselect=True)

    undoable(f"Symbiote: maya_select {nodes[0]}", select)
    selection = [str(n) for n in cmds.ls(selection=True, long=True) or []]
    return {"ok": True, "selection": selection[:MAX_SELECTED]}


def select(names: object, mode: object = "replace") -> dict[str, Any]:
    """Change the artist's selection: exact node names only, never a pattern."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_select, names, mode)


# maya_assign_material


def _mesh_shapes(node: str) -> list[str]:
    """The meshes a material goes on: the node itself if it is one, else the meshes right below it."""
    if node_type(node) == "mesh":
        return [node]
    # noIntermediate: a deformed mesh's ShapeOrig is listed too otherwise (measured), and it isn't the one shown.
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True) or []
    return [str(shape) for shape in shapes if node_type(str(shape)) == "mesh"]


def _engine_left_refusal(engine: str) -> str | None:
    """A mesh leaves its shading engine when it takes another: never a referenced or locked one (Maya's default
    initialShadingGroup is allowed, or no new mesh could take a material)."""
    if cmds.referenceQuery(engine, isNodeReferenced=True):
        return f"a mesh would leave {engine}, which comes from a referenced file"
    if (cmds.lockNode(engine, query=True, lock=True) or [False])[0]:
        return f"a mesh would leave {engine}, which is locked"
    return None


def _target_meshes(targets: list[str]) -> tuple[list[str], list[str]]:
    """The meshes the material goes on, each checked with its target, and the reasons any may not change.

    ``sets(forceElement=)`` is given these meshes, never a target transform: on a transform Maya puts every shape
    below it in the engine, a NURBS surface as much as a mesh (measured), and only the meshes were checked.
    """
    reasons: list[str] = []
    meshes: list[str] = []
    for target in targets:
        shapes = _mesh_shapes(target)
        reasons += edit_refusals([target] + [shape for shape in shapes if shape != target])
        if not shapes:
            # Never ask listConnections about an empty list: Maya warns "No objects specified" (measured), and a
            # refactor that passed no list at all would read the artist's selection instead.
            reasons.append(f"{target} has no mesh to take a material")
            continue
        engines = dict.fromkeys(str(e) for e in cmds.listConnections(shapes, type="shadingEngine") or [])
        reasons += [r for r in (_engine_left_refusal(engine) for engine in engines) if r]
        meshes += shapes
    return reasons, list(dict.fromkeys(meshes))


# What may feed an engine Symbiote reuses: its surface shader and its members (measured: an engine we make has only
# surfaceShader and dagSetMembers incoming). Anything else (displacementShader, volumeShader, a renderer's shader)
# would come with it to the new meshes, so such an engine is passed over and a new one made (re-review 5c-3).
_ENGINE_INPUTS = frozenset({"surfaceShader", "dagSetMembers", "groupNodes"})


def _engine_inputs_only_ours(engine: str) -> bool:
    found = cmds.listConnections(engine, source=True, destination=False, plugs=True, connections=True) or []
    own_plugs = [str(plug) for plug in found[0::2]]
    return all(plug.split(".", 1)[-1].split("[", 1)[0] in _ENGINE_INPUTS for plug in own_plugs)


def _engine_of(material: str) -> str | None:
    """A shading engine whose surface shader ``material`` is, with nothing else feeding it, and which the write tools
    may change (the first by name), or None when it has none that passes: then a new one is made (M3)."""
    found = cmds.listConnections(f"{material}.outColor", source=False, destination=True, type="shadingEngine") or []
    for engine in sorted(str(e) for e in found):
        shaders = cmds.listConnections(f"{engine}.surfaceShader", source=True, destination=False) or []
        if (
            [str(s) for s in shaders] == [material.rsplit("|", 1)[-1]]
            and _engine_inputs_only_ours(engine)
            and limits.edit_refusal(engine) is None
        ):
            return engine
    return None


def _existing_material(material: object) -> tuple[str, str | None, list[str]]:
    """The material, its engine (or None, to make one) and the reasons either may not be used."""
    node = resolve_node(material)
    kind = node_type(node)
    if kind not in MATERIAL_TYPES:
        return (
            node,
            None,
            [f"{node} is a {kind}, and it isn't a material Symbiote assigns ({', '.join(sorted(MATERIAL_TYPES))})"],
        )
    return node, _engine_of(node), edit_refusals([node])


def _new_material(create: object) -> tuple[str, str, dict[str, Any], list[str]]:
    """The type, name and values of a material to make, and the reasons it may not be made."""
    if not isinstance(create, Mapping) or not set(create) <= _CREATE_KEYS or "type" not in create:
        raise ToolError('create takes type, name and attributes, for example {"type": "standardSurface"}.')
    kind = create["type"]
    if not isinstance(kind, str) or kind not in MATERIAL_TYPES:
        raise ToolError(f"create.type must be one of {', '.join(sorted(MATERIAL_TYPES))}.")
    name = new_name(create.get("name", ""))
    given = checked_attributes(create.get("attributes", {}))
    unmeasured = limits.unmeasured_type_refusal(kind)  # openPBRSurface isn't in every Maya 2025
    reasons = [unmeasured] if unmeasured else []
    reasons += [r for r in (limits.attribute_refusal(kind, a, v) for a, v in given.items()) if r]
    return kind, name, given, reasons


def _make_material(kind: str, name: str, given: Mapping[str, Any]) -> str:
    if kind not in MATERIAL_TYPES:  # again, right before shadingNode (re-review N9)
        raise ToolError(f"{kind} isn't a material Symbiote makes.")
    shader = str(cmds.shadingNode(kind, asShader=True, name=name or f"{kind}#"))
    set_values(shader, kind, given)
    return shader


def _new_engine(shader: str, name: str) -> str:
    engine = str(cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name))
    cmds.connectAttr(f"{shader}.outColor", f"{engine}.surfaceShader")
    return engine


def _assign(names: object, material: object, create: object) -> dict[str, Any]:
    what = "Assigning a material"
    _ready(what)
    if (material in ("", None)) == (create is None):
        raise ToolError("Give either material or create, not both: material names an existing one, create makes one.")
    targets = checked_names(names, "names", MAX_TARGETS)
    reasons, meshes = _target_meshes(targets)
    if create is None:
        shader, engine, more = _existing_material(material)
        label = shader

        def work() -> tuple[str, str]:
            made = engine or _new_engine(shader, f"{shader}SG")
            cmds.sets(meshes, forceElement=made)
            return shader, made

    else:
        kind, name, given, more = _new_material(create)
        label = name or kind

        def work() -> tuple[str, str]:
            made_shader = _make_material(kind, name, given)
            made = _new_engine(made_shader, f"{made_shader}SG")
            cmds.sets(meshes, forceElement=made)
            return made_shader, made

    if reasons + more:
        raise refused(what, reasons + more)
    shader, engine = undoable(f"Symbiote: maya_assign_material {label}", work)
    return {"ok": True, "material": shader, "shading_engine": engine, "assigned": targets}


def assign_material(names: object, material: object = "", create: object = None) -> dict[str, Any]:
    """Assign an existing material, or one made here, to meshes: all of them or none."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_assign, names, material, create)


# maya_group


def _current_parents(members: list[str]) -> list[str]:
    """Each member's parent now: the member leaves it, and a new group lands under the members' common parent."""
    found = (cmds.listRelatives(member, parent=True, fullPath=True) or [] for member in members)
    return list(dict.fromkeys(str(parent) for parents in found for parent in parents))


def _member_refusals(members: list[str], parent: str | None) -> list[str]:
    touched = members + ([parent] if parent else [])
    reasons = edit_refusals([p for p in _current_parents(members) if p not in touched])
    reasons += edit_refusals(touched)
    reasons += [reason for reason in (exact_transform_refusal(node) for node in touched) if reason]
    if parent is not None:
        reasons += [
            f"{member} can't go inside {parent}, which is inside {member}"
            for member in members
            if parent == member or parent.startswith(member + "|")
        ]
    return reasons


def _group(names: object, group_name: object, parent: object) -> dict[str, Any]:
    what = "Grouping"
    _ready(what)
    if (group_name in ("", None)) == (parent in ("", None)):
        raise ToolError(
            "Give either group_name or parent, not both: group_name makes a group, parent names a transform."
        )
    members = checked_names(names, "names", MAX_TARGETS)
    under = resolve_node(parent) if parent not in ("", None) else None
    made_name = new_name(group_name) if under is None else ""
    reasons = _member_refusals(members, under)
    # A new group lands under the members' own parent with ghosting off, so only a named parent adds ghosts.
    reasons = reasons or [r for r in [limits.reparent_refusal(members, under) if under else None] if r]
    if reasons:
        raise refused(what, reasons)
    drawn_before = limits.drawn_now(members) if under is None else []

    def work() -> tuple[str, list[str]]:
        # Maya renames a member whose name its new parent already holds (measured: floor -> floor1).
        if under is not None:
            moved = cmds.parent(members, under) or []
            return under, [f"{under}|{str(leaf).rsplit('|', 1)[-1]}" for leaf in moved]
        made = cmds.group(members, name=made_name)  # Maya's unique name for it (measured)
        path = str(cmds.ls(made, long=True)[0])
        # Measured: members with different parents are grouped at the world root, with one shared parent under it.
        # Wherever Maya put it, what the members draw there is judged here, inside the undo entry.
        refusal = limits.raised_draw_refusal(drawn_before, limits.drawn_now([path]))
        if refusal:
            raise refused(what, [refusal])
        return path, [str(child) for child in cmds.listRelatives(path, children=True, fullPath=True) or []]

    group, moved = undoable(f"Symbiote: maya_group {made_name or under}", work)
    return {"ok": True, "group": group, "members": moved}


def group(names: object, group_name: object = "", parent: object = "") -> dict[str, Any]:
    """Put nodes in a new group, or under an existing transform."""
    refuse_while_a_dialog_is_open(MAYA_UI)
    return MAIN.checked(_group, names, group_name, parent)
