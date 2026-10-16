"""WAL needs a fixed SQLite, enforced locks AND a positively identified local filesystem."""
import contextlib
import ctypes
import pathlib
import sqlite3
import sys

import pytest

from ouroboros import store, store_import, store_switch, usage_store
from tests.test_store import publish


@pytest.fixture(autouse=True)
def release_connections():
    store.close_connections()
    yield
    store.close_connections()


@pytest.mark.parametrize("version,fixed", [
    ((3, 37, 2), False), ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 9), True),
    ((3, 45, 9), False), ((3, 50, 4), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 50, 8), True), ((3, 51, 2), False), ((3, 51, 3), True), ((3, 53, 1), True)])
@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
@pytest.mark.parametrize("filesystem,local", [
    ("apfs", True), ("ext4", True), ("fixed", True), ("nfs", False), ("cifs", False),
    ("fuse.drive", False), ("9p", False), ("unknown", False), (None, False)])
def test_wal_requires_all_three_facts(monkeypatch, version, fixed, tier, filesystem, local):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: filesystem)
    assert store._journal_mode(pathlib.Path("store.sqlite"), tier) == (
        "WAL" if fixed and tier == store.TIER_ENFORCED and local else "PERSIST")


@pytest.mark.parametrize("tier,version", [(store.TIER_NAME, (3, 53, 1)), (store.TIER_ENFORCED, (3, 50, 4))])
def test_ineligible_engine_or_tier_never_probes_filesystem(monkeypatch, tier, version):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(store, "_filesystem_type", lambda _: pytest.fail("unnecessary filesystem probe"))
    assert store._journal_mode(pathlib.Path("store.sqlite"), tier) == "PERSIST"


@pytest.mark.parametrize("kind,expected", [
    ("ext4", "ext4"), ("xfs", "xfs"), ("nfs", "nfs"), ("nfs4", "nfs4"), ("cifs", "cifs"),
    ("smb3", "smb3"), ("fuse", "fuse"), ("fuseblk", "fuseblk"), ("fuse.drive", "fuse.drive"), ("9p", "9p")])
def test_linux_uses_longest_mount_including_bind_and_escaped_names(tmp_path, kind, expected):
    mount = tmp_path / "nested mount"
    mount.mkdir()
    escaped = str(mount).replace(" ", r"\040")
    table = f"1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:2 /bind {escaped} rw - {kind} source rw\n"
    assert store._linux_filesystem(mount / "store.sqlite", table) == expected
    assert store._linux_filesystem(tmp_path / "elsewhere.sqlite", table) == "ext4"
    assert store._linux_filesystem(mount, "bad mountinfo") is None
    assert store._linux_filesystem(mount, "") is None


@pytest.mark.parametrize("backing", ["ext4", "nfs", "fuse.drive", "missing"])
def test_overlay_requires_every_backing_directory_to_be_known_local(tmp_path, backing):
    overlay, lower, upper = [tmp_path / part for part in ("overlay", "lower", "upper")]
    for directory in (overlay, lower, upper):
        directory.mkdir()
    table = (f"1 0 8:1 / / rw - ext4 /dev/root rw\n"
             f"2 1 0:2 / {lower} rw - {backing} source rw\n"
             f"3 1 0:3 / {overlay} rw - overlay overlay rw,lowerdir={lower},upperdir={upper}\n")
    assert store._linux_filesystem(overlay / "store.sqlite", table) == ("overlay-local" if backing == "ext4" else None)
    hidden = f"1 0 0:1 / / rw - overlay overlay rw,lowerdir=/host/invisible\n"
    assert store._linux_filesystem(tmp_path, hidden) is None


@pytest.mark.parametrize("platform,kind,expected", [
    ("darwin", "apfs", "apfs"), ("darwin", "hfs", "hfs"), ("darwin", "smbfs", None),
    ("freebsd14", "ufs", "ufs"), ("freebsd14", "zfs", None), ("freebsd14", "nfs", None)])
def test_bsd_statfs_reads_the_native_filesystem_name(monkeypatch, tmp_path, platform, kind, expected):
    class StatFS:
        def __call__(self, path, pointer):
            record = pointer._obj
            assert type(record).fstypename.offset == (72 if platform == "darwin" else 280)
            record.fstypename = kind.encode("ascii")
            return 0
    class LibC:
        statfs64 = StatFS()
        statfs = StatFS()
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_kw: LibC())
    assert store._filesystem_type(tmp_path) == expected


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4, 5, 6])
def test_windows_only_accepts_fixed_volume_including_volume_mounts(monkeypatch, tmp_path, kind):
    class Volume:
        def __call__(self, path, root, size):
            root.value = "C:\\mounted-volume\\"
            return 1
    class Drive:
        def __call__(self, root):
            assert root == "C:\\mounted-volume\\"
            return kind
    class Kernel:
        GetVolumePathNameW = Volume()
        GetDriveTypeW = Drive()
    monkeypatch.setattr(store.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: Kernel(), raising=False)
    assert store._filesystem_type(tmp_path) == ("fixed" if kind == 3 else None)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32", "unknown"])
def test_unreadable_filesystem_is_unknown(monkeypatch, tmp_path, platform):
    def unavailable(*_args, **_kwargs):
        raise OSError("filesystem unavailable")
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.setattr(pathlib.Path, "read_text", unavailable)
    monkeypatch.setattr(store, "_bsd_filesystem", unavailable)
    monkeypatch.setattr(store, "_windows_filesystem", unavailable)
    assert store._filesystem_type(tmp_path) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS statfs integration")
def test_native_statfs_identifies_the_test_volume(tmp_path):
    # Synthetic data, but the real ABI: an offset or symbol error must not silently
    # disable WAL on the APFS/HFS test host.
    assert store._bsd_filesystem(tmp_path) in {"apfs", "hfs"}


@pytest.mark.parametrize("money", [False, True])
def test_wal_is_full_and_export_checkpoints_to_standalone_delete(tmp_path, monkeypatch, money):
    # The shipped interpreter may predate the fix. Override eligibility only for
    # this serial synthetic lifecycle; the production version gate above stays strict.
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(store, "_filesystem_type", lambda _path: "apfs")
    if money:
        usage_store.migrate_from_journal(tmp_path)
    else:
        publish(tmp_path)
    with usage_store.hold(tmp_path) if money else store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("retained", 42)
        assert txn.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert txn.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        held = txn.conn
    path = tmp_path / (usage_store.STORE_REL if money else store.STORE_REL)
    assert path.with_name(path.name + "-wal").exists()
    if money:
        result = usage_store.export_journal(tmp_path)
        retired = path.with_name(result["store_retired_as"])
    else:
        with store.open_store(tmp_path, write=True) as txn:
            txn.set_meta("phase", store.EXPORTING)
        store_import.export_facts(tmp_path)
        retired = path.with_name(store_switch._retire(tmp_path))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        held.execute("SELECT 1")
    assert all(not path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    with contextlib.closing(sqlite3.connect(retired)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT value FROM meta WHERE key='retained'").fetchone()[0] == "42"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("money", [False, True])
@pytest.mark.parametrize("mode", ["PERSIST", "WAL"])
def test_sibling_build_is_standalone_before_publication(tmp_path, monkeypatch, money, mode):
    monkeypatch.setattr(store, "_journal_mode", lambda *_: mode)
    if money:
        usage_store.migrate_from_journal(tmp_path)
    else:
        store_import.ensure_current_store(tmp_path, with_children=False)
    assert list((tmp_path / "state").glob("*.import-*")) == []


@pytest.mark.parametrize("mode", ["PERSIST", "WAL"])
def test_failed_build_rolls_back_and_releases_its_temporary_journal(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(store, "_journal_mode", lambda *_: mode)
    path = tmp_path / "unpublished.sqlite"
    with pytest.raises(ValueError):
        with contextlib.closing(store.connect(path, store.TIER_ENFORCED, create=True)) as conn:
            conn.execute("CREATE TABLE pending(value)")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO pending VALUES (1)")
            raise ValueError("interrupted build")
    assert all(not path.with_name(path.name + suffix).exists() for suffix in ("-journal", "-wal", "-shm"))
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM pending").fetchall() == []
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
