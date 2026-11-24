"""Run the ``symbiote microagent`` CLI against this DCC session's tool server.

Uses ``subprocess`` and a poll: inside a DCC, ``QProcess`` kept reporting a child
as running after it had exited.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

from PySide6 import QtCore

from symbiote_dcc.adapter import DccAdapter
from symbiote_dcc.cli_version import (
    PARENT_WATCH_FEATURE,
    CliCheck,
    features,
    label,
    missing_features,
    parse_version,
)
from symbiote_dcc.net import dead_ca_paths
from symbiote_dcc.tool_server import BIND_HOST

logger = logging.getLogger(__name__)

# A GUI app on macOS does not inherit the shell's PATH.
_EXTRA_CLI_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"))
_POLL_INTERVAL_MS = 2000
_STOP_WAIT_SECONDS = 3
_VERSION_TIMEOUT_SECONDS = 15
# The tool server asks for a token (#2165). The config names this variable; the value reaches
# only the microagent's own environment.
TOKEN_ENV = "SYMBIOTE_TOOL_SERVER_TOKEN"
# `microagent start` exits with this (EX_CONFIG) on any terminal stop (symbiote_cli.microagent.autostart):
# a revoked session, a refused refresh, or another process taking the agent id. Only the
# server can say which, so the panel asks it rather than reading 78 as signed out.
TERMINAL_STOP_EXIT = 78


def find_cli() -> str | None:
    override = os.environ.get("SYMBIOTE_CLI")
    if override:
        return override
    search = os.pathsep.join([os.environ.get("PATH", ""), *_EXTRA_CLI_DIRS])
    return shutil.which("symbiote", path=search)


def check_cli(cli: str) -> CliCheck:
    """Run ``symbiote version`` once: why this CLI cannot be used (``None`` when it can), and its features."""
    try:
        run = subprocess.run(
            [cli, "version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CliCheck(f"could not run {cli} ({type(exc).__name__})", frozenset())
    output = run.stdout + run.stderr
    return CliCheck(_version_problem(cli, output), features(output))


def _version_problem(cli: str, output: str) -> str | None:
    version = parse_version(output)
    if version is None:
        return f"could not read the version of {cli}"
    missing = missing_features(output)
    if missing:
        return f"this CLI ({label(version)}) is missing {', '.join(missing)}; upgrade the symbiote CLI"
    return None


def agent_name(dcc: str) -> str:
    """Unique per DCC process: a second connection with one name is closed for good."""
    host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0])
    return f"{dcc}-{host}-{os.getpid()}"


def config_text(name: str, port: int, dcc: str, state_tool: str) -> str:
    """The microagent's config. YAML is a superset of JSON, and a DCC's Python may have no ``yaml``.

    The entry names the variable that holds the tool server's token, never the token.
    ``state_tool`` answers ``microagent_query_state`` with the DCC's scene summary (#2167). A
    CLI older than that ignores the key and the query times out, as it did before.
    """
    config = {
        # tier_switches: this panel has the "Imports" and "Scene changes" switches; the CLI forwards
        # it so the server can tell the model a missing tier is the artist's switch. A CLI too old to
        # know the key ignores it, and the server then gives no switch advice.
        "agent": {"name": name, "state_tool": state_tool, "tier_switches": True},
        "mcp_servers": {
            dcc: {
                "type": "tcp",
                "host": BIND_HOST,
                "port": str(port),
                "auth_token_env": TOKEN_ENV,
            }
        },
        "allowed_paths": [],
    }
    return json.dumps(config, indent=2) + "\n"


# Set by a DCC to find its own modules; inherited by the CLI, they would break or hijack it.
_DCC_PYTHON_VARIABLES = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")


def _drop_dead_ca_paths(environment: dict[str, str], dcc: str) -> None:
    """A dead CA path the DCC set would make the CLI's TLS setup fail with a bare ENOENT (net.dead_ca_paths)."""
    for name, value in dead_ca_paths(environment).items():
        del environment[name]
        logger.warning(
            "Symbiote: %s set %s to %s, which does not exist; the microagent starts without it", dcc, name, value
        )


def child_environment(parent: Mapping[str, str], server: str, token: str, *, dcc: str) -> dict[str, str]:
    """The microagent's environment: a copy of ``parent`` with the server and token set.

    The token goes in the child's copy only: not the DCC's own ``os.environ``, not argv, not a
    file. A stale value inherited from the DCC is replaced.
    """
    environment = {**parent, "SYMBIOTE_SERVER": server, TOKEN_ENV: token}
    environment.pop("SYMBIOTE_PROFILE", None)
    # The DCC's module paths are the DCC's: the CLI is its own Python and must not load from them.
    for name in _DCC_PYTHON_VARIABLES:
        environment.pop(name, None)
    _drop_dead_ca_paths(environment, dcc)
    return environment


class MicroagentRunner(QtCore.QObject):
    """Owns the child process and reports its state as readable text.

    ``exited`` carries the exit code of a child that stopped by itself; ``stop()`` never emits it.
    """

    stateChanged = QtCore.Signal(str)
    exited = QtCore.Signal(int)

    def __init__(self, directory: Path, adapter: DccAdapter, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self._directory = directory
        self._adapter = adapter
        self._process: subprocess.Popen | None = None
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._check_process)
        self.log_path: Path | None = None
        # Fixed for the process: the panel names it to web-chat before the agent connects.
        self.agent_id = agent_name(adapter.name)

    def start(self, profile: str, server: str, port: int, token: str) -> str | None:
        """Start the microagent; the agent id it registers under, or ``None`` when the tools are off."""
        cli = find_cli()
        if cli is None:
            self._say("off (symbiote CLI not found)")
            return None
        check = check_cli(cli)
        if check.problem is not None:
            self._say(f"off ({check.problem})")
            return None
        name = self.agent_id
        try:
            self._launch(cli, profile, server, port, token, check.has(PARENT_WATCH_FEATURE))
        except OSError as exc:
            # The panel opens the chat after this (#2168): a failed launch costs the tools only.
            logger.warning("Symbiote: could not start the microagent: %s", exc)
            self._say(f"off (could not start: {exc.strerror or exc})")
            return None
        self._poll.start()
        self._say(f"running as {name}")
        return name

    def _launch(self, cli: str, profile: str, server: str, port: int, token: str, watch_parent: bool) -> None:
        dcc = self._adapter.name
        runtime = self._directory / dcc
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = runtime / f"microagent-{os.getpid()}.yaml"
        config_path.write_text(config_text(self.agent_id, port, dcc, self._adapter.state_tool), encoding="utf-8")
        self.log_path = runtime / f"microagent-{os.getpid()}.log"
        environment = child_environment(os.environ, server, token, dcc=self._adapter.ui.display_name)
        command = [cli, "--profile", profile, "microagent", "start", "--config", str(config_path)]
        if watch_parent:
            # Quitting or crashing the DCC never reaches stop(): the CLI watches this PID and
            # exits with it (#2187). Older CLIs lack the option and are stopped by stop() only.
            command = [*command, "--exit-with-parent", str(os.getpid())]
        with open(self.log_path, "ab") as log:
            self._process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)

    def _say(self, state: str) -> None:
        self.stateChanged.emit(f"{self._adapter.ui.display_name} tools: {state}")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        self._poll.stop()
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(_STOP_WAIT_SECONDS)

    def _check_process(self) -> None:
        if self._process is None:
            return
        code = self._process.poll()
        if code is not None:
            self._poll.stop()
            self._process = None
            self._say(f"stopped (exit {code}), see {self.log_path}")
            self.exited.emit(code)
