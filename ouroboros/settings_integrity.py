"""Strict settings-snapshot integrity pin (benchmark isolation trust root).

An isolated benchmark server seeds its child with an exact settings snapshot
and pins its sha256 in ``OUROBOROS_SETTINGS_SHA256``: while the pin is
present the snapshot is an owner-authored trust root — every read verifies
the complete byte stream and every writer refuses. Extracted from
``config.py`` (which re-exports the public names, so every existing
``config.X`` import keeps working) to keep the SSOT module inside its size
ratchet.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import contextlib
import contextvars
import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SETTINGS_INTEGRITY_ENV = "OUROBOROS_SETTINGS_SHA256"
_TASK_SETTINGS = contextvars.ContextVar("ouroboros_task_settings", default=None)
# Only capture/projection holds this lock, never a task's execution lifetime.
SETTINGS_ENV_LOCK = threading.RLock()


@dataclass(frozen=True, repr=False)
class TaskSettingsSnapshot:
    """Private in-memory views; document values and env absence are distinct."""

    settings: Mapping
    environ: Mapping


def _next_task_setting(key: str) -> bool:
    from ouroboros.settings_scales import IMMEDIATE_SETTINGS, RESTART_REQUIRED_SETTINGS

    return key not in IMMEDIATE_SETTINGS and key not in RESTART_REQUIRED_SETTINGS and key != "OUROBOROS_RUNTIME_MODE"


def _projected_keys() -> set[str]:
    from ouroboros.settings_defaults import RETIRED_COMMA_LIST_SETTING_KEYS, settings_env_keys
    from ouroboros.model_slots import _LEGACY_SLOT_RENAMES

    return (set(settings_env_keys()) | set(RETIRED_COMMA_LIST_SETTING_KEYS)
            | {old for old, _new in _LEGACY_SLOT_RENAMES} | {PROJECTED_PROPOSALS_ENV})


# The values this process tree projected (``config.apply_settings_to_env``) for the keys whose absence takes a
# proposal — the model slots and the never-configured factory catalog — handed down with the environment: a read
# of a key the document does not set tells this tree's own echo from an operator's variable, so an absent slot
# keeps following the configured access and an absent catalog its factory (owner 2A).
PROJECTED_PROPOSALS_ENV = "OUROBOROS_PROJECTED_PROPOSALS"


def projected_proposals(environ: Mapping) -> dict:
    """``{key: value}`` this process tree last projected into ``environ`` for a proposal key."""
    try:
        echo = json.loads(environ.get(PROJECTED_PROPOSALS_ENV) or "{}")
    except ValueError:
        return {}
    return echo if isinstance(echo, dict) else {}


def operator_variables(environ: Mapping, keys) -> dict:
    """``{key: value}`` of an operator's own variables in ``environ`` for proposal ``keys``: present, not blank and not
    this tree's projection — what the loader lets stand in for a key the document does not set."""
    echo = projected_proposals(environ)
    return {key: environ[key] for key in keys if environ.get(key) and environ[key] != echo.get(key)}


def note_proposal_projection(environ, before: Mapping) -> None:
    """Record the proposal-key values a projection left in ``environ`` (``before``: their values before it),
    except where it kept or respelled an operator's own variable (``operator_variables`` of ``before``): our echo
    must never turn the operator's choice into a proposal the next read replaces."""
    from ouroboros.configured_subagents import SUBAGENTS_SETTING
    from ouroboros.server_runtime import MODEL_SLOT_KEYS

    keys = (*MODEL_SLOT_KEYS, SUBAGENTS_SETTING)
    echo, operator = projected_proposals(environ), operator_variables(before, keys)
    for key in keys:
        after = environ.get(key)
        if after is None or key in operator or (after == before.get(key) and after != echo.get(key)):
            echo.pop(key, None)
        else:
            echo[key] = after
    environ[PROJECTED_PROPOSALS_ENV] = json.dumps(echo, sort_keys=True)


@contextlib.contextmanager
def task_settings_scope(snapshot):
    """Bind one task's settings in memory only; concurrent tasks keep their own view."""
    token = _TASK_SETTINGS.set(snapshot)
    try:
        yield
    finally:
        _TASK_SETTINGS.reset(token)


def copy_task_settings_context(context) -> None:
    """Carry settings through context transfers that intentionally omit Main call state."""
    context.run(_TASK_SETTINGS.set, _TASK_SETTINGS.get())


def runtime_setting(key: str, default=None):
    """Environment-shaped runtime read; absence in the snapshot stays absent."""
    snapshot = _TASK_SETTINGS.get()
    if snapshot is not None and _next_task_setting(key):
        return snapshot.environ.get(key, default)
    return os.environ.get(key, default)


def runtime_environ() -> dict[str, str]:
    """Explicit child environment with this task's next-task settings overlaid."""
    with SETTINGS_ENV_LOCK:
        env = dict(os.environ)
    # Launcher authority belongs only to the launcher-owned server process. A
    # child shell/server (including an external-workspace test fixture) must not
    # inherit it and gain permission to run destructive managed bootstrap
    # against its own checkout.
    env.pop("OUROBOROS_MANAGED_BY_LAUNCHER", None)
    env.pop("OUROBOROS_MANAGED_REPO_DIR", None)
    snapshot = _TASK_SETTINGS.get()
    if snapshot is not None:
        for key in _projected_keys() | snapshot.settings.keys():
            if _next_task_setting(key):
                if key not in snapshot.environ:
                    env.pop(key, None)
                else:
                    env[key] = snapshot.environ[key]
    return env


def live_effort_range() -> dict[str, str]:
    """The owner's CURRENT effort range (``settings_scales.effort_range`` over the settings
    document as it reads now), for a participant started INSIDE a running task — a review
    wave, a direct ``delegate_start``: it reads this once at its start while the task that
    started it keeps the range of its own snapshot (``runtime_setting``)."""
    from ouroboros import config
    from ouroboros.settings_scales import effort_range

    return effort_range(config.load_settings())


def runtime_settings(*, settings_reader=None) -> dict:
    """Runtime document view; owner writers continue using config.load_settings."""
    from ouroboros import config

    settings = dict((settings_reader or config.load_settings)() or {})
    snapshot = _TASK_SETTINGS.get()
    if snapshot is not None:
        for key in settings.keys() | snapshot.settings.keys():
            if not _next_task_setting(key):
                continue
            if key not in snapshot.settings:
                settings.pop(key, None)
            else:
                settings[key] = copy.deepcopy(snapshot.settings[key])
    return settings


def task_settings_snapshot(settings: dict, environ: dict) -> TaskSettingsSnapshot:
    """Keep document-only values and exact projected presence without serializing either."""
    return TaskSettingsSnapshot(
        MappingProxyType(copy.deepcopy(settings)), MappingProxyType(dict(environ)))


class SettingsIntegrityError(RuntimeError):
    """The settings snapshot changed or became unreadable under a strict pin."""


def guard_settings_snapshot_mutation() -> None:
    """Refuse every settings writer while a benchmark snapshot is pinned."""
    if os.environ.get(SETTINGS_INTEGRITY_ENV):
        raise SettingsIntegrityError("strict isolated settings snapshot is immutable")


def guard_live_settings_write(settings_path: Path, home: Path) -> None:
    """Every settings-write precondition: the snapshot pin, then the pytest
    guard on the LIVE settings file (an isolated test root passes through)."""
    guard_settings_snapshot_mutation()
    if os.environ.get("OUROBOROS_ALLOW_LIVE_DATA_TESTS") == "1":
        return
    try:
        live_settings = settings_path.resolve(strict=False) == (
            home / "Ouroboros" / "data" / "settings.json"
        ).resolve(strict=False)
    except OSError:
        live_settings = False
    if ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules) and live_settings:
        raise RuntimeError(
            "Refusing to write live Ouroboros settings.json from pytest. "
            "Set OUROBOROS_SETTINGS_PATH/OUROBOROS_DATA_DIR to a temp path, "
            "or OUROBOROS_ALLOW_LIVE_DATA_TESTS=1 for an explicit live-data test."
        )


def expected_settings_sha256() -> str:
    value = str(os.environ.get(SETTINGS_INTEGRITY_ENV, "") or "").strip().lower()
    if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
        raise SettingsIntegrityError("settings integrity digest is malformed")
    return value


def read_settings_bytes_verified(settings_path: Path) -> bytes | None:
    """Read one stable file descriptor and verify its complete byte stream."""
    expected = expected_settings_sha256()
    try:
        with settings_path.open("rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        if expected:
            raise SettingsIntegrityError("settings snapshot is missing") from None
        return None
    except OSError as exc:
        if expected:
            raise SettingsIntegrityError("settings snapshot is unreadable") from exc
        return None
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise SettingsIntegrityError("settings snapshot changed")
    return raw


def read_settings_json_verified(settings_path: Path):
    """Decode the verified snapshot; under a pin a decode failure is typed.

    Returns the parsed JSON value (any type) or ``None`` for an absent or —
    without a pin — unreadable/undecodable file. One helper so the two
    config.py read paths cannot drift apart in their pin semantics.
    """
    raw_bytes = read_settings_bytes_verified(settings_path)
    if raw_bytes is None:
        return None
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if expected_settings_sha256():
            raise SettingsIntegrityError("settings snapshot is unreadable") from exc
        return None


def verify_settings_integrity(settings_path: Path) -> str | None:
    """Verify the strict child pin, returning the observed digest when present."""
    raw = read_settings_bytes_verified(settings_path)
    return hashlib.sha256(raw).hexdigest() if raw is not None else None
