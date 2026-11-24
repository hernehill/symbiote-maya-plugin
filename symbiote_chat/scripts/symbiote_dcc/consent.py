"""Asking the artist before a viewport picture leaves the workstation.

``ConsentPolicy`` and ``dialog_seconds`` need neither a DCC nor Qt, so they are
tested alone. Qt is imported only when a dialog is shown.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from symbiote_dcc.adapter import DialogHost
from symbiote_dcc.tool_switches import DIALOGS

logger = logging.getLogger(__name__)

SEND = "send"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

_SAFETY_MARGIN_SECONDS = 10  # leaves the server time to receive the reply
_MIN_DIALOG_SECONDS = 5
MAX_DEADLINE_SECONDS = 300  # a direct caller cannot hold the workstation longer
_SEND_ARM_SECONDS = 1.0  # a stray Enter right after the dialog appears must not send

_DIALOG_TEXT = (
    "Symbiote wants a picture of your 3D viewport ({viewer_name}).\n\n"
    "If you send it, the picture goes to Symbiote and its AI model provider for this "
    "conversation. It does not stay on this machine."
)


def dialog_text(viewer_name: str, reframed_node: str | None = None) -> str:
    """The dialog's message, naming the viewer that will be captured.

    When Symbiote moved the view since the artist last said yes, the message says so.
    """
    text = _DIALOG_TEXT.format(viewer_name=viewer_name)
    if reframed_node is None:
        return text
    return f"{text}\n\nSymbiote moved this view to frame {reframed_node}."


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def dialog_seconds(deadline_seconds: int, deadline_unix: float, now: float) -> int:
    """How long the dialog may stay open, or 0 when there is too little time to ask.

    The server's own deadline can only shorten the window: a skewed clock must not
    lengthen it past the budget the server granted. A missing, non-finite or
    non-numeric deadline gives 0, and the budget is capped at MAX_DEADLINE_SECONDS.
    """
    if not all(_finite_number(v) for v in (deadline_seconds, deadline_unix, now)):
        return 0
    budget = min(deadline_seconds, MAX_DEADLINE_SECONDS, deadline_unix - now)
    remaining = budget - _SAFETY_MARGIN_SECONDS
    return int(remaining) if remaining >= _MIN_DIALOG_SECONDS else 0


class ConsentPolicy:
    """Whether to ask before each capture; "don't ask again" lasts until the DCC closes.

    A "yes" belongs to the (server, profile) identity it was given under. Without an
    identity nothing is remembered, and any other identity is asked again.
    """

    def __init__(self) -> None:
        self._identity: tuple[str, str] | None = None
        self._remembered_for: tuple[str, str] | None = None
        self._subscribers: list[Callable[[], None]] = []
        self._reframed: str | None = None

    def should_ask(self) -> bool:
        if self._reframed is not None:
            return True  # the view moved since the artist last saw it
        return not self.is_remembered()

    def is_remembered(self) -> bool:
        """Whether "don't ask again" is on; what the panel's indicator shows."""
        return self._remembered_for is not None and self._remembered_for == self._identity

    def require_ask_after_reframe(self, node_path: str) -> None:
        """Void a remembered yes for the next capture: Symbiote moved the view.

        This keeps no UI state, so it fires no subscriber, and identity changes leave
        it in place because keeping it only makes the policy stricter.
        """
        self._reframed = node_path

    def reframed_node(self) -> str | None:
        """The node the view was moved to frame, until the artist approves that view."""
        return self._reframed

    def clear_reframed(self) -> None:
        """The artist agreed to send the moved view."""
        self._reframed = None

    def use_identity(self, server: str, profile: str) -> None:
        """Name who is signed in; a different identity does not inherit a remembered yes."""
        self._change(lambda: self._assign((server, profile)))

    def clear_identity(self) -> None:
        """Sign-out: forget the identity and any remembered yes."""
        self._change(lambda: self._assign(None, forget=True))

    def remember_for_session(self) -> None:
        self._change(lambda: setattr(self, "_remembered_for", self._identity))

    def reset(self) -> None:
        self._change(lambda: setattr(self, "_remembered_for", None))

    def _assign(self, identity: tuple[str, str] | None, forget: bool = False) -> None:
        if forget or identity != self._identity:
            self._remembered_for = None
        self._identity = identity

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._subscribers = [*self._subscribers, callback]

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        self._subscribers = [c for c in self._subscribers if c != callback]

    def _change(self, apply: Callable[[], None]) -> None:
        before = self.should_ask()
        apply()
        if self.should_ask() == before:
            return
        for callback in self._subscribers:
            try:
                callback()
            except Exception:  # a dead widget must not stop the change reaching the others
                logger.exception("Capture consent subscriber failed")


@dataclass(frozen=True)
class ConsentAnswer:
    outcome: str  # SEND, CANCELLED or TIMED_OUT
    dont_ask_again: bool = False


POLICY = ConsentPolicy()


def ask_artist(host: DialogHost, seconds: int, viewer_name: str, reframed_node: str | None = None) -> ConsentAnswer:
    """Show the modal dialog naming the viewer; closes itself as ``TIMED_OUT`` after ``seconds``."""
    from PySide6 import QtCore, QtWidgets

    dialog = QtWidgets.QDialog(host.main_window())
    dialog.setWindowTitle("Symbiote: send a viewport picture?")
    dialog.setStyleSheet(host.stylesheet())
    message = QtWidgets.QLabel(dialog_text(viewer_name, reframed_node))
    message.setTextFormat(QtCore.Qt.TextFormat.PlainText)  # names come from the scene
    message.setWordWrap(True)
    remember = QtWidgets.QCheckBox(f"Don't ask again until {host.display_name} closes")
    countdown = QtWidgets.QLabel()
    countdown.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    send = QtWidgets.QPushButton("Send")
    send.setAutoDefault(False)
    send.setDefault(False)
    send.setEnabled(False)  # armed by the timer, so a stray Enter cannot send
    cancel = QtWidgets.QPushButton("Cancel")
    cancel.setDefault(True)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addWidget(countdown, 1)
    buttons.addWidget(cancel)
    buttons.addWidget(send)
    layout = QtWidgets.QVBoxLayout(dialog)
    for part in (message, remember):
        layout.addWidget(part)
    layout.addLayout(buttons)

    started = time.monotonic()
    ends_at = started + seconds
    state: dict[str, str | None] = {"outcome": None}
    timer = QtCore.QTimer(dialog)

    def finish(outcome: str) -> None:
        if state["outcome"] is not None:
            return
        state["outcome"] = outcome
        timer.stop()
        dialog.accept()

    def tick() -> None:
        now = time.monotonic()
        left = int(ends_at - now + 0.999)
        if left <= 0:
            finish(TIMED_OUT)
            return
        countdown.setText(f"Closes in {left}s")
        if now - started >= _SEND_ARM_SECONDS:
            send.setEnabled(True)

    timer.timeout.connect(tick)
    send.clicked.connect(lambda: finish(SEND))
    cancel.clicked.connect(lambda: finish(CANCELLED))
    tick()
    timer.start(250)
    cancel.setFocus()
    with DIALOGS.showing():  # a microagent restart waits until the artist answers
        dialog.exec()  # closing with the window button or Esc leaves no outcome: cancelled
    timer.stop()
    outcome = state["outcome"] or CANCELLED
    answer = ConsentAnswer(outcome, remember.isChecked() and outcome == SEND)
    dialog.deleteLater()
    return answer


def declined(reason: str) -> dict[str, object]:
    return {"captured": False, "reason": reason}


def decline_reason(
    host: DialogHost,
    deadline_seconds: int,
    deadline_unix: float,
    viewer_name: str,
    *,
    now: Callable[[], float] = time.time,
) -> str | None:
    """The reason the capture must not happen, or ``None`` when the picture may be taken.

    The deadline comes first: with too little time left nothing is asked, even when the artist
    said "don't ask again". Then the policy; then the dialog, on the GUI thread.
    """
    seconds = dialog_seconds(deadline_seconds, deadline_unix, now())
    if seconds == 0:
        return TIMED_OUT  # also true when consent is remembered
    policy = POLICY
    if not policy.should_ask():
        return None
    answer = ask_artist(host, seconds, viewer_name, policy.reframed_node())
    if answer.outcome != SEND:
        return answer.outcome  # a moved view stays unapproved, so a retry asks again
    policy.clear_reframed()
    if answer.dont_ask_again:
        policy.remember_for_session()
    return None
