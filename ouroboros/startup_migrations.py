"""Visible lifecycle import of current boot state, never a reader fallback.

Run before producers on every startup door: ``ensure_current_store`` creates or
reconciles ``state/current.sqlite`` (the one importer; details in
``store_import``), the admissible legacy cancel latches become intents, and the
boot admissions no document derives (drives present on disk, the one-time upgrade
receipts) join the owner-held rows. ``state/migrations.json`` keeps the
watermarks: the store generation and the cancel-latch generation.
``python -m ouroboros.startup_migrations`` is the explicit rebuild job, including
its historical seal audit (no audit watermark).
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from ouroboros.utils import append_jsonl, read_json_dict, update_json_locked, utc_now_iso

log = logging.getLogger(__name__)
SCHEMA_GENERATION = 7


def watermarks(root) -> dict:
    path = Path(root) / "state" / "migrations.json"
    if not path.exists():
        return {}
    value = read_json_dict(path)
    if value is None:
        raise ValueError(f"migration watermarks unreadable: {path}")
    return value


def stamp(root, **facts):
    target = Path(root) / "state" / "migrations.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return update_json_locked(target, lambda current: {**current, **facts}, strict_existing_dict=True)


def migrate_cancel_latches(root, *, records, generation=SCHEMA_GENERATION, rebuild=False):
    """The admissible ``cancel_requested`` rows of RECORDS (``(task_id, document, error)`` as the
    importer admitted them; ``None`` reads the store's rows) become cancel intents once per
    generation; the explicit rebuild reruns it over the store."""
    from ouroboros.cancel_intents import migrate_legacy_cancel_latches

    if not rebuild and watermarks(root).get("cancel_latches_generation") == generation:
        return []
    migrated = migrate_legacy_cancel_latches(root, records=None if records is None else list(records))
    stamp(root, cancel_latches_generation=generation)
    return migrated


def boot_admissions(root) -> dict:
    """Owner-held rows no document derives: a drive directory present on disk owes its terminal
    files (a terminal result can land in the drive before the canonical start/result did), and
    the one-time upgrade notice receipts are imported from chat once."""
    from ouroboros.notice_receipts import import_upgrade_receipts
    from ouroboros.task_custody import own_child_drives

    root = Path(root)
    pending = {}
    for base in (root / "state/headless_tasks", root / "task_drives"):
        if base.exists():
            for path in base.iterdir():
                if path.is_dir() and not path.is_symlink():
                    tid = path.name
                    pending[tid] = {"task_id": tid, "drives": [str(child) for child in own_child_drives(root, tid)]}
    return {"pending_drives": pending, "upgrade_notices": import_upgrade_receipts(root)}


def prepare_startup_state(root, *, rebuild=False, repo_dir=None, strict=True):
    """Boot may continue with an unavailable store (every door then answers typed); the explicit
    repair reports its failure."""
    try:
        return _prepare_startup_state(root, rebuild=rebuild)
    except Exception as exc:
        log.error("Startup import failed; the current-facts store is unavailable", exc_info=True)
        try:
            append_jsonl(Path(root) / "logs/supervisor.jsonl", {"ts": utc_now_iso(), "type": "startup_migration",
                "phase": "failed", "job": "current_store", "error": type(exc).__name__})
        except Exception:
            log.error("Could not persist the startup migration failure", exc_info=True)
        if strict:
            raise
        return {"imported": False, "status": "unavailable"}


def _prepare_startup_state(root, *, rebuild):
    from ouroboros.store_import import ensure_current_store

    root = Path(root)
    try:
        watermarks(root)
    except ValueError:
        # A torn watermark file is rewritten empty: every job below then runs and restamps it.
        log.warning("Migration watermarks unreadable; rewriting them and re-running the lifecycle import")
        from ouroboros.utils import atomic_write_json
        atomic_write_json(root / "state" / "migrations.json", {}, trailing_newline=True)
    started = time.monotonic()
    report = ensure_current_store(root)
    if rebuild:
        from ouroboros.delegate_custody_current import rebuild as rebuild_custody
        report["custody_open"] = rebuild_custody(root, replace=True)
        report["cancel_latches"] = len(migrate_cancel_latches(root, records=None, rebuild=True))
    append_jsonl(root / "logs/supervisor.jsonl", {"ts": utc_now_iso(), "type": "startup_migration",
        "phase": "completed", "job": "current_store", "counts": {key: value for key, value in report.items()
                                                                  if key != "drives"},
        "duration_seconds": time.monotonic() - started})
    return {"imported": report.get("status") == "completed", **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    report = prepare_startup_state(args.data_root, rebuild=True)
    from ouroboros.model_send_seal import reconcile_model_send_seals
    report["audit"] = reconcile_model_send_seals(args.data_root)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
