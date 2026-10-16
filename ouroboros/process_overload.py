"""Sample an owned test tree and stop it before it consumes physical headroom.

RSS is summed (shared pages may be counted more than once). This intentionally
conservative admission/stop budget is not an OS memory/PID/file-descriptor quota.
Rapid growth between samples can exceed it, particularly on macOS.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import threading

from ouroboros import platform_layer as pl


@dataclass(frozen=True)
class MemberMemory:
    pid: int
    rss_bytes: int
    name: str


def _in_job(pid: int, job) -> bool:
    """Windows membership comes from the held Job, including reparented members."""
    import ctypes
    from ctypes import wintypes

    def read(handle):
        answer = wintypes.BOOL()
        if not pl._kernel32.IsProcessInJob(handle, job, ctypes.byref(answer)):
            raise OSError(ctypes.get_last_error(), "IsProcessInJob failed")
        return bool(answer.value)

    error, member = pl._windows_query(pid, read)
    if error == 87:  # The process exited while the process table was read.
        return False
    if error:
        return False  # An inaccessible stranger is not an attributed member.
    return bool(member)


def member_memory(container) -> list[MemberMemory]:
    """Read live RSS only for the container's Job, token or measured tree."""
    if pl.IS_WINDOWS:
        import psutil

        if container._job is None:
            return []
        members = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                if _in_job(proc.pid, container._job):
                    info = proc.info["memory_info"]
                    if info is None:
                        raise RuntimeError(f"RSS unavailable for Job member {proc.pid}")
                    members.append(MemberMemory(proc.pid, info.rss, proc.info["name"] or "unknown"))
            except psutil.NoSuchProcess:
                continue
        return members

    from ouroboros.process_containment import pids_with_env_marker

    pids = pids_with_env_marker(container._token)
    if pids is None:
        raise RuntimeError("contained process membership is unavailable")
    owned = set(pids)
    if container._birth and pl.process_start_time(container._root) == container._birth:
        owned.update([container._root, *pl.collect_descendant_pids(container._root)])
    # One ps RSS table per sample; psutil is intentionally a Windows-only dependency.
    result = subprocess.run(["ps", "-A", "-o", "pid=,rss=,comm="],
                            capture_output=True, text=True, timeout=5, check=True)
    members = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and int(fields[0]) in owned and int(fields[0]) != os.getpid():
            members.append(MemberMemory(int(fields[0]), int(fields[1]) * 1024, fields[2]))
    return members


class OverloadWatch:
    """A joined owner thread; a stop always survives as a nonempty reap error."""

    def __init__(self, container):
        from ouroboros.config import TEST_PROCESS_SAMPLE_SEC
        from ouroboros.test_parallelism import memory_budget, physical_memory

        initial = physical_memory()
        if initial is None:
            raise RuntimeError("overload_watch_unavailable: physical memory could not be read")
        self.limit_bytes = memory_budget(initial)
        self._container = container
        self._interval = TEST_PROCESS_SAMPLE_SEC
        self._done = threading.Event()
        self._thread = None
        self.diagnostic = ""

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="test-overload-watch", daemon=True)
        thread.start()
        self._thread = thread  # A failed start must never leave an unjoinable thread.

    def _run(self) -> None:
        from ouroboros.config import TEST_MEMORY_RESERVE_FRACTION
        from ouroboros.test_parallelism import physical_memory

        try:
            while not self._done.is_set():
                members = member_memory(self._container)
                memory = physical_memory()
                if memory is None:
                    raise RuntimeError("physical memory could not be read")
                total = sum(member.rss_bytes for member in members)
                reserve = int(memory.total_bytes * TEST_MEMORY_RESERVE_FRACTION)
                if members and (total > self.limit_bytes or memory.available_bytes < reserve):
                    largest = max(members, key=lambda member: member.rss_bytes)
                    self.diagnostic = (
                        f"overload_stop: tree_rss={total} limit={self.limit_bytes} "
                        f"available={memory.available_bytes} reserve={reserve}; "
                        f"largest pid={largest.pid} name={largest.name!r} rss={largest.rss_bytes}"
                    )
                    self._container.stop_tree()
                    return
                if self._done.wait(self._interval):
                    return
        except Exception as exc:
            # Missing samples must not silently disable a promised watch. The
            # owner gets a red outcome and the original diagnostic on reap.
            self.diagnostic = self.diagnostic or f"overload_watch_unavailable: {exc}"
            try:
                self._container.stop_tree()
            except Exception as stop_error:
                self.diagnostic += f"; tree stop unconfirmed: {stop_error}"

    def stop(self) -> str:
        self._done.set()
        if self._thread is not None:
            self._thread.join()  # all process-table/memory probes have bounded waits
        return self.diagnostic
