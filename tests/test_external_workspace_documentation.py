"""The public workspace contract must keep ordinary folders visible."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_external_workspace_docs_distinguish_plain_folders_from_git_operations():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    control = (ROOT / "ouroboros" / "tools" / "control.py").read_text(encoding="utf-8")
    assert "ordinary folders or separate Git worktree roots" in readme
    assert "ordinary file and process work runs directly" in readme
    assert "Ordinary folders support direct file/process work" in architecture
    assert "ordinary folder or Git worktree root" in control
    assert "ordinary file and process work is supported directly" in control
    assert "External workspaces must be separate Git worktree roots" not in readme
    assert "must be a git worktree root outside" not in control
