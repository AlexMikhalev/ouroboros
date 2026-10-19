"""A saved Presence spelling of a renamed builtin is its successor: a rename keeps saved choices
working and never makes the old name callable.

Both directions per carrier: a required selection saved under the old name admits and its ceiling
grants the successor; a frozen pre-update ceiling naming the old spelling still allows the successor;
an unknown old name tells the caller the rename; an unrelated unknown name gets no rename line; a
genuinely missing builtin is still refused.
"""
from __future__ import annotations

import pytest

from ouroboros.contracts.task_contract import TOOL_NAME_ALIASES
from ouroboros.presence_admission import PresenceAdmissionError
from ouroboros.presence_capabilities import (
    PresenceSelection, PresenceState, PresenceToolTarget, presence_state_fingerprint, save_presence_state,
)
from ouroboros.presence_profile import parse_presence_profile, presence_request_fingerprint
from ouroboros.skill_loader import load_skill
from tests.test_presence_admission import _admit, _binding, _install_behavior

PAIRS = sorted(TOOL_NAME_ALIASES.items())


def _select(drive_root, skill_dir, name):
    loaded = load_skill(skill_dir, drive_root)
    profile = parse_presence_profile(loaded.manifest, skill_dir)
    state = PresenceState((PresenceSelection(
        presence_request_fingerprint(profile.capability_requests[0]), PresenceToolTarget("builtin", name)),))
    save_presence_state(drive_root, loaded.name, state,
                        expected_state_fingerprint=presence_state_fingerprint(PresenceState()))


@pytest.mark.parametrize("old,new", PAIRS)
def test_a_required_selection_saved_under_the_old_name_admits_and_grants_the_successor(tmp_path, old, new):
    from ouroboros.presence_authority import presence_ceiling_allows_tool

    drive_root = tmp_path / "data"
    skill_dir = _install_behavior(drive_root)
    _select(drive_root, skill_dir, old)
    admission = _admit(drive_root, _binding(drive_root))
    names = [grant.name for grant in admission.capability_ceiling.tool_grants]
    assert new in names and old not in names
    assert presence_ceiling_allows_tool(admission.capability_ceiling, new)


def test_a_missing_builtin_is_still_refused(tmp_path):
    drive_root = tmp_path / "data"
    skill_dir = _install_behavior(drive_root)
    _select(drive_root, skill_dir, "does_not_exist")
    with pytest.raises(PresenceAdmissionError) as caught:
        _admit(drive_root, _binding(drive_root))
    assert caught.value.code == "presence_required_capability_unavailable"


@pytest.mark.parametrize("old,new", PAIRS)
def test_a_frozen_ceiling_with_the_old_spelling_allows_the_successor_only(old, new):
    from ouroboros.presence_authority import PresenceToolGrant, presence_ceiling_allows_tool

    ceiling = type("Ceiling", (), {"tool_grants": (PresenceToolGrant(old),)})()
    assert presence_ceiling_allows_tool(ceiling, new)
    assert not presence_ceiling_allows_tool(ceiling, "write_file")  # quiet: nothing else widens


@pytest.mark.parametrize("old,new", PAIRS)
def test_an_unknown_old_name_names_its_successor(old, new):
    from ouroboros.tool_policy import name_miss_guidance

    lines = name_miss_guidance(old, "builtin", [{"name": new, "description": "x"}], discovery=True)
    assert lines[0] == f"{old!r} was renamed {new!r} (not called)."
    quiet = name_miss_guidance("no_such_tool", "builtin", [{"name": new, "description": "x"}], discovery=True)
    assert not any("renamed" in line for line in quiet)
