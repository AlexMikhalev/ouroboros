"""A Windows reader may deny rename while the supervisor maintains JSONL logs."""

import ctypes
import logging
import os
from ctypes import wintypes

import pytest

from ouroboros.utils import append_jsonl
from supervisor import state


def _seed(tmp_path):
    path = tmp_path / "logs" / "events.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"event":"before"}\n')
    return path


@pytest.mark.parametrize("winerror", [32, 33])
def test_sharing_conflict_defers_without_losing_rows(tmp_path, monkeypatch, caplog, winerror):
    path = _seed(tmp_path)
    original = path.read_bytes()
    replace = os.replace

    def blocked(source, destination):
        if source == path:
            error = PermissionError("file in use")
            error.winerror = winerror
            raise error
        return replace(source, destination)

    monkeypatch.setattr(state.os, "replace", blocked)
    with caplog.at_level(logging.WARNING, logger=state.__name__):
        for _ in range(4):
            state.rotate_jsonl_log_if_needed(tmp_path, "events.jsonl", "events", max_bytes=1)
    assert path.read_bytes() == original
    assert not list((tmp_path / "archive").glob("events_*.jsonl"))
    assert len([record for record in caplog.records if "rotation deferred" in record.message]) == 1

    # The append-sidecar lock must have been released after each failed rename.
    append_jsonl(path, {"event": "after"})
    expected = path.read_bytes()
    monkeypatch.setattr(state.os, "replace", replace)
    state.rotate_jsonl_log_if_needed(tmp_path, "events.jsonl", "events", max_bytes=1)
    archives = list((tmp_path / "archive").glob("events_*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == expected
    assert path.read_bytes() == b""


def test_unrelated_io_failure_remains_visible(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    replace = os.replace

    def fail(source, destination):
        if source == path:
            raise OSError(28, "disk full")
        return replace(source, destination)

    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        state.rotate_jsonl_log_if_needed(tmp_path, "events.jsonl", "events", max_bytes=1)
    append_jsonl(path, {"event": "still writable"})


@pytest.mark.skipif(os.name != "nt", reason="actual Windows sharing semantics")
def test_real_reader_without_delete_sharing_cannot_kill_rotation(tmp_path):
    path = _seed(tmp_path)
    original = path.read_bytes()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = wintypes.BOOL
    # READ | WRITE sharing deliberately excludes DELETE.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        for _ in range(4):
            state.rotate_jsonl_log_if_needed(tmp_path, "events.jsonl", "events", max_bytes=1)
        assert path.read_bytes() == original
    finally:
        assert kernel.CloseHandle(handle)
    append_jsonl(path, {"event": "after reader closed"})
    expected = path.read_bytes()
    state.rotate_jsonl_log_if_needed(tmp_path, "events.jsonl", "events", max_bytes=1)
    archives = list((tmp_path / "archive").glob("events_*.jsonl"))
    assert len(archives) == 1 and archives[0].read_bytes() == expected
