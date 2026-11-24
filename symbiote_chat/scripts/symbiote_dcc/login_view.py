"""The sign-in screen and the worker thread behind it."""

from __future__ import annotations

import threading
import webbrowser

from PySide6 import QtCore, QtGui, QtWidgets

from symbiote_dcc.credentials import Tokens
from symbiote_dcc.login_flows import (
    UNSAFE_APPROVAL_LINK,
    LoginCancelled,
    LoginFailed,
    browser_login,
    device_login,
)
from symbiote_dcc.session_notice import SESSION_ENDED, SIGNED_OUT
from symbiote_dcc.settings import PanelSettings
from symbiote_dcc.url_policy import is_safe_external_url

MODE_BROWSER = "browser"
MODE_DEVICE = "device"
_BROWSER_LABEL = "Sign in with browser"
_SIGN_IN_AGAIN_LABEL = "Sign in again"


class LoginWorker(QtCore.QThread):
    deviceCode = QtCore.Signal(str, str)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, mode: str, server: str, profile: str, display_name: str, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self._mode = mode
        self._server = server
        self._profile = profile
        self._display_name = display_name
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _login(self) -> Tokens:
        if self._mode == MODE_BROWSER:
            return browser_login(self._server, self._profile, webbrowser.open, self._cancel, self._display_name)
        return device_login(self._server, self._profile, self.deviceCode.emit, self._cancel)

    def run(self) -> None:
        try:
            self.succeeded.emit(self._login())
        except LoginCancelled:
            self.failed.emit("Sign-in cancelled.")
        except LoginFailed as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # a worker thread has nowhere else to report
            self.failed.emit(f"Sign-in failed unexpectedly: {type(exc).__name__}: {exc}")


class LoginView(QtWidgets.QWidget):
    """Collects the server and profile, then starts a sign-in."""

    loginRequested = QtCore.Signal(str, object)
    cancelRequested = QtCore.Signal()

    def __init__(self, settings: PanelSettings, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._server = QtWidgets.QLineEdit(settings.server)
        self._profile = QtWidgets.QLineEdit(settings.profile)
        self._browser_button = QtWidgets.QPushButton(_BROWSER_LABEL)
        self._busy = False
        self._device_button = QtWidgets.QPushButton("Use a device code")
        self._cancel_button = QtWidgets.QPushButton("Cancel")
        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextFormat(QtCore.Qt.PlainText)
        self._status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._code = QtWidgets.QLabel("")
        self._code.setTextFormat(QtCore.Qt.PlainText)  # the code comes from the server
        self._code.setAlignment(QtCore.Qt.AlignCenter)
        self._code.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        font = self._code.font()
        font.setPointSize(font.pointSize() * 2)
        font.setBold(True)
        self._code.setFont(font)
        self._open_link = QtWidgets.QPushButton("Open the approval page")
        self._link = ""
        self._build_layout()
        self.set_busy(False)
        self._browser_button.clicked.connect(lambda: self._request(MODE_BROWSER))
        self._device_button.clicked.connect(lambda: self._request(MODE_DEVICE))
        self._cancel_button.clicked.connect(self.cancelRequested)
        self._open_link.clicked.connect(self._open_approval_page)

    def _build_layout(self) -> None:
        form = QtWidgets.QFormLayout()
        form.addRow("Server", self._server)
        form.addRow("Profile", self._profile)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(QtWidgets.QLabel("<h2>Sign in to Symbiote</h2>"))
        layout.addLayout(form)
        for widget in (
            self._browser_button,
            self._device_button,
            self._cancel_button,
            self._code,
            self._open_link,
            self._status,
        ):
            layout.addWidget(widget)
        layout.addStretch(2)

    def _request(self, mode: str) -> None:
        settings = PanelSettings(server=self._server.text(), profile=self._profile.text())
        self.loginRequested.emit(mode, settings)

    def _open_approval_page(self) -> None:
        if not is_safe_external_url(self._link):
            self.show_status(UNSAFE_APPROVAL_LINK)
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._link))

    def set_busy(self, busy: bool) -> None:
        for widget in (self._browser_button, self._device_button, self._server, self._profile):
            widget.setEnabled(not busy)
        self._cancel_button.setVisible(busy)
        if not busy:
            if self._busy:  # a sign-in finished: "Sign in again" has done its job
                self._browser_button.setText(_BROWSER_LABEL)
            self._code.clear()
            self._code.setVisible(False)
            self._open_link.setVisible(False)
        self._busy = busy

    def show_status(self, message: str) -> None:
        self._status.setText(message)

    def show_session_ended(self) -> None:
        """Say the Symbiote session ended; the browser button reads "Sign in again" until a sign-in finishes."""
        self.show_status(SESSION_ENDED)
        self._browser_button.setText(_SIGN_IN_AGAIN_LABEL)

    def show_signed_out(self) -> None:
        """Say the artist signed out of Symbiote from the panel."""
        self.show_status(SIGNED_OUT)

    def show_device_code(self, link: str, code: str) -> None:
        """Show the code and link; a link that is not http(s) is neither shown nor kept."""
        if not is_safe_external_url(link):
            self._link = ""
            self._code.setVisible(False)
            self._open_link.setVisible(False)
            self.show_status(UNSAFE_APPROVAL_LINK)
            return
        self._link = link
        self._code.setText(code)
        self._code.setVisible(True)
        self._open_link.setVisible(True)
        self.show_status(f"Enter this code at {link}, on any device. This panel continues by itself.")
