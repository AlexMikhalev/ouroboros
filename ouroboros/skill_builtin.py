"""The built-in skill source: the skills bundled with the app.

The repository's ``skills/`` folder is an ordinary source with id ``builtin``; its index
``skills/builtin.json`` names each bundled skill with its categories and whether the
maintainer recommends it for a new install. Installing a built-in copies the bundled
folder into ``data/skills/native/<name>`` (the per-install note ``.seed-origin`` keeps
older readers working) and writes the same install record every installer writes, with
the basis the built-in row of the source list gives (trusted by default; the derived list
reads ``OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS``). The acceptance gate admits exactly those bytes;
no synthetic review verdict is written, and grants follow the ordinary auto-grant
setting.

A new install gets only the built-ins its starter selection ticks (the starter installer
calls ``install_builtin_skill``); an existing install keeps what it has. Built-ins ride
app releases: at startup an unmodified built-in whose bundled version changed is re-seeded
in place, keeping its enablement, its Telegram state and the owner's grants while the
requested keys and permissions are unchanged; a copy with local changes is kept and the
owner chooses on update. Nothing re-installs a built-in the owner removed.
A built-in no longer bundled loses its launcher marker and keeps running as a skill that
left its source.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import tempfile
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

BUILTIN_SOURCE = {"id": "builtin", "kind": "builtin", "name": "Built in"}
SEED_MARKER = ".seed-origin"
_MANIFEST_NAMES = ("SKILL.md", "skill.json")


def bundled_skills_dir() -> pathlib.Path:
    from ouroboros.config import REPO_DIR

    return pathlib.Path(REPO_DIR) / "skills"


def builtin_trust() -> str:
    """The built-in source's trust: its row in the source list (while no list is saved, the derived row
    follows ``OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS``, as does a saved list without a built-in row)."""
    from ouroboros.config import get_trust_native_seeded_skills
    from ouroboros.skill_sources import BUILTIN_ID, load_sources, source_by_id

    row = source_by_id(load_sources(), BUILTIN_ID)
    if row is not None:
        return str(row["trust"])
    return "trusted" if get_trust_native_seeded_skills() else "review"


def _is_seed(entry: pathlib.Path) -> bool:
    return entry.is_dir() and not entry.name.startswith(".") and any(
        (entry / name).is_file() for name in _MANIFEST_NAMES
    )


def _manifest(skill_dir: pathlib.Path) -> Any:
    from ouroboros.skill_identity import _parse_manifest

    return _parse_manifest(skill_dir)


def _version(skill_dir: pathlib.Path) -> str:
    manifest = _manifest(skill_dir)
    return str(getattr(manifest, "version", "") or "").strip() if manifest is not None else ""


def _write_record(drive_root: pathlib.Path, name: str, *, version: str, digest: str, base_digest: Optional[str],
                  turn_on_when_ready: bool, installed_by: str, reason: str = "", task_id: str = "",
                  converted: bool = False) -> Dict[str, Any]:
    from ouroboros.skill_install_record import write_install_record

    record: Dict[str, Any] = {
        "source": dict(BUILTIN_SOURCE),
        "entry": name,
        "release": {"version": version, "digest": digest, "path": f"skills/{name}"},
        "basis": builtin_trust(),
        "official": True,  # the app's own bundled release, exactly as shipped
        "installed_by": installed_by,
        "turn_on_when_ready": bool(turn_on_when_ready),
    }
    if base_digest is not None:
        record["base_digest"] = base_digest  # "" = the original bytes are unknown
    if reason:
        record["reason"] = reason
    if task_id:
        record["task_id"] = task_id
    if converted:
        record["converted_from"] = SEED_MARKER
    return write_install_record(drive_root, name, record)


def install_builtin_skill(
    name: str,
    *,
    drive_root: Optional[pathlib.Path] = None,
    seed_dir: Optional[pathlib.Path] = None,
    turn_on_when_ready: bool = False,
    installed_by: str = "owner",
    reason: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    """Install, or re-seed in place, one bundled built-in; returns a typed result.

    Lands the bundled folder atomically, writes the install record, carries the owner's
    grants across a re-seed and applies the auto-grant setting. Enablement is left to the
    caller's ``reconcile_turn_on_when_ready`` (inside its lifecycle job) or the owner.
    Refuses a name another installed payload already uses.
    """
    from ouroboros.config import DATA_DIR
    from ouroboros.skill_acceptance import carry_grants_to_current
    from ouroboros.skill_loader import auto_grant_if_enabled, discover_skill_identity, load_skill

    drive = pathlib.Path(drive_root or DATA_DIR)
    seed_root = pathlib.Path(seed_dir or bundled_skills_dir())
    seed = seed_root / name
    if not _is_seed(seed):
        return {"ok": False, "name": name, "error": f"no bundled built-in skill named {name!r}"}
    target = drive / "skills" / "native" / name
    if target.exists() and not (target / SEED_MARKER).is_file():
        return {"ok": False, "name": name, "error": f"{name!r} in the native folder is user-managed; not replaced"}
    resolved_target = target.resolve()
    occupants = [skill for skill in discover_skill_identity(drive, name)
                 if pathlib.Path(skill.skill_dir).resolve() != resolved_target]
    if occupants:
        return {"ok": False, "name": name, "code": "name_taken",
                "error": f"another installed skill already uses the name {name!r}: {occupants[0].skill_dir}"}
    native_root = drive / "skills" / "native"
    staging_root = native_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(staging_root)))
    try:
        shutil.copytree(seed, staging / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / name / SEED_MARKER).write_text(f"seeded_from={seed_root.name}\nsource=builtin\n", encoding="utf-8")
        from ouroboros.marketplace.fetcher import land_staged_tree

        land_staged_tree(staging / name, target, replacement_suffix="replaced-builtin")
    except OSError as exc:
        return {"ok": False, "name": name, "error": f"could not land built-in {name!r}: {exc}"}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    skill = load_skill(target, drive)
    if skill is None or skill.load_error or not skill.content_hash:
        return {"ok": False, "name": name, "error": f"built-in {name!r} did not load: "
                f"{getattr(skill, 'load_error', '') or 'no manifest'}"}
    record = _write_record(drive, skill.name, version=str(skill.manifest.version or ""), digest=skill.content_hash,
                           base_digest=None, turn_on_when_ready=turn_on_when_ready,
                           installed_by=installed_by, reason=reason, task_id=task_id)
    skill = load_skill(target, drive)  # the gate now reads the record just written
    carried = carry_grants_to_current(drive, skill)
    granted = auto_grant_if_enabled(drive, load_skill(target, drive))
    return {"ok": True, "name": skill.name, "digest": skill.content_hash, "basis": record["basis"],
            "turn_on_when_ready": bool(turn_on_when_ready), "grants_carried": carried,
            "auto_granted_keys": list(granted.granted_keys), "auto_granted_permissions": list(granted.granted_permissions)}


def _changed_since_install(drive_root: pathlib.Path, name: str, target: pathlib.Path) -> Optional[bool]:
    """Whether the installed copy differs from its recorded base digest; ``None`` when no
    base or evidence of edits is recorded (the launcher's version re-seed still applies).
    A stale native-seed review proves edits even when the original release is unknown. Unreadable bytes
    count as changed: nothing proves them unmodified."""
    from ouroboros.skill_install_record import read_install_record
    from ouroboros.skill_loader import compute_content_hash

    record = read_install_record(drive_root, name) or {}
    base = str(record.get("base_digest") or "")
    if not base:
        return True if record.get("modified") else None
    try:
        return compute_content_hash(target) != base
    except Exception:
        return True


def reseed_changed_builtins(drive_root: pathlib.Path, *, seed_dir: Optional[pathlib.Path] = None,
                            log_obj: Any = None) -> int:
    """Re-seed in place the unmodified built-ins whose bundled version changed (marker-owned only).

    A copy with local changes against its recorded base is kept: the owner chooses on
    update (replace, keep or merge). A payload that differs at an equal version is kept
    and logged; a removed built-in is never resurrected; enablement and Telegram state
    live outside the payload and stay.
    """
    from ouroboros.skill_install_record import read_install_record

    logger = log_obj or log
    seed_root = pathlib.Path(seed_dir or bundled_skills_dir())
    native_root = pathlib.Path(drive_root) / "skills" / "native"
    if not seed_root.is_dir() or not native_root.is_dir():
        return 0
    upgraded = 0
    for seed in sorted(seed_root.iterdir()):
        target = native_root / seed.name
        if not _is_seed(seed) or not target.is_dir() or not (target / SEED_MARKER).is_file():
            continue
        seed_version, target_version = _version(seed), _version(target)
        if not seed_version or not target_version:
            continue
        if seed_version == target_version:
            _warn_equal_version_drift(seed, target, seed_version, logger)
            continue
        if _changed_since_install(pathlib.Path(drive_root), seed.name, target):
            logger.warning("Built-in skill %s has local changes (installed=%s, bundled=%s); kept — "
                           "the owner chooses on update", seed.name, target_version, seed_version)
            continue
        logger.info("Built-in skill %s version drift (bundled=%s, installed=%s) — re-seeding",
                    seed.name, seed_version, target_version)
        previous = read_install_record(pathlib.Path(drive_root), seed.name) or {}
        result = install_builtin_skill(
            seed.name, drive_root=pathlib.Path(drive_root), seed_dir=seed_root,
            turn_on_when_ready=previous.get("turn_on_when_ready") is True,
            installed_by=str(previous.get("installed_by") or "ouroboros"),
            reason=f"app update {target_version} -> {seed_version}",
        )
        if result.get("ok"):
            upgraded += 1
        else:
            logger.warning("Built-in skill %s re-seed failed: %s", seed.name, result.get("error"))
    return upgraded


def _warn_equal_version_drift(seed: pathlib.Path, target: pathlib.Path, version: str, logger: Any) -> None:
    from ouroboros.skill_loader import compute_content_hash

    try:
        seed_hash, target_hash = compute_content_hash(seed), compute_content_hash(target)
    except Exception as exc:
        logger.warning("Built-in skill %s version %s payload comparison unavailable (%s)",
                       seed.name, version, type(exc).__name__)
        return
    if seed_hash != target_hash:
        logger.warning("Built-in skill %s version %s payload differs (bundled=%s, installed=%s); "
                       "installed files retained because the manifest version is unchanged",
                       seed.name, version, seed_hash, target_hash)


def release_removed_builtins(drive_root: pathlib.Path, *, seed_dir: Optional[pathlib.Path] = None,
                             log_obj: Any = None) -> None:
    """A built-in no longer bundled keeps its files and records; only the launcher
    marker goes, so the copy is the owner's from now on."""
    logger = log_obj or log
    seed_root = pathlib.Path(seed_dir or bundled_skills_dir())
    native_root = pathlib.Path(drive_root) / "skills" / "native"
    if not native_root.is_dir():
        return
    for entry in native_root.iterdir():
        marker = entry / SEED_MARKER
        if not entry.is_dir() or entry.name.startswith(".") or not marker.is_file() or (seed_root / entry.name).is_dir():
            continue
        try:
            marker.unlink()
            logger.info("Built-in skill %r is no longer bundled; its installed copy is now user-managed.", entry.name)
        except OSError:
            logger.warning("Failed to release removed built-in %s", entry, exc_info=True)


def record_converted_builtin(drive_root: pathlib.Path, skill: Any, legacy: Any, *,
                             seed_dir: Optional[pathlib.Path] = None) -> bool:
    """Identity-conversion hook: give an existing launcher-seeded built-in its install record.

    The bytes the launcher trusted (a native-seed verdict on a version-1 hash of the
    current bytes) become the installed release. A stale native-seed verdict proves local
    edits, so the unknown original is recorded and reseeding preserves the copy. Without
    that evidence the bundled release supplies the base at the same version; otherwise
    the legacy version reseed still applies. Existing records are never rewritten.
    """
    from ouroboros.skill_install_record import read_install_record, write_install_record
    from ouroboros.skill_loader import compute_content_hash
    from ouroboros.utils import read_json_dict
    from ouroboros.skill_loader import skill_state_dir_path

    if getattr(skill, "location", "") != "native" or not (pathlib.Path(skill.skill_dir) / SEED_MARKER).is_file():
        return False
    if read_install_record(pathlib.Path(drive_root), skill.name) is not None:
        return False
    review = read_json_dict(skill_state_dir_path(pathlib.Path(drive_root), skill.name) / "review.json") or {}
    if review.get("review_profile") == "native_seed" and review.get("content_hash") not in set(legacy or ()):
        write_install_record(pathlib.Path(drive_root), skill.name, {
            "source": dict(BUILTIN_SOURCE), "entry": skill.name, "basis": "review", "legacy": True,
            "release": {"version": str(skill.manifest.version or ""), "digest": ""}, "base_digest": "",
            "modified": True, "official": False, "installed_by": "ouroboros", "turn_on_when_ready": False,
            "converted_from": SEED_MARKER,
        })
        log.warning("Built-in skill %s changed after native seeding; original unknown, installed files retained",
                    skill.name)
        return True
    covered = (str(review.get("review_profile") or "") == "native_seed"
               and str(review.get("content_hash") or "") in set(legacy or ()))
    version = str(skill.manifest.version or "")
    if covered:
        digest, base = skill.content_hash, skill.content_hash
    else:
        seed = pathlib.Path(seed_dir or bundled_skills_dir()) / skill.name
        if not _is_seed(seed):
            return False
        digest = compute_content_hash(seed)
        base = digest if _version(seed) == version else ""
        version = _version(seed) or version
    _write_record(pathlib.Path(drive_root), skill.name, version=version, digest=digest, base_digest=base,
                  turn_on_when_ready=False, installed_by="ouroboros", converted=True)
    return True


def ensure_builtin_skills(drive_root: Optional[pathlib.Path] = None, *, log_obj: Any = None) -> int:
    """Startup work for an existing data root: the one-time identity conversion (with
    install records for launcher-seeded built-ins), re-seeding changed built-ins, and
    releasing built-ins the app no longer bundles. Returns the number re-seeded."""
    from ouroboros.config import DATA_DIR
    from ouroboros.skill_identity import convert_skill_identities

    logger = log_obj or log
    drive = pathlib.Path(drive_root or DATA_DIR)
    seed_root = bundled_skills_dir()
    try:
        convert_skill_identities(
            drive, log_obj=logger,
            on_skill=lambda skill, legacy: record_converted_builtin(drive, skill, legacy, seed_dir=seed_root),
        )
    except Exception:
        logger.warning("Skill identity conversion raised", exc_info=True)
    upgraded = 0
    try:
        upgraded = reseed_changed_builtins(drive, seed_dir=seed_root, log_obj=logger)
    except Exception:
        logger.warning("Built-in skill re-seed raised", exc_info=True)
    try:
        release_removed_builtins(drive, seed_dir=seed_root, log_obj=logger)
    except Exception:
        logger.warning("Removed built-in release raised", exc_info=True)
    return upgraded


__all__ = [
    "BUILTIN_SOURCE",
    "builtin_trust",
    "bundled_skills_dir",
    "ensure_builtin_skills",
    "install_builtin_skill",
    "record_converted_builtin",
    "release_removed_builtins",
    "reseed_changed_builtins",
]
