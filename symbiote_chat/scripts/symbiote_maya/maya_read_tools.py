"""Maya's read tools. None of them changes the scene; each runs its work on Maya's main thread."""
# maya_object_info's settable field asks maya_write_limits, which only queries.

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import maya.cmds as cmds
from symbiote_dcc.tool_support import ToolError

from symbiote_maya import maya_write_limits as limits
from symbiote_maya.main_thread import MAIN
from symbiote_maya.maya_denylist import ENUM_LIMITS, VALUE_CAPS
from symbiote_maya.names import resolve_node

_MAX_LISTED = 200
_MAX_OBJECTS = 500
# Maya's named time units; any other unit is written "<number>fps".
_NAMED_FPS = {"game": 15.0, "film": 24.0, "pal": 25.0, "ntsc": 30.0, "show": 48.0, "palf": 50.0, "ntscf": 60.0}
_NUMBERED_FPS = re.compile(r"^(\d+(?:\.\d+)?)fps$")


def _fps() -> float:
    unit = cmds.currentUnit(query=True, time=True)
    if unit in _NAMED_FPS:
        return _NAMED_FPS[unit]
    numbered = _NUMBERED_FPS.match(unit)
    if numbered is None:
        raise ToolError(f"Maya reports a time unit this tool can't read: {unit!r}.")
    return float(numbered.group(1))


def _is_startup_camera(transform: str) -> bool:
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    return any(
        cmds.nodeType(shape) == "camera" and cmds.camera(shape, query=True, startupCamera=True) for shape in shapes
    )


def _top_level() -> list[str]:
    assemblies = cmds.ls(assemblies=True, long=True) or []
    return [node for node in assemblies if not _is_startup_camera(node)]


def _scene_info() -> dict[str, Any]:
    top_level = _top_level()
    selection = cmds.ls(selection=True, long=True) or []
    return {
        "scene_file": cmds.file(query=True, sceneName=True),
        "unsaved_changes": bool(cmds.file(query=True, modified=True)),
        "fps": _fps(),
        "frame": cmds.currentTime(query=True),
        "frame_range": [cmds.playbackOptions(query=True, min=True), cmds.playbackOptions(query=True, max=True)],
        "selection": selection[:_MAX_LISTED],
        "top_level": top_level[:_MAX_LISTED],
        "truncated": len(top_level) > _MAX_LISTED or len(selection) > _MAX_LISTED,
        "maya_version": cmds.about(version=True),
    }


def scene_info() -> dict[str, Any]:
    return MAIN.run(_scene_info)


# How to find each kind of object. Cameras, lights and meshes are shapes; the tool reports the transform the
# artist sees in the outliner.
_FINDERS: dict[str, Callable[[], list[str]]] = {
    "camera": lambda: cmds.ls(type="camera", long=True),
    "light": lambda: cmds.ls(lights=True, long=True),
    "mesh": lambda: cmds.ls(type="mesh", noIntermediate=True, long=True),
    "material": lambda: cmds.ls(materials=True),
}


def _transform_of(shape: str) -> str:
    parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
    return str(parents[0]) if parents else shape


def _list_objects(kind: str) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    for node in _FINDERS[kind]() or []:
        name = node if kind == "material" else _transform_of(node)
        entry = {"name": str(name), "type": str(cmds.nodeType(node))}
        if entry not in entries:
            entries.append(entry)
        if len(entries) > _MAX_OBJECTS:
            break
    return {"kind": kind, "objects": entries[:_MAX_OBJECTS], "truncated": len(entries) > _MAX_OBJECTS}


def list_objects(kind: object) -> dict[str, Any]:
    if not isinstance(kind, str) or kind not in _FINDERS:
        raise ToolError(f"kind must be one of {', '.join(_FINDERS)}.")
    return MAIN.run(_list_objects, kind)


def _plain(attributes: list[str] | None) -> list[str]:
    """Attribute names without the per-component and multi-element ones (controlPoints.xValue, attr[0])."""
    return [a for a in attributes or [] if "." not in a and "[" not in a]


def _numeric_attributes(node: str) -> tuple[dict[str, Any], bool]:
    """The first 200 keyable numeric attributes, and whether more were left out."""
    values: dict[str, Any] = {}
    for attribute in _plain(cmds.listAttr(node, keyable=True)):
        value = cmds.getAttr(f"{node}.{attribute}")
        if isinstance(value, (int, float)):
            values[str(attribute)] = value
            if len(values) > _MAX_LISTED:
                break
    kept = dict(list(values.items())[:_MAX_LISTED])
    return kept, len(values) > _MAX_LISTED


def _multi_strings_set(plug: str) -> bool:
    """A multi attribute holds strings (its first element says so) and has elements. No text is read."""
    indices = cmds.getAttr(plug, multiIndices=True) or []
    return bool(
        cmds.getAttr(plug, size=True) > 0 and indices and cmds.getAttr(f"{plug}[{indices[0]}]", type=True) == "string"
    )


def _is_string_set(node: str, attribute: str) -> bool:
    """Whether a string, stringArray or multi string attribute holds anything. The text is never returned."""
    if cmds.attributeQuery(attribute, node=node, attributeType=True) != "typed":
        return False
    plug = f"{node}.{attribute}"
    kind = cmds.getAttr(plug, type=True)
    if kind == "string":
        return cmds.getAttr(plug) not in (None, "")
    if kind == "stringArray":
        return len(cmds.getAttr(plug) or []) > 0
    return bool(
        kind == "TdataCompound" and cmds.attributeQuery(attribute, node=node, multi=True) and _multi_strings_set(plug)
    )


def _string_attributes_set(node: str) -> tuple[list[str], bool]:
    """Which string attributes hold text: their names only, because the text itself can carry instructions."""
    found = [str(a) for a in _plain(cmds.listAttr(node, settable=True)) if _is_string_set(node, a)]
    return found[:_MAX_LISTED], len(found) > _MAX_LISTED


def _materials(node: str) -> list[str]:
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or [node]
    engines = list(dict.fromkeys(cmds.listConnections(shapes, type="shadingEngine") or []))
    surfaces: list[str] = []
    for engine in engines:
        for surface in cmds.listConnections(f"{engine}.surfaceShader", source=True, destination=False) or []:
            if str(surface) not in surfaces:
                surfaces.append(str(surface))
    return surfaces


def _attribute_limits(attr: str, spec: Mapping[str, Any], length: tuple[str, float | None]) -> dict[str, Any]:
    """What a write may set ``attr`` to: its kind and default, Maya's bounds under Symbiote's cap, its fields.

    A length (doubleLinear) is shown in the scene's working linear unit, the one a write takes, with that unit.
    """
    unit, cm_per_unit = length if spec["kind"] == "doubleLinear" else ("", None)
    scale = cm_per_unit or 1.0

    def scene(value: Any) -> Any:  # the census's cm as the scene's unit; a unit Symbiote can't convert stays cm
        return value / scale if unit and isinstance(value, (int, float)) and not isinstance(value, bool) else value

    shown: dict[str, Any] = {"kind": spec["kind"], "default": scene(spec["default"])}
    if "min" in spec:
        shown["min"] = scene(spec["min"])
    cap = VALUE_CAPS.get(attr)
    highest = [v for v in (spec.get("max"), cap) if v is not None]
    if highest:
        shown["max"] = scene(min(highest))
    if unit:
        shown["unit"] = (
            unit if cm_per_unit is not None else f"cm (this scene works in {unit}, which Symbiote can't convert)"
        )
    if "fields" in spec:
        allowed = ENUM_LIMITS.get(attr, (None, ""))[0]
        shown["fields"] = [name for name, index in spec["fields"] if allowed is None or index in allowed]
    return shown


def _settable(node: str) -> dict[str, Any]:
    """The census attributes the write tools may set on ``node``, or why they change nothing on it."""
    refused = limits.version_refusal() or limits.edit_refusal(node)
    if refused:
        return {"refused": refused, "attributes": {}}
    census = limits.settable_attributes(str(cmds.nodeType(node)))
    length = limits.scene_length_unit()
    return {
        "refused": None,
        "attributes": {attr: _attribute_limits(attr, spec, length) for attr, spec in sorted(census.items())},
    }


def _object_info(name: object) -> dict[str, Any]:
    node = resolve_node(name)
    parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
    children = cmds.listRelatives(node, children=True, fullPath=True) or []
    is_transform = "transform" in (cmds.nodeType(node, inherited=True) or [])
    attributes, attributes_cut = _numeric_attributes(node)
    strings, strings_cut = _string_attributes_set(node)
    return {
        "name": node,
        "type": cmds.nodeType(node),
        "parent": str(parents[0]) if parents else None,
        "children": [str(c) for c in children[:_MAX_LISTED]],
        "transform": cmds.xform(node, query=True, worldSpace=True, matrix=True) if is_transform else None,
        "attributes": attributes,
        "string_attributes_set": strings,
        "material": _materials(node),
        "referenced": bool(cmds.referenceQuery(node, isNodeReferenced=True)),
        "locked": bool((cmds.lockNode(node, query=True, lock=True) or [False])[0]),
        "settable": _settable(node),
        "truncated": {
            "children": len(children) > _MAX_LISTED,
            "attributes": attributes_cut,
            "string_attributes_set": strings_cut,
        },
    }


def object_info(name: object) -> dict[str, Any]:
    return MAIN.run(_object_info, name)
