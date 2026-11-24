"""The confirmation dialog every DCC's import and delete tools show before they change the scene.

A ``QDialog``, not a ``QMessageBox``: a message box hides ``setDetailedText`` behind a click,
and the details hold the path or node the artist is agreeing to. Qt is imported only when a
dialog is shown, so the module imports without PySide6.

The dialog counts down and closes itself after ``CONFIRM_SECONDS``: the CLI runs one command
at a time, so a dialog nobody answers must not hold that slot for long. The countdown follows
``consent.ask_artist``'s, kept separate so each dialog can change on its own.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from symbiote_dcc.adapter import DialogHost
from symbiote_dcc.tool_switches import DIALOGS

CANCEL_LABEL = "Cancel"
CONFIRM_SECONDS = 40
_TICK_MSEC = 250
_clock: Callable[[], float] = time.monotonic  # the countdown's clock; tests move it on


class _Parts(NamedTuple):
    dialog: Any
    countdown: Any
    buttons: Any


class Answer(enum.Enum):
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    def __bool__(self) -> bool:
        # Every member would be truthy, so ``if not confirm.ask(...)`` would carry on after Cancel.
        raise TypeError("compare an Answer by identity, e.g. `answer is Answer.ACCEPTED`")


def ask(host: DialogHost, title: str, text: str, details: str, accept_label: str, *, default_accept: bool) -> Answer:
    """Show the modal dialog and return the artist's answer, or ``TIMED_OUT`` after ``CONFIRM_SECONDS``.

    Call it on the GUI thread. ``text`` and ``details`` come from the scene or the agent, so
    both are plain text; ``details`` sits in a read-only box that is visible without a click.
    Escape and closing the window mean Cancel. ``default_accept`` says which button Enter
    presses. The first outcome wins: a click after the dialog closed itself changes nothing.
    The dialog counts in ``DIALOGS`` while it is open, so a microagent restart waits.
    """
    parts = _build(host, title, text, details, accept_label, default_accept)
    timer, answer = _count_down(parts)
    try:
        with DIALOGS.showing():  # a microagent restart waits until the artist answers
            parts.dialog.exec()  # Esc or the window's close button leaves no answer: cancelled
    finally:
        timer.stop()
        parts.dialog.deleteLater()
    given = answer()
    return Answer.CANCELLED if given is None else given


def _count_down(parts: _Parts) -> tuple[Any, Callable[[], Answer | None]]:
    """Start the countdown and connect the buttons; returns the timer and a reader for the answer."""
    from PySide6 import QtCore

    ends_at = _clock() + CONFIRM_SECONDS
    state: dict[str, Answer | None] = {"answer": None}
    timer = QtCore.QTimer(parts.dialog)

    def finish(answer: Answer) -> None:
        if state["answer"] is not None:
            return
        state["answer"] = answer
        timer.stop()
        parts.dialog.accept()

    def tick() -> None:
        left = int(ends_at - _clock() + 0.999)
        if left <= 0:
            finish(Answer.TIMED_OUT)
            return
        parts.countdown.setText(f"Closes in {left}s")

    timer.timeout.connect(tick)
    parts.buttons.accepted.connect(lambda: finish(Answer.ACCEPTED))
    parts.buttons.rejected.connect(lambda: finish(Answer.CANCELLED))
    tick()
    timer.start(_TICK_MSEC)
    return timer, lambda: state["answer"]


def _build(host: DialogHost, title: str, text: str, details: str, accept_label: str, default_accept: bool) -> _Parts:
    """The dialog's widgets: message, details, then the countdown beside the buttons."""
    from PySide6 import QtCore, QtWidgets

    dialog = QtWidgets.QDialog(host.main_window())
    dialog.setWindowTitle(title)
    dialog.setStyleSheet(host.stylesheet())
    message = QtWidgets.QLabel(text)
    message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    message.setWordWrap(True)
    detail_box = QtWidgets.QPlainTextEdit(details)
    detail_box.setReadOnly(True)
    countdown = QtWidgets.QLabel()
    countdown.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    buttons = QtWidgets.QDialogButtonBox()
    roles = QtWidgets.QDialogButtonBox.ButtonRole
    accept = buttons.addButton(accept_label, roles.AcceptRole)
    cancel = buttons.addButton(CANCEL_LABEL, roles.RejectRole)
    for button, is_default in ((accept, default_accept), (cancel, not default_accept)):
        button.setAutoDefault(is_default)
        button.setDefault(is_default)
    (accept if default_accept else cancel).setFocus()
    footer = QtWidgets.QHBoxLayout()
    footer.addWidget(countdown, 1)
    footer.addWidget(buttons)
    layout = QtWidgets.QVBoxLayout(dialog)
    for part in (message, detail_box):
        layout.addWidget(part)
    layout.addLayout(footer)
    return _Parts(dialog, countdown, buttons)
