"""What Maya's write tools share: one undo chunk per call, the checks every call starts with, and setting values.

Every tool runs its work on Maya's main thread through ``MAIN.checked`` (tier and open dialogs re-checked there),
checks every part of the call before changing anything, and changes the scene inside one named undo chunk, so
one Ctrl-Z takes the whole call back. A call that fails part way is undone (``symbiote_dcc.undo``).

No string the model sends reaches Maya as code: node names pass ``names.resolve_node`` or ``names.new_name``,
attributes are census long names (``maya_write_limits``), and values are numbers, booleans or enum fields Maya's
own index replaces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

import maya.cmds as cmds
from symbiote_dcc import undo
from symbiote_dcc.tool_support import REFUSED_CREATE_NEXT, ToolError, checked_values, refusal_text

from symbiote_maya import maya_write_limits as limits
from symbiote_maya.names import resolve_node
from symbiote_maya.scene_writes import WRITES

MAX_ATTRIBUTES = 50
_VECTOR_KINDS = frozenset({"double3", "float3"})
_Result = TypeVar("_Result")


class _MayaUndo:
    """Maya's undo queue as the core's ``undo.UndoApi``: a named chunk, the top entry's name, and Ctrl-Z.

    Measured in mayapy 2027: ``undoInfo(query=True, undoName=True)`` names the chunk once it has changed something,
    and a chunk that changed nothing leaves no entry, so a failure before any change never undoes the artist's.
    """

    @contextmanager
    def group(self, label: str) -> Iterator[None]:
        cmds.undoInfo(openChunk=True, chunkName=label)
        try:
            yield
        finally:
            cmds.undoInfo(closeChunk=True)

    def top_label(self) -> str:
        return str(cmds.undoInfo(query=True, undoName=True) or "")

    def undo_last(self) -> None:
        cmds.undo()


def undoable(label: str, work: Callable[[], _Result]) -> _Result:
    """Run ``work`` as one undo entry, counted as a scene write; a Maya error part way is undone and reported."""
    WRITES.bump()  # counted first: a change that fails part way can only make a save's check stricter
    try:
        return undo.undoable(label, work, _MayaUndo())
    except RuntimeError as error:  # Maya refused a command; undoable has taken back what the call changed
        raise ToolError(f"Maya refused part of the change, so Symbiote undid all of it: {error}") from error


def refused(what: str, reasons: list[str]) -> ToolError:
    return ToolError(refusal_text(what, reasons))


def create_refused(what: str, reasons: list[str]) -> ToolError:
    return ToolError(refusal_text(what, reasons) + REFUSED_CREATE_NEXT)


def ready_refusal() -> str | None:
    """Why no write may run in this Maya at all: a version with no census, or undo switched off."""
    return limits.version_refusal() or limits.undo_refusal()


def node_type(node: str) -> str:
    return str(cmds.nodeType(node))


def checked_attributes(attributes: object) -> dict[str, Any]:
    """The call's ``{attribute: value}``: an object of at most 50 entries named by strings."""
    return checked_values(attributes, "attribute", MAX_ATTRIBUTES)


def checked_names(names: object, noun: str, most: int) -> list[str]:
    """A list of 1 to ``most`` node names, each resolved to the one node it names (no patterns)."""
    if not isinstance(names, list) or not names:
        raise ToolError(f"{noun} must be a list of at least one node name.")
    if len(names) > most:
        raise ToolError(f"Give at most {most} {noun} in one call.")
    return list(dict.fromkeys(resolve_node(name) for name in names))


def edit_refusals(nodes: list[str]) -> list[str]:
    return [reason for reason in (limits.edit_refusal(node) for node in nodes) if reason is not None]


def reshaped_meshes(node: str, kind: str) -> list[str]:
    """The meshes a change to ``node`` also reshapes: every mesh downstream of a primitive creator, whatever value
    changes (measured: a locked mesh fed by an unlocked polyCube changes shape). They are held to ``edit_refusal``
    with the node, so a creator never changes a mesh the write tools may not."""
    return limits.output_meshes(node) if kind in limits.PRIMITIVE_TYPES else []


def exact_transform_refusal(node: str) -> str | None:
    """A joint inherits from transform; the write tools take only the exact type (``edit_refusal`` refuses it)."""
    kind = node_type(node)
    return None if kind == "transform" else f"{node} is a {kind}, and it isn't a transform"


def value_refusals(node: str, kind: str, values: Mapping[str, Any]) -> list[str]:
    """Every reason a value may not be set on ``node``: its census check, then the face budgets it could break."""
    reasons = [r for r in (limits.attribute_refusal(kind, attr, value) for attr, value in values.items()) if r]
    if reasons:
        return reasons  # the face checks need values of the right kind
    if kind in limits.PRIMITIVE_TYPES:
        reasons.append(limits.subdivisions_refusal(node, values) or "")
    reasons.append(limits.draw_change_refusal(node, kind, values) or "")  # smoothing and ghosting, together
    return [reason for reason in reasons if reason]


def maya_value(kind: str, attr: str, value: Any) -> Any:
    """The value as setAttr takes it: an enum field's name becomes Maya's index (never its position in the list)."""
    spec = limits.settable_attributes(kind)[attr]
    if spec["kind"] == "enum":
        return limits.enum_index(spec, value)
    return value


def set_values(node: str, kind: str, values: Mapping[str, Any]) -> None:
    """Set checked census values on ``node``: a vector as its three numbers, an enum by Maya's index."""
    for attr, value in values.items():
        given = maya_value(kind, attr, value)
        if limits.settable_attributes(kind)[attr]["kind"] in _VECTOR_KINDS:
            cmds.setAttr(f"{node}.{attr}", *given)
        else:
            cmds.setAttr(f"{node}.{attr}", given)
