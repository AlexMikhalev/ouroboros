"""Where a skill is published, and the hub entry that lists it.

A skill installed from a git-hosted source publishes back to that origin: the hub for a hosted hub
entry, the author's repository for a hub entry that points at one, the repository itself for a plain
git source. Everything else (made here, built in, from a registry or a local folder) publishes to the
hub, or to a git source the owner names. Publishing is always a pull request through the existing
consent, scan and receipt path; files reaching the repository and the skill being listed in the hub's
catalog are separate results, because a pointer entry is listed by its own one-entry hub change.

A hub with ``hub.json`` receives the skill as one entry (``entries/<name>.json``) plus its hosted files
and its row in the generated v1 ``catalog.json`` (rows sorted by name, so parallel submissions touch
different lines); a repository without ``hub.json`` is an older hub and gets the v1 row only.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ouroboros.skill_publish_result import SkillPublishDestinationError, parse_skill_publish_destination

_GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class PublishDestination:
    """One repository a skill can be published to. ``branch`` empty: the repository's default."""

    kind: str  # hub | git
    owner: str
    repo: str
    branch: str
    folder: str  # hub: the hosted root; git: the folder holding skill folders; pointer: the skill folder
    source_id: str
    source_name: str
    pointer: bool = False

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    def skill_folder(self, skill: str) -> str:
        if self.pointer:
            return self.folder
        return "/".join(part for part in (self.folder, skill) if part)


def github_repository(url: str) -> tuple[str, str]:
    """``(owner, repository)`` of a GitHub https address; other hosts cannot be published to here."""
    match = _GITHUB_RE.match(str(url or "").strip())
    if not match:
        raise SkillPublishDestinationError("hub_destination_invalid")
    return match.group(1), match.group(2)


def _hub_destination(rows: List[Dict[str, Any]]) -> PublishDestination:
    hubs = [row for row in rows if row["kind"] == "hub"]
    row = next((r for r in hubs if r.get("part") == "official"), hubs[0] if hubs else None)
    if row is None:
        from ouroboros.config import get_ouroboroshub_catalog_url

        owner, repo, branch = parse_skill_publish_destination(get_ouroboroshub_catalog_url())
        return PublishDestination("hub", owner, repo, branch, "skills", "hub", "OuroborosHub")
    owner, repo = github_repository(row.get("url", ""))
    return PublishDestination("hub", owner, repo, str(row.get("ref") or ""), "skills", row["id"], row["name"])


def _row_destination(row: Dict[str, Any], rows: List[Dict[str, Any]]) -> PublishDestination:
    if row["kind"] == "hub":
        owner, repo = github_repository(row.get("url", ""))
        return PublishDestination("hub", owner, repo, str(row.get("ref") or ""), "skills", row["id"], row["name"])
    if row["kind"] == "git":
        owner, repo = github_repository(row.get("url", ""))
        return PublishDestination("git", owner, repo, str(row.get("ref") or ""), str(row.get("path") or ""),
                                  row["id"], row["name"])
    raise SkillPublishDestinationError("destination_not_git")


def resolve_publish_destination(drive_root: pathlib.Path, skill: str, requested: str = "", *,
                                rows: Optional[List[Dict[str, Any]]] = None) -> PublishDestination:
    """The destination named by ``requested`` (``hub`` or a source id), else the skill's origin, else the hub."""
    from ouroboros.skill_install_record import read_install_record
    from ouroboros.skill_source_catalog import record_matches_row
    from ouroboros.skill_sources import load_sources, source_by_id

    rows = rows if rows is not None else load_sources()
    requested = str(requested or "").strip()
    if requested == "hub":
        return _hub_destination(rows)
    if requested:
        row = source_by_id(rows, requested)
        if row is None:
            raise SkillPublishDestinationError("destination_unknown")
        return _row_destination(row, rows)
    record = read_install_record(pathlib.Path(drive_root), skill) or {}
    release = record.get("release") or {}
    if release.get("repo"):
        try:
            owner, repo = github_repository(release["repo"])
            return PublishDestination("git", owner, repo, "", str(release.get("path") or ""),
                                      str((record.get("source") or {}).get("id") or ""), f"{owner}/{repo}", pointer=True)
        except SkillPublishDestinationError:
            pass
    origin = next((row for row in rows if record and record_matches_row(record, row)), None)
    if origin is not None and origin["kind"] in {"hub", "git"}:
        try:
            return _row_destination(origin, rows)
        except SkillPublishDestinationError:
            pass
    return _hub_destination(rows)


def captured_release_digest(snapshot: Any) -> str:
    """The release digest of exactly the files a publication sends."""
    from ouroboros.skill_dependencies import _coerce_dependency_specs
    from ouroboros.skill_install_record import release_digest_from_inventory

    return release_digest_from_inventory([{"path": item.path, "sha256": item.sha256} for item in snapshot.public_files],
                                         _coerce_dependency_specs(snapshot.manifest.install_specs()))


def hub_entry(skill: str, snapshot: Any, *, login: str, digest: str, existing: Optional[Dict[str, Any]],
              category: List[str]) -> Dict[str, Any]:
    """The one-entry hub record for a hosted skill; an existing entry keeps its recorded owner."""
    manifest = snapshot.manifest
    entry: Dict[str, Any] = {
        "schema_version": 2, "name": skill, "description": manifest.description,
        "category": list(category or (existing or {}).get("category") or []),
        "owner": str((existing or {}).get("owner") or login), "version": manifest.version,
        "source": {"kind": "hosted", "path": f"skills/{skill}"}, "digest": digest, "withdrawn": None,
    }
    specs = manifest.install_specs()
    if specs:
        entry["install_specs"] = specs
    return entry


__all__ = [
    "PublishDestination", "captured_release_digest", "github_repository", "hub_entry",
    "resolve_publish_destination",
]
