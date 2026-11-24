"""The panel's sign-in flow: a chat or the tools ending, the CLI check, the session-ended path, and sign-in.

A mixin ``SymbioteChatPanel`` inherits; the names declared on the class are what it needs from the panel.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from symbiote_dcc.chat_view import SIGN_IN_REQUESTED
from symbiote_dcc.consent import POLICY
from symbiote_dcc.credentials import Tokens
from symbiote_dcc.login_view import MODE_BROWSER, LoginWorker
from symbiote_dcc.microagent_runner import TERMINAL_STOP_EXIT
from symbiote_dcc.panel_session import (
    Outcome,
    SessionAnswer,
    check_cli_session,
    revoke_cli_session,
    revoke_session,
    with_superseded_retry,
)
from symbiote_dcc.panel_state import forget_record
from symbiote_dcc.session_notice import (
    ACTION_CONTINUE,
    ACTION_SIGN_OUT,
    ACTION_SIGN_OUT_RETRY,
    CONTINUE,
    CONTINUE_NO_EMAIL,
    OPENING,
    RETRY,
    SEED_LOOP_ERROR,
    SESSION_ENDED_BUT_SIGNED_IN,
    SIGN_OUT,
    SIGN_OUT_UNREACHABLE,
    Action,
)
from symbiote_dcc.settings import PanelSettings, save_settings, validated, web_url_for

if TYPE_CHECKING:
    from PySide6 import QtWidgets

    from symbiote_dcc.adapter import DccAdapter
    from symbiote_dcc.chat_view import ChatView
    from symbiote_dcc.credentials import CredentialStore
    from symbiote_dcc.login_view import LoginView
    from symbiote_dcc.microagent_runner import MicroagentRunner
    from symbiote_dcc.panel_state import PanelLease

_LOSING_LOGIN_REVOKE_SECONDS = 5.0
_SIGN_OUT_REVOKE_SECONDS = 10.0
logger = logging.getLogger("symbiote_dcc.panel.sign_in")  # the panel's sign-in log
# A chat that ends this soon after a good mint did not keep the session it was seeded with.
MINT_LOOP_SECONDS = 10
EXIT_78 = "microagent_exit_78"  # why the tools check runs (it is logged)
TIERS_UNAUTHORIZED = "tiers_unauthorized"


class PanelSignIn:
    """Sign-in, the session-ended path and sign-out, for ``SymbioteChatPanel``."""

    # The panel's, read and written here. The log is a child of the panel module's logger.
    _sign_in_log: logging.Logger
    _adapter: DccAdapter
    _store: CredentialStore
    _settings: PanelSettings
    _login: LoginView
    _stack: QtWidgets.QStackedLayout
    _status: QtWidgets.QLabel
    _microagent: MicroagentRunner
    _worker: LoginWorker | None
    _chat: ChatView | None
    _closed: bool
    _family_id: str | None
    _family_minted_for: tuple[str, str] | None
    _lease: PanelLease | None
    _clock: Callable[[], float]
    _minted_at: float | None
    _chat_generation: int
    _microagent_tried: bool
    _workstation: str | None
    _show_notice: Callable[..., None]
    _show_refusal: Callable[[SessionAnswer], None]
    _run_session_call: Callable[[Callable[[], SessionAnswer], Callable[[SessionAnswer], None]], None]
    _mint_and_open: Callable[[], None]
    _discard_chat: Callable[[], None]
    _forget_server_tiers: Callable[[], None]
    _tools_stopped: Callable[[], None]
    _tiers_check_passed: bool

    def _init_sign_in(self) -> None:
        # Compare-and-save: a panel sign-in replaces only the refused token, else what the profile held
        # when the sign-in began; anything written since (a terminal `symbiote login`) wins.
        self._refused_refresh: str | None = None
        self._login_baseline_refresh: str | None = None
        self._login_baseline_damaged = False  # an unreadable entry can't be compared, only replaced
        self._sign_out_in_worker = False  # the server half is running on a worker, which finishes it
        self._signing_out = False  # a Sign out is on its way; a second click starts nothing

    # Signing in

    def _start_login(self, mode: str, requested: PanelSettings) -> None:
        try:
            settings = validated(requested)
        except ValueError as exc:
            self._login.show_status(str(exc))
            return
        if settings.profile != self._settings.profile:
            self._refused_refresh = None  # the refusal was another profile's
        self._settings = settings
        self._chat_generation += 1  # a check asked before this sign-in no longer applies
        self._note_login_baseline()
        save_settings(self._adapter.name, self._settings)
        self._login.set_busy(True)
        self._login.show_status("Waiting for sign-in...")
        self._worker = LoginWorker(
            mode,
            self._settings.server,
            self._settings.profile,
            self._adapter.ui.display_name,
            self,  # the panel, a QObject
        )
        self._worker.deviceCode.connect(self._login.show_device_code)
        self._worker.succeeded.connect(self._on_login_succeeded)
        self._worker.failed.connect(self._on_login_failed)
        self._worker.start()

    def _cancel_login(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def _on_login_failed(self, message: str) -> None:
        self._login.set_busy(False)
        self._login.show_status(message)

    def _note_login_baseline(self) -> None:
        """What the profile holds as a sign-in begins: the save replaces exactly that, and loses to a later write."""
        self._login_baseline_damaged = False
        try:
            held = self._store.tokens(self._settings.profile)
        except ValueError as exc:
            # Nothing to compare: the sign-in replaces the damaged entry, as its message says it will.
            self._sign_in_log.warning("Symbiote: the stored sign-in can't be read: %s", exc)
            self._login_baseline_refresh = None
            self._login_baseline_damaged = True
            return
        self._login_baseline_refresh = None if held is None else held.refresh

    def _on_login_succeeded(self, tokens: Tokens) -> None:
        """Compare-and-save. When a sign-in written meanwhile wins, end this login's CLI session and open on that one."""
        profile, server = self._settings.profile, self._settings.server
        expected = self._refused_refresh if self._refused_refresh is not None else self._login_baseline_refresh
        try:
            # A damaged entry seen at the start is replaced only while still damaged: valid now means the terminal wrote.
            replace_malformed = self._login_baseline_damaged
            saved = self._store.save_login_if(profile, server, tokens, expected, replace_malformed=replace_malformed)
        except (ValueError, OSError) as exc:
            # The credentials file can't be read, so it can't be rewritten without dropping
            # the CLI's sign-ins, or its lock is held or can't be taken (CredentialsLockTimeout
            # is an OSError); the message names the file and says what to do.
            self._sign_in_log.warning("Can't store the sign-in: %s", exc)
            self._login.set_busy(False)
            self._login.show_status(str(exc))
            return
        self._login.set_busy(False)
        self._login.show_status("")
        # Saved or lost, the file no longer holds the refused token (checked under the lock).
        self._refused_refresh = None
        if saved:
            self._mint_and_open()
        else:
            self._revoke_losing_login(tokens.refresh)

    def _revoke_losing_login(self, refresh: str) -> None:
        """A terminal sign-in won: end the CLI session this login made, by value, on a worker; then open on the file's."""
        server, profile = self._settings.server, self._settings.profile
        self._show_notice(OPENING)
        self._run_session_call(
            lambda: revoke_session(server, profile, refresh, _LOSING_LOGIN_REVOKE_SECONDS),
            self._on_losing_login_revoked,
        )

    def _on_losing_login_revoked(self, answer: SessionAnswer) -> None:
        if answer.outcome is not Outcome.OK:
            # Left alone, that session idles out; the chat opens on the terminal's either way.
            self._sign_in_log.warning(
                "Symbiote: couldn't end a superseded panel sign-in's session (%s)", answer.detail or answer.outcome
            )
        if not self._closed:
            self._mint_and_open()

    # The chat or the tools ended

    def _on_chat_ended(self, reason: str) -> None:
        """The chat's session ended (``reason`` from ChatView.chatEnded): ask whether the CLI's still lives."""
        if self._closed:
            return
        if self._minted_at is not None and self._clock() - self._minted_at < MINT_LOOP_SECONDS:
            # The session the panel just minted was not kept: another mint would loop, so say where it looked.
            self._sign_in_log.error(
                "The chat ended (%s) within %s s of a mint; not minting again", reason, MINT_LOOP_SECONDS
            )
            self._chat_generation += 1  # a check asked earlier no longer applies
            message = SEED_LOOP_ERROR.format(web=web_url_for(self._settings.server), server=self._settings.server)
            self._show_notice(message)
            return
        self._sign_in_log.info("The chat ended (%s); checking the CLI session", reason)
        self._check_cli(lambda answer, email, generation: self._on_cli_check(reason, answer, email, generation))

    def _on_microagent_exited(self, code: int) -> None:
        """Any exit stops the tiers fetch; exit 78 (any terminal stop) also asks the server whether the session lives."""
        if self._closed:
            return
        self._tools_stopped()
        if code != TERMINAL_STOP_EXIT:
            return
        self._sign_in_log.info("The microagent stopped for good (exit %s); checking the CLI session", code)
        self._check_cli(lambda answer, email, generation: self._on_tools_check(EXIT_78, answer, email, generation))

    def _check_cli_after_refused_tiers(self) -> None:
        """The tiers 401 outlasted the quick retries: is the CLI session still alive? (No Continue: the chat isn't ended.)"""
        self._sign_in_log.info("The server keeps refusing the stored sign-in for the tiers; checking the CLI session")
        self._check_cli(
            lambda answer, email, generation: self._on_tools_check(TIERS_UNAUTHORIZED, answer, email, generation)
        )

    def _on_tools_check(self, reason: str, answer: SessionAnswer, _email: str, generation: int) -> None:
        """ENDED takes the session-ended path; otherwise the status bar already says what is wrong with the tools."""
        if self._closed or generation != self._chat_generation:
            return
        if answer.outcome is Outcome.ENDED:
            self._on_session_ended(answer.presented)
        elif answer.outcome is Outcome.OK:
            self._tiers_check_passed = self._tiers_check_passed or reason == TIERS_UNAUTHORIZED
        else:
            self._sign_in_log.warning(
                "Symbiote: the tools check (%s) couldn't ask about the CLI session (%s)", reason, answer.detail
            )

    def _check_cli(self, on_answer: Callable[[SessionAnswer, str, int], None]) -> None:
        """Ask on a worker whether the CLI session lives; ``on_answer(answer, email, chat generation)``."""
        server, profile, store = self._settings.server, self._settings.profile, self._store

        def read_cli() -> Tokens | None:
            return store.current_tokens(profile, server)  # read when the call runs, never a token held earlier

        email: list[str] = []

        def call() -> SessionAnswer:
            answer = with_superseded_retry(lambda: check_cli_session(server, profile, read_cli), read_cli)
            if answer.outcome is Outcome.OK:
                email.append(store.user_email(profile))  # under the credentials lock, off the main thread
            return answer

        generation = self._chat_generation

        def deliver(answer: SessionAnswer) -> None:
            if self._settings.profile == profile:  # an answer about another profile's sign-in is dropped
                on_answer(answer, email[0] if email else "", generation)

        self._run_session_call(call, deliver)

    def _on_cli_check(self, reason: str, answer: SessionAnswer, email: str, generation: int) -> None:
        """The CLI session is live (offer Continue or Sign out), ended, or could not be asked.

        The chat asked to sign in: a live CLI session needs no new login, so Continue is offered; an
        ended one takes the session-ended path. Only a click (``SIGN_IN_REQUESTED``) then starts the
        system-browser login; a redirect or a frame stops at "Sign in again", so the artist clicks once.
        An answer for a chat since replaced or discarded is dropped.
        """
        if self._closed or generation != self._chat_generation:
            return
        if answer.outcome is Outcome.OK:
            label = CONTINUE.format(email=email) if email else CONTINUE_NO_EMAIL
            # Both actions are handled in panel._on_notice_action.
            self._show_notice(
                SESSION_ENDED_BUT_SIGNED_IN, (Action(ACTION_CONTINUE, label), Action(ACTION_SIGN_OUT, SIGN_OUT))
            )
        elif answer.outcome is Outcome.ENDED:
            # First, so the sign-in's compare-and-save expects the refused token (review N4).
            self._on_session_ended(answer.presented)
            if reason == SIGN_IN_REQUESTED:
                self._start_login(MODE_BROWSER, self._settings)  # the system browser, never the webview
        else:
            self._show_refusal(answer)

    def _on_session_ended(self, refused_refresh: str | None) -> None:
        """The CLI sign-in ended: stop the tools, clear the file only while it holds the refused token, ask to sign in.

        ``None``: nothing was presented (the profile was already empty, e.g. a terminal logout), so nothing is cleared.
        """
        self._microagent.stop()
        self._microagent_tried = False  # the next sign-in starts the tools again
        self._workstation = None
        self._family_id = None
        self._refused_refresh = refused_refresh
        if refused_refresh is not None:
            try:
                self._store.clear_if_holds(self._settings.profile, refused_refresh)
            except (ValueError, OSError) as exc:
                # Still asks for a sign-in; compare-and-save replaces the refused token then.
                self._sign_in_log.warning(
                    "Symbiote: couldn't remove the ended sign-in from the credentials file: %s", exc
                )
        self._discard_chat()
        self._forget_server_tiers()
        self._status.setText("")
        self._stack.setCurrentWidget(self._login)
        self._login.show_session_ended()

    # Sign out of Symbiote, from the Continue notice

    def _sign_out(self) -> None:
        """Stop the tools, read the chat's token once, then end both sessions on a worker.

        Order matters: the microagent holds a session of its own and would answer the server's
        revocation by reconnecting, so it stops first; the chat's refresh token is read before
        anything is revoked, since the page is the only place it lives.
        """
        if self._signing_out:
            return
        self._signing_out = True
        self._chat_generation += 1  # a check asked before this no longer applies
        self._microagent.stop()
        self._read_chat_token()

    def _retry_sign_out(self) -> None:
        """Retry on the "can't reach" notice: again from the token read; the microagent is already stopped."""
        if not self._signing_out:
            self._signing_out = True
            self._read_chat_token()

    def _read_chat_token(self) -> None:
        # The server and profile this sign-out is for, fixed now: a settings change meanwhile can't redirect it.
        server, profile = self._settings.server, self._settings.profile

        def read(chat_refresh: str | None) -> None:
            if self._closed:  # shutdown() saw the sign-out pending and finishes it on its own thread
                self._signing_out = False
                return
            self._end_sessions(chat_refresh, server, profile)

        if self._chat is None:
            read(None)
        else:
            self._chat.read_refresh_token(read)

    def _end_sessions(self, chat_refresh: str | None, server: str, profile: str) -> None:
        """On a worker: end the chat's session by value (a 401 means it is already over), then the CLI's."""
        store = self._store
        lease = self._lease
        record = lease.record_path if lease is not None else None
        self._sign_out_in_worker = True

        def call() -> SessionAnswer:
            # The file and the record are settled here, not in the panel's slot: Qt drops the answer of
            # a panel closed meanwhile (as Task 9's I1).
            return _sign_out_server_half(server, profile, store, record, chat_refresh)

        self._run_session_call(call, lambda answer: self._on_sign_out_answer(answer, profile))

    def _sign_out_on_close(self) -> bool:
        """True when a Sign out asked before close, not yet on a worker, was handed to a daemon thread.

        The page can't be read any more, so that thread ends the CLI's session (the server's cascade
        ends the chat's child with it) and clears the file only if it still holds what was revoked.
        It covers the chat, so the plain close revoke is skipped. Never joined.

        It signs out of the server and profile the chat was minted for, like the close revoke; the
        current settings only when nothing was minted.
        """
        if not self._signing_out or self._sign_out_in_worker:
            return False
        minted_for = self._family_minted_for
        server, profile = minted_for if minted_for is not None else (self._settings.server, self._settings.profile)
        thread = threading.Thread(
            target=_finish_sign_out_after_close,
            args=(server, profile, self._store),
            name="symbiote-close-sign-out",
            daemon=True,
        )
        thread.start()
        return True

    def _on_sign_out_answer(self, answer: SessionAnswer, profile: str) -> None:
        self._signing_out = False
        self._sign_out_in_worker = False
        if self._closed:
            return
        if answer.outcome not in (Outcome.OK, Outcome.ENDED):
            self._sign_in_log.warning("Symbiote: sign out couldn't reach the server (%s)", answer.detail)
            self._show_notice(SIGN_OUT_UNREACHABLE, (Action(ACTION_SIGN_OUT_RETRY, RETRY),))
            return
        self._finish_sign_out(answer.problem or None)  # the worker already cleared the file; problem: it couldn't

    def _finish_sign_out(self, problem: str | None) -> None:
        """Forget everything this sign-in held and show the login view (``problem``: what went wrong on the way)."""
        self._microagent_tried = False
        self._workstation = None
        self._family_id = None
        self._refused_refresh = None
        POLICY.clear_identity()
        self._discard_chat()
        self._forget_server_tiers()
        self._status.setText("")
        self._stack.setCurrentWidget(self._login)
        if problem is None:
            self._login.show_signed_out()
        else:
            self._login.show_status(problem)


def _finish_sign_out_after_close(server: str, profile: str, store: CredentialStore) -> None:
    """Close's thread for a pending Sign out: the CLI's session, then the file.

    No separate chat revoke: the page can't be read any more, and a revoke by family id needs the
    CLI access token, which may have expired (a bearer 401 is UNAVAILABLE). The CLI revoke presents
    the refresh token, and the server's cascade ends the chat's child in the same commit.

    The record stays: a terminal re-login may have replaced the CLI family the child hangs off, so
    the cascade proves nothing about the child. The next open's orphan cleanup confirms it against
    the server's session list.
    """
    try:
        answer = _sign_out_server_half(server, profile, store, None, None)
    except Exception:
        logger.warning("Symbiote: the sign-out asked before close didn't finish; sign out again", exc_info=True)
        return
    if answer.outcome not in (Outcome.OK, Outcome.ENDED):
        _log_unfinished(answer)


def _sign_out_server_half(
    server: str,
    profile: str,
    store: CredentialStore,
    record: Path | None,
    chat_refresh: str | None,
) -> SessionAnswer:
    """End the chat's session by value (``chat_refresh``, when read), then the CLI's, then settle the file and the record.

    The chat step is best effort and never stops the CLI revoke: that revoke presents the CLI refresh
    token, and the server's cascade ends the chat's child with it. After the CLI revoke answers
    OK/ENDED the file is compare-and-cleared with the token that call presented. The record names
    the chat's session, so it goes once that is known ended: the chat revoke answered OK, or the CLI
    revoke presented a token (the child dies with its parent). ENDED with nothing presented (no
    sign-in for this server) proves neither, so the record stays. A failed file clear is returned in
    ``problem`` and doesn't keep the record.
    """

    def read_cli() -> Tokens | None:
        return store.current_tokens(profile, server)

    chat = None if chat_refresh is None else revoke_session(server, profile, chat_refresh, _SIGN_OUT_REVOKE_SECONDS)
    if chat is not None and chat.outcome not in (Outcome.OK, Outcome.ENDED):
        logger.info("Symbiote: the chat's session wasn't ended by value (%s); the CLI revoke ends it", chat.detail)
    cli = with_superseded_retry(
        lambda: revoke_cli_session(server, profile, read_cli, _SIGN_OUT_REVOKE_SECONDS), read_cli
    )
    cli_done = cli.outcome in (Outcome.OK, Outcome.ENDED)
    problem = _clear_stored_sign_in(store, profile, cli.presented) if cli_done else None
    chat_ended = chat is not None and chat.outcome is Outcome.OK
    if record is not None and (chat_ended or (cli_done and cli.presented is not None)):
        forget_record(record)
    return SessionAnswer(cli.outcome, detail=cli.detail, presented=cli.presented, problem=problem or "")


def _clear_stored_sign_in(store: CredentialStore, profile: str, presented: str | None) -> str | None:
    """Compare-and-clear the profile; the problem text when that failed, never raising. None presented: nothing held."""
    if presented is None:
        return None
    try:
        store.clear_if_holds(profile, presented)
    except Exception as exc:  # never strand the artist: logged in full, shown, and the sign-out finishes
        logger.exception("Can't remove the stored sign-in")
        return f"Signed out of Symbiote, but the stored sign-in wasn't removed: {exc}"
    return None


def _log_unfinished(answer: SessionAnswer) -> None:
    logger.warning("Symbiote: the sign-out asked before close didn't finish (%s); the sign-in stays", answer.detail)
