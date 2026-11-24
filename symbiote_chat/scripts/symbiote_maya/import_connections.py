"""The import audit's connection rules: which plugs an import may wire, into its own nodes and out of them.

A connection carries a value the audit's one-frame read can't see (an Alembic property animates smoothLevel from
0 at frame 1 to 9 at frame 50, measured), so only the shapes measured in Maya 2025 and 2027 are allowed. Shapes are
written "source type.attr -> target type.attr" with every index dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import maya.cmds as cmds

# attributeQuery's numeric kinds (measured on the import types; Maya has no int64 attribute type).
NUMERIC_KINDS = frozenset(
    {
        "bool",
        "byte",
        "char",
        "short",
        "long",
        "enum",
        "float",
        "double",
        "doubleLinear",
        "doubleAngle",
        "floatLinear",
        "floatAngle",
        "time",
    }
)
_NUMERIC_COMPOUNDS = frozenset({"double2", "double3", "float2", "float3", "long2", "long3", "short2", "short3"})

# Into a non-numeric attribute of a new node: Alembic's time, mesh, transform and camera outputs, OBJ's and
# Alembic face sets' shading wiring. A number may be driven by anything except an Alembic property.
MEASURED_CONNECTIONS = frozenset(
    {
        "time.outTime -> AlembicNode.time",
        "AlembicNode.outPolyMesh -> mesh.inMesh",
        "AlembicNode.transOp -> unitConversion.input",
        "AlembicNode.outCamera -> unitConversion.input",
        "lambert.outColor -> shadingEngine.surfaceShader",
        "phong.outColor -> shadingEngine.surfaceShader",
        "openPBRSurface.outColor -> shadingEngine.surfaceShader",
        "shadingEngine.message -> materialInfo.shadingGroup",
        # GUI Maya links an imported OBJ material to its own materialInfo; mayapy doesn't (owner, Maya 2027 GUI).
        # A message carries no value or path, and both ends are the import's: a material type it may bring.
        "lambert.message -> materialInfo.material",
        "phong.message -> materialInfo.material",
        "groupId.message -> shadingEngine.groupNodes",
        "mesh.instObjGroups.objectGroups -> shadingEngine.dagSetMembers",
    }
)
# From a new node into a node that was already in the scene: a new mesh joins a shading group, a new shading group
# registers with the light linker and the render partition, a new material with the shader list (measured).
MEASURED_OUTGOING = frozenset(
    {
        "mesh.instObjGroups -> shadingEngine.dagSetMembers",  # into a default shading group only
        "mesh.instObjGroups.objectGroups -> shadingEngine.dagSetMembers",
        "shadingEngine.message -> lightLinker.link.object",
        "shadingEngine.message -> lightLinker.shadowLink.shadowObject",
        "shadingEngine.partition -> partition.sets",
        "groupId.message -> shadingEngine.groupNodes",  # a face set kept on Maya's default group (faceset_default)
        "lambert.message -> defaultShaderList.shaders",
        "phong.message -> defaultShaderList.shaders",
    }
)
# The only nodes already in the scene that may drive an imported one, each one of Maya's own read-only default
# nodes (ls(defaultNodes=True), never a name: measured, a studio can't rename them and an artist can't make one):
# Maya's time drives the AlembicNode, and a default material colours an Alembic face set's shading group and, in
# GUI Maya, feeds its materialInfo (owner, Maya 2027 GUI). AbcImport uses the initial surface (openPBR_shader1 in
# 2027, standardSurface1 in 2025) even when another shader feeds initialShadingGroup (measured), so the default
# material is found by query, not through that group.
# Shapes with "*" match any source type; the source must also be a default material.
SCENE_TIME = "time.outTime -> AlembicNode.time"
# A mesh whose faces stay on Maya's default shader takes that group's wireframe colour (measured, faceset_default).
DEFAULT_GROUP_COLOUR = "shadingEngine.memberWireframeColor -> mesh.instObjGroups.objectGroups.objectGrpColor"
DEFAULT_MATERIAL_SHAPES = frozenset({"*.outColor -> shadingEngine.surfaceShader", "*.message -> materialInfo.material"})
_INDEX = re.compile(r"\[\d+\]")


def connection_reasons(node: str, new_nodes: frozenset[str]) -> Iterator[str]:
    """Why ``node``'s connections refuse the import: what drives it, and what it drives outside the import."""
    yield from _incoming(node, new_nodes)
    yield from _outgoing(node, new_nodes)


def _incoming(node: str, new_nodes: frozenset[str]) -> Iterator[str]:
    found = cmds.listConnections(node, source=True, destination=False, plugs=True, connections=True) or []
    for target, source in zip(found[0::2], found[1::2]):
        source_node, source_attr = source.split(".", 1)
        target_attr = target.split(".", 1)[1]
        source_type = cmds.nodeType(source_node)
        if source_type == "AlembicNode" and _INDEX.sub("", source_attr) == "prop":
            yield f"{target} is driven by {source}, an Alembic property that can change it on any frame"
            continue
        shape = _shape(source_type, source_attr, cmds.nodeType(node), target_attr)
        if not _is_new(source_node, new_nodes):
            if not _is_scene_source(source_node, shape):
                yield f"{target} is driven by {source}, a node that was already in your scene"
            continue
        if _is_numeric(node, target_attr):
            continue
        if shape not in MEASURED_CONNECTIONS:
            yield f"{target} is connected from {source}, a connection this import may not make"


def _outgoing(node: str, new_nodes: frozenset[str]) -> Iterator[str]:
    found = cmds.listConnections(node, source=False, destination=True, plugs=True, connections=True) or []
    for source, target in zip(found[0::2], found[1::2]):
        target_node, target_attr = target.split(".", 1)
        if _is_new(target_node, new_nodes):
            continue  # wiring inside the import: its target's incoming check reads it
        target_type = cmds.nodeType(target_node)
        shape = _shape(cmds.nodeType(node), source.split(".", 1)[1], target_type, target_attr)
        # A shading group, partition or shader list it joins must be Maya's own; the light linker isn't a default
        # node (measured), so its type decides.
        if shape not in MEASURED_OUTGOING or (target_type != "lightLinker" and not _is_default(target_node)):
            yield f"{source} drives {target}, a node that was already in your scene"


def _is_scene_source(source_node: str, shape: str) -> bool:
    if not _is_default(source_node):
        return False
    if shape in (SCENE_TIME, DEFAULT_GROUP_COLOUR):
        return True
    general = "*." + shape.split(".", 1)[1]
    return general in DEFAULT_MATERIAL_SHAPES and bool(cmds.ls(source_node, materials=True))


def _is_default(name: str) -> bool:
    """One of Maya's own read-only nodes (time1, initialShadingGroup, the default materials...)."""
    return bool(cmds.ls(name, defaultNodes=True))


def _is_new(name: str, new_nodes: frozenset[str]) -> bool:
    return str((cmds.ls(name, long=True) or [name])[0]) in new_nodes


def _shape(source_type: str, source_attr: str, target_type: str, target_attr: str) -> str:
    return f"{source_type}.{_INDEX.sub('', source_attr)} -> {target_type}.{_INDEX.sub('', target_attr)}"


def _is_numeric(node: str, attribute: str) -> bool:
    leaf = _INDEX.sub("", attribute).rsplit(".", 1)[-1]
    kind = cmds.attributeQuery(leaf, node=node, attributeType=True)
    return kind in NUMERIC_KINDS or kind in _NUMERIC_COMPOUNDS
