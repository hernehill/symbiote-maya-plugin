"""The panel's two tool switches, the studio's tiers they combine with, and open dialogs.

The server says which tiers the studio allows (``GET /microagent/workstation-tiers``); the
artist's switches can only narrow that. Stdlib only: the panel wraps the fetch in a thread.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from symbiote_dcc.mcp_protocol import READ, AllowedTiers
from symbiote_dcc.net import BadReply, Redirected, Unreachable, get_json
from symbiote_dcc.settings import config_dir

logger = logging.getLogger(__name__)

TIERS_PATH = "/microagent/workstation-tiers"
IMPORT = "import"
WRITE = "write"
UNSUPPORTED_STATUS = "this server doesn't support tool switches yet; read tools only"
UNREACHABLE_STATUS = "couldn't reach the server for tool switches; read tools only"
STALE_SIGN_IN_STATUS = "the server didn't accept the sign-in yet; read tools only"
TOOLS_STOPPED_STATUS = "tools are stopped; read tools only"
STUDIO_OFF = "turned off for this studio"
_NOT_FOUND = 404
_UNAUTHORIZED = 401
# A 401 means the panel read an access token the microagent is about to refresh (only the microagent
# refreshes the stored token now, so the fetch runs only while it does). Each try re-reads the stored
# token: up to 5 tries, 2 s apart, before the backoff below; a 401 past them asks whether the CLI session
# still lives.
STALE_SIGN_IN_RETRY_SECONDS = 2
STALE_SIGN_IN_RETRIES = 5
# A failed fetch is tried again after 30 s, then twice as long each time, at most 5 minutes.
_FIRST_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 300


@dataclass(frozen=True)
class ToolSwitches:
    """The artist's switches: "Imports" and "Scene changes" (writes)."""

    imports: bool = True
    writes: bool = False


def effective_tiers(server: Mapping[str, object] | None, switches: ToolSwitches) -> frozenset[str]:
    """``read``, plus each tier both the studio (``True`` exactly) and the switch allow."""
    if server is None:
        return frozenset({READ})
    tiers = {READ}
    if server.get(IMPORT) is True and switches.imports:
        tiers.add(IMPORT)
    if server.get(WRITE) is True and switches.writes:
        tiers.add(WRITE)
    return frozenset(tiers)


def allowed_tiers(
    server: Mapping[str, object] | None, switches: ToolSwitches, *, fetching: bool = False
) -> AllowedTiers:
    """What the tool server serves, marking the tiers the studio allows but the artist switched off.

    ``fetching``: the first fetch of the studio's settings is on its way (only matters while
    ``server`` is unknown).
    """
    served = effective_tiers(server, switches)
    studio_on = set() if server is None else {tier for tier in (IMPORT, WRITE) if server.get(tier) is True}
    known = server is not None
    return AllowedTiers(served, frozenset(studio_on - served), studio_known=known, fetching=fetching and not known)


def switches_path(dcc: str) -> Path:
    return config_dir() / f"{dcc}-tools.json"


def load_switches(dcc: str) -> tuple[ToolSwitches, str | None]:
    """The saved switches, and a message when the file can't be used.

    A damaged file turns both switches off until the artist sets them again: the panel
    must not offer more than the artist last chose.
    """
    path = switches_path(dcc)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ToolSwitches(), None
    except (OSError, ValueError) as exc:
        return _damaged(path, type(exc).__name__)
    if not isinstance(raw, dict) or set(raw) != {"imports", "writes"}:
        return _damaged(path, "it doesn't hold exactly imports and writes")
    if not all(isinstance(raw[key], bool) for key in ("imports", "writes")):
        return _damaged(path, "a switch isn't true or false")
    return ToolSwitches(imports=raw["imports"], writes=raw["writes"]), None


def _damaged(path: Path, why: str) -> tuple[ToolSwitches, str]:
    message = f"Couldn't read {path} ({why}); imports and scene changes are off until you switch them on."
    return ToolSwitches(imports=False, writes=False), message


def save_switches(dcc: str, switches: ToolSwitches) -> None:
    """Write the switches whole: a crash mid-write leaves the old file, never half a file."""
    path = switches_path(dcc)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=f".{dcc}-tools-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(json.dumps(asdict(switches), indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class TiersFetch:
    """One fetch's outcome: the studio's tiers (``None`` when unknown), the status to show,
    whether to try again, and whether the server refused the token (401) rather than failed."""

    server: Mapping[str, bool] | None
    status: str
    retry: bool
    stale_sign_in: bool = False


def _unreachable(why: str) -> TiersFetch:
    logger.warning("Symbiote: couldn't fetch the studio's tool tiers: %s", why)
    return TiersFetch(None, UNREACHABLE_STATUS, True)


def _stale_sign_in() -> TiersFetch:
    logger.warning("Symbiote: the server refused the stored sign-in for the tool tiers (HTTP 401); retrying")
    return TiersFetch(None, STALE_SIGN_IN_STATUS, True, stale_sign_in=True)


def fetch_server_tiers(server: str, profile: str, access_token: str, timeout: float) -> TiersFetch:
    """Ask ``server`` which tiers the studio allows. Blocks: never call it on the DCC's main thread."""
    try:
        status, body = get_json(server + TIERS_PATH, timeout, profile, access_token)
    except BadReply as exc:
        if exc.status == _NOT_FOUND:
            return TiersFetch(None, UNSUPPORTED_STATUS, False)
        if exc.status == _UNAUTHORIZED:
            return _stale_sign_in()
        return _unreachable(f"HTTP {exc.status}, {exc.content_type}")
    except Unreachable as exc:
        return _unreachable(exc.reason)
    except Redirected as exc:
        return _unreachable(f"HTTP {exc.status} redirect to {exc.host}")
    if status == _NOT_FOUND:
        return TiersFetch(None, UNSUPPORTED_STATUS, False)
    if status == _UNAUTHORIZED:
        return _stale_sign_in()
    if status != 200:
        return _unreachable(f"HTTP {status}")
    if not all(isinstance(body.get(tier), bool) for tier in (IMPORT, WRITE)):
        return _unreachable("the reply doesn't say true or false for import and write")
    return TiersFetch({IMPORT: body[IMPORT], WRITE: body[WRITE]}, "", False)


def retry_delay_seconds(failures_before: int) -> int:
    """Seconds to wait before retry number ``failures_before + 1``."""
    delay: int = _FIRST_RETRY_SECONDS << min(failures_before, 8)
    return min(delay, _MAX_RETRY_SECONDS)


class OpenDialogs:
    """Counts the confirmation dialogs on screen, so a microagent restart can wait for them.

    A restart would cut off the tool call that is waiting on the artist's answer. The panel
    polls ``is_open`` from its restart timer; nothing is called back when a dialog closes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = 0

    def is_open(self) -> bool:
        with self._lock:
            return self._open > 0

    @contextmanager
    def showing(self) -> Iterator[None]:
        """Wrap a dialog's ``exec`` on the DCC's main thread, where the panel's restart runs.

        Only main-thread dialogs: the restart checks ``is_open`` on that thread, so a dialog
        counted from another thread could open between the check and the restart.
        """
        with self._lock:
            self._open += 1
        try:
            yield
        finally:
            with self._lock:
                self._open -= 1


DIALOGS = OpenDialogs()
