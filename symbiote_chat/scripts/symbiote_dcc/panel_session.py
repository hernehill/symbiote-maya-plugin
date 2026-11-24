"""The panel's own sign-in: mint, check, list and revoke sessions on the server, Qt-free.

A panel session is a child of the CLI session (``POST /api/auth/sessions/panel``). Every reply
is mapped to an ``Outcome`` by the server's error CODE, never by the status alone: a 401 can
mean "ended" or "superseded" (another process rotated the token a moment ago), and the two need
opposite handling. A reply that is not JSON (a captive portal, an auth proxy), or a JSON 401 that
isn't the server's ``{"success": false, "error": ...}`` refusal, is never success and never "ended".

Rule for every function that takes ``read_cli``: it calls ``read_cli()`` itself, immediately
before the request, and never takes a token value from its caller, so the CLI's rotation between
two calls can't leave a stale token in flight. ``read_cli()`` returning None (no entry, or one for
another server, for example after a terminal ``symbiote logout``) is ``ENDED``, with no request.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from symbiote_dcc import net
from symbiote_dcc.adapter import DccAdapter
from symbiote_dcc.credentials import Tokens

# Mirrors the default of the server's SESSION_REUSE_GRACE_SECONDS (agent_mcp_server/settings.py):
# a change to that grace window changes this wait too.
SUPERSEDED_WAIT_SECONDS = 30
SUPERSEDED_ERROR = "refresh_token_superseded"
PARENT_NOT_CLI_ERROR = "panel_session_parent_not_cli"
READ_ONLY_ERROR = "platform_read_only"
SESSION_NOT_FOUND_ERROR = "session_not_found"

_PANEL_PATH = "/api/auth/sessions/panel"
_CHECK_PATH = "/api/auth/sessions/current/check"
_REVOKE_PATH = "/api/auth/sessions/current/revoke"
_LIST_PATH = "/api/auth/sessions"
_DEFAULT_TIMEOUT = 15.0


class Outcome(Enum):
    OK = "ok"
    ENDED = "ended"
    SUPERSEDED = "superseded"
    PARENT_NOT_CLI = "parent_not_cli"
    READ_ONLY = "read_only"
    SERVER_TOO_OLD = "server_too_old"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SessionAnswer:
    outcome: Outcome
    # repr=False: an answer that reaches a log or an exception message never shows a token.
    tokens: Tokens | None = field(default=None, repr=False)
    family_id: str | None = None
    # The CLI refresh token the call presented, kept for compare-and-clear.
    presented: str | None = field(default=None, repr=False)
    rows: tuple[Mapping[str, Any], ...] = ()
    detail: str = ""
    # A local problem the call met after the server answered (the sign-out's file couldn't be cleared).
    problem: str = ""


ReadCli = Callable[[], Optional[Tokens]]


def outcome_for(status: int, body: Mapping[str, Any]) -> Outcome:
    """The outcome of a JSON reply, by the server's error code."""
    if status in (200, 204):
        return Outcome.OK
    code = body.get("error")
    if status == 401 and code == SUPERSEDED_ERROR:
        return Outcome.SUPERSEDED
    if status == 401 and isinstance(code, str) and body.get("success") is False:
        # Every refusal the server's session routes send is {"success": false, "error": ...}. A
        # JSON 401 without it (an authenticating proxy in front) says nothing about the session.
        return Outcome.ENDED
    if status == 403 and code == PARENT_NOT_CLI_ERROR:
        return Outcome.PARENT_NOT_CLI
    if status == 403 and code == READ_ONLY_ERROR:
        return Outcome.READ_ONLY
    if status in (404, 405):
        return Outcome.SERVER_TOO_OLD
    return Outcome.UNAVAILABLE


def _error_detail(status: int, body: Mapping[str, Any]) -> str:
    code = body.get("error")
    return f"HTTP {status}: {code}" if isinstance(code, str) else f"HTTP {status}"


def _failed(
    error: net.Unreachable | net.BadReply | net.Redirected, presented: str | None, *, empty_ok: bool
) -> SessionAnswer:
    """The answer for a request that raised: transport trouble is UNAVAILABLE, a non-JSON reply by status."""
    if isinstance(error, net.BadReply):
        if error.status == 204 and empty_ok:
            return SessionAnswer(Outcome.OK, presented=presented)
        outcome = Outcome.SERVER_TOO_OLD if error.status in (404, 405) else Outcome.UNAVAILABLE
        return SessionAnswer(outcome, presented=presented, detail=str(error))
    if isinstance(error, net.Redirected):
        return SessionAnswer(Outcome.UNAVAILABLE, presented=presented, detail=f"redirected to {error.host}")
    return SessionAnswer(Outcome.UNAVAILABLE, presented=presented, detail=error.reason)


def _ended() -> SessionAnswer:
    return SessionAnswer(Outcome.ENDED, presented=None)


def _post(
    url: str,
    payload: Mapping[str, Any],
    profile: str,
    timeout: float,
    presented: str,
    *,
    headers: Mapping[str, str] | None = None,
    empty_ok: bool = False,
) -> tuple[SessionAnswer, Mapping[str, Any]]:
    """POST and map the reply. Returns the answer and the JSON body (empty when there was none)."""
    try:
        status, body = net.post_json(url, dict(payload), timeout, profile, headers=headers or {})
    except (net.Unreachable, net.BadReply, net.Redirected) as error:
        return _failed(error, presented, empty_ok=empty_ok), {}
    outcome = outcome_for(status, body)
    return SessionAnswer(outcome, presented=presented, detail=_error_detail(status, body)), body


def _bearer_answer(answer: SessionAnswer) -> SessionAnswer:
    """A call made with the CLI ACCESS token never says the session ended.

    That token lives minutes, and its 401 means nothing about the refresh session; the CLI owns rotation,
    so the caller skips this time. Never carries a presented refresh token either.
    """
    if answer.outcome is Outcome.ENDED:
        return SessionAnswer(Outcome.UNAVAILABLE, detail="cli access token expired")
    return SessionAnswer(answer.outcome, detail=answer.detail)


def mint_panel_session(
    server: str, profile: str, read_cli: ReadCli, user_agent: str, timeout: float = _DEFAULT_TIMEOUT
) -> SessionAnswer:
    """Start a panel session, a child of the CLI's, presenting the CLI refresh token (never rotated)."""
    cli = read_cli()
    if cli is None:
        return _ended()
    answer, body = _post(
        f"{server}{_PANEL_PATH}",
        {"refresh_token": cli.refresh},
        profile,
        timeout,
        cli.refresh,
        headers={"User-Agent": user_agent},
    )
    if answer.outcome is not Outcome.OK:
        return answer
    access, refresh, family = body.get("access_token"), body.get("refresh_token"), body.get("family_id")
    if not (isinstance(access, str) and isinstance(refresh, str) and isinstance(family, str)):
        return SessionAnswer(Outcome.UNAVAILABLE, presented=cli.refresh, detail="the reply carried no session")
    return SessionAnswer(Outcome.OK, tokens=Tokens(access, refresh), family_id=family, presented=cli.refresh)


def check_cli_session(server: str, profile: str, read_cli: ReadCli, timeout: float = _DEFAULT_TIMEOUT) -> SessionAnswer:
    """Whether the CLI session is still live (204), without issuing anything."""
    cli = read_cli()
    if cli is None:
        return _ended()
    answer, _ = _post(
        f"{server}{_CHECK_PATH}", {"refresh_token": cli.refresh}, profile, timeout, cli.refresh, empty_ok=True
    )
    return answer


def revoke_cli_session(server: str, profile: str, read_cli: ReadCli, timeout: float) -> SessionAnswer:
    """End the CLI session the file holds now."""
    cli = read_cli()
    if cli is None:
        return _ended()
    answer, _ = _post(
        f"{server}{_REVOKE_PATH}", {"refresh_token": cli.refresh}, profile, timeout, cli.refresh, empty_ok=True
    )
    return answer


def revoke_session(server: str, profile: str, refresh: str, timeout: float) -> SessionAnswer:
    """End the session ``refresh`` belongs to.

    Only for a refresh token the panel holds in memory and never stored: the chat's, read once from
    the page, or the CLI pair of a panel login that lost to the terminal.
    """
    answer, _ = _post(f"{server}{_REVOKE_PATH}", {"refresh_token": refresh}, profile, timeout, refresh, empty_ok=True)
    return answer


def list_sessions(server: str, profile: str, read_cli: ReadCli, timeout: float = _DEFAULT_TIMEOUT) -> SessionAnswer:
    """The person's live sessions, one row each, from the CLI access token read now."""
    cli = read_cli()
    if cli is None:
        return _ended()
    try:
        status, body = net.get_json(f"{server}{_LIST_PATH}", timeout, profile, cli.access)
    except (net.Unreachable, net.BadReply, net.Redirected) as error:
        return _failed(error, None, empty_ok=False)
    answer = _bearer_answer(SessionAnswer(outcome_for(status, body), detail=_error_detail(status, body)))
    if answer.outcome is not Outcome.OK:
        return answer
    rows = body.get("sessions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return SessionAnswer(Outcome.UNAVAILABLE, detail="the session list was malformed")
    return SessionAnswer(Outcome.OK, rows=tuple(rows))


def revoke_family(server: str, profile: str, family_id: str, read_cli: ReadCli, timeout: float) -> SessionAnswer:
    """End one of the person's sessions by id, with the CLI access token read now.

    ``session_not_found`` means it is gone already (OK); any other 404 is an old server.
    """
    try:
        family = str(uuid.UUID(family_id))
    except (ValueError, AttributeError, TypeError):
        return SessionAnswer(Outcome.UNAVAILABLE, detail="the session id is not a UUID")
    cli = read_cli()
    if cli is None:
        return _ended()
    answer, body = _post(
        f"{server}{_LIST_PATH}/{family}/revoke",
        {},
        profile,
        timeout,
        "",
        headers={"Authorization": f"Bearer {cli.access}"},
    )
    if answer.outcome is Outcome.SERVER_TOO_OLD and body.get("error") == SESSION_NOT_FOUND_ERROR:
        return SessionAnswer(Outcome.OK, detail=answer.detail)
    return _bearer_answer(answer)


def with_superseded_retry(
    call: Callable[[], SessionAnswer],
    read_cli: ReadCli,
    *,
    wait_seconds: float = SUPERSEDED_WAIT_SECONDS,
    poll_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SessionAnswer:
    """Run ``call``; on SUPERSEDED wait for the credentials file to change, then retry once.

    Another process rotated the token a moment ago and is about to write the new one. No change
    inside the window is UNAVAILABLE, never ENDED: the session is still alive.
    """
    answer = call()
    if answer.outcome is not Outcome.SUPERSEDED:
        return answer
    deadline = clock() + wait_seconds
    while True:
        current = read_cli()
        if current is None:
            return _ended()
        if current.refresh != answer.presented:
            break
        if clock() >= deadline:
            return SessionAnswer(
                Outcome.UNAVAILABLE, presented=answer.presented, detail="the CLI's sign-in did not settle in time"
            )
        sleep(poll_seconds)
    retried = call()
    if retried.outcome is Outcome.SUPERSEDED:
        return SessionAnswer(Outcome.UNAVAILABLE, presented=retried.presented, detail="the sign-in kept changing")
    return retried


def panel_user_agent(adapter: DccAdapter) -> str:
    """``symbiote-dcc/<plugin version> (<DCC> <DCC version>)``.

    Call it once, on the DCC's main thread (Maya's ``cmds`` must run there), and hand workers the string.
    """
    plugin = adapter.version_label.split()[-1] if adapter.version_label.split() else ""
    try:
        host = adapter.host_version()
    except Exception:  # a DCC that can't say its version is left out, not fatal
        host = ""
    inside = " ".join(part for part in (adapter.ui.display_name, host) if part)
    return f"symbiote-dcc/{plugin} ({inside})"
