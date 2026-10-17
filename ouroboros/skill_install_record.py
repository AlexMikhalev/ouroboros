"""Install record: where an installed skill came from and why its bytes may run.

One host-owned file per installed name in the state plane
(``data/state/skills/<name>/install.json``), never inside the payload: it is not part of
the release digest and a payload edit cannot rewrite it. The record keeps the source row
as it was at install time (a later change of that row's trust never rewrites an existing
install), the exact release, the base digest that "Modified" compares against, and the
basis the acceptance gate reads.

Installs made before sources existed carry a payload sidecar instead. ``convert_legacy_records``
reads those sidecars once (startup) and writes the equivalent record: an OuroborosHub sidecar names
every file of the original release with its SHA-256, so its base digest is exact; a ClawHub one does
not, so its record says the original version is unknown (``legacy: true`` with an empty base).
Legacy installs keep the basis they ran under (``review``): an old "official" label never becomes
a trusted basis by conversion. The sidecars themselves stay where they are.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.skill_loader import skill_state_dir, skill_state_dir_path
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

INSTALL_RECORD_FILENAME = "install.json"
INSTALL_RECORD_SCHEMA_VERSION = 1
# trusted: installed from a source row the owner trusts; review: a review decides;
# owner_skip: the owner skipped review for these bytes; owner_edit: the owner (or
# Ouroboros on the owner's instruction) changed them; cyber: Cyber Pro judgment.
INSTALL_BASES = frozenset({"trusted", "review", "owner_skip", "owner_edit", "cyber"})
LEGACY_HUB_SOURCE_ID = "ouroboroshub-legacy"


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
    """Validate and atomically persist ``record``; returns what was written.

    ``release.digest`` is required, except on a ``legacy`` record whose original release is unknown.
    """
    basis = str(record.get("basis") or "")
    if basis not in INSTALL_BASES:
        raise ValueError(f"unknown install basis {basis!r}")
    release = record.get("release")
    if not isinstance(release, dict) or not (str(release.get("digest") or "") or record.get("legacy")):
        raise ValueError("install record needs release.digest")
    source = record.get("source")
    if not isinstance(source, dict) or not str(source.get("id") or ""):
        raise ValueError("install record needs source.id")
    payload = dict(record)
    payload["schema_version"] = INSTALL_RECORD_SCHEMA_VERSION
    payload.setdefault("entry", name)
    payload.setdefault("base_digest", str(release.get("digest") or ""))
    payload.setdefault("installed_at", utc_now_iso())
    payload["updated_at"] = utc_now_iso()
    atomic_write_json(skill_state_dir(drive_root, name) / INSTALL_RECORD_FILENAME, payload)
    return payload


def update_install_record(drive_root: pathlib.Path, name: str, **changes: Any) -> Optional[Dict[str, Any]]:
    """Merge ``changes`` into an existing record (``None`` removes a key); ``None`` when there is none."""
    current = read_install_record(drive_root, name)
    if current is None:
        return None
    for key, value in changes.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    current.pop("updated_at", None)
    return write_install_record(drive_root, name, current)


def clear_install_record(drive_root: pathlib.Path, name: str) -> None:
    try:
        install_record_path(drive_root, name).unlink()
    except FileNotFoundError:
        pass


def release_digest_from_inventory(files: List[Dict[str, Any]], install_specs: Any = None) -> str:
    """The release digest of a file inventory (``[{path, sha256}]``) without the files themselves.

    The same reduction ``skill_identity.payload_digest`` applies to a tree: paths in the loader's
    order (by path parts), each followed by NUL and its raw SHA-256, per-install notes skipped, then
    the declared install specs.
    """
    from ouroboros.marketplace.install_specs import install_specs_hash, normalize_install_specs
    from ouroboros.skill_identity import DIGEST_PREFIX, PER_INSTALL_NOTE_FILENAMES

    digest = hashlib.sha256()
    pairs = sorted(((str(item.get("path") or ""), str(item.get("sha256") or "").lower()) for item in files),
                   key=lambda pair: pathlib.PurePosixPath(pair[0]).parts)
    for rel, sha in pairs:
        if "/" not in rel and rel in PER_INSTALL_NOTE_FILENAMES:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha))
    auto_specs, _manual, _errors = normalize_install_specs(install_specs or [])
    if auto_specs:
        digest.update(b"\0install_specs\0")
        digest.update(install_specs_hash(auto_specs).encode("utf-8"))
    return DIGEST_PREFIX + digest.hexdigest()


def _legacy_hub_source(catalog_url: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    from ouroboros.skill_sources import repo_from_catalog_url

    repo, _ref = repo_from_catalog_url(catalog_url)
    matches = [row for row in rows if row["kind"] == "hub" and row.get("url") == repo]
    row = next((r for r in matches if r.get("part") != "official"), matches[0] if matches else None)
    if row is None:
        return {"id": LEGACY_HUB_SOURCE_ID, "kind": "hub", "name": "OuroborosHub (earlier address)", "url": repo}
    return {key: row[key] for key in ("id", "kind", "name", "url", "part") if row.get(key)}


def legacy_hub_record(sidecar: Dict[str, Any], rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The install record an OuroborosHub payload sidecar stands for, or ``None`` when it is unusable."""
    from ouroboros.skill_dependencies import _coerce_dependency_specs

    files = sidecar.get("files")
    if sidecar.get("source") != "ouroboroshub" or not isinstance(files, list) or not files:
        return None
    try:
        specs = ((sidecar.get("install_specs") or {}).get("raw")) if isinstance(sidecar.get("install_specs"), dict) else None
        digest = release_digest_from_inventory([item for item in files if isinstance(item, dict)],
                                               _coerce_dependency_specs(specs))
    except (ValueError, TypeError):
        return None
    return {
        "source": _legacy_hub_source(str(sidecar.get("catalog_url") or ""), rows),
        "entry": str(sidecar.get("slug") or ""),
        "release": {"version": str(sidecar.get("version") or ""), "digest": digest},
        "base_digest": digest, "installed_at": str(sidecar.get("installed_at") or utc_now_iso()),
        "basis": "review", "official": False, "installed_by": "owner", "turn_on_when_ready": False,
        "converted_from": ".ouroboroshub.json",
    }


def _legacy_clawhub_record(drive_root: pathlib.Path, name: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from ouroboros.marketplace.provenance import read_provenance

    provenance = read_provenance(drive_root, name) or {}
    if provenance.get("source") != "clawhub" or not provenance.get("slug"):
        return None
    row = next((r for r in rows if r["kind"] == "registry"), None)
    source = {key: row[key] for key in ("id", "kind", "name", "url") if row and row.get(key)} or {
        "id": "clawhub", "kind": "registry", "name": "ClawHub"}
    return {
        "source": source, "entry": str(provenance["slug"]), "legacy": True,
        "release": {"version": str(provenance.get("version") or ""), "digest": ""}, "base_digest": "",
        "installed_at": str(provenance.get("installed_at") or utc_now_iso()), "basis": "review",
        "official": False, "installed_by": "owner", "turn_on_when_ready": False, "converted_from": ".clawhub.json",
    }


def convert_legacy_records(drive_root: pathlib.Path, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Write install records for sidecar-era installs that have none. Idempotent; never raises."""
    report: Dict[str, Any] = {"converted": [], "skipped": []}
    try:
        if rows is None:
            from ouroboros.skill_sources import load_sources

            rows = load_sources()
        skills_root = pathlib.Path(drive_root) / "skills"
        for bucket, marker in (("ouroboroshub", ".ouroboroshub.json"), ("clawhub", ".clawhub.json")):
            root = skills_root / bucket
            for payload in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) \
                    if root.is_dir() else []:
                name = payload.name
                if not (payload / marker).is_file() or install_record_path(drive_root, name).is_file():
                    continue
                if bucket == "ouroboroshub":
                    record = legacy_hub_record(read_json_dict(payload / marker) or {}, rows)
                else:
                    record = _legacy_clawhub_record(pathlib.Path(drive_root), name, rows)
                if record is None:
                    report["skipped"].append(name)
                    continue
                write_install_record(pathlib.Path(drive_root), name, record)
                report["converted"].append(name)
    except Exception:
        log.warning("Legacy install record conversion failed", exc_info=True)
    return report


__all__ = [
    "INSTALL_BASES", "INSTALL_RECORD_FILENAME", "LEGACY_HUB_SOURCE_ID", "clear_install_record",
    "convert_legacy_records", "install_record_path", "legacy_hub_record", "read_install_record",
    "release_digest_from_inventory", "update_install_record", "write_install_record",
]
