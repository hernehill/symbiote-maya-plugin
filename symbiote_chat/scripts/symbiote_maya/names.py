"""Node names from the model: checked strictly before any Maya command sees one.

A name the model sends is never a pattern: ``ls`` treats ``*`` and ``?`` as wildcards and ``.`` starts an
attribute, so only plain node names (with an optional namespace and path) get through, and only when they
name exactly one node.
"""

from __future__ import annotations

import re
from typing import Any

import maya.cmds as cmds
from symbiote_dcc.tool_support import ToolError

_MAX_NAME = 512
_MAX_SHOWN = 10
_PART = r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)*"
_NODE_NAME = re.compile(rf"^\|?{_PART}(?:\|{_PART})*$")
_NEW_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _well_formed(name: object) -> str:
    if not isinstance(name, str):
        raise ToolError("A node name must be text.")
    if len(name) > _MAX_NAME:
        raise ToolError(f"A node name is at most {_MAX_NAME} characters.")
    if _NODE_NAME.fullmatch(name) is None:
        raise ToolError(
            "That is not a plain node name. Use letters, digits and underscores, with ':' for a namespace and "
            "'|' for a path: no wildcards, no attributes (no '.'), no spaces."
        )
    return name


def resolve_node(name: object) -> str:
    """The full path of the one node ``name`` names; a name that is malformed, absent or ambiguous is refused."""
    text = _well_formed(name)
    matches: list[Any] = cmds.ls(text, long=True) or []
    if not matches:
        raise ToolError(f"There is no node named {text} in this Maya scene.")
    # Unreachable while _NODE_NAME has no '.' (ls can't then return a plug); kept as a guard if the pattern widens.
    if len(matches) == 1 and "." in matches[0]:
        raise ToolError(f"{text} resolved to an attribute ({matches[0]}), not a node. Give a node name.")
    if len(matches) > 1:
        shown = ", ".join(str(m) for m in sorted(matches)[:_MAX_SHOWN])
        more = f" (and {len(matches) - _MAX_SHOWN} more)" if len(matches) > _MAX_SHOWN else ""
        raise ToolError(f"{text} names more than one node: {shown}{more}. Ask again with the full path.")
    return str(matches[0])


def new_name(name: object) -> str:
    """A name for a node that doesn't exist yet: ``""`` (let Maya choose) or a plain name, with no namespace or path."""
    if name == "":
        return ""
    if not isinstance(name, str) or _NEW_NAME.fullmatch(name) is None:
        raise ToolError(
            "A new node name is 1 to 64 letters, digits and underscores, starting with a letter or underscore: "
            "no namespace (':') and no path ('|')."
        )
    return name
