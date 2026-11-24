"""Runs one panel-session call (mint, check, revoke) off the DCC's main thread."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6 import QtCore

from symbiote_dcc.panel_session import Outcome, SessionAnswer

logger = logging.getLogger(__name__)

# Workers still running when their panel closed. Unparented and held here until they finish:
# Qt 6 aborts the whole DCC when a running QThread is destroyed along with its parent.
UNFINISHED_WORKERS: set[QtCore.QThread] = set()


class SessionCallWorker(QtCore.QThread):
    """Runs ``call`` on its own thread; ``done`` always carries a ``SessionAnswer``, never an exception.

    The call must touch no Qt object: it reads the credentials file and talks to the server only.
    Once its thread has finished it releases itself, as ``TiersWorker`` does.
    """

    done = QtCore.Signal(object)
    released = QtCore.Signal(object)

    def __init__(self, call: Callable[[], SessionAnswer], parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self._call = call
        # The worker lives on the main thread, so Qt queues this there, after run() has returned.
        self.finished.connect(self._release)

    def _release(self) -> None:
        UNFINISHED_WORKERS.discard(self)
        self.released.emit(self)
        self.deleteLater()

    def run(self) -> None:
        try:
            answer = self._call()
        except (ValueError, OSError) as exc:
            # A damaged credentials file names itself and what to do; a held lock
            # (CredentialsLockTimeout is an OSError) says to try again.
            answer = SessionAnswer(Outcome.UNAVAILABLE, detail=str(exc))
        except Exception as exc:  # a worker thread has nowhere else to report: logged and shown
            logger.exception("Symbiote: a panel session call failed unexpectedly")
            answer = SessionAnswer(Outcome.UNAVAILABLE, detail=f"unexpected error: {exc}")
        self.done.emit(answer)
