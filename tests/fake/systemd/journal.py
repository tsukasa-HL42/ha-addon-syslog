"""
Stand-in for ``systemd.journal`` used by the offline test bench.

The journal is a JSON-lines file named by the FAKE_JOURNAL environment
variable; the tests append entries to it while the forwarder runs. Each entry
carries ``__SEQ`` (its order), ``__CURSOR`` and ``__REALTIME_TIMESTAMP`` in
microseconds. Field conversion mimics python-systemd 235: timestamps become
datetimes in the local time zone, PRIORITY and _PID become integers.

Positioning follows sd-journal: after ``seek_cursor``/``seek_realtime`` the
next ``get_next`` returns the matching entry; after ``seek_tail`` a
``get_previous`` returns the last entry and ``get_next`` only returns entries
appended later.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

NOP = 0
APPEND = 1
INVALIDATE = 2

_CONVERT_TIMESTAMP = ("__REALTIME_TIMESTAMP", "_SOURCE_REALTIME_TIMESTAMP")
_CONVERT_INT = ("PRIORITY", "_PID")
# python-systemd 235: fromtimestamp(t / 1000000, _LOCAL_TIMEZONE)
_LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo


def _convert(raw: dict) -> dict:
    entry = dict(raw)
    for key in _CONVERT_TIMESTAMP:
        if key in entry:
            entry[key] = datetime.fromtimestamp(
                int(entry[key]) / 1_000_000, _LOCAL_TIMEZONE
            )
    for key in _CONVERT_INT:
        if key in entry:
            try:
                entry[key] = int(entry[key])
            except (TypeError, ValueError):
                pass
    return entry


class Reader:
    def __init__(self, path: str | None = None, **_kwargs) -> None:
        self._file = os.environ["FAKE_JOURNAL"]
        self._raw: list[dict] = []
        self._size = -1
        self._current = -1  # index of the entry we are on
        self._at_tail = False
        self._reload()

    # -- file handling -------------------------------------------------------
    def _reload(self) -> bool:
        try:
            size = os.path.getsize(self._file)
        except OSError:
            size = 0
        if size == self._size:
            return False
        with open(self._file, encoding="utf-8") as handle:
            data = handle.read()
        complete = data[: data.rfind("\n") + 1]
        self._raw = [json.loads(line) for line in complete.splitlines() if line]
        self._size = len(complete.encode("utf-8"))
        return True

    # -- positioning ---------------------------------------------------------
    def seek_tail(self) -> None:
        self._reload()
        self._current = len(self._raw) - 1
        self._at_tail = True

    def seek_head(self) -> None:
        self._current = -1
        self._at_tail = False

    def seek_cursor(self, cursor: str) -> None:
        if not isinstance(cursor, str) or not cursor.startswith("s="):
            raise ValueError("invalid cursor")
        target = int(cursor[2:])
        self._reload()
        index = next(
            (i for i, e in enumerate(self._raw) if e["__SEQ"] >= target),
            len(self._raw),
        )
        self._current = index - 1
        self._at_tail = False

    def seek_realtime(self, realtime) -> None:
        if isinstance(realtime, datetime):
            usec = int(realtime.timestamp() * 1_000_000)
        elif isinstance(realtime, int):
            usec = realtime
        else:
            usec = int(realtime * 1_000_000)
        self._reload()
        index = next(
            (
                i
                for i, e in enumerate(self._raw)
                if int(e["__REALTIME_TIMESTAMP"]) >= usec
            ),
            len(self._raw),
        )
        self._current = index - 1
        self._at_tail = False

    def test_cursor(self, cursor: str) -> bool:
        if 0 <= self._current < len(self._raw):
            return self._raw[self._current]["__CURSOR"] == cursor
        return False

    def get_previous(self, skip: int = 1) -> dict:
        if self._at_tail:
            self._at_tail = False
            if 0 <= self._current < len(self._raw):
                return _convert(self._raw[self._current])
            return {}
        if self._current - 1 < 0:
            return {}
        self._current -= 1
        return _convert(self._raw[self._current])

    def get_next(self, skip: int = 1) -> dict:
        if self._at_tail:
            self._at_tail = False
            return {}
        if self._current + 1 >= len(self._raw):
            self._reload()
        if self._current + 1 >= len(self._raw):
            return {}
        self._current += 1
        return _convert(self._raw[self._current])

    # -- waiting and iteration -----------------------------------------------
    def wait(self, timeout=None) -> int:
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        while True:
            if self._reload():
                return APPEND
            if deadline is not None and time.monotonic() >= deadline:
                return NOP
            time.sleep(0.02)

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        entry = self.get_next()
        if not entry:
            raise StopIteration
        return entry

    def close(self) -> None:
        pass
