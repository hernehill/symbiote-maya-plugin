"""Which panel session each open panel holds, so the session of a panel that crashed is found next time.

One record per panel instance (two Maya windows on one profile each have their own), under
``<config dir>/<dcc>/``: ``panel-<profile>-<instance>.json`` names the session family the panel
holds, never a token, and ``panel-<profile>-<instance>.lock`` is locked by that panel for its
whole life. A record whose lock nobody holds belongs to a panel that is gone: an orphan.

Liveness is the lock alone, never a pid probe, so a reused pid or Windows' ``os.kill`` can't
make a dead panel look alive or a live one dead. The OS releases the lock when the process
dies, however it dies.
"""

from __future__ import annotations

import errno
import importlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from symbiote_dcc.credentials_lock import write_atomically

logger = logging.getLogger(__name__)

LockBackend = Callable[[int], bool]

_WINDOWS_BUSY = (errno.EACCES, errno.EDEADLK)  # as credentials_lock._try_lock
_INSTANCE = r"\d+-[0-9a-f]{8}"


def try_lock(fd: int, *, backend: LockBackend | None = None) -> bool:
    """True when this call took ``fd``'s lock, False only when another holder has it; other errors raise.

    The same primitives and error mapping as ``credentials_lock._try_lock``, which can't be
    imported for it: that file is byte-copied into the CLI and stays as it is.
    """
    if backend is not None:
        return backend(fd)
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")
            os.lseek(fd, 0, os.SEEK_SET)  # msvcrt.locking locks from the file position
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        busy = exc.errno in _WINDOWS_BUSY if os.name == "nt" else isinstance(exc, BlockingIOError)
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


def _open_lock_file(path: Path) -> int:
    no_follow = 0 if os.name == "nt" else os.O_NOFOLLOW  # Windows has no O_NOFOLLOW
    return os.open(path, os.O_RDWR | os.O_CREAT | no_follow, 0o600)


def _remove(path: Path) -> None:
    """Delete ``path``; Windows refuses while a scanner holds it open, so that is logged and left."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        logger.warning("Symbiote: couldn't remove %s yet (%s); the next panel to open removes it", path, exc)


def _let_go(fd: int, lock_path: Path) -> None:
    """Unlock and close ``fd``, then remove its lock file."""
    try:
        _unlock(fd)
    finally:
        os.close(fd)
    _remove(lock_path)


def _folder(directory: Path, dcc: str) -> Path:
    return directory / dcc


@dataclass
class PanelLease:
    """This panel's lock, held from open to close, and where its record goes."""

    instance: str
    lock_path: Path
    record_path: Path
    _fd: int | None = field(default=None, repr=False)

    def release(self) -> None:
        """Unlock, close and remove the lock file. The record stays: a later open revokes what it names.

        Only Sign out (``forget_panel``) or a close that revoked the session removes the record.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        _let_go(fd, self.lock_path)


def acquire_lease(directory: Path, dcc: str, profile: str, *, backend: LockBackend | None = None) -> PanelLease:
    """Take a new instance's lock under ``<directory>/<dcc>/``; raises OSError when it can't."""
    folder = _folder(directory, dcc)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    stem = f"panel-{profile}-{instance}"
    lock_path = folder / f"{stem}.lock"
    fd = _open_lock_file(lock_path)
    try:
        if not try_lock(fd, backend=backend):
            raise OSError(errno.EBUSY, f"another panel holds {lock_path}")
    except BaseException:
        os.close(fd)
        raise
    return PanelLease(instance, lock_path, folder / f"{stem}.json", fd)


def record_panel(lease: PanelLease, family_id: str) -> None:
    """Note the session family this panel holds now (mode 0600, never a token)."""
    write_atomically(lease.record_path, json.dumps({"family_id": family_id}), prefix=".panel-")


def forget_record(path: Path) -> None:
    """Delete the record at ``path`` (gone already is fine; a Windows refusal is logged and left)."""
    _remove(path)


def forget_panel(lease: PanelLease) -> None:
    """Delete this panel's record only, once its session has been revoked."""
    forget_record(lease.record_path)


@dataclass
class OrphanRecord:
    """A record no live panel holds. Its lock is held here until ``discard``, so no other scan takes it."""

    family_id: str | None  # None when the record is damaged: discarded, never revoked
    path: Path
    _fd: int | None = field(default=None, repr=False)
    _lock_path: Path | None = field(default=None, repr=False)

    def discard(self) -> None:
        """Delete the record, then unlock, close and remove its lock file."""
        _remove(self.path)
        if self._fd is None or self._lock_path is None:
            return
        fd, self._fd = self._fd, None
        _let_go(fd, self._lock_path)


def _family_in(record: Path) -> str | None:
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    family = raw.get("family_id") if isinstance(raw, dict) else None
    return family if isinstance(family, str) else None


def _take(record: Path, backend: LockBackend | None) -> OrphanRecord | None:
    """The record as an orphan holding its lock, or None when a live panel holds it or it has gone."""
    lock_path = record.with_suffix(".lock")
    fd = _open_lock_file(lock_path)  # O_CREAT: a record whose lock file is missing is an orphan
    try:
        free = try_lock(fd, backend=backend)
    except BaseException:
        os.close(fd)
        raise
    if not free:
        os.close(fd)
        return None
    try:
        family = _family_in(record)
    except FileNotFoundError:  # another panel's scan took and discarded it first
        _unlock(fd)
        os.close(fd)
        return None
    except BaseException:
        _unlock(fd)
        os.close(fd)
        raise
    return OrphanRecord(family, record, fd, lock_path)


def orphaned_records(
    directory: Path, dcc: str, profile: str, *, backend: LockBackend | None = None
) -> list[OrphanRecord]:
    """This profile's records whose lock no live panel holds. Each keeps its lock until ``discard``."""
    folder = _folder(directory, dcc)
    if not folder.is_dir():
        return []
    pattern = re.compile(rf"panel-{re.escape(profile)}-{_INSTANCE}\.json")
    records = sorted(path for path in folder.iterdir() if pattern.fullmatch(path.name))
    orphans: list[OrphanRecord] = []
    try:
        for record in records:
            orphan = _take(record, backend)
            if orphan is not None:
                orphans.append(orphan)
    except BaseException:
        for orphan in orphans:  # let go of what was taken, deleting nothing
            if orphan._fd is not None:
                _unlock(orphan._fd)
                os.close(orphan._fd)
        raise
    return orphans
