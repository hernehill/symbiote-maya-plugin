"""A loopback-only MCP server, over TCP, that the microagent connects to."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import socketserver
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from symbiote_dcc.mcp_protocol import READ_ONLY, AllowedTiers, handle_line

logger = logging.getLogger(__name__)

# The only address the tool server listens on; the microagent's config names the same one.
BIND_HOST = "127.0.0.1"
NONCE_META_KEY = "symbiote/toolServerNonce"
PROOF_META_KEY = "symbiote/toolServerProof"
CLIENT_PROOF_LABEL = b"symbiote-tool-server/client\x00"
SERVER_PROOF_LABEL = b"symbiote-tool-server/server\x00"
NONCE_MIN_CHARS = 43
NONCE_MAX_CHARS = 128
_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
FIRST_LINE_DEADLINE_SECONDS = 10
FIRST_LINE_MAX_BYTES = 65_536
MAX_PENDING_CONNECTIONS = 16
_MAX_LINE_BYTES = 1_048_576
_RECV_BYTES = 65_536
_JSON_OBJECT_START = b"{"


def proof(token: str, label: bytes, nonce: str) -> str:
    """HMAC-SHA256 of ``label + nonce`` keyed by the session token, as lowercase hex."""
    # The nonce is checked as URL-safe ASCII before this is called, so "ascii" gives the UTF-8 bytes.
    return hmac.new(token.encode("utf-8"), label + nonce.encode("ascii"), hashlib.sha256).hexdigest()


class _Handler(socketserver.BaseRequestHandler):
    """Serves one client; the first line must be an ``initialize`` carrying the session token."""

    server: _Server

    def handle(self) -> None:
        server = self.server
        if not server.pending.acquire(blocking=False):
            return
        try:
            first_and_rest = self._judge_first_line()
        finally:
            server.pending.release()
        if first_and_rest is None:
            return
        first, rest, nonce = first_and_rest
        self.request.settimeout(None)
        try:
            self._serve(first + b"\n", bytearray(rest), nonce)
        except OSError:  # the client went away mid-conversation
            return

    def _judge_first_line(self) -> tuple[bytes, bytes, str] | None:
        """The first line, the bytes read past it and the client's nonce when it authenticated, else None.

        Nothing before authentication may raise: an exception would reach ``handle_error``
        and print a traceback to the DCC console for an unauthenticated caller.
        """
        try:
            buffer = self._read_first_line()
            if buffer is None:
                return None
            first, _, rest = buffer.partition(b"\n")
            # A browser page can reach this port. Its request starts with an HTTP request
            # line, so closing here means the JSON body behind it is never read or run.
            if not first.lstrip().startswith(_JSON_OBJECT_START):
                return None
            nonce = self._authenticated_nonce(first)
            return None if nonce is None else (first, rest, nonce)
        except Exception as exc:  # noqa: BLE001 - refuse; the client learns nothing
            # The type only: the payload is an unauthenticated caller's bytes.
            logger.debug("tool server: refused a connection (%s)", type(exc).__name__)
            return None

    def _read_first_line(self) -> bytes | None:
        """Everything read up to and past the first newline, or None when refused."""
        deadline = time.monotonic() + FIRST_LINE_DEADLINE_SECONDS
        buffer = b""
        while b"\n" not in buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.request.settimeout(left)
            try:
                chunk = self.request.recv(_RECV_BYTES)
            except OSError:  # includes the timeout
                return None
            if not chunk:
                return None
            buffer += chunk
            if len(buffer) > FIRST_LINE_MAX_BYTES and buffer.find(b"\n", 0, FIRST_LINE_MAX_BYTES + 1) < 0:
                return None
        if buffer.index(b"\n") > FIRST_LINE_MAX_BYTES:
            return None
        return buffer

    def _authenticated_nonce(self, line: bytes) -> str | None:
        """The client's nonce when its ``initialize`` proves it holds the token, else None."""
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return None
        if not isinstance(message, dict) or message.get("method") != "initialize":
            return None
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return None
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            return None
        nonce, client_proof = meta.get(NONCE_META_KEY), meta.get(PROOF_META_KEY)
        if not isinstance(nonce, str) or not isinstance(client_proof, str):
            return None
        if not NONCE_MIN_CHARS <= len(nonce) <= NONCE_MAX_CHARS or not _NONCE_PATTERN.fullmatch(nonce):
            return None
        expected = proof(self.server.token, CLIENT_PROOF_LABEL, nonce)
        try:
            matches = hmac.compare_digest(client_proof.encode("utf-8"), expected.encode("ascii"))
        except UnicodeEncodeError:  # a lone surrogate cannot be a proof
            return None
        if not matches or not self.server.remember_nonce(nonce):
            return None
        return nonce

    def _serve(self, line: bytes, buffer: bytearray, nonce: str) -> None:
        while True:
            with self.server.handling():
                reply = handle_line(line, self.server.tools, self.server.server_name, self.server.current_tiers)
                if nonce and reply is not None:
                    # The first reply proves to the client that this server holds the token too.
                    reply["result"]["_meta"] = {PROOF_META_KEY: proof(self.server.token, SERVER_PROOF_LABEL, nonce)}
                    nonce = ""
                if reply is not None:
                    self.request.sendall(json.dumps(reply).encode("utf-8") + b"\n")
            line = self._next_line(buffer)
            if not line:
                return

    def _next_line(self, buffer: bytearray) -> bytes:
        """Remove and return the next line from ``buffer``, reading more as needed.

        At EOF a final line without a newline is still returned; empty means the client is gone.
        """
        while b"\n" not in buffer and len(buffer) < _MAX_LINE_BYTES:
            chunk = self.request.recv(_RECV_BYTES)
            if not chunk:
                break
            buffer += chunk
        cut = buffer.find(b"\n", 0, _MAX_LINE_BYTES)
        cut = cut + 1 if cut >= 0 else min(len(buffer), _MAX_LINE_BYTES)
        line = bytes(buffer[:cut])
        del buffer[:cut]
        return line


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, tools: Mapping[str, Mapping[str, Any]], server_name: str, token: str) -> None:
        self.tools = tools
        self.server_name = server_name
        self.token = token
        # Replaced whole, never changed in place: a request reads one consistent value.
        self.allowed_tiers = READ_ONLY
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(MAX_PENDING_CONNECTIONS)
        self._seen_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()
        super().__init__((BIND_HOST, 0), _Handler)

    def server_bind(self) -> None:
        # Windows lets another user bind the same port unless the listener is exclusive;
        # Linux and macOS already refuse a second bind on a held port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def current_tiers(self) -> AllowedTiers:
        return self.allowed_tiers

    @contextmanager
    def handling(self) -> Iterator[None]:
        """Count one request, from reading it to sending its reply, as in flight."""
        with self._in_flight_lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def busy(self) -> bool:
        with self._in_flight_lock:
            return self._in_flight > 0

    def remember_nonce(self, nonce: str) -> bool:
        """Record ``nonce``; False when this server has accepted it before."""
        with self._nonce_lock:
            if nonce in self._seen_nonces:
                return False
            self._seen_nonces.add(nonce)
            return True


class ToolServer:
    """Serves ``tools`` on an ephemeral ``BIND_HOST`` port until stopped.

    It starts serving read tools only; ``set_allowed_tiers`` widens or narrows that.
    """

    def __init__(self, tools: Mapping[str, Mapping[str, Any]], server_name: str) -> None:
        self._token = secrets.token_urlsafe(32)
        self._server = _Server(tools, server_name, self._token)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def token(self) -> str:
        return self._token

    @property
    def allowed_tiers(self) -> AllowedTiers:
        return self._server.allowed_tiers

    def set_allowed_tiers(self, tiers: AllowedTiers) -> None:
        """Serve ``tiers`` from the next request on, on every connection."""
        self._server.allowed_tiers = tiers

    def busy(self) -> bool:
        """Whether a request is being handled now; a microagent restart would cut off its reply."""
        return self._server.busy()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
