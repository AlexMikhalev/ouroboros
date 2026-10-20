"""Installation identity and mutation authority for stored data-root pointers.

Only the configured canonical root may mint an identity. A copied/moved root
gets its own identity; a stored path never acquires authority by existing.
Identity-free isolated roots remain usable by tools and hermetic tests.
"""
from __future__ import annotations

import json
import logging
import pathlib
import uuid
from typing import Any

from ouroboros.store import StoreError, name_lock
from ouroboros.utils import atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)
IDENTITY_PATH = pathlib.Path("state/install.json")
TASK_ROOT_FIELDS = ("budget_drive_root", "drive_root", "child_drive_root", "headless_child_drive_root")


class StoreForeign(StoreError):
    """A stored pointer names another installation; no mutation was attempted."""

    reason = reason_code = "foreign_install_root"
    rollback_confirmed = True

    def __init__(self, path: Any, install_root: Any) -> None:
        self.path, self.install_root = pathlib.Path(path), pathlib.Path(install_root)
        super().__init__(f"{self.path} belongs to another Ouroboros install at {self.install_root}; "
                         "move it explicitly to work on it here")


def layout_root(path: Any) -> pathlib.Path:
    """Canonical parent of the existing headless/task-drive layouts, recursively."""
    root = pathlib.Path(path).resolve(strict=False)
    while True:
        parents = root.parents
        if (root.name == "data" and len(parents) > 3 and parents[1].name == "headless_tasks"
                and parents[2].name == "state"):
            root = parents[3]
        elif len(parents) > 1 and root.parent.name == "task_drives":
            root = parents[1]
        else:
            return root


def canonical_root() -> pathlib.Path:
    from ouroboros.config import resolve_data_dir

    return layout_root(resolve_data_dir())


def _read(root: pathlib.Path) -> dict | None:
    try:
        record = json.loads((root / IDENTITY_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise StoreError(f"install identity unreadable at {root / IDENTITY_PATH}: {exc}") from exc
    if (not isinstance(record, dict) or not record.get("id") or not record.get("root")
            or not isinstance(record.get("copied_from", []), list)
            or any(not isinstance(path, str) or not path for path in record.get("copied_from", []))):
        raise StoreError(f"install identity invalid at {root / IDENTITY_PATH}")
    return record


def initialize_install() -> dict:
    """Atomically establish this process's canonical identity at its first write/boot.

    The existing startup owned-process check reaches this before recovery. The
    same gate covers standalone writers. The lock serializes worker first touch;
    ordinary checks only read the small identity file.
    """
    root = canonical_root()
    record = _read(root)
    if record and record["root"] == str(root):
        return record
    from ouroboros.store import _ADMISSION_NO_WAIT, _WAIT_BUDGET
    budget = _WAIT_BUDGET.get()
    wait = 0.0 if _ADMISSION_NO_WAIT.get() else min(5.0, budget if budget is not None else 5.0)
    with name_lock(root, "install.lock", timeout_sec=wait):
        record = _read(root)
        if record and record["root"] == str(root):
            return record
        previous = record.get("root") if record else None
        origins = record.get("copied_from", []) if record else []
        record = {"id": uuid.uuid4().hex, "root": str(root), "created_at": utc_now_iso()}
        if previous:
            record["copied_from"] = list(dict.fromkeys([*origins, previous]))
        atomic_write_json(root / IDENTITY_PATH, record, trailing_newline=True, fsync=True)
        if previous:
            log.warning("This data directory moved from %s to %s and now has a separate Ouroboros "
                        "install identity. Saved paths keep their original owners; move tasks "
                        "explicitly to work on them here.", previous, root)
        return record


def _foreign_root(path: Any, *, initialize: bool = True) -> pathlib.Path | None:
    current = canonical_root()
    record = initialize_install() if initialize else _read(current)
    origins = (record or {}).get("copied_from", [])
    root = pathlib.Path(path).resolve(strict=False)
    # Ancestor identities also cover descendants of a layout drive and nested
    # isolated roots. Stop at the configured root: its ancestors do not own it.
    for candidate in (root, *root.parents):
        candidate = layout_root(candidate)
        if candidate == current:
            return None
        if str(candidate) in origins or _read(candidate) is not None:
            return candidate
    return None


def owned_root(path: Any) -> bool:
    """Whether this process may mutate PATH (unreadable identity raises, never grants)."""
    return _foreign_root(path) is None


def require_owned_root(path: Any) -> pathlib.Path:
    """Refuse before any lock, transaction, file copy or process registration."""
    root = pathlib.Path(path).resolve(strict=False)
    foreign = _foreign_root(root)
    if foreign is not None:
        raise StoreForeign(root, foreign)
    return root


def check_store_root(path: Any, *, write: bool) -> None:
    """Even a read transaction uses rw SQLite/journal/name-lock IO: refuse foreign roots.

    An own read stays creation-free; copied history in the current install is
    readable before its first boot/write establishes the identity.
    """
    foreign = _foreign_root(path, initialize=write)
    if foreign is not None:
        raise StoreForeign(pathlib.Path(path).resolve(strict=False), foreign)


def require_task_roots(task: dict) -> None:
    """Validate stored authorities before a duty follows any of a task's data roots."""
    for key in TASK_ROOT_FIELDS:
        if task.get(key):
            require_owned_root(task[key])


def rest_foreign_member(rest: Any, task_id: str, stored: dict, error: StoreForeign) -> None:
    """Disclose on this install's task, then rest its unchanged pointer until an explicit move.

    No foreign row is read. The reason write advances the member signature, so
    take its new witness once; a concurrent relocation prevents this rest.
    """
    from ouroboros import store
    from ouroboros.task_results import write_task_result

    fields = {"reason_code": error.reason, "reason_detail": str(error)}
    pointers = {key: stored.get(key) for key in TASK_ROOT_FIELDS}

    def unchanged(row: dict) -> bool:
        return bool(row) and all(row.get(key) == value for key, value in pointers.items())

    def disclose(current: dict, _patch: dict) -> dict | None:
        if not unchanged(current) or all(current.get(key) == value for key, value in fields.items()):
            return None
        return {"status": current["status"], **fields}

    write_task_result(rest.root, task_id, stored["status"], strict_existing_dict=True, _field_projector=disclose)
    with store.open_store(rest.root, write=False) as txn:
        current = txn.load(task_id) or {}
        if not unchanged(current) or any(current.get(key) != value for key, value in fields.items()):
            return
        rest.signatures[task_id] = list(txn.row(task_id, "write_seq", "updated_at"))
    rest.wait(task_id, error.reason, report=fields)
