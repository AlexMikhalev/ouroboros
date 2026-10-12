"""DESIGN navigation follows body identity and actual review delivery."""

from __future__ import annotations

import subprocess
from hashlib import sha256

import pytest

# Initialize review's module-level root aliases before registry tests patch
# review_helpers.REPO_ROOT; a lazy registry import would retain the fake root.
from ouroboros.tools import review as _review  # noqa: F401
from ouroboros.tools.governance_context import governance_context
from tests.test_governance_context import repo as _governance_repo
from tests.test_plan_review_engine import CLEAN, DECK_SPEC, _call, _slots, _user_text
from tests.test_plan_review_engine import harness as _harness

governance_repo = _governance_repo
harness = _harness


DESIGN = "# Design\n\n## Interaction meaning\n\nOriginal interaction rule.\n\n## Appearance\n\nOriginal appearance rule.\n"


def _design(root, text=DESIGN):
    path = root / "docs" / "DESIGN.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("delegated", [False, True], ids=["native", "external-session"])
def test_deep_review_separates_installed_governance_from_candidate_sources(
    governance_repo, tmp_path, monkeypatch, delegated,
):
    from ouroboros import deep_self_review, review_execution
    from ouroboros.tools import review_helpers
    from ouroboros.tools.scope_required_sources import source_text_identity
    from tests.test_deep_review_slot import (
        _FakeSessionExecutor, _REPORT, _ScriptedLLM, _native_row, _session_row, _tool_call,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(review_helpers, "REPO_ROOT", governance_repo)
    source = _design(governance_repo)
    candidate = tmp_path / "candidate"
    _design(candidate, "## Proposed replacement\n")
    proposed_bible = "# Proposed constitution\nCandidate-only proposal.\n"
    (candidate / "BIBLE.md").write_text(proposed_bible)
    required = [{"root": "active_workspace", "path": "BIBLE.md",
                 **source_text_identity(proposed_bible.encode())}]
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"root": "system_repo", "path": "BIBLE.md"}, "c1"),
                        _tool_call("read_file", {"path": "BIBLE.md"}, "c2")]},
        {"content": _REPORT},
    ])
    if delegated:
        monkeypatch.setattr(review_execution, "_review_route_executor", _FakeSessionExecutor)
        monkeypatch.setattr(deep_self_review, "_session_route_reason", lambda row: "")
    report, usage = deep_self_review.run_deep_self_review(
        candidate, tmp_path / "drive", llm, lambda _: None,
        slot=_session_row() if delegated else _native_row(), required_sources=required)
    if delegated:
        request = _FakeSessionExecutor.instances[-1].assignment.request
        brief = request.session_task
        assert request.session_root == str(candidate)
        assert request.policy["native_required_sources"] == required
    else:
        brief = next(m["content"] for m in llm.calls[0]["messages"] if m["role"] == "user")
        installed, subject = usage["native_tool_receipts"]
        assert installed["opened_root"] == "system_repo"
        assert subject["opened_root"] == "active_workspace"
        assert subject["source_revision"] == required[0]["source_revision"]
        assert "BIBLE.md read in full" in report
    assert (governance_repo / "BIBLE.md").read_text() in brief
    assert proposed_bible not in brief and "Proposed replacement" not in brief
    assert str(source) in brief and "Interaction meaning" in brief

@pytest.mark.parametrize("surface", ["triad", "scope", "deep_self_review"])
@pytest.mark.parametrize("delivery", ["packet", "retrieving"])
@pytest.mark.parametrize("touched", ["ouroboros/event.py", "web/modules/chat.js"])
def test_noninline_design_keeps_map_and_direct_source(governance_repo, surface, delivery, touched):
    path = _design(governance_repo)
    context = governance_context(governance_repo, surface=surface, delivery=delivery,
                                 touched_paths=[touched], usable_window_tokens=0)

    assert "Interaction meaning" in context.navigation
    assert str(path) in context.navigation
    assert "Original interaction rule." not in context.navigation + context.selected_inline
    assert "availability is not evidence of reading or application" in context.navigation
    assert "docs/DESIGN.md" not in context.inline_whole_documents
    row = next(row for row in context.manifest if row["path"] == "docs/DESIGN.md")
    assert row["disposition"] == "navigation" and row["chars"] == len(DESIGN)
    if touched.startswith("web/"):
        assert "inline share exhausted" in row["reason"]
    if delivery == "packet":
        assert "no repository tools" in context.navigation and "NOT delivered" in context.navigation


def test_inline_design_is_content_without_duplicate_map(governance_repo):
    _design(governance_repo)
    context = governance_context(governance_repo, surface="triad", touched_paths=["web/ui.js"],
                                 usable_window_tokens=1_000_000)
    assert context.inline_whole_documents["docs/DESIGN.md"] == DESIGN
    assert "Original interaction rule." in context.selected_inline
    assert "Interaction meaning" not in context.navigation


@pytest.mark.parametrize("layer", ["body", "core"])
def test_final_packet_preserves_design_delivery_and_omission_facts(governance_repo, layer):
    from ouroboros.tools.review_admission import assemble_packet_prompt

    path = _design(governance_repo)
    context = governance_context(governance_repo, surface="triad", delivery="packet", layer=layer,
                                 touched_paths=["web/ui.js"], usable_window_tokens=0)
    prompt, stable_end = assemble_packet_prompt(
        layer=layer, checklist_section="Review the change.", governance=context,
        goal_section="Check signals", scope_section="Change", files_section="No DESIGN body attached",
        diff_text="diff --git a/web/ui.js b/web/ui.js\n", changed_files="web/ui.js")
    assert ("Interaction meaning" in prompt) == (layer == "body")
    assert (str(path) in prompt) == (layer == "body")
    assert "Original interaction rule." not in prompt
    if layer == "body":
        assert "Interaction meaning" not in prompt[:stable_end]
        assert "were NOT delivered" in prompt[stable_end:]


@pytest.mark.parametrize("delivery", ["packet", "retrieving"])
def test_missing_design_is_named_without_claiming_content(governance_repo, delivery):
    path = governance_repo / "docs" / "DESIGN.md"
    path.unlink()
    context = governance_context(governance_repo, surface="triad", delivery=delivery,
                                 touched_paths=["web/ui.js"], usable_window_tokens=1_000_000)
    assert str(path) in context.navigation and "source unavailable" in context.navigation
    assert "No DESIGN content or navigation delivered" in context.navigation
    assert "docs/DESIGN.md" not in context.inline_whole_documents
    row = next(row for row in context.manifest if row["path"] == "docs/DESIGN.md")
    assert row["disposition"] == "navigation" and row["chars"] == 0
    assert "not found" in row["reason"]


def test_foreign_review_indexes_own_design_without_ouroboros_rules(governance_repo, tmp_path):
    _design(governance_repo)
    foreign = tmp_path / "foreign"
    _design(foreign, "# Foreign product\n\n## Foreign colors\n\nIts own rule.\n")
    context = governance_context(governance_repo, surface="triad", layer="core", subject_root=foreign,
                                 touched_paths=["web/ui.js"], usable_window_tokens=1_000_000)
    assert "Foreign colors" in context.navigation
    assert "Interaction meaning" not in context.navigation + context.selected_inline
    assert str(governance_repo / "docs/DESIGN.md") not in context.navigation
    assert next(row for row in context.manifest if row["path"] == "docs/DESIGN.md")["disposition"] == "not_applicable"


@pytest.mark.parametrize("delegated", [False, True], ids=["native", "external-session"])
def test_code_and_coupling_brief_retains_installed_design_source(governance_repo, tmp_path, monkeypatch, delegated):
    from ouroboros.tools import review_brief_coupling as coupling

    source = _design(governance_repo)
    candidate = tmp_path / "candidate"
    _design(candidate, "# Candidate proposal\n\n## Candidate replacement\n\nProposed rule.\n")
    monkeypatch.setattr(coupling, "first_send_bound", lambda brief: 1_000_000)
    text, manifest = coupling.build_retrieving_brief(candidate, coupling.BriefInputs(
        governance_repo_dir=governance_repo, delegated=delegated, diff_text="diff --git a/event.py b/event.py\n",
        touched_paths=("event.py",), required_sources=[], touched_manifest=[],
        checklist_section="Review the change.", parts=("change", "coupling")))

    assert "Part 1" in text and "Part 2" in text
    assert "Interaction meaning" in text and str(source) in text
    assert "Candidate replacement" not in text
    assert next(row for row in manifest["governance_manifest"]
                if row["path"] == "docs/DESIGN.md")["disposition"] == "navigation"


@pytest.mark.parametrize("root", [None, "system_repo"])
def test_native_registry_reads_installed_design_outside_home(tmp_path, monkeypatch, root):
    from ouroboros.review_native_episode import inspection_registry
    from ouroboros.tools import review_helpers
    from ouroboros.tools.core_file_tools import delivered_source_prefix

    # Docker topology: /app and /candidate are siblings of HOME=/root.
    installed, candidate, home = (tmp_path / name for name in ("app", "candidate", "root"))
    home.mkdir()
    source = _design(installed)
    proposed = _design(candidate, "# Proposed\n\n## Candidate replacement\n\nProposed rule.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(review_helpers, "REPO_ROOT", installed)
    context = governance_context(installed, surface="triad", delivery="retrieving",
                                 touched_paths=["event.py"], usable_window_tokens=0)
    assert str(source) in context.navigation and "Interaction meaning — lines 3-6" in context.navigation

    registry, reader, _ = inspection_registry(str(candidate), tmp_path / "data")
    arguments = {"path": str(source), "start_line": 3, "max_lines": 4}
    if root:
        arguments["root"] = root
    result = registry.execute_result("read_file", arguments)
    assert result.status == "ok" and "Original interaction rule." in result.text, result.text
    assert "Proposed rule." not in result.text and "Original appearance rule." not in result.text
    assert reader.last_read_view["target"] == str(source)
    assert reader.last_read_view["source_revision"] == sha256(source.read_bytes()).hexdigest()
    assert reader.last_read_view["opened_root"] == "system_repo"
    assert delivered_source_prefix(reader.last_read_view, result.text, len(result.text)) == "".join(
        DESIGN.splitlines(keepends=True)[2:6])
    result = registry.execute_result("read_file", {"root": "system_repo", "path": "docs/DESIGN.md"})
    assert "Original interaction rule." in result.text and "Proposed rule." not in result.text

    for arguments in ({"path": "docs/DESIGN.md"},
                      {"path": str(proposed)},
                      {"root": "active_workspace", "path": "docs/DESIGN.md"}):
        result = registry.execute_result("read_file", arguments)
        assert result.status == "ok" and "Proposed rule." in result.text, result.text
        assert "Original interaction rule." not in result.text
        assert reader.last_read_view["target"] == str(proposed)
        assert reader.last_read_view["source_revision"] == sha256(proposed.read_bytes()).hexdigest()
        assert reader.last_read_view["opened_root"] == "active_workspace"

    # Explicit roots never silently substitute another tree; off-home unrelated
    # files and mutation remain outside this inspection registry's authority.
    unrelated = _design(tmp_path / "unrelated", "## Unrelated\nMust not be delivered.\n")
    for arguments in ({"root": "active_workspace", "path": str(source)},
                      {"root": "system_repo", "path": str(proposed)},
                      {"path": str(unrelated)}, {"root": "user_files", "path": str(unrelated)}):
        result = registry.execute_result("read_file", arguments)
        assert result.status != "ok", result.text
        assert "Must not be delivered." not in result.text
    result = registry.execute_result("write_file", {"root": "system_repo", "path": str(source), "content": "changed"})
    assert result.status != "ok" and source.read_text() == DESIGN

    source.unlink()
    result = registry.execute_result("read_file", {"path": str(source)})
    assert "NOT_FOUND" in result.text and "Proposed rule." not in result.text


def _plan_request(harness, ctx, spec):
    harness.state["slots"] = _slots(("api1", "m/a"), ("sess1", "cursor=grok", "session"))
    substrate = harness.install({"api1": CLEAN, "sess1": CLEAN})
    _call(ctx, spec=spec)
    assert substrate.calls
    return substrate.calls[0]["request"]


def test_plan_api_and_session_receive_same_map_without_rule_content(harness, monkeypatch):
    from ouroboros.review_native_episode import inspection_registry
    from ouroboros.tools import review_helpers

    source = _design(harness.system)
    _design(harness.workspace, "## Proposed DESIGN\nCandidate rule.\n")
    monkeypatch.setattr(review_helpers, "REPO_ROOT", harness.system)
    request = _plan_request(harness, harness.make_ctx(), {
        **DECK_SPEC, "affected_paths": [str(harness.system / "ouroboros/loop.py")]})
    api = _user_text(request.messages[0]["content"])
    for text in (api, request.session_task):
        assert "Interaction meaning" in text and str(source) in text
        assert "Original interaction rule." not in text
        assert "availability is not evidence of reading or application" in text
    assert "need_evidence" in api and "::lines=A-B" in api
    assert "Read relevant original DESIGN sections with your own tools" in request.session_task
    registry, reader, _ = inspection_registry(str(harness.workspace), harness.drive)
    result = registry.execute_result("read_file", {"path": str(source), "start_line": 3, "max_lines": 4})
    assert "Original interaction rule." in result.text and "Candidate rule." not in result.text
    assert reader.last_read_view["target"] == str(source)
    result = registry.execute_result("read_file", {"path": "docs/DESIGN.md"})
    assert "Candidate rule." in result.text and "Original interaction rule." not in result.text


@pytest.mark.parametrize("subject, affected", [
    ("candidate", "event.py"), ("candidate", "."), ("foreign", "event.py"),
    ("unknown", "event.py"), ("folderless", ""),
])
def test_plan_navigation_respects_body_identity_without_changing_gate(harness, subject, affected):
    from ouroboros.tools.plan_review import _PlanRequest, build_plan_review_packet_for_dry_run

    source = _design(harness.system)
    _design(harness.workspace, "# Workspace\n\n## Workspace design\n\nWorkspace rule.\n")
    if subject in {"candidate", "foreign"}:
        subprocess.run(["git", "init", "-q"], cwd=harness.workspace, check=True, capture_output=True)
        origin = str(harness.system) if subject == "candidate" else "https://example.invalid/foreign.git"
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=harness.workspace,
                       check=True, capture_output=True)
    ctx = harness.make_ctx(active_workspace=subject != "folderless")
    spec = {**DECK_SPEC, "affected_paths": [affected] if affected else []}
    packet = build_plan_review_packet_for_dry_run(ctx, _PlanRequest(goal="change", plan="change it", spec=spec))

    assert packet["constitutional"] is False  # The existing gate is out of scope.
    assert ("Interaction meaning" in packet["system_prompt"]) == (subject == "candidate")
    assert (str(source) in packet["system_prompt"]) == (subject == "candidate")
    assert "Workspace design" not in packet["system_prompt"]


def _research_context(harness, subject):
    ctx = harness.make_ctx(active_workspace=subject not in {"main", "folderless"})
    if subject == "folderless":
        ctx.project_id = "folderless-research"
    elif subject in {"candidate", "foreign"}:
        subprocess.run(["git", "init", "-q"], cwd=harness.workspace, check=True, capture_output=True)
        origin = str(harness.system) if subject == "candidate" else "https://example.invalid/foreign.git"
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=harness.workspace,
                       check=True, capture_output=True)
    elif subject in {"body_copy", "foreign_copy"}:
        ctx.workspace_mode = "self_worktree"
        ctx.task_metadata["workspace_copy"] = {
            "execution_root": str(harness.workspace), "source_root": str(harness.system),
            "source_is_system_repo": subject == "body_copy",
        }
    return ctx


@pytest.mark.parametrize("flag_location", ["contract", "metadata"])
@pytest.mark.parametrize("subject, flag, expected", [
    ("main", True, True), ("candidate", True, True), ("body_copy", True, True),
    ("folderless", True, True), ("foreign", True, False), ("unknown", True, False),
    ("foreign_copy", True, False), ("main", None, False), ("main", False, False),
    ("candidate", None, False), ("folderless", None, False), ("unknown", None, False),
])
def test_empty_path_plan_api_and_session_require_declared_body_subject(
    harness, subject, flag, expected, flag_location,
):
    from tests.test_plan_review_engine import _state

    source = _design(harness.system)
    _design(harness.workspace, "# Workspace\n\n## Workspace design\n\nWorkspace rule.\n")
    ctx = _research_context(harness, subject)
    if flag is not None:
        declaration = ctx.task_contract if flag_location == "contract" else ctx.task_metadata
        declaration["context_requires_self_body_docs"] = flag
    # Neither prose nor a DESIGN lookalike supplies body identity or subject.
    ctx.task_contract["objective"] = "Research and brainstorm Ouroboros DESIGN"
    request = _plan_request(harness, ctx, {**DECK_SPEC, "affected_paths": []})

    assert _state(harness)["waves"][-1]["constitutional"] is False
    for text in (_user_text(request.messages[0]["content"]), request.session_task):
        assert ("Interaction meaning" in text) is expected
        assert (str(source) in text) is expected
        assert "Workspace design" not in text and "Original interaction rule." not in text
        if expected:
            assert "availability is not evidence of reading or application" in text
    if expected:
        assert "This reviewer has no repository tools" in _user_text(request.messages[0]["content"])
        assert "Read relevant original DESIGN sections with your own tools" in request.session_task


@pytest.mark.parametrize("subject", ["main", "candidate", "body_copy", "folderless"])
def test_declared_body_research_flag_cannot_override_actual_foreign_file_target(harness, subject):
    from tests.test_plan_review_engine import _state

    source = _design(harness.system)
    ctx = _research_context(harness, subject)
    ctx.task_contract["context_requires_self_body_docs"] = True
    foreign_target = harness.drive / "foreign-product" / "event.py"
    request = _plan_request(harness, ctx, {**DECK_SPEC, "affected_paths": [str(foreign_target)]})

    assert _state(harness)["waves"][-1]["constitutional"] is False
    for text in (_user_text(request.messages[0]["content"]), request.session_task):
        assert "Interaction meaning" not in text and str(source) not in text


def test_plan_missing_design_is_explicit_without_new_refusal(harness):
    request = _plan_request(harness, harness.make_ctx(), {
        **DECK_SPEC, "affected_paths": [str(harness.system / "ouroboros/loop.py")]})
    for text in (_user_text(request.messages[0]["content"]), request.session_task):
        assert "DESIGN.md source unavailable" in text
        assert str(harness.system / "docs/DESIGN.md") in text


def test_plan_selected_design_excerpt_is_content_in_evidence(harness):
    source = _design(harness.system)
    locator = f"{source}::lines=3-5"
    request = _plan_request(harness, harness.make_ctx(), {
        **DECK_SPEC, "affected_paths": [str(harness.system / "ouroboros/loop.py")], "evidence": [locator]})
    api_system = _user_text(request.messages[0]["content"])
    api_user = _user_text(request.messages[1]["content"])
    assert "Original interaction rule." not in api_system
    assert "Original interaction rule." in api_user and locator in api_user
    assert "Original appearance rule." not in api_user
    assert "Original interaction rule." in request.session_task
