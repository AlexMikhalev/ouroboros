"""Install record: where an installed skill came from and why its bytes may run.

One host-owned file per installed name in the state plane
(``data/state/skills/<name>/install.json``), never inside the payload: it is not part of
the release digest and a payload edit cannot rewrite it. The record keeps the source row
as it was at install time (a later change of that row's trust never rewrites an existing
install), the exact release, the base digest that "Modified" compares against, and the
basis the acceptance gate reads.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional

from ouroboros.skill_loader import skill_state_dir, skill_state_dir_path
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

INSTALL_RECORD_FILENAME = "install.json"
INSTALL_RECORD_SCHEMA_VERSION = 1
# trusted: installed from a source row the owner trusts; review: a review decides;
# owner_skip: the owner skipped review for these bytes; owner_edit: the owner (or
# Ouroboros on the owner's instruction) changed them; cyber: Cyber Pro judgment.
INSTALL_BASES = frozenset({"trusted", "review", "owner_skip", "owner_edit", "cyber"})


def install_record_path(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    return skill_state_dir_path(drive_root, name) / INSTALL_RECORD_FILENAME


def read_install_record(drive_root: pathlib.Path, name: str) -> Optional[Dict[str, Any]]:
    """The record, or ``None`` when the skill was not installed from a source."""
    data = read_json_dict(install_record_path(drive_root, name))
    if not data or data.get("schema_version") != INSTALL_RECORD_SCHEMA_VERSION:
        return None
    if str(data.get("basis") or "") not in INSTALL_BASES:
        return None
    return data


def write_install_record(drive_root: pathlib.Path, name: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and atomically persist ``record``; returns what was written."""
    basis = str(record.get("basis") or "")
    if basis not in INSTALL_BASES:
        raise ValueError(f"unknown install basis {basis!r}")
    release = record.get("release")
    if not isinstance(release, dict) or not str(release.get("digest") or ""):
        raise ValueError("install record needs release.digest")
    source = record.get("source")
    if not isinstance(source, dict) or not str(source.get("id") or ""):
        raise ValueError("install record needs source.id")
    payload = dict(record)
    payload["schema_version"] = INSTALL_RECORD_SCHEMA_VERSION
    payload.setdefault("entry", name)
    payload.setdefault("base_digest", release["digest"])
    payload.setdefault("installed_at", utc_now_iso())
    payload["updated_at"] = utc_now_iso()
    atomic_write_json(skill_state_dir(drive_root, name) / INSTALL_RECORD_FILENAME, payload)
    return payload


def clear_install_record(drive_root: pathlib.Path, name: str) -> None:
    try:
        install_record_path(drive_root, name).unlink()
    except FileNotFoundError:
        pass
