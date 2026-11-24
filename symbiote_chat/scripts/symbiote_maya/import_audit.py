"""The import audit: what an import brought that can carry code, a path or heavy work, read after it lands.

``audit`` runs on Maya's main thread, inside the import's own call, so no artist action comes between the
import and it. It only reads: it never creates a probe node, and every default it compares against is pinned in
``import_defaults`` (derived by ``tools/derive_import_defaults.py`` from each pair's measurement). An attribute it
can't place is refused. mtoa's defaults are pinned per (year, platform) pair and used only when exactly the mtoa
measured for the running pair is loaded (``arnold_tables``).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterator, Mapping
from typing import Any, NamedTuple

import maya.cmds as cmds
from symbiote_dcc import paths

from symbiote_maya import import_defaults, maya_denylist
from symbiote_maya.import_connections import NUMERIC_KINDS, connection_reasons
from symbiote_maya.maya_versions import platform_name, running_platform, running_version

# The node types each format's fixtures produce (measured in Maya 2027 by make_import_fixtures.py; the
# report for Task 11 lists them per fixture). studio_shot.abc's face sets bring shading groups, its control a
# nurbsCurve. The check is the exact type: a joint is not a transform.
ABC_TYPES = frozenset(
    {
        "AlembicNode",
        "camera",
        "groupId",
        "materialInfo",
        "mesh",
        "nurbsCurve",
        "shadingEngine",
        "transform",
        "unitConversion",
    }
)
OBJ_TYPES = frozenset({"groupId", "lambert", "materialInfo", "mesh", "phong", "shadingEngine", "transform"})
TYPES_BY_EXTENSION = {".abc": ABC_TYPES, ".obj": OBJ_TYPES}
_FORMAT_NAMES = {".abc": "Alembic", ".obj": "OBJ files"}

# Maya evaluates `...` and expands $VAR in a file path.
EXPRESSION_CHARACTERS = "`$"
EXPRESSION_REASON = "Maya expands them in file paths"

STRING_KINDS = frozenset({"string", "stringArray"})
# Names of UV and colour sets, and of an Alembic face set, which an import may choose. Maya's own menus build MEL
# from them by concatenation, so they must be plain names: letters, digits, _ . - (Blender writes UVMap.001).
SET_NAMES = frozenset({"uvSet[*].uvSetName", "colorSet[*].colorName", "currentUVSet", "currentColorSet"})
SET_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}")
# Strings that are really a menu choice, and every value Maya's own menu sets (measured: polyOptions
# -colorMaterialChannel writes these six). GUI Maya creates meshes with 'Ambient+Diffuse', its polygon display
# preference, where mayapy gives the pinned 'Diffuse' (owner, Maya 2027 GUI), so the default can't be the rule.
DISPLAY_CHOICES = {
    ("mesh", "displayColorChannel"): frozenset(
        {"None", "Ambient", "Ambient+Diffuse", "Diffuse", "Specular", "Emission"}
    ),
}
FACE_SET_NAME = "AbcFacesetName"  # a string AbcImport adds to each face set's shading group (measured)
# A dynamic number no plugin defines: AbcImport makes its default the file's value (measured), so there is
# nothing to compare it with. One a renderer or Maya would read as heavy work is refused by its name.
_RENDERER_NAME = re.compile(r"^(ai|rs|rman|vray|vr|mtoa|arnold|redshift|prman|octane|cycles|mr|mi)(?=[A-Z0-9_])")
_HEAVY_NAME = re.compile(r"subdiv|level|sample|rays|depth|resolution|iteration|bounce|tess|triangles", re.IGNORECASE)
_INDEX = re.compile(r"\[\d+\]")
_ARNOLD_PLUGIN = "mtoa"
NO_STRING_DEFAULT = "is a string attribute Symbiote has no pinned default for"


class ArnoldTables(NamedTuple):
    """The mtoa defaults an audit holds an import to: the running pair's, only when its measured mtoa is loaded.

    ``loaded`` is the mtoa version Maya has loaded (``None``: not loaded; ``""``: loaded, but it gave no version).
    ``measured`` is the one Symbiote measured for the running (year, platform) pair (``None``: none measured there,
    or mtoa isn't loaded, so not looked up). ``strings`` and ``numbers`` are that pair's tables when the two agree,
    and empty otherwise. ``why`` says why a loaded mtoa has no table here, once (``""`` when there is nothing to say).
    """

    loaded: str | None
    measured: str | None
    strings: Mapping[tuple[str, str], Any]
    numbers: Mapping[tuple[str, str], Any]
    why: str = ""


def _unmeasured(loaded: str, measured: str | None, year: str, platform: str) -> str:
    name = f"mtoa {loaded}" if loaded else "mtoa (unknown version)"
    where = f"Maya {year} on {platform_name(platform)}"
    if measured is None:
        return f"{name} is loaded; Symbiote hasn't measured mtoa for {where}"
    return f"{name} is loaded; Symbiote measured mtoa {measured} for {where}"


def arnold_tables() -> ArnoldTables:
    """The running pair's mtoa tables when its measured mtoa is loaded; otherwise none (see ``ArnoldTables``).

    A version string that differs at all is another mtoa: its attributes and defaults were never measured, so no
    table is used, and its strings and numbers fall to the rules for an unpinned plug-in.
    """
    if not cmds.pluginInfo(_ARNOLD_PLUGIN, query=True, loaded=True):
        return ArnoldTables(None, None, {}, {})
    version = cmds.pluginInfo(_ARNOLD_PLUGIN, query=True, version=True)
    loaded = str(version) if version else ""
    year, platform = running_version(), running_platform()
    table = import_defaults.ARNOLD_TABLES.get((year, platform))
    measured = None if table is None else str(table["mtoa"])
    if table is None or not loaded or measured != loaded:
        return ArnoldTables(loaded, measured, {}, {}, _unmeasured(loaded, measured, year, platform))
    return ArnoldTables(loaded, measured, table["strings"], table["numbers"])


def audit(new_nodes: list[str], confirmed_path: str) -> list[str]:
    """Why the import of ``confirmed_path`` that made ``new_nodes`` must be refused; empty when it may stay.

    Each reason names the node and the attribute. Nodes are taken in name order, so the reasons are stable.
    """
    extension = os.path.splitext(confirmed_path)[1].lower()
    allowed = TYPES_BY_EXTENSION[extension]
    new = frozenset(new_nodes)
    arnold = arnold_tables()  # once per audit: the running pair's, for every node
    schema: dict[tuple[str, str], _Shape] = {}  # each type's attributes are read once (500 meshes took 41s)
    reasons: list[str] = []
    for node in sorted(new_nodes):
        node_type = cmds.nodeType(node)
        if node_type not in allowed:
            reasons.append(f"Symbiote doesn't import {node_type} nodes from {_FORMAT_NAMES[extension]} yet ({node})")
            continue
        reasons.extend(_attribute_reasons(node, node_type, confirmed_path, schema, arnold))
        reasons.extend(connection_reasons(node, new))
        if node_type == "mesh":
            reasons.extend(_set_membership_reasons(node))
            reasons.extend(_face_budget_reasons(node))
    if arnold.why and any(reason.endswith(NO_STRING_DEFAULT) for reason in reasons):
        # Said once, and first: the refusal shows only the first reasons, and this one explains the rest.
        return [f"{arnold.why}, so mtoa's attributes have no pinned defaults here", *reasons]
    return reasons


def _attribute_reasons(
    node: str, node_type: str, confirmed_path: str, schema: dict[tuple[str, str], _Shape], arnold: ArnoldTables
) -> Iterator[str]:
    user = frozenset(cmds.listAttr(node, userDefined=True) or [])
    for plug, leaf, kind in _attributes(node, node_type, user, schema):
        where = f"{node}.{plug}"
        if kind in STRING_KINDS and leaf in user:
            if node_type == "shadingEngine" and plug == FACE_SET_NAME:
                yield from _set_name_reasons(where, cmds.getAttr(where))
            else:
                yield f"{where} is a string attribute the file added"
        elif kind in STRING_KINDS:
            yield from _string_reasons(node, node_type, plug, leaf, confirmed_path, arnold)
        elif kind in NUMERIC_KINDS:
            yield from _number_reasons(node, node_type, plug, leaf, leaf in user, arnold)


class _Shape(NamedTuple):
    """What a type's attribute is: whether a file can set it, its kind, and whether any part is a multi."""

    writable: bool
    kind: str
    in_multi: bool


def _shape(node: str, name: str, leaf: str) -> _Shape:
    writable = bool(cmds.attributeQuery(leaf, node=node, writable=True))
    if not writable:
        return _Shape(False, "", False)
    kind = cmds.attributeQuery(leaf, node=node, attributeType=True)
    in_multi = kind in NUMERIC_KINDS and any(cmds.attributeQuery(p, node=node, multi=True) for p in name.split("."))
    return _Shape(True, kind, in_multi)


def _attributes(
    node: str, node_type: str, user: frozenset[str], schema: dict[tuple[str, str], _Shape]
) -> Iterator[tuple[str, str, str]]:
    """(plug, leaf name, kind) of each writable string or numeric attribute; a multi's elements each.

    Outputs aren't writable: Maya computes them, so a file can't set one. Numbers inside a multi (vertices,
    colours) are not read. A static attribute's shape is the same on every node of a type, so it is read once;
    a dynamic one is read on its own node.
    """
    for name in cmds.listAttr(node) or []:
        leaf = name.rsplit(".", 1)[-1]
        shape = schema.get((node_type, name)) if leaf not in user else None
        if shape is None:
            shape = _shape(node, name, leaf)
            if leaf not in user:
                schema[(node_type, name)] = shape
        if not shape.writable:
            continue
        if shape.kind == "typed":
            for plug in _elements(node, name.split(".")):
                yield plug, leaf, cmds.getAttr(f"{node}.{plug}", type=True)
        elif shape.kind in NUMERIC_KINDS and not shape.in_multi:
            yield name, leaf, shape.kind


def _elements(node: str, parts: list[str]) -> list[str]:
    """The plugs ``parts`` names, with every existing index of each multi part filled in."""
    plugs = [""]
    for part in parts:
        named = [f"{plug}.{part}" if plug else part for plug in plugs]
        if not cmds.attributeQuery(part, node=node, multi=True):
            plugs = named
            continue
        plugs = [
            f"{plug}[{index}]" for plug in named for index in cmds.getAttr(f"{node}.{plug}", multiIndices=True) or []
        ]
    return plugs


def _string_reasons(
    node: str, node_type: str, plug: str, leaf: str, confirmed_path: str, arnold: ArnoldTables
) -> Iterator[str]:
    where = f"{node}.{plug}"
    key = _INDEX.sub("[*]", plug)
    value = cmds.getAttr(where)
    if cmds.attributeQuery(leaf, node=node, usedAsFilename=True):
        yield from _path_reasons(where, value)
    if node_type == "AlembicNode" and key in ("abc_File", "abc_layerFiles"):
        expected: Any = confirmed_path if key == "abc_File" else [confirmed_path]
        if _plain(value) != expected:
            yield f"{where} names a file other than the one you confirmed"
        return
    if key in SET_NAMES:
        yield from _set_name_reasons(where, value)
        return
    if (node_type, key) in DISPLAY_CHOICES:
        if value not in DISPLAY_CHOICES[(node_type, key)]:
            yield f"{where} is not one of the values Maya offers for it"
        return
    default = _string_default(node_type, key, arnold)
    if default is _MISSING:
        yield f"{where} {NO_STRING_DEFAULT}"
    elif _plain(value) != _plain(default):
        denied = maya_denylist.denial(leaf)
        yield f"{where} was set by the file" + (f", and {denied}" if denied else "")


_MISSING = object()


def _string_default(node_type: str, key: str, arnold: ArnoldTables) -> Any:
    for table in (import_defaults.STRING_DEFAULTS, arnold.strings):
        if (node_type, key) in table:
            return table[(node_type, key)]
    return _MISSING


def _plain(value: Any) -> Any:
    """A string or string array as Maya's getAttr gives it; no text, ``None`` and no elements are all equal."""
    if value is None or value == "" or (isinstance(value, (list, tuple)) and not value):
        return ""
    return list(value) if isinstance(value, (list, tuple)) else value


def _set_name_reasons(where: str, value: Any) -> Iterator[str]:
    if value not in (None, "") and SET_NAME.fullmatch(str(value)) is None:
        yield f"{where} is not a plain set name (letters, digits, _ . - only)"


def _set_membership_reasons(node: str) -> Iterator[str]:
    """The current UV and colour sets must be sets the mesh has."""
    for current, names in (("currentUVSet", "uvSet[*].uvSetName"), ("currentColorSet", "colorSet[*].colorName")):
        value = cmds.getAttr(f"{node}.{current}")
        multi, leaf = names.split("[*].")
        indices = cmds.getAttr(f"{node}.{multi}", multiIndices=True) or []
        if value and value not in [cmds.getAttr(f"{node}.{multi}[{i}].{leaf}") for i in indices]:
            yield f"{node}.{current} names a set the mesh doesn't have"


def _path_reasons(where: str, value: Any) -> Iterator[str]:
    for text in value if isinstance(value, (list, tuple)) else [value]:
        if not text:
            continue
        try:
            paths.check_local_path(
                text,
                windows=os.name == "nt",
                display_name="Maya",
                expression_chars=EXPRESSION_CHARACTERS,
                expression_reason=EXPRESSION_REASON,
            )
        except ValueError:
            yield f"{where} names a path Symbiote won't let Maya open"
            continue
        if paths.is_network_reference(text, windows=os.name == "nt"):
            yield f"{where} names a file on another machine"


def _number_reasons(
    node: str, node_type: str, plug: str, leaf: str, dynamic: bool, arnold: ArnoldTables
) -> Iterator[str]:
    where = f"{node}.{plug}"
    value = cmds.getAttr(where)
    cap = maya_denylist.VALUE_CAPS.get(leaf)
    if cap is not None and value > cap:
        yield f"{where} is {value}, over the limit of {cap}"
    if cap is not None and value < maya_denylist.CAP_FLOOR:
        yield f"{where} is {value}, under the limit of {maya_denylist.CAP_FLOOR}"
    if maya_denylist.denial(leaf) or leaf in maya_denylist.HEAVY_ATTRIBUTES:
        default = import_defaults.NUMBER_DEFAULTS.get((node_type, leaf), _MISSING)
        if default is _MISSING:
            yield f"{where} is an attribute Symbiote has no pinned default for"
        elif not _same_number(value, default):
            yield f"{where} is {value}, not its default {default}"
    elif leaf not in import_defaults.MAYA_ATTRIBUTES.get(node_type, frozenset()):
        yield from _plugin_number_reasons(node, node_type, where, leaf, value, dynamic, arnold)


def _plugin_number_reasons(
    node: str, node_type: str, where: str, leaf: str, value: Any, dynamic: bool, arnold: ArnoldTables
) -> Iterator[str]:
    """A number Maya itself doesn't define: a plugin's (pinned for the pair's measured mtoa), or one the file added.

    A plugin's own attribute keeps the default the plugin gave it (``listDefault``, for an mtoa with no table here).
    A dynamic one has no default to compare with (AbcImport sets it to the file's value), so it passes only when
    its name isn't a renderer's or heavy.
    """
    default = arnold.numbers.get((node_type, leaf), _MISSING)
    if default is _MISSING and not dynamic:
        default = (cmds.attributeQuery(leaf, node=node, listDefault=True) or [_MISSING])[0]
    if default is not _MISSING:
        if not _same_number(value, default):
            yield f"{where} is {value}, a plugin's attribute away from its default {default}"
    elif _RENDERER_NAME.match(leaf) or _HEAVY_NAME.search(leaf) or leaf in maya_denylist.VALUE_CAPS:
        yield f"{where} is a renderer's or heavy setting the file added ({value}), with no default to hold it to"


def _same_number(value: Any, default: Any) -> bool:
    return math.isclose(float(value), float(default), rel_tol=1e-6, abs_tol=1e-9)


def _face_budget_reasons(node: str) -> Iterator[str]:
    if not cmds.getAttr(f"{node}.displaySmoothMesh"):
        return
    faces = cmds.polyEvaluate(node, face=True)
    if not isinstance(faces, int):
        yield f"{node}: Maya couldn't count its faces ({faces})"
        return
    smoothed = faces * 4 ** int(cmds.getAttr(f"{node}.smoothLevel"))
    if smoothed > maya_denylist.SMOOTHED_FACE_BUDGET:
        yield f"{node} would draw {smoothed} smoothed faces, over {maya_denylist.SMOOTHED_FACE_BUDGET}"
