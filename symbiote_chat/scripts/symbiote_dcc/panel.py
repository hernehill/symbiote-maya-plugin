"""The Python panel: sign-in screen, then web-chat with a one-line status bar.

The chat runs on the panel's own session, a child the server mints from the CLI's sign-in
(#2273): web-chat rotates that one, so the CLI's tokens are never in the page and a DCC that
crashes mid-rotation can't get the CLI's session revoked as theft.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtWidgets

from symbiote_dcc.adapter import DccAdapter
from symbiote_dcc.chat_view import ChatView
from symbiote_dcc.consent import POLICY
from symbiote_dcc.credentials import CredentialStore, Tokens
from symbiote_dcc.login_view import LoginView, LoginWorker
from symbiote_dcc.mcp_protocol import READ_ONLY
from symbiote_dcc.microagent_runner import MicroagentRunner
from symbiote_dcc.panel_session import (
    Outcome,
    ReadCli,
    SessionAnswer,
    list_sessions,
    mint_panel_session,
    panel_user_agent,
    revoke_family,
    with_superseded_retry,
)
from symbiote_dcc.panel_signin import PanelSignIn
from symbiote_dcc.panel_state import PanelLease, acquire_lease, forget_record, orphaned_records, record_panel
from symbiote_dcc.session_notice import (
    ACTION_CONTINUE,
    ACTION_SIGN_OUT,
    ACTION_SIGN_OUT_RETRY,
    OPENING,
    PARENT_NOT_CLI,
    RETRY,
    UNREACHABLE,
    SessionNoticeView,
    refusal_notice,
)
from symbiote_dcc.session_worker import UNFINISHED_WORKERS, SessionCallWorker
from symbiote_dcc.settings import (
    config_dir,
    load_panel_settings,
    web_url_for,
)
from symbiote_dcc.tool_server import ToolServer
from symbiote_dcc.tool_switches import (
    DIALOGS,
    IMPORT,
    STALE_SIGN_IN_RETRIES,
    STALE_SIGN_IN_RETRY_SECONDS,
    STUDIO_OFF,
    TOOLS_STOPPED_STATUS,
    UNREACHABLE_STATUS,
    WRITE,
    TiersFetch,
    ToolSwitches,
    allowed_tiers,
    fetch_server_tiers,
    load_switches,
    retry_delay_seconds,
    save_switches,
)

logger = logging.getLogger(__name__)

PURPOSE_OPEN = "open"
PURPOSE_CONTINUE = "continue"
# Ending the child a new mint replaces: best effort, so it must not hold the mint up for long.
_REPLACED_REVOKE_SECONDS = 5.0
# Closing must not hold the DCC up: the revoke on close gets this long, on a thread nobody joins.
CLOSE_REVOKE_SECONDS = 2.0
_WORKER_STOP_WAIT_MS = 2000
_FETCH_TIMEOUT_SECONDS = 15
_REVOKE_TIMEOUT_SECONDS = 15
# Switch changes within this window restart the microagent once.
_RESTART_COALESCE_MS = 1000
_SWITCH_LABELS = {IMPORT: "Imports", WRITE: "Scene changes"}
_WAITING = "waiting for the studio's settings"
_NO_SETTINGS = "no studio settings: read tools only"
# Workers still running when their panel closed (see session_worker): fetches and session calls.
_UNFINISHED_WORKERS = UNFINISHED_WORKERS


def _revoke_listed_orphans(server: str, profile: str, read_cli: ReadCli, families: list[str]) -> None:
    """Revoke each family the server lists as a panel child of this CLI sign-in; nothing else.

    A record can name a session that is no longer a child of this sign-in (the CLI signed in
    again, or as someone else): that one is only forgotten. Failures are logged, never raised:
    a session left behind ends on its own once idle.
    """
    listed = list_sessions(server, profile, read_cli)
    if listed.outcome is not Outcome.OK:
        logger.warning("Symbiote: couldn't list sessions to end a closed panel's (%s)", listed.detail or listed.outcome)
        return
    parent = next((row.get("family_id") for row in listed.rows if row.get("current") is True), None)
    if parent is None:
        logger.warning("Symbiote: the session list has no current session; a closed panel's session is left")
        return
    children = {
        row.get("family_id")
        for row in listed.rows
        if row.get("kind") == "panel" and row.get("parent_family_id") == parent
    }
    for family in families:
        if family not in children:
            logger.info("Symbiote: a closed panel's session %s is not this sign-in's; forgetting it", family)
            continue
        answer = revoke_family(server, profile, family, read_cli, _REVOKE_TIMEOUT_SECONDS)
        if answer.outcome is not Outcome.OK:
            logger.warning("Symbiote: couldn't end a closed panel's session (%s)", answer.detail or answer.outcome)


def _revoke_orphans(server: str, profile: str, read_cli: ReadCli, directory: Path, dcc: str) -> None:
    """End the sessions of this profile's panels that closed without ending theirs (a crash, a kill).

    Runs on the session worker before the mint. The list and the revoke use the CLI's access
    token, which the panel never refreshes itself: when it has expired, cleanup waits for an open
    after the CLI has refreshed it. Every orphan found is discarded, whatever happens.
    """
    cli = read_cli()
    if cli is None:
        return  # the mint says the sign-in ended
    if cli.expiry <= time.time():
        logger.info("Symbiote: the CLI's access token has expired; a closed panel's session is left for later")
        return
    orphans = orphaned_records(directory, dcc, profile)
    try:
        families = [orphan.family_id for orphan in orphans if orphan.family_id is not None]
        if families:
            _revoke_listed_orphans(server, profile, read_cli, families)
    except Exception:  # cleanup is best effort: the chat opens either way
        logger.exception("Symbiote: ending a closed panel's session failed")
    finally:
        for orphan in orphans:
            try:
                orphan.discard()
            except OSError as exc:  # one stuck file must not keep the others' locks or stop the mint
                logger.warning("Symbiote: couldn't discard %s: %s", orphan.path, exc)


def _revoke_replaced(server: str, profile: str, read_cli: ReadCli, family_id: str) -> None:
    """End the child session a new mint replaces. Anything but OK is logged: the mint goes ahead."""
    answer = revoke_family(server, profile, family_id, read_cli, _REPLACED_REVOKE_SECONDS)
    if answer.outcome is not Outcome.OK:
        logger.warning("Symbiote: couldn't end the panel's previous session (%s)", answer.detail or answer.outcome)


def _record_session(lease: PanelLease, family_id: str) -> None:
    try:
        record_panel(lease, family_id)
    except OSError as exc:
        # The chat still opens; only a crash's cleanup is lost, and that session idles out.
        logger.error("Symbiote: couldn't record the panel's session in %s: %s", lease.record_path, exc)


class TiersWorker(QtCore.QThread):
    """Fetches the studio's tiers off the DCC's main thread; ``fetched`` carries the generation.

    Once its thread has finished, it releases itself: ``released`` tells the panel to forget
    it, and ``deleteLater`` frees it, rather than keeping it as the panel's child until close.
    """

    fetched = QtCore.Signal(int, object)
    released = QtCore.Signal(object)

    def __init__(self, generation: int, server: str, profile: str, access_token: str, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self.generation = generation  # the sign-in it was asked for
        self._server = server
        self._profile = profile
        self._access_token = access_token
        # The worker lives on the main thread, so Qt queues this there, after run() has returned.
        self.finished.connect(self._release)

    def _release(self) -> None:
        _UNFINISHED_WORKERS.discard(self)
        self.released.emit(self)
        self.deleteLater()

    def run(self) -> None:
        try:
            outcome = fetch_server_tiers(self._server, self._profile, self._access_token, _FETCH_TIMEOUT_SECONDS)
        except Exception:  # a worker thread has nowhere else to report: logged, shown, retried
            logger.exception("Symbiote: the tool tiers fetch failed unexpectedly")
            outcome = TiersFetch(None, UNREACHABLE_STATUS, True)
        self.fetched.emit(self.generation, outcome)


class _ClickableLabel(QtWidgets.QLabel):
    clicked = QtCore.Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        inside = self.rect().contains(event.position().toPoint())
        if event.button() == QtCore.Qt.LeftButton and inside:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _end_chat_session(server: str, profile: str, family_id: str, read_cli: ReadCli, record: Path) -> None:
    """Close's thread body: end the chat's session; on OK delete this instance's record, else keep it."""
    try:
        answer = revoke_family(server, profile, family_id, read_cli, CLOSE_REVOKE_SECONDS)
    except Exception:  # a thread nobody joins: log it, the record stays for the next open
        logger.warning("Symbiote: ending the chat's session at close failed; the next open ends it", exc_info=True)
        return
    if answer.outcome is not Outcome.OK:
        logger.info("Symbiote: the chat's session wasn't ended at close (%s); the next open ends it", answer.outcome)
        return
    forget_record(record)


class SymbioteChatPanel(QtWidgets.QWidget, PanelSignIn):
    _sign_in_log = logger.getChild("sign_in")

    def __init__(self, adapter: DccAdapter, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._adapter = adapter
        self._clock = clock
        self._minted_at: float | None = None  # the clock at the last OK mint, open or Continue
        self._family_id: str | None = None  # the child session the chat shows, from the last OK mint
        # (server, profile) that child was minted under: a later failed sign-in elsewhere changes the
        # settings, not the child, so close revokes it with the sign-in it belongs to.
        self._family_minted_for: tuple[str, str] | None = None
        self._minting = False  # a mint is on its way
        self.setStyleSheet(adapter.ui.stylesheet())
        self._config_dir = config_dir()
        self._store = CredentialStore(self._config_dir, adapter.name)
        self._settings, settings_problem = load_panel_settings(adapter.name)
        # Held while the panel is open: a record whose lock no panel holds is a crashed panel's.
        # Without one the panel still builds, and says why before any mint (_take_lease).
        self._lease: PanelLease | None = None
        self._lease_profile: str | None = None  # the profile the lease was taken for
        self._lease_problem: OSError | None = None
        self._take_lease()
        self._init_sign_in()
        # Computed here, once: Maya's cmds must run on the main thread, and workers get the string.
        self._user_agent = panel_user_agent(adapter)
        self._closed = False
        self._chat_generation = 0  # bumped when a chat opens or is discarded; a check answer must match
        self._session_workers: list[SessionCallWorker] = []
        self._workstation: str | None = None  # what the microagent registered, for the chat's address
        self._microagent_tried = False  # started once per sign-in, by the first minted session
        self._worker: LoginWorker | None = None
        self._chat: ChatView | None = None
        self._switches, switches_problem = load_switches(adapter.name)
        self._server_tiers: dict[str, bool] | None = None  # None until the server says
        self._fetching = False  # a fetch for this sign-in is on its way
        self._tiers_workers: list[TiersWorker] = []  # started and not yet seen finished
        self._tiers_generation = 0  # bumped at sign-out, so a late answer is ignored
        self._tiers_failures = 0
        self._tiers_check_passed = False  # a tiers 401 check found the CLI session live; don't ask again
        self._stale_sign_in_retries = 0  # quick retries after a 401 since the last answer
        self._tool_server = ToolServer(adapter.tools, f"symbiote-{adapter.name}")
        self._tool_server.start()
        self._microagent = MicroagentRunner(config_dir(), adapter, self)
        self._build_ui()
        self._build_timers()
        if switches_problem is not None:
            logger.warning("Symbiote: %s", switches_problem)
            self._tiers_status.setText(switches_problem)
        if settings_problem is None:
            self._resume_or_ask()
        else:
            logger.warning("Saved panel settings can't be used: %s", settings_problem)
            self._login.show_status(settings_problem)

    def _build_ui(self) -> None:
        self._login = LoginView(self._settings, self)
        self._login.loginRequested.connect(self._start_login)
        self._login.cancelRequested.connect(self._cancel_login)
        self._notice = SessionNoticeView(self)
        self._notice.actionChosen.connect(self._on_notice_action)
        self._stack = QtWidgets.QStackedLayout()
        self._stack.addWidget(self._login)
        self._stack.addWidget(self._notice)
        self._status = QtWidgets.QLabel("")
        self._status.setTextFormat(QtCore.Qt.PlainText)  # CLI paths and messages, never HTML
        self._captures = _ClickableLabel("Captures aren't being asked about")
        self._captures.setToolTip("Click to be asked before each capture again")
        self._captures.setCursor(QtCore.Qt.PointingHandCursor)
        self._captures.clicked.connect(POLICY.reset)
        self._captures.setVisible(POLICY.is_remembered())
        POLICY.subscribe(self._sync_captures_label)
        self._reload = QtWidgets.QPushButton("Reload")
        self._reload.setVisible(False)
        self._reload.clicked.connect(self._reload_chat)
        self._imports_box = self._switch_box(IMPORT, self._switches.imports)
        self._writes_box = self._switch_box(WRITE, self._switches.writes)
        self._tiers_status = QtWidgets.QLabel("")
        self._tiers_status.setTextFormat(QtCore.Qt.PlainText)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self._status, 1)
        bar.addWidget(self._tiers_status)
        bar.addWidget(self._imports_box)
        bar.addWidget(self._writes_box)
        bar.addWidget(self._captures)
        self._version = QtWidgets.QLabel(self._adapter.version_label)
        self._version.setTextFormat(QtCore.Qt.PlainText)
        bar.addWidget(self._version)
        bar.addWidget(self._reload)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._stack, 1)
        layout.addLayout(bar)
        self._microagent.stateChanged.connect(self._status.setText)
        self._microagent.exited.connect(self._on_microagent_exited)

    def _switch_box(self, tier: str, checked: bool) -> QtWidgets.QCheckBox:
        box = QtWidgets.QCheckBox(_SWITCH_LABELS[tier])
        box.setChecked(checked)
        box.toggled.connect(self._on_switch_toggled)
        return box

    def _build_timers(self) -> None:
        self._restart_timer = QtCore.QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(_RESTART_COALESCE_MS)
        self._restart_timer.timeout.connect(self._restart_microagent)
        self._retry_timer = QtCore.QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._fetch_tiers)

    # Switches and tiers

    def _on_switch_toggled(self, _checked: bool) -> None:
        switches = ToolSwitches(imports=self._imports_box.isChecked(), writes=self._writes_box.isChecked())
        if switches == self._switches:
            return
        self._switches = switches
        try:
            save_switches(self._adapter.name, switches)
        except OSError as exc:
            # The switch still applies until the DCC closes; the artist learns it won't persist.
            logger.warning("Symbiote: couldn't save the tool switches: %s", exc)
            self._tiers_status.setText(f"Couldn't save the switches: {exc.strerror or exc}")
        self._apply_tiers()

    def _apply_tiers(self) -> None:
        """Serve what the studio and the switches allow, then restart if the served set changed."""
        first_fetch = self._fetching and self._tiers_failures == 0
        allowed = allowed_tiers(self._server_tiers, self._switches, fetching=first_fetch)
        changed = allowed.tiers != self._tool_server.allowed_tiers.tiers
        # First: from the next request the tool server serves the new set, and a tool waiting
        # on a dialog sees it when the artist confirms. The restart lets the server see it.
        self._tool_server.set_allowed_tiers(allowed)
        self._sync_switch_boxes()
        if changed and self._chat is not None:
            self._restart_timer.start()

    def _sync_switch_boxes(self) -> None:
        """Grey a switch the studio has off, and both while the studio's settings are unknown.

        A ticked, enabled box while only read tools are served reads as "on". The boxes keep the
        artist's saved choice (``isChecked``) and show it again once the settings arrive.
        """
        unknown = None
        if self._server_tiers is None:
            unknown = _WAITING if self._fetching and self._tiers_failures == 0 else _NO_SETTINGS
        for tier, box in ((IMPORT, self._imports_box), (WRITE, self._writes_box)):
            studio_off = self._server_tiers is not None and self._server_tiers.get(tier) is not True
            reason = unknown or (STUDIO_OFF if studio_off else None)
            box.setEnabled(reason is None)
            box.setText(_SWITCH_LABELS[tier] if reason is None else f"{_SWITCH_LABELS[tier]} ({reason})")
            box.setToolTip(reason or "")

    def _fetch_tiers(self) -> None:
        """Ask the server for the studio's tiers on a worker thread; never blocks this one.

        Only while the microagent runs: it is the only process that refreshes the stored access token.
        """
        if not self._microagent.is_running():
            self._tools_stopped()
            return
        self._tiers_workers = [w for w in self._tiers_workers if w.isRunning()]
        if any(w.generation == self._tiers_generation for w in self._tiers_workers):
            return  # this sign-in's answer is on the way; one from before a sign-out doesn't count
        try:
            tokens = self._store.tokens_for_server(self._settings.profile, self._settings.server)
        except ValueError as exc:
            logger.warning("Symbiote: no sign-in to fetch the tool tiers with: %s", exc)
            self._on_tiers_fetched(self._tiers_generation, TiersFetch(None, UNREACHABLE_STATUS, True))
            return
        if tokens is None:
            return  # signed out: sign-in fetches again
        worker = TiersWorker(self._tiers_generation, self._settings.server, self._settings.profile, tokens.access, self)
        worker.fetched.connect(self._on_tiers_fetched)
        worker.released.connect(self._forget_fetch)
        self._tiers_workers = [*self._tiers_workers, worker]
        worker.start()
        if not self._fetching:
            self._fetching = True
            self._apply_tiers()  # a refusal now says the settings are on their way

    def _forget_fetch(self, worker: TiersWorker) -> None:
        """A fetch's thread finished and the worker is being deleted: never touch it again."""
        self._tiers_workers = [w for w in self._tiers_workers if w is not worker]

    def _on_tiers_fetched(self, generation: int, outcome: TiersFetch) -> None:
        if generation != self._tiers_generation or not self._microagent.is_running():
            return  # asked for before a sign-out, or the microagent has stopped since
        if outcome.stale_sign_in and self._stale_sign_in_retries < STALE_SIGN_IN_RETRIES:
            # The microagent refreshes the stored sign-in within seconds; the retry reads the stored
            # token again. Still fetching: the switches keep saying so, and so does a refused tool.
            self._stale_sign_in_retries += 1
            self._retry_timer.start(STALE_SIGN_IN_RETRY_SECONDS * 1000)
            return
        if outcome.stale_sign_in:
            if not self._tiers_check_passed:
                self._check_cli_after_refused_tiers()  # the backoff below goes on; ENDED stops it
        elif outcome.server is not None:
            self._tiers_check_passed = False
        self._stale_sign_in_retries = 0
        self._fetching = False
        self._server_tiers = None if outcome.server is None else dict(outcome.server)
        self._tiers_status.setText(outcome.status)
        if outcome.retry:
            self._retry_timer.start(retry_delay_seconds(self._tiers_failures) * 1000)
            self._tiers_failures += 1
        else:
            self._retry_timer.stop()
            self._tiers_failures = 0
        self._apply_tiers()

    def _tools_stopped(self) -> None:
        """The microagent isn't running: no fetch, no retry, read tools only, and a late answer is dropped."""
        self._tiers_generation += 1
        self._retry_timer.stop()
        self._tiers_failures = 0
        self._stale_sign_in_retries = 0
        self._fetching = False
        self._server_tiers = None
        self._tiers_check_passed = False
        self._tiers_status.setText(TOOLS_STOPPED_STATUS)
        self._apply_tiers()
        self._restart_timer.stop()  # the drop to read tools must not respawn what just exited

    def _forget_server_tiers(self) -> None:
        """Signed out: back to read tools only, and no answer or retry from before counts."""
        self._tiers_generation += 1
        self._retry_timer.stop()
        self._restart_timer.stop()
        self._tiers_failures = 0
        self._stale_sign_in_retries = 0
        self._tiers_check_passed = False
        self._fetching = False
        self._server_tiers = None
        self._tiers_status.setText("")
        self._apply_tiers()

    # The microagent

    def _start_microagent(self) -> str | None:
        """Start the microagent, then ask for the studio's tiers if it runs (it keeps the sign-in fresh)."""
        agent = self._microagent.start(
            self._settings.profile,
            self._settings.server,
            self._tool_server.port,
            self._tool_server.token,
        )
        self._fetch_tiers()
        return agent

    def _restart_microagent(self) -> None:
        if self._chat is None:
            return
        if DIALOGS.is_open() or self._tool_server.busy():
            # A restart now would cut off a call in flight, or one waiting on the artist's
            # answer. Look again after another window: returning lets that call reach this
            # thread and finish. The tool server already serves the new tiers.
            self._restart_timer.start()
            return
        self._microagent.stop()
        self._start_microagent()

    def _sync_captures_label(self) -> None:
        self._captures.setVisible(POLICY.is_remembered())

    def _resume_or_ask(self) -> None:
        try:
            tokens = self._store.tokens_for_server(self._settings.profile, self._settings.server)
        except ValueError as exc:
            # A damaged credentials file or entry: keep the login screen up and show the
            # message, which names the file and what to do, rather than failing to build the
            # panel. Signing in again replaces a damaged entry; a damaged file needs moving aside.
            logger.warning("Stored sign-in can't be used: %s", exc)
            self._login.show_status(str(exc))
            return
        if tokens is not None:
            self._mint_and_open()

    # The panel's own session

    def _take_lease(self) -> bool:
        """Hold this panel's lease for the current profile, taking it now if needed; False when it can't."""
        profile = self._settings.profile
        if self._lease is not None and self._lease_profile != profile:
            # Signed in on another profile. The old record stays: that profile's next open ends its session.
            # The old child goes with it: the new lease's record doesn't name it, so close mustn't revoke
            # it against that record.
            self._lease.release()
            self._lease = None
            self._family_id = None
            self._family_minted_for = None
        if self._lease is not None:
            return True
        try:
            self._lease = acquire_lease(self._config_dir, self._adapter.name, profile)
        except OSError as exc:
            logger.warning("Symbiote: can't keep track of this panel's session: %s", exc)
            self._lease_problem = exc
            return False
        self._lease_profile = profile
        self._lease_problem = None
        return True

    def _mint_and_open(self) -> None:
        self._mint(PURPOSE_OPEN)

    def _mint(self, purpose: str, replaces: str | None = None) -> None:
        """Mint the panel's session on a worker, after ending any a crashed panel left; never blocks.

        ``purpose``: ``open`` shows a new chat with it; ``continue`` re-seeds the chat already shown.
        ``replaces``: the child session this mint supersedes; it is ended first, best effort.

        No mint without a lease: a session the panel can't record could never be ended after a crash.
        """
        if not self._take_lease() or self._lease is None:
            self._show_notice(f"{UNREACHABLE}: {self._lease_problem}", (RETRY,))
            return
        server, profile, store = self._settings.server, self._settings.profile, self._store
        directory, dcc, user_agent, lease = self._config_dir, self._adapter.name, self._user_agent, self._lease

        def read_cli() -> Tokens | None:
            return store.current_tokens(profile, server)

        def call() -> SessionAnswer:
            _revoke_orphans(server, profile, read_cli, directory, dcc)
            if replaces is not None:
                _revoke_replaced(server, profile, read_cli, replaces)
            answer = with_superseded_retry(lambda: mint_panel_session(server, profile, read_cli, user_agent), read_cli)
            if answer.outcome is Outcome.OK and answer.family_id is not None:
                # Here, not in the panel's slot: Qt drops a queued answer to a panel destroyed meanwhile,
                # and an unrecorded child could never be ended. After close the lock is gone, so the
                # record is an orphan the next open ends.
                _record_session(lease, answer.family_id)
            return answer

        self._minting = True
        self._show_notice(OPENING)
        self._run_session_call(call, lambda answer: self._on_mint_answer(answer, purpose, (server, profile)))

    def _run_session_call(self, call: Callable[[], SessionAnswer], on_done: Callable[[SessionAnswer], None]) -> None:
        worker = SessionCallWorker(call, self)
        worker.done.connect(on_done)
        worker.released.connect(self._forget_session_worker)
        self._session_workers = [*self._session_workers, worker]
        worker.start()

    def _forget_session_worker(self, worker: SessionCallWorker) -> None:
        self._session_workers = [w for w in self._session_workers if w is not worker]

    def _on_mint_answer(
        self, answer: SessionAnswer, purpose: str = PURPOSE_OPEN, minted_for: tuple[str, str] | None = None
    ) -> None:
        """A mint answered, for ``purpose`` (open or continue); the worker has already recorded it.

        ``minted_for``: the (server, profile) the mint presented; the current settings when not given.
        """
        self._minting = False
        if self._closed:
            return  # recorded with no lock held, so the next panel to open ends it
        if answer.outcome is Outcome.OK and answer.tokens is not None and answer.family_id is not None:
            self._refused_refresh = None
            self._minted_at = self._clock()
            self._family_id = answer.family_id
            self._family_minted_for = minted_for or (self._settings.server, self._settings.profile)
            if purpose == PURPOSE_CONTINUE and self._chat is not None:
                self._reseed_chat(self._chat, answer.tokens)
                return
            if not self._microagent_tried:
                self._microagent_tried = True
                self._workstation = self._start_microagent()
            self._open_chat(answer.tokens)
        elif answer.outcome is Outcome.ENDED:
            self._on_session_ended(answer.presented)
        else:
            self._show_refusal(answer)

    def _show_refusal(self, answer: SessionAnswer) -> None:
        if answer.outcome is Outcome.PARENT_NOT_CLI:
            # Not a dead session, so nothing is cleared: a panel sign-in replaces what the profile holds.
            self._stack.setCurrentWidget(self._login)
            self._login.show_status(PARENT_NOT_CLI)
            return
        self._show_notice(*refusal_notice(answer))

    def _show_notice(self, message: str, actions: tuple[str, ...] = ()) -> None:
        self._notice.show_notice(message, actions)
        self._stack.setCurrentWidget(self._notice)

    def _on_notice_action(self, action: str) -> None:
        if self._signing_out and action in (RETRY, ACTION_CONTINUE):
            return  # a sign-out is on its way: minting now would hand the chat a session it is about to lose
        if action == RETRY:
            self._mint_and_open()
        elif action == ACTION_CONTINUE:
            self._mint(PURPOSE_CONTINUE, self._family_id)
        elif action == ACTION_SIGN_OUT:
            self._sign_out()
        elif action == ACTION_SIGN_OUT_RETRY:
            self._retry_sign_out()

    def _reseed_chat(self, chat: ChatView, tokens: Tokens) -> None:
        """Sign the chat already built in again with the freshly minted child; no new ChatView."""
        self._chat_generation += 1  # a check asked before this no longer applies
        self._stack.setCurrentWidget(chat)
        self._reload.setVisible(True)
        chat.reseed(tokens)

    def _open_chat(self, tokens: Tokens) -> None:
        """Show web-chat signed in with the panel's session; the microagent is started elsewhere."""
        POLICY.use_identity(self._settings.server, self._settings.profile)
        web_url = web_url_for(self._settings.server)
        self._discard_chat()
        # The chat names the workstation only when the tools are on (#2168): a panel whose
        # microagent never started would send an id the server can only ignore and log.
        self._chat_generation += 1
        self._chat = ChatView(web_url, self, workstation=self._workstation)
        self._stack.addWidget(self._chat)
        self._stack.setCurrentWidget(self._chat)
        self._reload.setVisible(True)
        self._chat.chatEnded.connect(self._on_chat_ended)
        self._chat.open_session(tokens)

    def _reload_chat(self) -> None:
        """Reload the chat; from a notice (the chat ended, or never kept its sign-in) open a new one instead."""
        if self._chat is None or self._minting or self._signing_out:
            return
        if self._stack.currentWidget() is self._notice:
            self._mint(PURPOSE_OPEN, self._family_id)  # the chat's session is gone: a fresh mint and a fresh chat
        else:
            self._chat.reload()

    def _discard_chat(self) -> None:
        if self._chat is None:
            return
        self._chat_generation += 1
        self._chat.close_session()
        self._stack.removeWidget(self._chat)
        self._chat.deleteLater()
        self._chat = None
        self._reload.setVisible(False)

    def _let_workers_outlive_the_panel(self) -> None:
        """A fetch or a mint can wait out its timeout; the pane mustn't wait, or destroy it running."""
        workers: list[QtCore.QThread] = [*self._tiers_workers, *self._session_workers]
        for worker in workers:
            if worker.isRunning() and not worker.wait(_WORKER_STOP_WAIT_MS):
                worker.setParent(None)
                _UNFINISHED_WORKERS.add(worker)
        self._tiers_workers = []
        self._session_workers = []

    def _revoke_chat_on_close(self) -> None:
        """End the chat's own session on a daemon thread that is never joined (needs no page, no Qt).

        Everything the thread uses is captured here, before the lease goes. It never touches the
        CLI's session; if it fails or is cut off, the record stays and the next open ends the child.
        It presents the sign-in the child was minted under, not the current settings: a failed sign-in
        on another profile or server changes those and leaves the child.
        """
        family, lease, minted_for = self._family_id, self._lease, self._family_minted_for
        if family is None or lease is None or minted_for is None:
            return
        (server, profile), store = minted_for, self._store

        def read_cli() -> Tokens | None:
            return store.current_tokens(profile, server)

        thread = threading.Thread(
            target=_end_chat_session,
            args=(server, profile, family, read_cli, lease.record_path),
            name="symbiote-close-revoke",
            daemon=True,
        )
        thread.start()

    def shutdown(self) -> None:
        """Called when the pane closes: no child process or port may outlive it."""
        # First: a write already queued on the main thread, or a delete confirmed after this,
        # re-checks its tier and finds read only, so nothing changes once the pane has gone.
        self._tool_server.set_allowed_tiers(READ_ONLY)
        self._closed = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(_WORKER_STOP_WAIT_MS)
        POLICY.unsubscribe(self._sync_captures_label)
        self._tiers_generation += 1
        self._restart_timer.stop()
        self._retry_timer.stop()
        self._let_workers_outlive_the_panel()
        self._microagent.stop()
        self._tool_server.stop()
        if not self._sign_out_on_close():  # a pending Sign out ends the chat's session itself
            self._revoke_chat_on_close()
        self._discard_chat()
        # Last: the record stays unless the close thread ends the session, so the next open does it.
        if self._lease is not None:
            self._lease.release()
