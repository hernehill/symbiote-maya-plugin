"""What the panel shows in place of the chat when there is no session to open it with."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from PySide6 import QtCore, QtWidgets

from symbiote_dcc.panel_session import Outcome, SessionAnswer

# Action labels, shown on buttons; ``actionChosen`` carries the id of the action clicked, which for a
# plain label (Retry) is the label itself.
RETRY = "Retry"
CONTINUE = "Continue as {email}"
CONTINUE_NO_EMAIL = "Continue"  # the stored sign-in names no email
SIGN_OUT = "Sign out of Symbiote"

ACTION_CONTINUE = "continue"
ACTION_SIGN_OUT = "sign_out"
ACTION_SIGN_OUT_RETRY = "sign_out_retry"  # Retry on the sign-out notice; plain "Retry" opens the chat


class Action(NamedTuple):
    """A button whose label can change (it names the email) while its id stays put."""

    id: str
    label: str


OPENING = "Opening Symbiote..."
SESSION_ENDED = "Your Symbiote session ended. Sign in again."
SIGNED_OUT = "Signed out. Sign in to continue."
SESSION_ENDED_BUT_SIGNED_IN = "The chat's sign-in ended, but you are still signed in to Symbiote."
READ_ONLY_MODE = "Symbiote is in read-only mode"
SERVER_TOO_OLD = "This Symbiote server is older than the plugin"
UNREACHABLE = "Can't reach Symbiote"
SIGN_OUT_UNREACHABLE = "Can't reach Symbiote; you're still signed in."
PARENT_NOT_CLI = "This profile's sign-in can't start a chat; sign in again from the panel."

# The chat ended within MINT_LOOP_SECONDS of the panel handing it a session: it isn't keeping it.
SEED_LOOP_ERROR = (
    "The chat at {web} didn't keep the sign-in the panel gave it (server {server}). "
    "Check the panel's server address, then use Reload."
)

_REFUSALS = {
    Outcome.READ_ONLY: READ_ONLY_MODE,
    Outcome.SERVER_TOO_OLD: SERVER_TOO_OLD,
    Outcome.PARENT_NOT_CLI: PARENT_NOT_CLI,
}


def refusal_notice(answer: SessionAnswer) -> tuple[str, tuple[str, ...]]:
    """The message and actions for an answer that opened no chat (anything but OK or ENDED).

    Anything without its own copy is "can't reach", with the detail after it and a Retry.
    """
    message = _REFUSALS.get(answer.outcome)
    if message is not None:
        return message, ()
    return (f"{UNREACHABLE}: {answer.detail}" if answer.detail else UNREACHABLE), (RETRY,)


class SessionNoticeView(QtWidgets.QWidget):
    """One plain-text message and a row of action buttons."""

    actionChosen = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._label = QtWidgets.QLabel(self)
        self._label.setTextFormat(QtCore.Qt.PlainText)  # server details, never HTML
        self._label.setWordWrap(True)
        self._row = QtWidgets.QHBoxLayout()
        layout = QtWidgets.QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self._label)
        layout.addLayout(self._row)
        layout.addStretch(1)
        self._buttons: list[QtWidgets.QPushButton] = []
        self._actions: tuple[Action, ...] = ()

    def show_notice(self, message: str, actions: Sequence[str | Action] = ()) -> None:
        self._label.setText(message)
        for old in self._buttons:
            old.setVisible(False)
            old.deleteLater()
        self._actions = tuple(a if isinstance(a, Action) else Action(a, a) for a in actions)
        self._buttons = [self._button(action) for action in self._actions]

    def _button(self, action: Action) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(action.label, self)
        button.clicked.connect(lambda _checked=False, chosen=action.id: self.actionChosen.emit(chosen))
        self._row.addWidget(button)
        return button

    def message(self) -> str:
        text: str = self._label.text()
        return text

    def actions(self) -> tuple[str, ...]:
        return tuple(action.label for action in self._actions)
