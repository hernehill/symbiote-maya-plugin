"""JSON over ``urllib``, verified like ``requests`` and the CLI.

Maya's Python has no ``requests``, and neither mayapy nor hython verifies HTTPS with
the default context (CERTIFICATE_VERIFY_FAILED), so sign-in uses ``urllib`` with a
context built on ``certifi``'s bundle. Verification is never turned off: a failure is
``Unreachable``.
"""

from __future__ import annotations

import base64
import binascii
import errno
import http.client
import json
import logging
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from symbiote_dcc import host_ssl
from symbiote_dcc.settings import config_dir

if TYPE_CHECKING:
    import ssl
else:
    ssl = host_ssl.require_ssl()  # a host whose Python can't load ssl fails here, with the reason

# requests honoured REQUESTS_CA_BUNDLE and CURL_CA_BUNDLE; each adds to certifi here.
_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
logger = logging.getLogger(__name__)
# A DCC may set these to a path that is not there (Maya 2027's GUI does). Each maps to whether its value names
# the kind of path the variable needs. The panel's HTTPS and the microagent's environment both drop a dead one.
CA_PATH_VARIABLES: Mapping[str, Callable[[str], bool]] = {
    "SSL_CERT_FILE": os.path.isfile,
    "SSL_CERT_DIR": os.path.isdir,
}
_warned_dead: set[tuple[str, str]] = set()  # each dead path is named once per process, not on every fetch


def dead_ca_paths(environment: Mapping[str, str]) -> dict[str, str]:
    """The CA path variables in ``environment`` whose value names nothing; an empty value is unset, not dead."""
    dead = {}
    for name, exists in CA_PATH_VARIABLES.items():
        value = environment.get(name, "")
        if value and not exists(value):
            dead[name] = value
    return dead


# The CLI's own CA: it replaces the profile's ca_cert (symbiote-cli's resolve_ca_cert).
ENV_CA_CERT = "SYMBIOTE_CA_CERT"
_CA_ENV_VARS = (*_BUNDLE_ENV_VARS, ENV_CA_CERT)
MAX_REPLY_BYTES = 1024 * 1024


class Unreachable(Exception):
    """A connection or TLS failure; ``reason`` is written for the artist."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BadReply(Exception):
    """The server answered, but not with a JSON object."""

    def __init__(self, status: int, content_type: str, too_large: bool = False) -> None:
        super().__init__(f"HTTP {status}, {content_type}")
        self.status = status
        self.content_type = content_type
        self.too_large = too_large


class Redirected(Exception):
    """The server answered a sign-in POST with a redirect, which is never followed.

    ``host`` is the Location's host only: the query can carry a one-time code.
    """

    def __init__(self, status: int, host: str) -> None:
        super().__init__(f"HTTP {status} redirect to {host}")
        self.status = status
        self.host = host


def _load_certifi() -> Any | None:
    try:
        import certifi
    except ImportError:
        return None
    return certifi


def _load_tomllib() -> Any | None:
    try:
        import tomllib
    except ImportError:  # Python 3.9 and 3.10
        return None
    return tomllib


def _check_chain(start: Path, path: Path, source: str) -> None:
    """Every directory from ``start`` up to the home directory or the root must be
    ours (or root's) and not writable by others, as symbiote-cli's tls.py
    ``_check_not_writable_by_others`` requires. A sticky directory (/tmp) is fine:
    it only lets you replace your own entries."""
    uid = os.getuid()
    for target in [start, *start.parents]:
        try:
            st = target.stat()
        except OSError as exc:
            raise Unreachable(
                f"Cannot inspect {target} while checking the CA file {path} (from {source}): {exc}"
            ) from exc
        if st.st_uid not in (uid, 0):
            raise Unreachable(
                f"{target} is owned by uid {st.st_uid}, not you: refusing to trust {path} (from {source})"
            )
        sticky_dir = stat.S_ISDIR(st.st_mode) and (st.st_mode & stat.S_ISVTX)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not sticky_dir:
            raise Unreachable(
                f"{target} is writable by other users: refusing to trust {path} (from {source}). "
                f"Fix with: chmod go-w {target}"
            )
        if target == Path.home() or target == target.parent:
            break


_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]*?-----END CERTIFICATE-----")
_BEGIN = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----")


def _certificate_blocks(pem: bytes, path: Path, source: str) -> list[str]:
    """Every ``CERTIFICATE`` block in the file, as ASCII text; refuse anything else.

    Text outside the blocks (comments, UTF-8 labels as in p11-kit bundles) is ignored.
    Any other block type (TRUSTED CERTIFICATE, a key) is refused, because loading
    through ``cadata`` would silently drop it.
    """
    if b"PRIVATE KEY" in pem:
        raise Unreachable(
            f"{path} contains a PRIVATE KEY (from {source}). This looks like a server keypair, not a CA bundle."
        )
    begins = _BEGIN.findall(pem)
    for kind in begins:
        if kind != b"CERTIFICATE":
            raise Unreachable(
                f"{path} (from {source}) contains a {kind.decode('ascii')} block; only CERTIFICATE blocks are supported"
            )
    blocks = [block.decode("ascii") for block in _BLOCK.findall(pem)]
    if not blocks:
        raise Unreachable(f"{path} contains no certificate (from {source})")
    if len(blocks) != len(begins):
        raise Unreachable(f"{path} (from {source}) has a malformed CERTIFICATE block")
    return blocks


def _read_ca_file(value: str, source: str) -> tuple[str, Path, list[str]]:
    """The certificate blocks of one extra CA file, after the CLI's file refusals.

    ``_load_blocks`` then makes the CLI's is-a-CA check. The CLI also re-serialises each
    certificate through ``cryptography``, which DCC Pythons don't ship; that alone isn't ported.
    """
    raw = Path(value.strip()).expanduser()
    try:
        path = raw.resolve()
        st = os.stat(path)
    except FileNotFoundError as exc:
        raise Unreachable(f"CA certificate not found: {raw} (from {source})") from exc
    except NotADirectoryError as exc:
        raise Unreachable(f"CA certificate not found: {raw} (from {source})") from exc
    except RuntimeError as exc:  # a symlink loop, before Python 3.13
        raise Unreachable(f"CA certificate path {raw} loops through symlinks (from {source})") from exc
    except OSError as exc:
        raise Unreachable(
            f"cannot inspect {raw} while validating the CA (from {source}): "
            f"{errno.errorcode.get(exc.errno or 0, type(exc).__name__)}"
        ) from exc
    if not stat.S_ISREG(st.st_mode):
        raise Unreachable(f"CA certificate is not a regular file: {path} (from {source})")
    if os.name == "posix":  # the CLI skips this on Windows too: no ACL check
        _check_chain(path, path, source)
        _check_chain(raw, path, source)
    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise Unreachable(f"CA certificate unreadable: {path} (from {source}): {exc.strerror}") from exc
    return source, path, _certificate_blocks(pem, path, source)


def _der(block: str, path: Path, source: str) -> bytes:
    body = "".join(block.split("-----")[2].split())
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Unreachable(f"{path} (from {source}) has a malformed CERTIFICATE block") from exc


def _unique(blocks: list[str], path: Path, source: str) -> list[str]:
    """One block per certificate: duplicates match on their DER bytes, not their wrapping."""
    by_der: dict[bytes, str] = {}
    for block in blocks:
        by_der.setdefault(_der(block, path, source), block)
    return list(by_der.values())


def _load_blocks(context: ssl.SSLContext, source: str, path: Path, blocks: list[str]) -> None:
    """Trust every block of one file, or refuse naming the file.

    The blocks are first loaded into an empty context and counted, so a block OpenSSL
    drops is noticed even when certifi already holds a duplicate of it, and a block that
    is not a CA certificate (a server's own certificate) is refused, as the CLI does.
    """
    unique = _unique(blocks, path, source)
    cadata = "".join(block + "\n" for block in unique)
    try:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe.load_verify_locations(cadata=cadata)
        stats = probe.cert_store_stats()
        context.load_verify_locations(cadata=cadata)
    except ssl.SSLError as exc:
        raise Unreachable(f"CA certificate could not be loaded: {path} (from {source}): {type(exc).__name__}") from exc
    if stats["x509"] != len(unique):
        raise Unreachable(
            f"Only {stats['x509']} of {len(unique)} certificates in {path} (from {source}) could be loaded"
        )
    if stats["x509_ca"] != stats["x509"]:
        raise Unreachable(
            f"{path} (from {source}) holds a certificate that is not a CA. "
            f"Use the issuing CA, not the server's own certificate."
        )


def _profile_ca_cert(profile: str) -> str:
    """The profile's ``ca_cert`` from ``config.toml``, as ``symbiote-cli`` reads it
    (``[profile]`` section, key ``ca_cert``; client/config.py)."""
    path = config_dir() / "config.toml"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise Unreachable(f"Can't read {path}: {exc.strerror or type(exc).__name__}") from exc
    tomllib = _load_tomllib()
    if tomllib is None:
        if b"ca_cert" in raw:
            raise Unreachable("This Python can't read config.toml; set SYMBIOTE_CA_CERT")
        return ""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unreachable(f"{path} is not valid TOML ({type(exc).__name__})") from exc
    section = data.get(profile, {})
    value = section.get("ca_cert", "") if isinstance(section, dict) else ""
    return value.strip() if isinstance(value, str) else ""


def _cli_ca_cert(profile: str) -> tuple[str, str]:
    """``(source, value)`` of the CLI's CA, by its precedence: ``SYMBIOTE_CA_CERT`` replaces
    the profile's ``ca_cert``, and when it is set ``config.toml`` is not read at all."""
    override = os.environ.get(ENV_CA_CERT, "").strip()
    if override:
        return ENV_CA_CERT, override
    return f"profile '{profile}'", _profile_ca_cert(profile)


def tls_context(profile: str) -> ssl.SSLContext:
    """A verifying context: certifi's bundle, plus each CA bundle variable, plus the CLI's CA."""
    certifi = _load_certifi()
    if certifi is None:
        raise Unreachable("No CA bundle: this Python has no certifi")
    dead = dead_ca_paths(os.environ)
    _warn_dead(dead)
    sources: list[tuple[str, str]] = [(name, os.environ.get(name, "")) for name in _BUNDLE_ENV_VARS if name not in dead]
    sources.append(_cli_ca_cert(profile))
    files = [_read_ca_file(value, source) for source, value in sources if value.strip()]
    context = ssl.create_default_context(cafile=certifi.where())
    for source, path, blocks in files:
        _load_blocks(context, source, path, blocks)
    return context


def _warn_dead(dead: Mapping[str, str]) -> None:
    for name, value in dead.items():
        if (name, value) not in _warned_dead:
            _warned_dead.add((name, value))
            logger.warning(
                "Symbiote: this application (%s) set %s to %s, which does not exist; HTTPS uses the other CAs",
                os.path.basename(sys.executable) or "the DCC",
                name,
                value,
            )


_UNREADABLE_LOCATION = "an unreadable location"


def _location_host(location: str) -> str:
    """The Location's host, never its query; the refusal must not depend on parsing it."""
    try:
        return urllib.parse.urlsplit(location).hostname or "(the same host)"
    except ValueError:
        return _UNREADABLE_LOCATION


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001
        host = _location_host(headers.get("Location", ""))
        fp.close()  # Python 3.9 leaves the 3xx response open when this raises
        raise Redirected(code, host)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _open(request: urllib.request.Request, timeout: float, context: ssl.SSLContext | None):
    handlers: list[Any] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def _read_capped(response: Any, status: int, content_type: str) -> bytes:
    body: bytes = response.read(MAX_REPLY_BYTES + 1)  # an untyped urllib response
    if len(body) > MAX_REPLY_BYTES:
        raise BadReply(status, content_type, too_large=True)
    return body


def _parse(status: int, content_type: str, body: bytes) -> tuple[int, dict[str, Any]]:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadReply(status, content_type) from exc
    if not isinstance(parsed, dict):
        raise BadReply(status, content_type)
    return status, parsed


def _exchange(request: urllib.request.Request, timeout: float, context: ssl.SSLContext | None):
    try:
        with _open(request, timeout, context) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.status, content_type, _read_capped(response, response.status, content_type)
    except urllib.error.HTTPError as exc:
        content_type = exc.headers.get("Content-Type", "")
        try:
            if 300 <= exc.code < 400:
                raise Redirected(exc.code, _location_host(exc.headers.get("Location", ""))) from exc
            return exc.code, content_type, _read_capped(exc, exc.code, content_type)
        finally:
            exc.close()


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    profile: str,
    context: ssl.SSLContext | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST ``payload`` as JSON. Returns ``(status, body)`` for every status.

    HTTPS is verified with ``context``, or ``tls_context(profile)`` when none is given
    (a sign-in builds one and reuses it). Redirects are never followed. ``headers`` are
    sent besides the JSON content type (a panel session names the DCC in ``User-Agent``).
    """
    return _request_json(
        url,
        lambda: urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={**(headers or {}), "Content-Type": "application/json"},
            method="POST",
        ),
        timeout,
        profile,
        context,
    )


def get_json(url: str, timeout: float, profile: str, bearer: str) -> tuple[int, dict[str, Any]]:
    """GET ``url`` with ``bearer`` as the Authorization token. Returns ``(status, body)`` for every status.

    HTTPS is verified with ``tls_context(profile)``, as a sign-in is. Redirects are never followed.
    """
    return _request_json(
        url,
        lambda: urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"}, method="GET"),
        timeout,
        profile,
        None,
    )


def _request_json(
    url: str,
    build: Callable[[], urllib.request.Request],
    timeout: float,
    profile: str,
    context: ssl.SSLContext | None,
) -> tuple[int, dict[str, Any]]:
    try:
        request = build()
        if context is None and url.lower().startswith("https:"):
            context = tls_context(profile)
        status, content_type, body = _exchange(request, timeout, context)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise Unreachable(reason if isinstance(reason, str) else type(reason).__name__) from exc
    except http.client.HTTPException as exc:
        raise Unreachable(f"{type(exc).__name__}: the connection broke mid-reply") from exc
    except ValueError as exc:
        raise Unreachable(f"The address {url.split('?')[0]!r} is not valid") from exc
    return _parse(status, content_type, body)
