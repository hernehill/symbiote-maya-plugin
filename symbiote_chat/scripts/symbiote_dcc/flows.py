"""The two flows that change the scene only once the artist confirms: an import and a change.

Each holds the confirmation lock for its whole life and runs on the DCC's main thread. The order
of their steps is the safety argument, so it lives here once and every DCC fills in the steps.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, NamedTuple, TypeVar

from symbiote_dcc import confirm, paths
from symbiote_dcc.adapter import DialogHost
from symbiote_dcc.tool_support import MainThread, ToolError, one_dialog_at_a_time, refuse_while_a_dialog_is_open

_Loader = TypeVar("_Loader")
_Subject = TypeVar("_Subject")

CANCELLED_IMPORT = "The artist cancelled the import."
CHANGED_WHILE_OPEN = "{what} changed while the dialog was open, so nothing was {verb_past}; ask again."
TIMED_OUT_REASON = "The confirmation closed itself after {seconds}s with no answer, so nothing was {verb_past}."

Recheck = Callable[[], "str | None"]


class ImportDialog(NamedTuple):
    """The import confirmation: ``confirm.ask``'s title, text, details and accept label."""

    title: str
    text: str
    details: str
    accept_label: str


@dataclass(frozen=True)
class _ImportSteps(Generic[_Loader]):
    host: DialogHost
    main: MainThread
    recheck: Recheck
    windows: bool
    expression_chars: str
    expression_reason: str
    pick_loader: Callable[[str], _Loader]
    describe: Callable[[str], ImportDialog]
    pre_import: Callable[[str], None]
    do_import: Callable[[str, _Loader], dict[str, Any]]

    def check_local(self, path: str) -> None:
        try:
            paths.check_local_path(
                path,
                windows=self.windows,
                display_name=self.host.display_name,
                expression_chars=self.expression_chars,
                expression_reason=self.expression_reason,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


def confirmed_import(
    host: DialogHost,
    main: MainThread,
    path: str,
    recheck: Recheck,
    *,
    windows: bool,
    expression_chars: str,
    expression_reason: str,
    pick_loader: Callable[[str], _Loader],
    describe: Callable[[str], ImportDialog],
    pre_import: Callable[[str], None],
    do_import: Callable[[str, _Loader], dict[str, Any]],
) -> dict[str, Any]:
    """Import ``path`` once the artist confirms; nothing on disk is touched before they do.

    Call it on the tool server's thread, inside the tool call, with the ``recheck`` taken there.
    On the main thread, in this order: the tier; the path's text (``paths.check_local_path``);
    ``pick_loader(path)``, by extension only; the dialog (``describe(path)`` gives its title,
    text, details and accept label as an ``ImportDialog``; Enter imports); the tier again; the
    path, followed through every link component by component (``paths.resolve_checked``),
    stays local and the file it reaches exists; ``pre_import(path)``; ``do_import(path,
    loader)``, whose result is returned.

    ``do_import`` gets the path the artist confirmed, not the resolved one. A local process
    that swaps a link between the check and the load can still redirect the load; that is a
    known limit, and it needs someone already able to write the artist's folders.
    """
    steps = _ImportSteps(
        host, main, recheck, windows, expression_chars, expression_reason, pick_loader, describe, pre_import, do_import
    )
    refuse_while_a_dialog_is_open(host)  # a capture dialog is not ours to stack on
    with one_dialog_at_a_time(host):
        return main.run(_import, steps, path)


def _import(steps: _ImportSteps[_Loader], path: str) -> dict[str, Any]:
    steps.main.refuse_if_switched_off(steps.recheck)
    steps.check_local(path)
    loader = steps.pick_loader(path)
    answer = confirm.ask(steps.host, *steps.describe(path), default_accept=True)
    if answer is confirm.Answer.TIMED_OUT:
        return timed_out_result({"imported": False}, "imported")
    if answer is not confirm.Answer.ACCEPTED:
        return {"imported": False, "reason": CANCELLED_IMPORT}
    steps.main.refuse_if_switched_off(steps.recheck)
    _check_confirmed_file(steps, path)
    steps.pre_import(path)
    return steps.do_import(path, loader)


def _check_confirmed_file(steps: _ImportSteps[Any], path: str) -> None:
    """The path, followed through every link (its own and its folders'), stays local; the file is there.

    ``paths.resolve_checked`` checks each prefix and link target as text before it looks at
    it, so a link onto a share is refused before anything contacts it. Only the resolved path
    is stat-ed.
    """
    try:
        resolved = paths.resolve_checked(
            path,
            windows=steps.windows,
            display_name=steps.host.display_name,
            expression_chars=steps.expression_chars,
            expression_reason=steps.expression_reason,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not os.path.isfile(resolved):
        raise ToolError(f"No file at {path} on this workstation")


@dataclass(frozen=True)
class ChangeDialog:
    """What the confirmation shows, and what the tool returns if the artist cancels.

    ``what`` names the thing being changed and ``verb_past`` what the tool does to it
    ("deleted", "saved"), for the refusal when it changed while the dialog was open.
    """

    title: str
    text: str
    details: str
    accept_label: str
    what: str
    verb_past: str
    cancelled: Mapping[str, Any]


def confirmed_change(
    host: DialogHost,
    main: MainThread,
    recheck: Recheck,
    *,
    check: Callable[[], _Subject],
    describe: Callable[[_Subject], ChangeDialog],
    revalidate: Callable[[_Subject], bool],
    perform: Callable[[_Subject], dict[str, Any]],
) -> dict[str, Any]:
    """Change the scene once the artist confirms a dialog that defaults to Cancel.

    Call it on the tool server's thread, inside the tool call, with the ``recheck`` taken there.
    On the main thread, after the tier and open-dialog checks, in this order: ``check()``
    refuses or returns the subject; the dialog; the tier again; ``revalidate(subject)`` says
    whether the subject is still what the artist saw; ``perform(subject)``, whose result is
    returned.
    """
    refuse_while_a_dialog_is_open(host)  # a capture dialog is not ours to stack on
    with one_dialog_at_a_time(host):
        return main.checked(_change, host, main, recheck, check, describe, revalidate, perform, owns_dialog=True)


def _change(
    host: DialogHost,
    main: MainThread,
    recheck: Recheck,
    check: Callable[[], _Subject],
    describe: Callable[[_Subject], ChangeDialog],
    revalidate: Callable[[_Subject], bool],
    perform: Callable[[_Subject], dict[str, Any]],
) -> dict[str, Any]:
    subject = check()
    dialog = describe(subject)
    answer = confirm.ask(host, dialog.title, dialog.text, dialog.details, dialog.accept_label, default_accept=False)
    if answer is confirm.Answer.TIMED_OUT:
        return timed_out_result(dialog.cancelled, dialog.verb_past)
    if answer is not confirm.Answer.ACCEPTED:
        return dict(dialog.cancelled)
    main.refuse_if_switched_off(recheck)
    if not revalidate(subject):
        raise ToolError(CHANGED_WHILE_OPEN.format(what=dialog.what, verb_past=dialog.verb_past))
    return perform(subject)


def timed_out_result(cancelled: Mapping[str, Any], verb_past: str) -> dict[str, Any]:
    """The cancelled result, saying the dialog closed itself unanswered; ``cancelled`` is not changed."""
    reason = TIMED_OUT_REASON.format(seconds=confirm.CONFIRM_SECONDS, verb_past=verb_past)
    return {**cancelled, "reason": reason, "timed_out": True}
