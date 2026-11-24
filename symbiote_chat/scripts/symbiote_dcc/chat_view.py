"""The embedded web-chat, signed in once with the panel's own session (never the CLI's)."""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtWebEngineCore import (
    QWebEngineLoadingInfo,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from symbiote_dcc.credentials import Tokens
from symbiote_dcc.url_policy import (
    Navigation,
    canonical_origin,
    is_oidc_login,
    is_safe_external_url,
    is_web_chat_blob,
    is_web_chat_login,
    is_web_chat_url,
    main_frame_navigation,
    oidc_route,
    subframe_navigation_allowed,
    web_chat_entry_url,
)

logger = logging.getLogger(__name__)

# web-chat/src/utils/jwt.ts TOKEN_KEYS
_ACCESS_KEY = "web_chat_access_token"
_REFRESH_KEY = "web_chat_refresh_token"
# Read only: whether web-chat still holds a refresh token. A rotation writes access, then refresh,
# and never clears in between, so a live session always reads present.
_PRESENCE_JS = f"JSON.stringify({{origin: location.origin, present: localStorage.getItem({_REFRESH_KEY!r}) !== null}})"
# Read once, by Sign out, to end the chat's own session. The value is never logged.
_TOKEN_JS = f"JSON.stringify({{origin: location.origin, token: localStorage.getItem({_REFRESH_KEY!r})}})"
# Sign out waits this long for the page to answer, then goes on without the chat's token (its
# session then idles out) so a hung page can't hold the sign-out; a page answers in milliseconds.
TOKEN_READ_TIMEOUT_MS = 3000
_SEED_SCRIPT_NAME = "symbiote-seed-tokens"
_CONSOLE_LINES_KEPT = 200
# Scripts the panel runs see a pristine DOM and globals; page JS cannot patch them there.
# localStorage is per origin, so it is the same store in every world.
_ISOLATED_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld
_LoadStatus = QWebEngineLoadingInfo.LoadStatus

# ChatView.chatEnded's reasons.
LOGIN_ROUTE = "login_route"  # web-chat landed on /login
LEFT_WEB_CHAT = "left_web_chat"  # an unclicked navigation off web-chat was refused, and no refresh token is left
NO_REFRESH_TOKEN = "no_refresh_token"
SIGN_IN_REQUESTED = "sign_in_requested"  # the artist clicked a link to the OIDC login
SIGN_IN_REDIRECTED = "sign_in_redirected"  # the OIDC login without a click: a redirect, form, script, frame or popup


def _encoded(url: QtCore.QUrl) -> str:
    """The URL as url_policy must see it: Qt's plain toString() shows xn-- hosts in Unicode."""
    encoded: str = url.toString(QtCore.QUrl.ComponentFormattingOption.FullyEncoded)  # Qt is untyped here
    return encoded


def _refuse_unknown_schemes(settings: QWebEngineSettings) -> None:
    settings.setUnknownUrlSchemePolicy(QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)


def _write_tokens_js(origin: str, tokens: Tokens) -> str:
    return (
        f"if (location.origin === {json.dumps(origin)}) {{"
        f"localStorage.setItem({_ACCESS_KEY!r}, {json.dumps(tokens.access)});"
        f"localStorage.setItem({_REFRESH_KEY!r}, {json.dumps(tokens.refresh)});"
        "}"
    )


def _open_externally(url: QtCore.QUrl) -> None:
    """Hand an http(s) URL to the system browser; drop and log anything else."""
    if is_safe_external_url(_encoded(url)):
        QtGui.QDesktopServices.openUrl(url)
    else:
        logger.warning("Refused to open a %r link outside the panel", url.scheme())


class _ExternalLinkPage(QWebEnginePage):
    """Target of target=_blank links and window.open.

    Web URLs go to the system browser. A blob web-chat made (an attachment preview) is
    saved through the opener, since the system browser cannot read the panel's blobs.
    """

    def __init__(self, profile: QWebEngineProfile, opener: _ChatPage):
        super().__init__(profile, opener)
        self._opener = opener

    def acceptNavigationRequest(self, url: QtCore.QUrl, nav_type, is_main_frame: bool) -> bool:
        encoded = _encoded(url)
        if is_oidc_login(encoded):
            self._opener.request_sign_in(clicked=False)  # a popup: not the artist's click on the chat
        elif oidc_route(encoded) is not None:
            logger.warning("Refused to open an OIDC route outside the panel's sign-in")
        elif is_web_chat_blob(encoded, self._opener.origin):
            self._opener.download(url)
        else:
            _open_externally(url)
        self.deleteLater()
        return False


class _ChatPage(QWebEnginePage):
    """Keeps web-chat in the panel; clicked web links go to the system browser."""

    def __init__(self, profile: QWebEngineProfile, origin: str, parent: QtCore.QObject):
        super().__init__(profile, parent)
        self.origin = origin  # canonical_origin() of web-chat
        # Backstop for acceptNavigationRequest: Chromium itself never hands an unknown
        # scheme to the OS. The profile sets it too, for pages made from it.
        _refuse_unknown_schemes(self.settings())
        self._allow_script_windows(False)
        self.urlChanged.connect(self._on_url_changed)
        self.console_lines: deque[str] = deque(maxlen=_CONSOLE_LINES_KEPT)
        # Set by ChatView: told when an unclicked navigation off web-chat was refused.
        self.on_leave_refused: Callable[[], None] | None = None
        # Set by ChatView: told when the page asked for the OIDC login, which the panel runs instead.
        # ``clicked``: the artist clicked a main-frame link to it.
        self.on_sign_in_requested: Callable[[bool], None] | None = None

    def request_sign_in(self, clicked: bool) -> None:
        logger.info("The chat asked to sign in (clicked: %s); the panel signs in instead", clicked)
        if self.on_sign_in_requested is not None:
            self.on_sign_in_requested(clicked)

    def _on_url_changed(self, url: QtCore.QUrl) -> None:
        self._allow_script_windows(is_web_chat_url(_encoded(url), self.origin))

    def _allow_script_windows(self, allow: bool) -> None:
        # web-chat opens attachment previews and connector sign-in after an await, when
        # Chromium no longer counts the click, so it may open windows on its own. Any
        # other site in the panel needs a click. Popups only reach _ExternalLinkPage.
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, allow)

    def javaScriptConsoleMessage(self, level, message: str, line: int, source: str) -> None:
        self.console_lines.append(f"{level.name}: {message} ({source}:{line})")

    def acceptNavigationRequest(self, url: QtCore.QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame:
            if is_oidc_login(_encoded(url)):
                self.request_sign_in(clicked=False)
                return False
            if subframe_navigation_allowed(_encoded(url), self.origin):
                return True
            logger.warning("Refused to load a %r URL in a frame", url.scheme())
            return False
        clicked = nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        decision = main_frame_navigation(_encoded(url), self.origin, clicked)
        if decision is Navigation.SIGN_IN:
            self.request_sign_in(clicked)
        elif decision is Navigation.OPEN_EXTERNALLY:
            _open_externally(url)
        elif decision is Navigation.REFUSE:
            logger.warning("Refused to navigate the panel to a %r URL", url.scheme())
        elif decision is Navigation.LEAVE_REFUSED:
            logger.info("Refused an unclicked navigation off web-chat to %s", url.host())
            if self.on_leave_refused is not None:
                self.on_leave_refused()
        return decision is Navigation.STAY

    def createWindow(self, window_type) -> QWebEnginePage:
        return _ExternalLinkPage(self.profile(), self)


class ChatView(QtWidgets.QWidget):
    """Shows web-chat signed in with the panel session it is handed; nothing is written back.

    ``chatEnded(reason)`` says the chat's session is over, for one of the reasons at the top of this
    module: ``LOGIN_ROUTE``, ``LEFT_WEB_CHAT``, ``NO_REFRESH_TOKEN``, or the OIDC login, which never runs
    in the panel: ``SIGN_IN_REQUESTED`` when the artist clicked it, ``SIGN_IN_REDIRECTED`` otherwise.
    At most once per ``open_session``.
    """

    chatEnded = QtCore.Signal(str)

    def __init__(
        self,
        web_url: str,
        parent: QtWidgets.QWidget,
        *,
        workstation: str | None = None,
    ) -> None:
        super().__init__(parent)
        # One serialization of web-chat's origin for the seed and the navigation
        # checks, matching location.origin; raises ValueError for an unusable address.
        self._origin = canonical_origin(web_url)
        # The address only adds the panel's microagent id (#2168); the origin is unchanged.
        self._url = QtCore.QUrl(web_chat_entry_url(self._origin, workstation))
        self._page_ready = False
        self._ended = False  # chatEnded fires once per open_session
        self._navigation = 0  # bumped on every navigation; a read of the page spans exactly one
        # Off the record: Chromium keeps nothing on disk, so the panel session dies with the pane.
        self._web_profile = QWebEngineProfile(self)
        _refuse_unknown_schemes(self._web_profile.settings())
        self._web_profile.downloadRequested.connect(self._save_download)
        self._page = _ChatPage(self._web_profile, self._origin, self)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page)
        self._page.on_leave_refused = lambda: self._check_presence(LEFT_WEB_CHAT)
        self._page.on_sign_in_requested = lambda clicked: self._end(
            SIGN_IN_REQUESTED if clicked else SIGN_IN_REDIRECTED
        )
        self._page.loadingChanged.connect(self._on_loading_changed)
        self._page.loadStarted.connect(self._next_navigation)
        self._page.urlChanged.connect(self._on_url_changed)
        # The panel has no address bar: say so when a redirect leaves web-chat.
        self._away_banner = QtWidgets.QLabel(self)
        self._away_banner.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self._away_banner.setVisible(False)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._away_banner)
        layout.addWidget(self._view)

    @property
    def console_lines(self) -> list[str]:
        return list(self._page.console_lines)

    def open_session(self, tokens: Tokens) -> None:
        """Load web-chat signed in with ``tokens``, the panel's own session."""
        self._page_ready = False
        self._ended = False
        self._seed_on_document_creation(tokens)
        self._view.load(self._url)

    def reseed(self, tokens: Tokens) -> None:
        """Sign the existing page in again with ``tokens``, then load web-chat's entry URL.

        The write runs in the isolated world and only on web-chat's origin; the document-creation
        seed covers a page that is elsewhere (an identity provider) when the entry URL loads.
        Re-arms ``chatEnded``.
        """
        self._page_ready = False
        self._ended = False
        self._page.runJavaScript(_write_tokens_js(self._origin, tokens), _ISOLATED_WORLD.value, lambda _result: None)
        self._seed_on_document_creation(tokens)
        self._view.load(self._url)

    def reload(self) -> None:
        """Reload web-chat; away from it (a redirect, a sign-in page), go back to it."""
        self._page_ready = False
        if self._on_web_chat():
            self._view.reload()
        else:
            self._view.load(self._url)

    def _on_web_chat(self) -> bool:
        return is_web_chat_url(_encoded(self._page.url()), self._origin)

    def _on_url_changed(self, url: QtCore.QUrl) -> None:
        self._next_navigation()
        away = not is_web_chat_url(_encoded(url), self._origin)
        if away:
            self._away_banner.setText(
                f"Showing {url.host()}, not Symbiote. Don't enter a password unless you"
                " expect this site. Reload returns to Symbiote."
            )
        self._away_banner.setVisible(away)
        if is_web_chat_login(_encoded(url), self._origin):
            self._end(LOGIN_ROUTE)
        elif not away:
            self._check_presence()

    def _end(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            self.chatEnded.emit(reason)

    def _check_presence(self, reason: str = NO_REFRESH_TOKEN) -> None:
        """Ask the page, read only, whether it still holds a refresh token.

        Only once a web-chat document is loaded and Qt reports web-chat. The answer counts only if the page says it is
        web-chat and no navigation came since the question. No timer: asked on web-chat
        URL changes, loads and refused leaves.
        """
        if not self._page_ready or not self._on_web_chat():
            return
        asked_at = self._navigation

        def answered(result: object) -> None:
            if self._navigation == asked_at and self._on_web_chat() and self._absent(result):
                self._end(reason)

        self._page.runJavaScript(_PRESENCE_JS, _ISOLATED_WORLD.value, answered)

    def _absent(self, result: object) -> bool:
        """True when the page, on web-chat's origin, reports no refresh token."""
        try:
            report = json.loads(result) if isinstance(result, str) else None
        except ValueError:
            return False
        return isinstance(report, dict) and report.get("origin") == self._origin and report.get("present") is False

    def read_refresh_token(self, callback: Callable[[str | None], None]) -> None:
        """Hand ``callback`` the page's refresh token once, or None; it is called exactly once.

        Read only, in the isolated world, and only from web-chat's origin. None when the page is off
        web-chat, was navigated since the question, holds no token, answers for another origin, or
        gives no answer within ``TOKEN_READ_TIMEOUT_MS``. The token goes to ``callback`` only.
        """
        if not self._on_web_chat():
            callback(None)
            return
        asked_at = self._navigation
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        settled: list[bool] = []

        def settle(token: str | None) -> None:
            if settled:
                return
            settled.append(True)
            timer.stop()
            timer.deleteLater()
            callback(token)

        def answered(result: object) -> None:
            ok = self._navigation == asked_at and self._on_web_chat()
            settle(self._token_in(result) if ok else None)

        timer.timeout.connect(lambda: settle(None))
        timer.start(TOKEN_READ_TIMEOUT_MS)
        self._page.runJavaScript(_TOKEN_JS, _ISOLATED_WORLD.value, answered)

    def _token_in(self, result: object) -> str | None:
        """The refresh token in the page's report when it is web-chat's, else None."""
        try:
            report = json.loads(result) if isinstance(result, str) else None
        except ValueError:
            return None
        if not isinstance(report, dict) or report.get("origin") != self._origin:
            return None
        token = report.get("token")
        return token if isinstance(token, str) and token else None

    def close_session(self) -> None:
        """Release the page before its profile, as QtWebEngine requires."""
        self._page_ready = False
        self._view.setPage(None)
        self._page.deleteLater()

    def _seed_on_document_creation(self, tokens: Tokens) -> None:
        """Sign web-chat in on its first load; later loads keep the profile's own storage."""
        self._drop_seed()
        script = QWebEngineScript()
        script.setName(_SEED_SCRIPT_NAME)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(_ISOLATED_WORLD)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(_write_tokens_js(self._origin, tokens))
        self._page.scripts().insert(script)

    def _drop_seed(self) -> None:
        scripts = self._page.scripts()
        for old in scripts.find(_SEED_SCRIPT_NAME):
            scripts.remove(old)

    def _on_loading_changed(self, info: QWebEngineLoadingInfo) -> None:
        """Track whether a web-chat document is loaded, from Qt's load status.

        Not loadFinished(ok): Qt ends a navigation the panel refused or sent to the
        browser, or one Chromium blocked, with loadFinished(False) and a stopped load,
        while the document on screen is untouched, and web-chat, a single-page app, never
        loads again to set it right.
        """
        status = info.status()
        if status == _LoadStatus.LoadSucceededStatus:
            self._page_ready = True
            if self._on_web_chat():
                self._check_presence()
                # Seeded once. Seeding again would undo a sign-out made in web-chat when
                # the identity provider redirects back to it.
                self._drop_seed()
        elif status == _LoadStatus.LoadFailedStatus or (
            status == _LoadStatus.LoadStoppedStatus and not self._page_ready
        ):
            self._page_ready = False
            logger.error("web-chat did not load from %s (error %s)", self._origin, info.errorCode())

    def _next_navigation(self, *_args: object) -> None:
        self._navigation += 1

    def _save_download(self, download) -> None:
        if not self._on_web_chat():
            # Only web-chat may put a save dialog in front of the artist.
            logger.warning("Refused a download while the panel shows %s", self._page.url().host())
            download.cancel()
            return
        target, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save file", download.downloadFileName())
        if not target:
            download.cancel()
            return
        info = QtCore.QFileInfo(target)
        download.setDownloadDirectory(info.absolutePath())
        download.setDownloadFileName(info.fileName())
        download.accept()
