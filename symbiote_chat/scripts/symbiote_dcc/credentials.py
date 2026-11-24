"""Read and write the CLI's credentials file, so the panel stays signed in the same way.

The format matches ``symbiote_cli.client.auth.AuthManager``. A DCC's bundled Python has
no ``httpx``, so the CLI module cannot be imported here. Every write takes the lock the
CLI and the deploy tooling share (``credentials_lock``), only around read -> change -> write.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from symbiote_dcc.credentials_lock import credentials_lock, write_atomically

_LOGIN_REFRESH_TTL = timedelta(days=365)  # the CLI's CLI_SESSION_IDLE_SECONDS
_REQUIRED_FIELDS = ("server", "access_token", "refresh_token")
# Signing in can't repair a damaged file: it would have to drop the CLI's other sign-ins.
_UNREADABLE_FILE_HELP = "move the file aside (it also holds the CLI's sign-ins), then sign in again."


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str

    @property
    def expiry(self) -> int:
        """The access token's ``exp`` claim, 0 when it cannot be read."""
        exp = jwt_claims(self.access).get("exp")
        return int(exp) if isinstance(exp, (int, float)) else 0


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's claims without verifying it; the server verifies every use."""
    segments = token.split(".")
    if len(segments) != 3:
        return {}
    padded = segments[1] + "=" * (-len(segments[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def _user_from(claims: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "roles": claims.get("roles") or [],
        "name": claims.get("name") or claims.get("email", ""),
    }


def _iso_from_exp(claims: dict[str, Any]) -> str:
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _tokens_from(entry: dict[str, Any]) -> Tokens:
    return Tokens(access=entry["access_token"], refresh=entry["refresh_token"])


class CredentialStore:
    """Per-profile access to ``<config dir>/credentials``."""

    def __init__(self, directory: Path, owner: str) -> None:
        self._directory = directory
        self._owner = owner
        self._path = directory / "credentials"

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 2, "profiles": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"{self._path} is not valid JSON ({exc}); {_UNREADABLE_FILE_HELP}") from exc
        if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
            return raw
        raise ValueError(f"{self._path} is not a version 2 credentials file; {_UNREADABLE_FILE_HELP}")

    def _write(self, store: dict[str, Any]) -> None:
        """Replace the file; the caller holds ``credentials_lock``."""
        write_atomically(self._path, json.dumps(store, indent=2), prefix=f".creds-{self._owner}-")

    def entry(self, profile: str) -> dict[str, Any] | None:
        """The profile's sign-in, None when there is none.

        Raises ValueError naming the file and profile when the entry is not an object or
        lacks a string ``server``, ``access_token`` or ``refresh_token``: a broken sign-in
        must not pass for being signed out.
        """
        found = self._read()["profiles"].get(profile)
        if found is None:
            return None
        if isinstance(found, dict) and all(isinstance(found.get(k), str) for k in _REQUIRED_FIELDS):
            return found
        raise ValueError(f"{self._path} has a malformed entry for profile {profile!r}. Sign in again to replace it.")

    def tokens(self, profile: str) -> Tokens | None:
        raw = self.entry(profile)
        return None if raw is None else _tokens_from(raw)

    def tokens_for_server(self, profile: str, server: str) -> Tokens | None:
        """The profile's tokens when its sign-in is for ``server``, from one read of the file.

        Reading the entry and the tokens separately lets a CLI sign-in or sign-out land
        between the two reads, leaving an entry of None beside a set of tokens.
        """
        raw = self.entry(profile)
        if raw is None or raw["server"] != server:
            return None
        return _tokens_from(raw)

    @staticmethod
    def _login_entry(server: str, tokens: Tokens) -> dict[str, Any]:
        claims = jwt_claims(tokens.access)
        refresh_expires = datetime.now(timezone.utc) + _LOGIN_REFRESH_TTL
        return {
            "server": server,
            "access_token": tokens.access,
            "refresh_token": tokens.refresh,
            "user": _user_from(claims),
            "expires_at": _iso_from_exp(claims),
            "refresh_expires_at": refresh_expires.isoformat(),
        }

    def save_login(self, profile: str, server: str, tokens: Tokens) -> None:
        """Store a brand new sign-in for ``profile``, replacing any earlier one."""
        entry = self._login_entry(server, tokens)
        with credentials_lock(self._directory):
            store = self._read()
            self._write({**store, "version": 2, "profiles": {**store["profiles"], profile: entry}})

    def current_tokens(self, profile: str, server: str) -> Tokens | None:
        """The profile's tokens for ``server``, read under the lock and released before returning.

        None when there is no entry or it is for another server (a terminal sign-out, or a sign-in elsewhere).
        """
        with credentials_lock(self._directory):
            return self.tokens_for_server(profile, server)

    def clear_if_holds(self, profile: str, refresh_token: str) -> bool:
        """Remove the profile only while it still holds ``refresh_token``; True when it was removed."""
        with credentials_lock(self._directory):
            store = self._read()
            current = store["profiles"].get(profile)
            if not isinstance(current, dict) or current.get("refresh_token") != refresh_token:
                return False
            remaining = {k: v for k, v in store["profiles"].items() if k != profile}
            self._write({**store, "profiles": remaining})
            return True

    @staticmethod
    def _may_replace(current: Any, expected_refresh: str | None, replace_malformed: bool) -> bool:
        well_formed = isinstance(current, dict) and all(isinstance(current.get(k), str) for k in _REQUIRED_FIELDS)
        if replace_malformed and not well_formed:
            return True
        return isinstance(current, dict) and current.get("refresh_token") == expected_refresh

    def save_login_if(
        self,
        profile: str,
        server: str,
        tokens: Tokens,
        expected_refresh: str | None,
        *,
        replace_malformed: bool = False,
    ) -> bool:
        """Save a sign-in only when the profile is empty or still holds ``expected_refresh``.

        ``replace_malformed``: the caller saw a malformed entry, so one still malformed is replaced
        too. A well-formed entry other than ``expected_refresh`` means something signed in since.
        Returns False, writing nothing, when something else signed in since the caller read the file.
        """
        entry = self._login_entry(server, tokens)
        with credentials_lock(self._directory):
            store = self._read()
            current = store["profiles"].get(profile)
            if current is not None and not self._may_replace(current, expected_refresh, replace_malformed):
                return False
            self._write({**store, "version": 2, "profiles": {**store["profiles"], profile: entry}})
            return True

    def user_email(self, profile: str) -> str:
        """The signed-in person's email: the entry's, else the access token's; "" when there is none."""
        with credentials_lock(self._directory):
            raw = self.entry(profile)
        if raw is None:
            return ""
        user = raw.get("user")
        email = user.get("email") if isinstance(user, dict) else None
        if isinstance(email, str) and email:
            return email
        claim = jwt_claims(raw["access_token"]).get("email")
        return claim if isinstance(claim, str) else ""

    def clear(self, profile: str) -> None:
        with credentials_lock(self._directory):
            store = self._read()
            remaining = {k: v for k, v in store["profiles"].items() if k != profile}
            self._write({**store, "profiles": remaining})
