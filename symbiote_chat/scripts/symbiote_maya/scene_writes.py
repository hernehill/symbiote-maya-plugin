"""How many times a Symbiote tool has changed something the scene file saves.

A tool that waits on a dialog before saving compares ``WRITES.value`` from before the dialog with the value
after it: a change made while the dialog was open (a call serviced inside its event loop) shows as a difference.
"""

from __future__ import annotations

import threading


class WriteCounter:
    """A count that only goes up, safe to bump and read from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def bump(self) -> int:
        """Count one change to the scene; returns the new count."""
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


WRITES = WriteCounter()
