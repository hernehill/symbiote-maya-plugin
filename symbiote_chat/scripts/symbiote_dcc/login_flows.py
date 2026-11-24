"""Browser (loopback) and device-code sign-in, the same server flows ``symbiote login`` uses.

Runs on a worker thread. ``cancel`` is checked often enough that closing the panel
ends a sign-in promptly.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

from symbiote_dcc import host_ssl, net
from symbiote_dcc.credentials import Tokens
from symbiote_dcc.url_policy import is_safe_external_url

if TYPE_CHECKING:
    import ssl
else:
    ssl = host_ssl.require_ssl()  # a host whose Python can't load ssl fails here, with the reason

BROWSER_TIMEOUT_SECONDS = 300.0
UNSAFE_USER_CODE = (
    "The server's sign-in code is not a code (4 to 32 letters, digits or dashes), so the "
    "panel will not show it. Sign in with the browser instead, and tell your administrator."
)
_USER_CODE = re.compile(r"[A-Za-z0-9-]{4,32}")
UNSAFE_APPROVAL_LINK = (
    "The server's approval page address is not a web address (http or https), so the "
    "panel will not show or open it. Sign in with the browser instead, and tell your "
    "administrator."
)
_HTTP_TIMEOUT_SECONDS = 30.0
_CANCEL_POLL_SECONDS = 0.2
_DEVICE_AUTHORIZE = "/api/auth/oidc/device/authorize"
_DEVICE_TOKEN = "/api/auth/oidc/device/token"
_CLI_EXCHANGE = "/api/auth/oidc/cli-exchange"

_RETURN_PAGE = (
    b"<!doctype html><body style='font-family:sans-serif;padding:2em'>"
    b"<h1>%s</h1><p>You can close this window and go back to %s.</p></body>"
)


class LoginCancelled(Exception):
    """The artist cancelled, or the panel closed."""


class LoginFailed(Exception):
    """Sign-in did not complete; the message is written for the artist."""


def _pkce_challenge() -> str:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _CallbackServer(HTTPServer):
    """The loopback server the browser returns to; ``outcome`` is set by the first callback."""

    def __init__(self, display_name: str) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.display_name = display_name
        self.outcome: dict[str, str] | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # the query string carries a one-time code

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        error = (query.get("error") or [""])[0]
        if parsed.path != "/callback" or not (code or error):
            self.send_response(404)
            self.end_headers()
            return
        if self.server.outcome is None:
            self.server.outcome = {"error": error} if error else {"code": code}
        self.send_response(401 if error else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        headline = b"Sign-in failed." if error else b"Signed in."
        name = html.escape(self.server.display_name).encode("utf-8")
        self.wfile.write(_RETURN_PAGE % (headline, name))


def _wait_for_callback(server: _CallbackServer, cancel: threading.Event) -> dict[str, str]:
    deadline = time.monotonic() + BROWSER_TIMEOUT_SECONDS
    server.timeout = _CANCEL_POLL_SECONDS
    while server.outcome is None:
        if cancel.is_set():
            raise LoginCancelled
        if time.monotonic() > deadline:
            raise LoginFailed("Browser sign-in timed out. Try again.")
        server.handle_request()
    return server.outcome


def _tokens_from(body: dict[str, Any]) -> Tokens:
    try:
        return Tokens(access=body["access_token"], refresh=body["refresh_token"])
    except KeyError as exc:
        raise LoginFailed("The server's sign-in answer had no tokens.") from exc


def _unreachable(server: str, exc: net.Unreachable) -> LoginFailed:
    return LoginFailed(f"Could not reach {server} ({exc.reason}).")


def _context(server: str, profile: str) -> ssl.SSLContext | None:
    """One verifying context per sign-in (None for plain http), built before anything is sent.

    A refusal here is about the configured CAs, not the network, and says so.
    """
    if not server.lower().startswith("https:"):
        return None
    try:
        return net.tls_context(profile)
    except net.Unreachable as exc:
        raise LoginFailed(f"Can't set up trust for {server}: {exc.reason}") from exc


def _post(
    server: str,
    profile: str,
    path: str,
    payload: dict[str, str],
    context: ssl.SSLContext | None = None,
) -> tuple[int, dict[str, Any]]:
    try:
        return net.post_json(f"{server}{path}", payload, _HTTP_TIMEOUT_SECONDS, profile, context)
    except net.Unreachable as exc:
        raise _unreachable(server, exc) from exc
    except net.Redirected as exc:
        raise LoginFailed(
            f"{server} redirected the sign-in (HTTP {exc.status}, to {exc.host}); refusing to follow it."
        ) from exc
    except net.BadReply as exc:
        if exc.too_large:
            raise LoginFailed(f"The server's answer was too large (HTTP {exc.status}).") from exc
        raise LoginFailed(f"The server's answer wasn't JSON (HTTP {exc.status}, {exc.content_type}).") from exc


def browser_login(
    server: str,
    profile: str,
    open_url: Callable[[str], object],
    cancel: threading.Event,
    display_name: str,
) -> Tokens:
    """Open the system browser, wait for its redirect to 127.0.0.1, exchange the code.

    The TLS context is built first: a CA problem found after the IdP flow would burn the code.
    """
    context = _context(server, profile)
    callback = _CallbackServer(display_name)
    try:
        redirect = f"http://127.0.0.1:{callback.server_address[1]}/callback"
        query = urllib.parse.urlencode({"cli_redirect": redirect, "cli_pkce_challenge": _pkce_challenge()})
        open_url(f"{server}/api/auth/oidc/login?{query}")
        outcome = _wait_for_callback(callback, cancel)
    finally:
        callback.server_close()
    if "error" in outcome:
        raise LoginFailed("Sign-in was refused. Contact your administrator.")
    status, body = _post(server, profile, _CLI_EXCHANGE, {"code": outcome["code"]}, context)
    if status != 200:
        raise LoginFailed(f"The server refused the sign-in (HTTP {status}).")
    return _tokens_from(body)


def _sleep(seconds: float, cancel: threading.Event) -> None:
    if cancel.wait(seconds):
        raise LoginCancelled


def _poll_device_token(
    server: str,
    profile: str,
    device_code: str,
    interval: float,
    cancel: threading.Event,
    context: ssl.SSLContext | None = None,
) -> Tokens:
    context = context or _context(server, profile)
    while True:
        _sleep(interval, cancel)
        status, body = _post(server, profile, _DEVICE_TOKEN, {"device_code": device_code}, context)
        if status not in (200, 400):
            raise LoginFailed(f"The server answered HTTP {status} while waiting.")
        error = body.get("error")
        if error is None:
            return _tokens_from(body)
        if error == "slow_down":
            interval *= 2
        elif error == "expired_token":
            raise LoginFailed("The code expired before it was approved. Try again.")
        elif error == "access_denied":
            raise LoginFailed("Sign-in was declined.")
        elif error != "authorization_pending":
            raise LoginFailed(f"Unexpected sign-in response: {error}")


def device_login(server: str, profile: str, show_code: Callable[[str, str], object], cancel: threading.Event) -> Tokens:
    """RFC 8628: show a code and link, then poll until someone approves it."""
    context = _context(server, profile)
    status, start = _post(server, profile, _DEVICE_AUTHORIZE, {}, context)
    if status != 200:
        raise LoginFailed(f"The server cannot start device sign-in (HTTP {status}).")
    link = start.get("verification_uri_complete") or start["verification_uri"]
    if not is_safe_external_url(link):
        raise LoginFailed(UNSAFE_APPROVAL_LINK)
    code = start.get("user_code")
    if not isinstance(code, str) or not _USER_CODE.fullmatch(code):
        raise LoginFailed(UNSAFE_USER_CODE)
    show_code(link, code)
    return _poll_device_token(server, profile, start["device_code"], float(start.get("interval") or 5), cancel, context)
