"""Full exact-commit qualification at adoption and evolution publication."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace


def full_proof_error(ctx, repo, commit="") -> str:
    from ouroboros.commit_admission import preflight_test_proof_matches

    proof = getattr(ctx, "_preflight_test_proof", None)
    try:
        if (getattr(ctx, "_preflight_tests_passed", False) and proof is not None
                and (not commit or proof.head == commit) and preflight_test_proof_matches(ctx, repo)):
            return ""
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return "a full green exact-tree test proof is required; selected, skipped or stale evidence cannot qualify"


def qualify_commit(repo, commit, data_dir, *, task_id="") -> dict:
    """Use an immutable checkout, so later dirty edits cannot qualify an earlier SHA."""
    from ouroboros import preflight_runner as pr
    from ouroboros.test_environment import default_temp_parent

    repo = Path(repo)
    root = Path(tempfile.mkdtemp(prefix="ouroboros-qualification-", dir=default_temp_parent()))
    tree = root / "repo"
    added = False
    try:
        result = pr._run_git(repo, ["-c", "core.autocrlf=false", "-c", "core.eol=lf",
                                    "worktree", "add", "--detach", str(tree), commit], timeout=60)
        if result.returncode:
            raise ValueError(result.stderr.strip() or "could not materialize the exact adoption commit")
        added = True
        ctx = SimpleNamespace(repo_dir=tree, drive_root=Path(data_dir), task_id=task_id)
        error = pr.run_hermetic_pytest(tree, ctx=ctx, selected=True, base="HEAD",
                                      full_tests=True, test_reason="exact-commit adoption qualification")
        if error or (error := full_proof_error(ctx, tree, commit)):
            raise ValueError(error)
        return dict(ctx._preflight_test_disclosure)
    finally:
        pr._release_hermetic_tree(repo, root, tree, added, "")
