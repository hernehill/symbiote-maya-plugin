"""Check a tool call's arguments against the tool's declared top-level ``inputSchema``.

A model that guesses a parameter name would otherwise get a raw ``TypeError`` from the handler
and give up. The refusal names the tool and lists what it takes, so the model can retry.
Stdlib only. Only a string ``type`` is checked: a union such as ``["string", "null"]`` is
not, and nested schemas (``items``, nested ``properties``) are not either.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# A JSON type name to the Python check for it. bool is an int in Python and not a JSON number.
_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


_MAX_ECHOED_KEYS = 10
_MAX_KEY_CHARS = 64


def _echo_keys(keys: list[str]) -> str:
    """The keys the model sent, bounded: a model can send a huge or very long list of them."""
    shown = [key if len(key) <= _MAX_KEY_CHARS else f"{key[:_MAX_KEY_CHARS]}..." for key in keys[:_MAX_ECHOED_KEYS]]
    more = len(keys) - _MAX_ECHOED_KEYS
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _article(type_name: str) -> str:
    return "an" if type_name[0] in "aeiou" else "a"


def _signature(name: str, properties: Mapping[str, Any], required: list[str]) -> str:
    if not properties:
        return f"{name} takes no arguments."
    parts = []
    for key, spec in properties.items():
        type_name = spec.get("type") if isinstance(spec, Mapping) else None
        label = type_name if isinstance(type_name, str) else "any"
        parts.append(f"{key} ({label}{', required' if key in required else ''})")
    return f"{name} takes: {', '.join(parts)}."


def _type_problems(properties: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    problems = []
    for key, value in arguments.items():
        spec = properties.get(key)
        type_name = spec.get("type") if isinstance(spec, Mapping) else None
        if not isinstance(type_name, str):
            continue
        check = _TYPE_CHECKS.get(type_name)
        if check is not None and not check(value):
            problems.append(f"{key} must be {_article(type_name)} {type_name}")
    return problems


def refusal(name: str, schema: Mapping[str, Any], arguments: Any) -> str | None:
    """Why these arguments are refused (``None`` when they pass), phrased for the model."""
    properties = schema.get("properties") or {}
    required = [key for key in schema.get("required") or [] if isinstance(key, str)]
    signature = _signature(name, properties, required)
    if not isinstance(arguments, dict):
        return f"{signature} The arguments must be an object."
    problems = []
    if schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            problems.append(f"Not accepted: {_echo_keys(unknown)}.")
    missing = [key for key in required if key not in arguments]
    if missing:
        problems.append(f"Missing: {', '.join(missing)}.")
    type_problems = _type_problems(properties, arguments)
    if type_problems:
        problems.append(f"{'; '.join(type_problems)}.")
    return f"{signature} {' '.join(problems)}" if problems else None
