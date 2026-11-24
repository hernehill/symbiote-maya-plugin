"""Which URLs the embedded web-chat may follow, hand to the OS, or count as web-chat itself.

Pure string decisions, so they are tested without Qt; ``chat_view`` applies them.
"""

from __future__ import annotations

import posixpath
import re
from enum import Enum
from urllib.parse import unquote, urlencode, urlsplit

_WEB_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_FRAME_ABOUT_URLS = ("about:blank", "about:srcdoc")
# web-chat reads this once (web-chat/src/utils/panelWorkstation.ts) and sends it with each message.
_WORKSTATION_PARAM = "workstation"
# The server's OIDC routes (agent_mcp_server, proxied by web-chat), on any origin.
_OIDC_ROOT = "/api/auth/oidc"
_OIDC_LOGIN = "login"
# Percent-decoding rounds before a path that still changes is taken as an OIDC route (fail closed).
_MAX_DECODES = 5
_SLASHES = re.compile(r"/+")

Origin = tuple[str, str, int]

NON_ASCII_HOST = "The server address must use an ASCII host name; write an international name in its xn-- form"


class Navigation(Enum):
    STAY = "stay"
    OPEN_EXTERNALLY = "open_externally"
    REFUSE = "refuse"
    LEAVE_REFUSED = "leave_refused"  # an unclicked http(s) navigation off web-chat: the chat's session ended
    SIGN_IN = "sign_in"  # the OIDC login: the panel signs in through the system browser instead


def origin_of(url: str) -> Origin | None:
    """``(scheme, host, port)`` of an http(s) URL with a host, else ``None``."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _WEB_SCHEMES or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def canonical_origin(url: str) -> str:
    """The origin of ``url`` serialized as the browser's ``location.origin`` is.

    Lowercase scheme and host, no default port, no path. Every
    comparison with web-chat's origin uses this one value. A URL with a user name, or
    one that is not http(s) with an ASCII host, raises ``ValueError``.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ValueError(f"{url!r} is not a web address") from exc
    if parts.username is not None or parts.password is not None:
        raise ValueError("The server address must not contain a user name")
    origin = origin_of(url)
    if origin is None:
        raise ValueError(f"{url!r} is not an http(s) address with a host")
    scheme, host, port = origin
    if not host.isascii():
        # Python's IDNA 2003 and Chromium's UTS #46 encode some names differently
        # (faß.example), so a Unicode host could be compared as the wrong origin.
        raise ValueError(NON_ASCII_HOST)
    if ":" in host:
        host = f"[{host}]"  # IPv6
    return f"{scheme}://{host}" + ("" if port == _DEFAULT_PORTS[scheme] else f":{port}")


def web_chat_entry_url(web_chat_origin: str, workstation: str | None) -> str:
    """The address the panel opens web-chat at, naming the panel's own microagent (#2168).

    The chat then knows which workstation it runs in and skips listing agents. Only the
    query changes: the origin, and so every check above and below, is web-chat's.
    """
    if workstation is None:
        return web_chat_origin
    return f"{web_chat_origin}/?{urlencode({_WORKSTATION_PARAM: workstation})}"


def is_safe_external_url(url: str) -> bool:
    """True only for http(s) URLs with a host: the one kind the system browser should get.

    A chat reply can carry ``file:``, ``smb:``, ``javascript:`` or app-handler links;
    handing those to the OS would open files, mount shares or launch applications.

    The URL must also be exactly what would be opened and read as it is shown: printable
    ASCII only (``urlsplit`` drops whitespace and controls, and bidi overrides reorder
    how a link reads; a real international host arrives as xn--), and no user name, which can make
    ``https://trusted.example@evil.example/`` read as the trusted site.
    """
    if not isinstance(url, str) or not all(0x21 <= ord(c) <= 0x7E for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    return origin_of(url) is not None


def is_web_chat_url(page_url: str, web_chat_origin: str) -> bool:
    """True when the page Qt reports is on web-chat's own origin.

    ``page_url`` comes from Qt, not from the page, so a site the panel navigated to
    cannot claim to be web-chat: only web-chat may open windows by script or start a download.
    """
    page = origin_of(page_url)
    return page is not None and page == origin_of(web_chat_origin)


def is_web_chat_login(url: str, web_chat_origin: str) -> bool:
    """True when ``url`` is web-chat's own ``/login`` route (where an ended session lands)."""
    if not is_web_chat_url(url, web_chat_origin):
        return False
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return False
    return path in ("/login", "/login/")


def oidc_route(url: str) -> str | None:
    """The route under ``/api/auth/oidc/`` an http(s) URL on any origin names (``"login"``, ``"callback"``), else None.

    The path is read the way a server might: percent-decoded until it settles, lower-cased,
    backslashes as slashes, repeated slashes collapsed, ``.`` and ``..`` resolved. ``""`` is an
    OIDC route with no name: the bare root, a path under the root only before ``..`` is resolved,
    or a path still changing after ``_MAX_DECODES`` rounds of decoding, which could hide anything.
    """
    if origin_of(url) is None:
        return None
    path = urlsplit(url.strip()).path
    for _ in range(_MAX_DECODES):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        if unquote(path) != path:
            return ""
    as_sent = _SLASHES.sub("/", path.replace("\\", "/").lower())
    resolved = _route_under_root(posixpath.normpath(as_sent))
    if resolved is not None:
        return resolved
    # Under the root only until ``..`` is resolved (``/api/auth/oidc/login/../../x``): a server
    # that routes before resolving would still serve it, so it is an OIDC route with no name.
    return None if _route_under_root(as_sent) is None else ""


def _route_under_root(path: str) -> str | None:
    if path == _OIDC_ROOT:
        return ""
    if not path.startswith(_OIDC_ROOT + "/"):
        return None
    return path[len(_OIDC_ROOT) + 1 :].split("/", 1)[0]


def main_frame_navigation(url: str, web_chat_origin: str, clicked: bool) -> Navigation:
    """Where a main-frame navigation goes.

    First, on any origin, clicked or not: the OIDC login is a ``SIGN_IN`` the panel runs in the
    system browser, and every other OIDC route (the callback) is refused, so no sign-in completes
    in the panel. web-chat (and its own ``blob:`` URLs) stays in the panel. Clicked links to other web
    sites open in the system browser. Other web sites reached without a click are refused too
    (``LEAVE_REFUSED``): the panel signs in with its own session, so such a navigation is
    web-chat's session ending, as its sign-out redirect to the identity provider. Every
    other scheme is refused.
    """
    route = oidc_route(url)
    if route is not None:
        return Navigation.SIGN_IN if route == _OIDC_LOGIN else Navigation.REFUSE
    if is_web_chat_url(_unwrap_blob(url), web_chat_origin):
        return Navigation.STAY
    if not is_safe_external_url(url):
        return Navigation.REFUSE
    return Navigation.OPEN_EXTERNALLY if clicked else Navigation.LEAVE_REFUSED


def subframe_navigation_allowed(url: str, web_chat_origin: str) -> bool:
    """True for frames loading a web page, ``about:blank``/``about:srcdoc``, or web-chat's blobs.

    Anything else (``file:``, ``smb:``, ``data:``, app handlers) is refused: QtWebEngine
    hands unknown schemes from any frame to the OS protocol handler. No OIDC route loads in a frame.
    """
    if oidc_route(url) is not None:
        return False
    if url.strip().lower() in _FRAME_ABOUT_URLS:
        return True
    if url.strip().lower().startswith("blob:"):
        return is_web_chat_blob(url, web_chat_origin)
    return is_safe_external_url(url)


def is_web_chat_blob(url: str, web_chat_origin: str) -> bool:
    """True for a ``blob:`` URL that web-chat's own origin created."""
    return url.strip().lower().startswith("blob:") and is_web_chat_url(_unwrap_blob(url.strip()), web_chat_origin)


def is_oidc_login(url: str) -> bool:
    """True when ``url`` is the OIDC login, in any spelling, on any origin."""
    return oidc_route(url) == _OIDC_LOGIN


def _unwrap_blob(url: str) -> str:
    scheme, _, rest = url.partition(":")
    return rest if scheme.lower() == "blob" else url
