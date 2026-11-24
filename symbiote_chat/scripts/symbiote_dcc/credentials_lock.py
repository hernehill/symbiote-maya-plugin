"""One lock for every writer of ~/.symbiote/credentials: the CLI, the DCC panels, the deploy tools.

Canonical here; credentials_lock_sync.py --update copies it verbatim to the CLI and tools/deploy.
Keep lines ≤ 100: the copies are formatted at 100. Stdlib only, Python 3.9+. Held only around
read -> change -> write, never across a network call; reads take no lock. POSIX: flock (whole
file), O_NOFOLLOW against a planted symlink. Windows: msvcrt.locking on byte 0, no such guard.
"""

from __future__ import annotations

import errno
import importlib
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE_NAME = "credentials.lock"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05
_WINDOWS_BUSY = (errno.EACCES, errno.EDEADLK)


class CredentialsLockTimeout(TimeoutError):
    """Another process held the credentials lock past the timeout."""


class _Held(threading.local):
    def __init__(self) -> None:
        self.dirs: set[str] = set()  # the real paths this thread holds the lock in


_held = _Held()


def _try_lock(fd: int) -> bool:
    """True when this call took the lock, False only when another holder has it."""
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if os.name == "nt":
            busy = exc.errno in _WINDOWS_BUSY
        else:
            busy = isinstance(exc, BlockingIOError)
        if busy:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def credentials_lock(config_dir: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold ``<config_dir>/credentials.lock``; not re-entrant within a thread."""
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(config_dir, 0o700)
    key = os.path.realpath(config_dir)
    if key in _held.dirs:
        raise RuntimeError("credentials_lock is not re-entrant")
    path = config_dir / LOCK_FILE_NAME
    no_follow = 0 if os.name == "nt" else os.O_NOFOLLOW  # Windows has no O_NOFOLLOW
    fd = os.open(path, os.O_RDWR | os.O_CREAT | no_follow, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_SET)  # msvcrt.locking locks from the file position
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                message = f"Another Symbiote process has held {path} for {timeout:g} s; try again."
                raise CredentialsLockTimeout(message)
            time.sleep(LOCK_POLL_SECONDS)
        _held.dirs.add(key)
        try:
            yield
        finally:
            _held.dirs.discard(key)
            _unlock(fd)
    finally:
        os.close(fd)


def _replace(source: str, target: Path) -> None:
    # Windows refuses to replace a file a reader has open; the reader lets go in moments.
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.replace(source, target)
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_POLL_SECONDS)


def write_atomically(path: Path, text: str, prefix: str = ".creds-") -> None:
    """Replace ``path`` with ``text`` (UTF-8) through a 0600 temp file beside it. No fsync."""
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(text.encode("utf-8"))
        _replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
