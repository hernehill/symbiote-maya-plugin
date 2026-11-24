"""Panel settings: which server and CLI profile a DCC's panel signs in to."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from symbiote_dcc.url_policy import NON_ASCII_HOST

DEFAULT_SERVER = "http://localhost:4602"
_AGENT_PORT = 4602
_WEB_CHAT_PORT = 3000
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class PanelSettings:
    server: str
    profile: str


def config_dir() -> Path:
    """The directory the CLI keeps its credentials in."""
    override = os.environ.get("SYMBIOTE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".symbiote"


def validated(settings: PanelSettings) -> PanelSettings:
    """Return normalized settings, or raise ``ValueError`` with a message for the artist."""
    server = settings.server.strip().rstrip("/")
    parts = urlsplit(server)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Server must look like https://symbiote.example.com")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Server must not contain a user name")
    if not parts.hostname.isascii():
        raise ValueError(NON_ASCII_HOST)
    profile = settings.profile.strip()
    if not _PROFILE_PATTERN.match(profile):
        raise ValueError("Profile can only use letters, numbers, - and _")
    return PanelSettings(server=server, profile=profile)


def web_url_for(server: str) -> str:
    """Where web-chat lives for ``server``.

    A dev stack serves the agent API on 4602 and web-chat on 3000. Every other
    deployment serves both from one origin.
    """
    parts = urlsplit(server)
    if parts.port == _AGENT_PORT:
        return f"{parts.scheme}://{parts.hostname}:{_WEB_CHAT_PORT}"
    return f"{parts.scheme}://{parts.netloc}"


def settings_path(dcc: str) -> Path:
    return config_dir() / f"{dcc}-chat.json"


def default_settings(dcc: str) -> PanelSettings:
    return PanelSettings(server=DEFAULT_SERVER, profile=dcc)


def load_settings(dcc: str) -> PanelSettings:
    path = settings_path(dcc)
    if not path.exists():
        return default_settings(dcc)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validated(
        PanelSettings(
            server=raw.get("server", DEFAULT_SERVER),
            profile=raw.get("profile", dcc),
        )
    )


def load_panel_settings(dcc: str) -> tuple[PanelSettings, str | None]:
    """Settings for the panel to open with, and a message when the saved ones can't be used.

    A file saved before a rule tightened (a user name, a non-ASCII host) must not stop
    the pane opening: its values come back for the artist to correct on the login view,
    a password masked, with a message naming the value.
    """
    path = settings_path(dcc)
    try:
        return load_settings(dcc), None
    except json.JSONDecodeError:
        return default_settings(dcc), f"Couldn't read {path}; using the default server. Sign in to replace it."
    except ValueError as exc:
        raw = json.loads(path.read_text(encoding="utf-8"))
        server = _mask_password(str(raw.get("server", DEFAULT_SERVER)))
        shown = PanelSettings(server=server, profile=str(raw.get("profile", dcc)))
        return shown, f"The saved server {server} can't be used: {exc}. Correct it and sign in."


def _mask_password(server: str) -> str:
    parts = urlsplit(server)
    if parts.password is None:
        return server
    userinfo, _, host = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}:***@{host}"))


def save_settings(dcc: str, settings: PanelSettings) -> None:
    path = settings_path(dcc)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
