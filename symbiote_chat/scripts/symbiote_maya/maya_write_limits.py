"""What Maya's write tools may change: which nodes, which attributes, and what values, checked before any change.

Each check returns the reason a thing is refused, or ``None`` when it is allowed; the reasons reach the chat
through ``refusal_text``. Every write tool calls ``version_refusal`` first, and every undoable one (all but save)
``undo_refusal``; ``edit_refusal`` runs on every node a change touches (targets, parents, group members, materials,
shading engines, delete targets, the meshes a creator feeds; not a selection, which changes no node), and
``attribute_refusal`` on every value, with ``create_face_refusal``, ``smooth_refusal`` (a mesh) or
``subdivision_refusal`` (a primitive creator) where faces grow.

Attributes come from the census (``maya_allowlist_data.CENSUS[<Maya year>]``): only the long names Maya itself
defines on a settable type, measured with no plug-in loaded, so short names, aliases, dynamic attributes and
Arnold's ``ai*`` attributes are refused. A Maya (year, platform, update) not measured is refused outright (``maya_versions``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

import maya.cmds as cmds
from symbiote_dcc import values

from symbiote_maya import maya_versions
from symbiote_maya.maya_allowlist_data import API_MEASURED, CENSUS
from symbiote_maya.maya_denylist import (
    CAP_FLOOR,
    CREATABLE_TYPES,
    CREATE_FACE_BUDGET,
    EDITABLE_TYPES,
    ENUM_LIMITS,
    LIGHT_TYPES,
    MATERIAL_TYPES,
    PRIMITIVE_SUBDIVISIONS,
    PRIMITIVE_TYPES,
    SETTABLE_TYPES,
    SMOOTHED_FACE_BUDGET,
    VALUE_CAPS,
    edit_type_denial,
    type_denial,
    write_denial,
)

__all__ = [
    "CREATABLE_TYPES",
    "EDITABLE_TYPES",
    "LIGHT_TYPES",
    "MATERIAL_TYPES",
    "PRIMITIVE_TYPES",
    "SETTABLE_TYPES",
    "angle_unit_refusal",
    "attribute_refusal",
    "DrawnMesh",
    "draw_change_refusal",
    "drawn_refusal",
    "ghost_copies_of",
    "drawn_now",
    "planned_ghost",
    "raised_draw_refusal",
    "reparent_refusal",
    "create_face_refusal",
    "edit_refusal",
    "enum_index",
    "output_meshes",
    "primitive_faces",
    "scene_length_unit",
    "settable_attributes",
    "smooth_refusal",
    "subdivision_refusal",
    "subdivisions_refusal",
    "undo_refusal",
    "unmeasured_type_refusal",
    "version_refusal",
]

# Measured: setAttr wraps an integer outside its kind (2**31 + 5 in a long reads -2147483643, 40000 in a short
# -25536), and a float attribute stores a double past float32's range as infinity. Byte is 0-255 (Maya refuses
# outside it, measured on a dynamic byte).
_INTEGER_RANGES = {"long": (-(2**31), 2**31 - 1), "short": (-(2**15), 2**15 - 1), "byte": (0, 255)}
_FLOAT32_MAX = 3.4028234663852886e38
_FLOATS = frozenset({"double", "doubleLinear", "doubleAngle", "time"})
_SHOWS_SMOOTH_MESH = "displaySmoothMesh"
_MAX_SHOWN = 60
_CAPS = frozenset({"subdivisionsCaps", "subdivisionsCap"})  # the only subdivisions that may be 0 (measured)
_ATTRIBUTE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _shown(value: object) -> str:
    """The model's value as a refusal quotes it: its repr, cut short so a huge value can't flood the chat."""
    try:
        text = repr(value)
    except ValueError:  # Python 3.11+ refuses to print an int of more than 4300 digits
        return "a number too long to show"
    return text if len(text) <= _MAX_SHOWN else text[: _MAX_SHOWN - 3] + "..."


def version_refusal() -> str | None:
    """Refuse every write on a Maya (year, platform, update) whose attributes weren't measured: its census may differ."""
    return maya_versions.measured_refusal("write tools", API_MEASURED)


def settable_attributes(node_type: str, version: str | None = None) -> Mapping[str, Any]:
    """The census attributes a write tool may set on ``node_type`` in this Maya (empty when there are none)."""
    types: Mapping[str, Mapping[str, Any]] = CENSUS.get(version or maya_versions.running_version(), {})
    return types.get(node_type, {})


def unmeasured_type_refusal(node_type: str) -> str | None:
    """A type this Maya's census didn't measure, so this Maya may not have it (openPBRSurface isn't in every
    Maya 2025): a write tool doesn't make one."""
    running = maya_versions.running_version()
    if node_type in CENSUS.get(running, {}):
        return None
    return f"{node_type} wasn't measured in this Maya ({running}), so Symbiote doesn't make one here"


def _number_refusal(attr: str, spec: Mapping[str, Any], value: object) -> str | None:
    kind = spec["kind"]
    if kind in _INTEGER_RANGES:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{attr} takes a whole number, and {_shown(value)} isn't one"
        low, high = _INTEGER_RANGES[kind]
        if not low <= value <= high:
            return f"{attr} takes a whole number from {low} to {high}, and {_shown(value)} is outside it"
    elif not values.finite_number(value) or not isinstance(value, (int, float)):
        return f"{attr} takes a finite number, and {_shown(value)} isn't one"
    elif kind not in _FLOATS and abs(value) > _FLOAT32_MAX:
        return f"{attr} holds a single-precision number, and {_shown(value)} is too large for it"
    return _hard_limit_refusal(attr, spec, value)


# Centimetres in one of each working linear unit currentUnit names. Maya holds a length in cm.
_CM_PER_UNIT = {"mm": 0.1, "cm": 1.0, "m": 100.0, "km": 100_000.0, "in": 2.54, "ft": 30.48, "yd": 91.44}
_CM_PER_UNIT["mi"] = 160_934.4


def scene_length_unit() -> tuple[str, float | None]:
    """The scene's working linear unit, and the centimetres in one of it (None for a unit Symbiote can't convert).

    Measured in mayapy 2027: setAttr, getAttr and xform take and give lengths in this unit, and Maya shows a
    length's limits converted to it (polySphere.radius's minimum, 0.01 cm, reads as 0.0001 in metres and 0.1 in
    millimetres). The census holds lengths, their defaults and limits, in cm.
    """
    unit = str(cmds.currentUnit(query=True, linear=True))
    return unit, _CM_PER_UNIT.get(unit)


def _hard_limit_refusal(attr: str, spec: Mapping[str, Any], value: Any) -> str | None:
    """Maya's own minimum and maximum (the census holds them): setAttr refuses outside them (measured).

    A length's limits are in cm and its value in the scene's unit, so the limit is converted before it is compared.
    """
    if "min" not in spec and "max" not in spec:
        return None
    unit, cm_per_unit = "", 1.0
    if spec["kind"] == "doubleLinear":
        unit, known = scene_length_unit()
        if known is None:
            return f"{attr} is a length, and this scene's working linear unit is {unit}, which Symbiote can't convert"
        cm_per_unit = known

    def shown(limit: float) -> str:  # a length's limit in the scene's unit; any other as the census holds it
        return f"{limit / cm_per_unit:g} {unit}" if unit else str(limit)

    if "min" in spec and value < spec["min"] / cm_per_unit:
        return f"{attr} must be at least {shown(spec['min'])}, and {_shown(value)} is below it"
    if "max" in spec and value > spec["max"] / cm_per_unit:
        return f"{attr} must be at most {shown(spec['max'])}, and {_shown(value)} is above it"
    return None


def enum_index(spec: Mapping[str, Any], value: object) -> int:
    """Maya's index for an enum value given as a field name or an index; ``ValueError`` when it is neither.

    Indices can skip (smoothDrawType is 0, 2, 3), so a name's index is Maya's, not its position.
    """
    fields: list[list[Any]] = spec["fields"]
    if isinstance(value, str) and values.enum_choice(value, [name for name, _index in fields]) is not None:
        return int(next(index for name, index in fields if name == value))
    if isinstance(value, int) and not isinstance(value, bool) and value in {index for _name, index in fields}:
        return value
    raise ValueError(f"{_shown(value)} is not a field of this enum")


def _enum_refusal(attr: str, spec: Mapping[str, Any], value: object) -> str | None:
    try:
        index = enum_index(spec, value)
    except ValueError:
        choices = ", ".join(f"{name} ({index})" for name, index in spec["fields"])
        return f"{attr} is one of {choices}, and {_shown(value)} isn't"
    allowed, why = ENUM_LIMITS.get(attr, (None, ""))
    if allowed is not None and index not in allowed:
        return f"{attr} may be set to {why}, and {_shown(value)} isn't one"
    return None


def _vector_refusal(attr: str, spec: Mapping[str, Any], value: object, census: Mapping[str, Any]) -> str | None:
    vector = values.vector(value, 3)
    if vector is None:
        return f"{attr} takes three finite numbers, and {_shown(value)} isn't that"
    if spec["kind"] == "float3" and any(abs(v) > _FLOAT32_MAX for v in vector):
        return f"{attr} holds single-precision numbers, and {_shown(value)} is too large for them"
    # Each child keeps Maya's own minimum and maximum (ghostColorPre's are 0-1); setAttr refuses outside them.
    children = zip(spec.get("children", ()), vector)
    found = (_hard_limit_refusal(child, census[child], v) for child, v in children if child in census)
    return next((reason for reason in found if reason), None)


def _value_refusal(attr: str, spec: Mapping[str, Any], value: object, census: Mapping[str, Any]) -> str | None:
    kind = spec["kind"]
    if isinstance(value, str) and kind != "enum":
        return f"{attr} takes a number, not text; text is only accepted as an enum field's name"
    if kind == "bool":
        return None if values.is_strict_bool(value) else f"{attr} takes true or false, and {_shown(value)} isn't either"
    if kind == "enum":
        return _enum_refusal(attr, spec, value)
    if kind in ("float3", "double3"):
        return _vector_refusal(attr, spec, value, census)
    return _number_refusal(attr, spec, value)


def _is_angle(spec: Mapping[str, Any], census: Mapping[str, Any]) -> bool:
    """A doubleAngle, or a vector whose children are (rotate: rotateX, rotateY, rotateZ)."""
    if spec["kind"] == "doubleAngle":
        return True
    children = spec.get("children", ())
    return bool(children) and all(census.get(child, {}).get("kind") == "doubleAngle" for child in children)


def angle_unit_refusal(attr: str) -> str | None:
    """Refuse an angle unless the scene works in degrees.

    Measured in mayapy 2027: setAttr, getAttr and xform all read and write angles in the scene's working angle
    unit (currentUnit), so in a radians scene rotate 90 is 90 radians. The write tools and the census's limits
    mean degrees; rather than convert on every path, an angle is refused until the scene works in degrees.
    """
    unit = str(cmds.currentUnit(query=True, angle=True))
    if unit in ("deg", "degree"):
        return None
    return (
        f"{attr} is an angle, and this scene's working angle unit is {unit}: Symbiote sets angles in degrees only. "
        "The artist can set Preferences > Settings > Working Units > Angle to degrees, then ask again"
    )


def attribute_refusal(node_type: str, attr: str, value: object) -> str | None:
    """Why ``attr`` may not be set to ``value`` on a ``node_type``, or ``None``. ``attr`` is a bare long name."""
    running = maya_versions.running_version()  # asked once per check: the gate and the census share it
    refused = maya_versions.measured_refusal("write tools", API_MEASURED, year=running) or type_denial(node_type)
    if refused:
        return refused
    if not isinstance(attr, str) or not _ATTRIBUTE_NAME.fullmatch(attr):
        return f"{_shown(attr)} isn't an attribute name: letters, digits and underscores, at most 128"
    why = write_denial(attr)
    if why:
        return f"{attr} is never set by Symbiote: {why}"
    census = settable_attributes(node_type, running)
    spec = census.get(attr)
    if spec is None:
        return (
            f"{attr} isn't an attribute Symbiote may set on a {node_type}. Use the attribute's full name as Maya "
            "lists it (translateX, not tx); aliases and added attributes aren't settable"
        )
    refused = (angle_unit_refusal(attr) if _is_angle(spec, census) else None) or _value_refusal(
        attr, spec, value, census
    )
    if refused:
        return refused
    cap = VALUE_CAPS.get(attr)
    if cap is not None and isinstance(value, (int, float)) and value > cap:
        return f"{attr} is {_shown(value)}, over the limit of {cap}"
    if cap is not None and isinstance(value, (int, float)) and value < CAP_FLOOR:  # caps are two-sided
        return f"{attr} must be at least {CAP_FLOOR}, and {_shown(value)} is below it"
    return None


def primitive_faces(primitive: str, given: Mapping[str, object]) -> int:
    """The faces a primitive makes, from its subdivisions: ``given`` over this Maya's defaults (measured formulas).

    ``given`` holds whole numbers: ``attribute_refusal`` has passed each value first. A subdivision Maya itself
    refuses (below 1, or a negative cap) raises ``ValueError``, so a count is never zero or negative.
    """
    if primitive not in PRIMITIVE_TYPES:
        raise KeyError(primitive)
    defaults = settable_attributes(primitive)

    def sub(name: str) -> int:
        number = given.get(name, defaults[name]["default"])
        if isinstance(number, bool) or not isinstance(number, int):
            raise TypeError(f"{primitive}.{name} must be a whole number, not {_shown(number)}")
        if number < (0 if name in _CAPS else 1):
            raise ValueError(f"{primitive}.{name} can't be {number}")
        return number

    if primitive == "polyCube":
        width, height, depth = sub("subdivisionsWidth"), sub("subdivisionsHeight"), sub("subdivisionsDepth")
        return 2 * (width * height + height * depth + width * depth)
    if primitive == "polyPlane":
        return sub("subdivisionsWidth") * sub("subdivisionsHeight")
    around = sub("subdivisionsAxis") * sub("subdivisionsHeight")
    if primitive == "polyCylinder":  # two caps: an n-gon each, or rings of quads and triangles
        caps = sub("subdivisionsCaps")
        return around + (2 * sub("subdivisionsAxis") * caps if caps else 2)
    if primitive == "polyCone":  # one cap
        caps = sub("subdivisionsCap")
        return around + (sub("subdivisionsAxis") * caps if caps else 1)
    return around  # polySphere and polyTorus


def _smoothed_refusal(faces: int, level: int, copies: int = 1) -> str | None:
    smoothed = faces * 4**level * copies
    if smoothed <= SMOOTHED_FACE_BUDGET:
        return None
    ghosts = f" with ghosting's {copies} copies" if copies > 1 else ""
    return (
        f"smoothing {faces} faces at level {level} draws {smoothed} faces{ghosts}, over the limit of "
        f"{SMOOTHED_FACE_BUDGET}"
    )


def create_face_refusal(
    primitive: str, given: Mapping[str, object], smooth_level: int | None = None, copies: int = 1
) -> str | None:
    """A create over the face budget, counted from its subdivisions, and again as drawn: smoothed at
    ``smooth_level`` and drawn ``copies`` times (ghosting)."""
    faces = primitive_faces(primitive, given)
    if faces > CREATE_FACE_BUDGET:
        return f"a {primitive} with those subdivisions makes {faces} faces, over the limit of {CREATE_FACE_BUDGET}"
    if smooth_level is not None:
        return _smoothed_refusal(faces, smooth_level, copies)
    return _ghosted_refusal(faces, copies)


def smooth_refusal(mesh: str, attr: str, value: object) -> str | None:
    """Setting ``smoothLevel``, or showing the smooth mesh at its current level, on an existing mesh over budget,
    with every ghost it is drawn with counted (``draw_change_refusal`` with this one value)."""
    if attr == "smoothLevel" and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        return f"smoothLevel takes a whole number from 0, and {_shown(value)} isn't one"
    return draw_change_refusal(mesh, "mesh", {attr: value})


# Ghosting: the node drawn again at each ghost frame. Judged on the state a call leaves (security re-review 5c-7):
# the call's values over the node's current ones, never the value alone, the way smoothing reads smoothLevel.
GHOST_ATTRIBUTES = ("ghosting", "ghostingMode", "ghostPreFrames", "ghostPostFrames", "ghostUseDriver")
_DRAW_ATTRIBUTES = frozenset({*GHOST_ATTRIBUTES, "smoothLevel", _SHOWS_SMOOTH_MESH})


def _ghost_state_refusal(state: Mapping[str, Any]) -> str | None:
    """Why ghosting may not be on in ``state`` (every GHOST_ATTRIBUTES value, an enum by Maya's index)."""
    if not state["ghosting"]:
        return None
    allowed, why = ENUM_LIMITS["ghostingMode"]
    if state["ghostingMode"] not in allowed:
        return f"ghosting would be on with ghostingMode {state['ghostingMode']}, and ghosting is allowed with {why}"
    if state["ghostUseDriver"]:
        return (
            "ghosting would be on with ghostUseDriver, which draws a ghost at each of a driver's keys; no cap bounds it"
        )
    for attr in ("ghostPreFrames", "ghostPostFrames"):
        if not CAP_FLOOR <= state[attr] <= VALUE_CAPS[attr]:
            return f"ghosting would be on with {attr} at {state[attr]}, outside {CAP_FLOOR} to {VALUE_CAPS[attr]}"
    return None


def _extra_ghosts(state: Mapping[str, Any]) -> int:
    """The extra copies a node's ghosting draws: each ghost frame, whatever the mode (the larger count)."""
    return int(state["ghostPreFrames"]) + int(state["ghostPostFrames"]) if state["ghosting"] else 0


def _node_ghost_state(node: str, changes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    given = changes or {}
    state = {attr: given[attr] if attr in given else cmds.getAttr(f"{node}.{attr}") for attr in GHOST_ATTRIBUTES}
    if isinstance(state["ghostingMode"], str):
        state["ghostingMode"] = enum_index(settable_attributes("transform")["ghostingMode"], state["ghostingMode"])
    return state


def _long(node: str) -> str:
    return str((cmds.ls(node, long=True) or [node])[0])


def _chain(path: str) -> list[str]:
    """The node at ``path`` and every node above it, top first."""
    parts = path.split("|")
    return ["|".join(parts[:end]) for end in range(2, len(parts) + 1)]


def _chain_extras(nodes: list[str], changes: Mapping[str, Mapping[str, Any]]) -> int:
    """The extra copies every ghosted node in ``nodes`` draws, summed: nested ghosting draws each level's ghosts
    (security re-review). Ghost drawing is viewport work mayapy can't count, so the sum is the conservative choice,
    and a ghosted node is taken to ghost everything below it at any depth."""
    return sum(_extra_ghosts(_node_ghost_state(node, changes.get(node))) for node in nodes)


def ghost_copies_of(node: str) -> int:
    """How many times a mesh at ``node`` is drawn: once, plus the ghosts of the node and every node above it."""
    return 1 + _chain_extras(_chain(_long(node)), {})


class DrawnMesh(NamedTuple):
    """A mesh as a call leaves it: its faces, the smooth level it is drawn at (None: not smoothed), its copies."""

    name: str
    faces: int
    level: int | None
    copies: int


def drawn_refusal(meshes: list[DrawnMesh]) -> str | None:
    """Every mesh a call affects, as it leaves them, against SMOOTHED_FACE_BUDGET once: faces x 4^level x copies."""
    total = sum(m.faces * 4 ** (m.level or 0) * m.copies for m in meshes)
    if total <= SMOOTHED_FACE_BUDGET:
        return None
    if len(meshes) == 1 and meshes[0].level is not None:
        return _smoothed_refusal(meshes[0].faces, meshes[0].level, meshes[0].copies)
    if len(meshes) == 1:
        return _ghosted_refusal(meshes[0].faces, meshes[0].copies)
    return f"the {len(meshes)} meshes it reaches would draw {total} faces with their ghosts, over the limit of {SMOOTHED_FACE_BUDGET}"


def _drawn_total(meshes: list[DrawnMesh]) -> int:
    return sum(m.faces * 4 ** (m.level or 0) * m.copies for m in meshes)


def raised_draw_refusal(before: list[DrawnMesh], after: list[DrawnMesh]) -> str | None:
    """``after`` against the budget, unless it draws no more than ``before``: a change that lowers what is drawn
    (ghosting off, a lower smoothLevel) is allowed even on a subtree still over the budget, as subdivisions are."""
    if _drawn_total(after) <= _drawn_total(before):
        return None
    return drawn_refusal(after)


def drawn_now(nodes: list[str]) -> list[DrawnMesh]:
    """Every mesh at or below each node, as it is drawn now."""
    return [_projected_mesh(mesh, {}) for node in nodes for mesh in _meshes_at(_long(node), str(cmds.nodeType(node)))]


def _ghosted_refusal(faces: int, copies: int) -> str | None:
    drawn = faces * copies
    if copies == 1 or drawn <= SMOOTHED_FACE_BUDGET:
        return None
    return f"ghosting draws {drawn} faces ({faces} faces, {copies} times), over the limit of {SMOOTHED_FACE_BUDGET}"


def _projected_mesh(mesh: str, changes: Mapping[str, Mapping[str, Any]], extra_ghosts: int = 0) -> DrawnMesh:
    """``mesh`` with ``changes`` (by full path) over its current values; ``extra_ghosts`` from a new parent."""
    own = changes.get(mesh, {})
    if "smoothLevel" in own:
        level: int | None = int(own["smoothLevel"])  # a level being set counts, shown or not
    elif _shows_smooth(
        own[_SHOWS_SMOOTH_MESH] if _SHOWS_SMOOTH_MESH in own else cmds.getAttr(f"{mesh}.{_SHOWS_SMOOTH_MESH}")
    ):
        level = int(cmds.getAttr(f"{mesh}.smoothLevel"))
    else:
        level = None
    copies = 1 + extra_ghosts + _chain_extras(_chain(mesh), changes)
    return DrawnMesh(mesh, int(cmds.polyEvaluate(mesh, face=True)), level, copies)


def _meshes_at(path: str, kind: str) -> list[str]:
    if kind == "mesh":
        return [path]
    below = cmds.listRelatives(path, allDescendents=True, type="mesh", fullPath=True, noIntermediate=True)
    return [str(mesh) for mesh in below or []]


def draw_change_refusal(node: str, kind: str, changes: Mapping[str, Any]) -> str | None:
    """A call setting smoothing or ghosting on ``node``: the ghost state it leaves, then every mesh at or below the
    node as the call leaves it (all its values together, security re-review), against the face budget once."""
    if not _DRAW_ATTRIBUTES & changes.keys():
        return None
    path = _long(node)
    if any(attr in changes for attr in GHOST_ATTRIBUTES):
        refused = _ghost_state_refusal(_node_ghost_state(path, changes))
        if refused:
            return refused
    meshes = _meshes_at(path, kind)
    before = [_projected_mesh(mesh, {}) for mesh in meshes]
    return raised_draw_refusal(before, [_projected_mesh(mesh, {path: changes}) for mesh in meshes])


def reparent_refusal(members: list[str], parent: str) -> str | None:
    """Members moved under ``parent``: every mesh in them, drawn with the ghosts of ``parent`` and every node above
    it on top of their own, against the face budget once (security re-review)."""
    above = _chain_extras(_chain(_long(parent)), {})
    if not above:
        return None
    meshes = [mesh for member in members for mesh in _meshes_at(_long(member), cmds.nodeType(member))]
    # Each mesh keeps its own chain's ghosts, including those of the ancestors it leaves (counted conservatively).
    return raised_draw_refusal(
        [_projected_mesh(mesh, {}) for mesh in meshes], [_projected_mesh(mesh, {}, above) for mesh in meshes]
    )


def planned_ghost(node_type: str, given: Mapping[str, Any]) -> tuple[str | None, int]:
    """For a node a create makes: why its ghosting may not be on, and the extra copies it draws."""
    if not any(attr in given for attr in GHOST_ATTRIBUTES):
        return None, 0
    census = settable_attributes(node_type)
    state = {attr: given.get(attr, census[attr]["default"]) for attr in GHOST_ATTRIBUTES}
    if isinstance(state["ghostingMode"], str):
        state["ghostingMode"] = enum_index(census["ghostingMode"], state["ghostingMode"])
    return _ghost_state_refusal(state), _extra_ghosts(state)


def _shows_smooth(value: object) -> bool:
    """A displaySmoothMesh value other than Base Mesh Only (0); a value that isn't a field counts as showing it."""
    try:
        return enum_index(settable_attributes("mesh")[_SHOWS_SMOOTH_MESH], value) != 0
    except ValueError:
        return True


def output_meshes(creator: str) -> list[str]:
    """The meshes downstream of a creator, through any history between them (listHistory(future=True), measured)."""
    downstream = cmds.listHistory(creator, future=True) or []
    full = cmds.ls(downstream, long=True) or []  # full paths: a short name may be shared by two meshes
    return [str(node) for node in full if cmds.nodeType(node) == "mesh"]


def subdivision_refusal(creator: str, attr: str, value: object) -> str | None:
    """A change to one of a primitive creator's subdivisions over budget: ``subdivisions_refusal`` for one value."""
    return subdivisions_refusal(creator, {attr: value})


def subdivisions_refusal(creator: str, changes: Mapping[str, object]) -> str | None:
    """Changes to a primitive creator's subdivisions that take it, or the meshes it feeds, over budget.

    The changes are counted together, so a call that raises two subdivisions is checked as it would land. Each
    mesh's new count is projected from its current one by the creator's ratio, so history between them (a
    polySmooth) counts, and never below the creator's own new count; a mesh that shows its smooth mesh is checked
    again at its smoothLevel. Changes that don't raise the count are always allowed.
    """
    primitive = cmds.nodeType(creator)
    names = PRIMITIVE_SUBDIVISIONS.get(primitive, ())
    raised = {attr: value for attr, value in changes.items() if attr in names}
    if not raised:
        return None
    current = {name: cmds.getAttr(f"{creator}.{name}") for name in names}
    try:
        before = primitive_faces(primitive, current)
        after = primitive_faces(primitive, {**current, **raised})
    except (TypeError, ValueError) as error:
        return f"{creator}: {error}"
    if after <= before:
        return None
    if after > CREATE_FACE_BUDGET:
        return f"{creator} would make {after} faces, over the limit of {CREATE_FACE_BUDGET}"
    for mesh in output_meshes(creator):
        projected = math.ceil(int(cmds.polyEvaluate(mesh, face=True)) * after / before)
        refused = _mesh_growth_refusal(mesh, max(projected, after))
        if refused:
            return refused
    return None


def _mesh_growth_refusal(mesh: str, faces: int) -> str | None:
    if faces > CREATE_FACE_BUDGET:
        return f"{mesh} would have {faces} faces after its history, over the limit of {CREATE_FACE_BUDGET}"
    copies = ghost_copies_of(mesh)
    if not cmds.getAttr(f"{mesh}.{_SHOWS_SMOOTH_MESH}"):
        refused = _ghosted_refusal(faces, copies)
    else:
        refused = _smoothed_refusal(faces, int(cmds.getAttr(f"{mesh}.smoothLevel")), copies)
    return f"{mesh}: {refused}" if refused else None


def _is_startup_camera(node: str, node_type: str) -> bool:
    """Asked of the node itself and, for a transform, of each camera shape below it (both answer, measured)."""
    if node_type == "camera":
        return bool(cmds.camera(node, query=True, startupCamera=True))
    if node_type != "transform":
        return False
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
    cameras = [shape for shape in shapes if cmds.nodeType(shape) == "camera"]
    if not cameras:
        return False  # camera(query=True) raises on a transform without a camera shape (measured)
    asked = [node, *cameras]
    return any(bool(cmds.camera(each, query=True, startupCamera=True)) for each in asked)


def edit_refusal(node: str) -> str | None:
    """Why the write tools may not change ``node`` (a resolved full name), or ``None``. Asked of the real node."""
    node_type = cmds.nodeType(node)
    refused = edit_type_denial(node_type)
    if refused:
        return f"{node}: {refused}"
    if cmds.referenceQuery(node, isNodeReferenced=True):
        return f"{node} comes from a referenced file; change it in that file instead"
    if (cmds.lockNode(node, query=True, lock=True) or [False])[0]:
        return f"{node} is locked"
    if cmds.ls(node, defaultNodes=True):
        return f"{node} is one of Maya's own default nodes"
    if _is_startup_camera(node, node_type):
        return f"{node} is one of Maya's startup cameras (persp, top, front, side)"
    return None


def undo_refusal() -> str | None:
    """Refuse while undo is off: every write is all-or-nothing, and that needs undo."""
    if cmds.undoInfo(query=True, state=True):
        return None
    return "undo is switched off in this Maya, and Symbiote only changes a scene it can undo; turn undo back on"
