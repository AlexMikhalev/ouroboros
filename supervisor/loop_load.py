"""The supervisor consumer's load instruments, in memory only.

The supervisor has one consumer thread (the ``server.py`` loop) and one process-wide
queue lock Q (``supervisor.queue._queue_lock``). These windows let the one compact
``supervisor_load`` row per minute (``ouroboros/server_liveness.py``) say where the
consumer's time went: for Q, how long its outermost acquisitions waited and held it and
from which code site the window's longest hold came. Each window is a measurement only:
nothing here touches disk, a store or the event bus, decides anything or can raise into
the code it measures, and its one reader drains it (returns it and starts the next).
Stdlib only, so the queue module builds Q from it at import.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

_REPO_PREFIX = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep


def code_site(site: Optional[Tuple[str, int, str]]) -> Optional[str]:
    """``path:line in function`` (repository files relative), or None when no site was seen."""
    if not site:
        return None
    filename, lineno, name = site
    if filename.startswith(_REPO_PREFIX):
        filename = filename[len(_REPO_PREFIX):]
    return f"{filename.replace(os.sep, '/')}:{lineno} in {name}"


class _LockWindow:
    """Outermost holds of one lock that ended in the window: their waits, holds, longest site."""

    __slots__ = ("count", "wait_total", "wait_max", "hold_total", "hold_max", "hold_max_site")

    def __init__(self) -> None:
        self.count = 0
        self.wait_total = self.wait_max = self.hold_total = self.hold_max = 0.0
        self.hold_max_site: Optional[Tuple[str, int, str]] = None

    def note(self, waited: float, held: float, site: Optional[Tuple[str, int, str]]) -> None:
        self.count += 1
        self.wait_total += waited
        self.hold_total += held
        if waited > self.wait_max:
            self.wait_max = waited
        if held > self.hold_max:
            self.hold_max, self.hold_max_site = held, site

    def facts(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "wait_total_sec": round(self.wait_total, 3), "wait_max_sec": round(self.wait_max, 3),
            "hold_total_sec": round(self.hold_total, 3), "hold_max_sec": round(self.hold_max, 3),
            "hold_max_site": code_site(self.hold_max_site),
        }


class MeasuredRLock:
    """Q: a ``threading.RLock`` that also times its OUTERMOST acquisitions.

    The lock semantics are the wrapped RLock's own (``with``, ``acquire(blocking, timeout)``,
    ``release`` raising the RLock's RuntimeError for a thread that does not hold it,
    ``_is_owned``), so every existing caller keeps working unchanged. An outermost
    acquisition keeps how long it waited before the grant and its caller's code site; its
    matching release records that wait and the hold in the current window. A re-entrant
    acquisition only counts depth; a refused non-blocking or timed-out attempt records
    nothing. The owner-only depth and stamps are guarded by Q itself, the window by one
    small lock taken only while Q is held or by ``drain``, never before Q: the reader never
    waits for Q, since an instrument that took Q would measure itself.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._waited = self._granted = 0.0
        self._site: Optional[Tuple[str, int, str]] = None
        self._stats = threading.Lock()
        self._window = _LockWindow()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._acquire(blocking, timeout)

    def __enter__(self) -> bool:
        return self._acquire(True, -1)

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    def _acquire(self, blocking: bool, timeout: float) -> bool:
        if self._lock._is_owned():
            self._lock.acquire()  # re-entrant: granted at once, not measured again
            self._depth += 1
            return True
        asked = time.monotonic()
        if not self._lock.acquire(blocking, timeout):
            return False
        self._granted = time.monotonic()
        self._waited, self._depth = self._granted - asked, 1
        try:
            frame = sys._getframe(2)  # 0 this method, 1 acquire/__enter__, 2 the caller
            self._site = (frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)
        except ValueError:
            self._site = None
        finally:
            frame = None  # never keep a caller's frame alive
        return True

    def release(self) -> None:
        try:
            if self._lock._is_owned():
                self._depth -= 1
                if not self._depth:
                    held = time.monotonic() - self._granted
                    with self._stats:
                        self._window.note(self._waited, held, self._site)
                    self._site = None
        finally:
            self._lock.release()  # a thread that does not hold Q gets the RLock's own error

    def _is_owned(self) -> bool:
        return self._lock._is_owned()

    def drain(self) -> Dict[str, Any]:
        """The window's facts (holds that ended since the last drain), then a fresh window."""
        with self._stats:
            window, self._window = self._window, _LockWindow()
        return window.facts()

    def __repr__(self) -> str:
        return f"<MeasuredRLock {self._lock!r}>"
