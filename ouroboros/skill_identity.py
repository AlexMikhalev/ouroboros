"""Skill release identity: one versioned digest of a payload and its declared install specs.

A release digest names "these payload bytes with these declared dependencies" on every
machine. Per-install notes (launcher markers, legacy marketplace sidecars, local lineage
notes) are not part of a release, so the same release installed twice has one digest and
a review or an endorsement of it can be recognized anywhere. ``DIGEST_PREFIX`` tags the
algorithm so a stored digest is never compared with a value computed another way.

The file walk, confinement and sensitive-file policy are the loader's own
(``skill_loader._iter_payload_files``); this module only decides what a release is.
"""

from __future__ import annotations

import hashlib
import pathlib
from typing import Any, Dict, List, Optional

DIGEST_PREFIX = "sha256v2:"

# Top-level per-install notes that never belong to a release. Legacy names stay listed so
# payloads installed by older versions keep the digest of the bytes they actually run.
PER_INSTALL_NOTE_FILENAMES = frozenset({
    ".seed-origin",
    ".ouroboroshub.json",
    ".clawhub.json",
    ".self_authored.json",
})


def is_release_digest(value: Any) -> bool:
    text = str(value or "")
    return text.startswith(DIGEST_PREFIX) and len(text) == len(DIGEST_PREFIX) + 64


def payload_digest(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
    install_specs: Any = None,
) -> str:
    """Digest of the runtime payload plus the effective declared install specs.

    Unreadable files fail closed through ``SkillPayloadUnreadable`` exactly as the
    loader's content hash does.
    """
    from ouroboros.marketplace.install_specs import install_specs_hash, normalize_install_specs
    from ouroboros.skill_loader import SkillPayloadUnreadable, _iter_payload_files

    root = pathlib.Path(skill_dir).resolve()
    digest = hashlib.sha256()
    for file_path in _iter_payload_files(
        root, manifest_entry=manifest_entry, manifest_scripts=manifest_scripts,
    ):
        rel = file_path.relative_to(root).as_posix()
        if "/" not in rel and rel in PER_INSTALL_NOTE_FILENAMES:
            continue
        file_digest = hashlib.sha256()
        try:
            with file_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(64 * 1024), b""):
                    file_digest.update(chunk)
        except OSError as exc:
            raise SkillPayloadUnreadable(rel, exc) from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
    auto_specs, _manual, _errors = normalize_install_specs(install_specs or [])
    if auto_specs:
        # The declared dependencies are part of what a release installs; the pseudo path
        # cannot collide with a file because real relative paths never start with NUL.
        digest.update(b"\0install_specs\0")
        digest.update(install_specs_hash(auto_specs).encode("utf-8"))
    return DIGEST_PREFIX + digest.hexdigest()
