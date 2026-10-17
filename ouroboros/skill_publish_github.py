"""GitHub transport and exact pull-request settlement for skill publication."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import Any, Dict, List, Tuple

from ouroboros.secret_masking import redact_known_values
from ouroboros.skill_publish_result import validate_skill_publish_receipt
from ouroboros.tools.github import GhResult, _gh_run, github_cli_configured, github_token_from_env_or_settings
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import truncate_within_limit

_HEX_OID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class SkillPublishGitHubError(RuntimeError):
    """Closed, candidate-free GitHub transport failure."""

    def __init__(self, reason_code: str, repair_hint: str, *, status: str = "partial",
                 detail: str = "", http_status: int | None = None, operation: str = "") -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.repair_hint = repair_hint
        self.status = status
        # Transport errors already fit (600-char head plus prefix). Also bound
        # malformed success responses before they become diagnostic detail.
        self.detail = truncate_within_limit(
            redact_known_values(detail, [github_token_from_env_or_settings()]), 640,
        ) if detail else ""
        self.http_status = http_status
        self.operation = operation


def github_repair_hint(result: GhResult, *, operation: str, repository: str,
                       branch: str = "", default: str) -> str:
    """One actionable hint from PRODUCER evidence only — the failure class the transport
    observed and gh's own HTTP marker. It states the fact and names the existing Settings
    field; it never asserts a cause (a 403 can be a permission OR a rate limit) and never
    retries: the model reads ``error_detail`` and decides."""
    if result.failure == "cli_missing":
        return "Install the GitHub CLI (gh) on this machine, then retry."
    if not github_cli_configured():
        return "No GitHub credential is configured; add GITHUB_TOKEN in Settings → Secrets, then retry."
    where = f"{operation} on {repository}" + (f" (branch {branch})" if branch else "")
    if result.http_status in (401, 403):
        return (f"GitHub answered HTTP {result.http_status} for {where}; read error_detail. If the "
                "token lacks access, update GITHUB_TOKEN in Settings → Secrets, then retry.")
    if result.http_status == 409:
        return f"GitHub reported a conflict (HTTP 409) for {where}; resolve it on GitHub, then retry."
    if result.failure == "timeout":
        return (f"gh did not finish {where} within its time limit; the outcome may be unknown — "
                "inspect it on GitHub before retrying.")
    return default


def _json_value(
    ctx: ToolContext,
    args: List[str],
    *,
    reason_code: str,
    operation: str,
    repository: str,
    branch: str = "",
    timeout: int = 30,
    input_data: str | None = None,
    object_required: bool = False,
) -> Any:
    result = _gh_run(args, ctx, timeout=timeout, input_data=input_data)
    if result.ok:
        try:
            data = json.loads(result.text) if result.text else {}
        except json.JSONDecodeError:
            pass
        else:
            if not object_required or isinstance(data, dict):
                return data
        # gh exited 0 but the body is not the expected shape: a parser fact, not a
        # connectivity guess. The bounded, redacted body rides as error_detail.
        hint = (f"GitHub answered {operation} on {repository}, but not with the expected JSON "
                f"{'object' if object_required else 'value'}; read error_detail.")
    else:
        hint = github_repair_hint(result, operation=operation, repository=repository, branch=branch,
                                  default="Inspect GitHub connectivity and repository access, then retry.")
    raise SkillPublishGitHubError(
        reason_code, hint, detail=result.text, http_status=result.http_status, operation=operation,
    )


def _json_object(
    ctx: ToolContext,
    args: List[str],
    *,
    reason_code: str,
    operation: str,
    repository: str,
    branch: str = "",
    timeout: int = 30,
    input_data: str | None = None,
) -> Dict[str, Any]:
    return _json_value(
        ctx,
        args,
        reason_code=reason_code,
        operation=operation,
        repository=repository,
        branch=branch,
        timeout=timeout,
        input_data=input_data,
        object_required=True,
    )


def github_login(ctx: ToolContext) -> str:
    result = _gh_run(["api", "/user", "--jq", ".login"], ctx)
    raw = result.text.strip()
    if not result.ok or not raw or len(raw) > 80:
        raise SkillPublishGitHubError(
            "github_actor_unavailable",
            github_repair_hint(result, operation="user", repository="<account>",
                               default="Repair GitHub authentication, then retry."),
            status="blocked",
            detail=result.text, http_status=result.http_status, operation="user",
        )
    return raw


def fetch_branch_head(ctx: ToolContext, owner: str, repo: str, branch: str) -> Tuple[str, str]:
    """``(head commit, branch)`` of the destination; an empty branch is the repository's default."""
    if not branch:
        info = _json_object(ctx, ["api", f"/repos/{owner}/{repo}"], reason_code="upstream_read_failed",
                            operation="repos", repository=f"{owner}/{repo}")
        branch = str(info.get("default_branch") or "")
        if not branch:
            raise SkillPublishGitHubError("upstream_read_failed", "Inspect the destination repository, then retry.",
                                          detail=json.dumps(info)[:600], operation="repos")
    ref = _json_object(
        ctx,
        ["api", f"/repos/{owner}/{repo}/git/refs/heads/{branch}"],
        reason_code="upstream_read_failed",
        operation="git/refs", repository=f"{owner}/{repo}", branch=branch,
    )
    ref_object = ref.get("object")
    base_sha = str((ref_object.get("sha") if isinstance(ref_object, dict) else "") or "")
    if not _HEX_OID_RE.fullmatch(base_sha):
        raise SkillPublishGitHubError(
            "upstream_read_failed",
            "Inspect the configured Hub base branch, then retry.",
            detail=json.dumps(ref), operation="git/refs",
        )
    return base_sha.lower(), branch


def fetch_upstream_file(ctx: ToolContext, owner: str, repo: str, sha: str, path: str) -> bytes | None:
    """One file of the destination at an exact commit; ``None`` when the commit has no such file."""
    repository = f"{owner}/{repo}"
    result = _gh_run(["api", f"/repos/{repository}/contents/{urllib.parse.quote(path)}?ref={sha}"], ctx)
    if not result.ok and result.http_status == 404:
        return None
    try:
        if not result.ok:
            raise ValueError("unreadable")
        return base64.b64decode(str(json.loads(result.text or "{}").get("content") or ""))
    except (ValueError, AttributeError) as exc:
        raise SkillPublishGitHubError(
            "upstream_read_failed",
            github_repair_hint(result, operation="contents", repository=repository,
                               default="Inspect the destination repository, then retry."),
            detail=f"{path} at {sha}: {result.text or exc}", http_status=result.http_status, operation="contents",
        ) from exc


def fetch_tree_paths(ctx: ToolContext, owner: str, repo: str, sha: str, folder: str) -> List[str]:
    """File paths under ``folder`` (relative to it) at an exact commit of the destination."""
    tree = _json_object(ctx, ["api", f"/repos/{owner}/{repo}/git/trees/{sha}?recursive=1"],
                        reason_code="upstream_read_failed", operation="git/trees", repository=f"{owner}/{repo}")
    prefix = folder.strip("/") + "/" if folder.strip("/") else ""
    return sorted(str(item.get("path"))[len(prefix):] for item in tree.get("tree") or []
                  if isinstance(item, dict) and item.get("type") == "blob" and str(item.get("path") or "").startswith(prefix))


def fetch_upstream_catalog(ctx: ToolContext, owner: str, repo: str, base_branch: str) -> Tuple[Dict[str, Any], str]:
    base_sha, base_branch = fetch_branch_head(ctx, owner, repo, base_branch)
    content = _json_object(
        ctx,
        ["api", f"/repos/{owner}/{repo}/contents/catalog.json?ref={base_sha}"],
        reason_code="upstream_read_failed",
        operation="contents", repository=f"{owner}/{repo}", branch=base_branch,
    )
    try:
        catalog_bytes = base64.b64decode(str(content.get("content") or ""))
        catalog = json.loads(catalog_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SkillPublishGitHubError(
            "upstream_catalog_invalid",
            "Repair the upstream Hub catalog, then retry.",
            detail=f"catalog.json at {base_sha}: {exc}", operation="contents",
        ) from exc
    if not isinstance(catalog, dict):
        raise SkillPublishGitHubError(
            "upstream_catalog_invalid",
            "Repair the upstream Hub catalog, then retry.",
            detail=f"catalog.json at {base_sha}: top-level JSON is {type(catalog).__name__}, not an object",
            operation="contents",
        )
    return catalog, base_sha


def prepare_publish_repository(
    ctx: ToolContext,
    attempt: Any,
    *,
    owner: str,
    repo: str,
    base_branch: str,
    login: str,
) -> None:
    """Make sure the account has its fork (its own repository needs none). The submission branch is
    created at the destination's exact commit, which a fork shares, so the fork is not synchronized."""
    repository = f"{login}/{repo}"
    if login.casefold() == owner.casefold():
        attempt.mark("fork_ready", repository=repository, actor=login)
        return
    existing = _gh_run(["repo", "view", repository, "--json", "name"], ctx)
    if not existing.ok:
        created = _gh_run(
            ["repo", "fork", f"{owner}/{repo}", "--clone=false"],
            ctx,
            timeout=60,
        )
        if not created.ok:
            raise SkillPublishGitHubError(
                "fork_prepare_failed",
                github_repair_hint(created, operation="repo fork", repository=f"{owner}/{repo}",
                                   default="Repair the GitHub fork, then retry."),
                detail=created.text, http_status=created.http_status, operation="repo fork",
            )
    attempt.mark("fork_ready", repository=repository, actor=login)


def _sync_fork(ctx: ToolContext, attempt: Any, login: str, repo: str, base_branch: str) -> None:
    """Bring a fork up to the destination branch; only when GitHub refused a branch at its commit."""
    merged = _gh_run(["api", "-X", "POST", f"/repos/{login}/{repo}/merge-upstream", "-f", f"branch={base_branch}"],
                     ctx, timeout=45)
    if not merged.ok:
        raise SkillPublishGitHubError(
            "fork_sync_failed",
            github_repair_hint(merged, operation="merge-upstream", repository=f"{login}/{repo}", branch=base_branch,
                               default="Repair or synchronize the GitHub fork, then retry."),
            detail=merged.text, http_status=merged.http_status, operation="merge-upstream",
        )
    if attempt is not None:
        attempt.mark("fork_synced", repository=f"{login}/{repo}", actor=login)


def ensure_branch(ctx: ToolContext, login: str, repo: str, branch: str, base_sha: str, *,
                  base_branch: str = "", attempt: Any = None) -> str:
    existing = _gh_run(
        ["api", f"/repos/{login}/{repo}/git/ref/heads/{branch}"],
        ctx,
    )
    if existing.ok:
        raise SkillPublishGitHubError(
            "submission_branch_exists",
            "Remove the old submission branch or bump the skill version, then retry.",
            detail=existing.text, http_status=existing.http_status, operation="git/ref",
        )
    create = ["api", "-X", "POST", f"/repos/{login}/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}",
              "-f", f"sha={base_sha}"]
    try:
        created = _json_object(ctx, create, reason_code="branch_create_failed",
                               operation="git/refs", repository=f"{login}/{repo}", branch=branch)
    except SkillPublishGitHubError as exc:
        # A fork that never saw this commit refuses a ref to it (422): synchronize once, then retry.
        if exc.http_status != 422 or not base_branch:
            raise
        _sync_fork(ctx, attempt, login, repo, base_branch)
        created = _json_object(ctx, create, reason_code="branch_create_failed",
                               operation="git/refs", repository=f"{login}/{repo}", branch=branch)
    branch_sha = str((created.get("object") or {}).get("sha") or "")
    if not _HEX_OID_RE.fullmatch(branch_sha):
        raise SkillPublishGitHubError(
            "branch_create_failed",
            "Inspect the GitHub submission branch, then retry.",
            detail=json.dumps(created), operation="git/refs",
        )
    return branch_sha.lower()


def commit_payload(
    ctx: ToolContext,
    login: str,
    repo: str,
    branch: str,
    base_sha: str,
    headline: str,
    additions: List[Dict[str, str]],
    deletions: List[Dict[str, str]] | None = None,
) -> Tuple[str, str]:
    query = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
""".strip()
    file_changes: Dict[str, Any] = {"additions": additions}
    if deletions:
        file_changes["deletions"] = deletions
    payload = {
        "query": query,
        "variables": {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": f"{login}/{repo}",
                    "branchName": branch,
                },
                "message": {
                    "headline": headline,
                    "body": ("Co-authored-by: Ouroboros <311266734+ouroboros-agent@users.noreply.github.com>"),
                },
                "fileChanges": file_changes,
                "expectedHeadOid": base_sha,
            }
        },
    }
    result = _json_object(
        ctx,
        ["api", "graphql", "--input", "-"],
        timeout=60,
        input_data=json.dumps(payload),
        reason_code="commit_create_failed",
        operation="graphql", repository=f"{login}/{repo}", branch=branch,
    )
    if result.get("errors"):
        raise SkillPublishGitHubError(
            "commit_create_failed",
            "Inspect the GitHub submission branch, then retry.",
            detail=json.dumps(result), operation="graphql",
        )
    commit = ((result.get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}
    commit_sha = str(commit.get("oid") or "")
    commit_url = str(commit.get("url") or "")
    if not _HEX_OID_RE.fullmatch(commit_sha):
        raise SkillPublishGitHubError(
            "commit_create_failed",
            "Inspect the GitHub submission branch, then retry.",
            detail=json.dumps(result), operation="graphql",
        )
    return commit_sha.lower(), commit_url[:360]


def _receipt_from_url(
    url: str,
    *,
    repository: str,
    skill: str,
    snapshot_hash: str,
    ruleset_sha256: str,
) -> Dict[str, Any] | None:
    candidate = str(url or "").strip()
    if "\n" in candidate or "\r" in candidate:
        return None
    try:
        segments = urllib.parse.urlsplit(candidate).path.split("/")
        number = int(segments[-1]) if segments[-1].isdigit() else 0
    except (TypeError, ValueError):
        return None
    receipt = {
        "kind": "github_pull_request",
        "repository": repository,
        "url": candidate,
        "number": number,
        "skill": skill,
        "snapshot_hash": snapshot_hash,
        "ruleset_sha256": ruleset_sha256,
    }
    return validate_skill_publish_receipt(
        receipt,
        expected_repository=repository,
        expected_skill=skill,
        expected_snapshot_hash=snapshot_hash,
        expected_ruleset_sha256=ruleset_sha256,
    )


def _lookup_open_pr_receipt(
    ctx: ToolContext,
    *,
    owner: str,
    repo: str,
    base_branch: str,
    login: str,
    branch: str,
    commit_sha: str,
    skill: str,
    snapshot_hash: str,
    ruleset_sha256: str,
) -> Dict[str, Any] | None:
    rows = _json_value(
        ctx,
        [
            "api",
            "--method",
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            "-f",
            "state=open",
            "-f",
            f"head={login}:{branch}",
            "-f",
            f"base={base_branch}",
            "-f",
            "per_page=100",
        ],
        reason_code="pr_open_indeterminate",
        operation="pulls", repository=f"{owner}/{repo}", branch=branch,
        timeout=30,
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    head = row.get("head") if isinstance(row.get("head"), dict) else {}
    base = row.get("base") if isinstance(row.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    head_owner = head_repo.get("owner") if isinstance(head_repo.get("owner"), dict) else {}
    if (
        str(head.get("sha") or "").casefold() != commit_sha.casefold()
        or str(head.get("ref") or "") != branch
        or str(head_owner.get("login") or "").casefold() != login.casefold()
        or str(base.get("ref") or "") != base_branch
    ):
        return None
    receipt = _receipt_from_url(
        str(row.get("html_url") or ""),
        repository=f"{owner}/{repo}",
        skill=skill,
        snapshot_hash=snapshot_hash,
        ruleset_sha256=ruleset_sha256,
    )
    return receipt if receipt is not None and receipt["number"] == row.get("number") else None


def create_pr_receipt(
    ctx: ToolContext,
    attempt: Any,
    *,
    owner: str,
    repo: str,
    base_branch: str,
    login: str,
    branch: str,
    title: str,
    body: str,
    commit_sha: str,
) -> Dict[str, Any] | None:
    repository = f"{owner}/{repo}"
    attempt.mark(
        "pr_create_attempted",
        repository=repository,
        actor=login,
        branch=branch,
        commit_sha=commit_sha,
    )
    result = _gh_run(
        [
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            base_branch,
            "--head",
            f"{login}:{branch}",
            "--title",
            title,
            "--body-file",
            "-",
        ],
        ctx,
        timeout=60,
        input_data=body,
    )
    direct = None
    if result.ok:
        direct = _receipt_from_url(
            result.text,
            repository=repository,
            skill=attempt.skill,
            snapshot_hash=attempt.snapshot_hash,
            ruleset_sha256=str(attempt.scanner.get("ruleset_sha256") or ""),
        )
    if direct is not None:
        return direct
    try:
        settled = _lookup_open_pr_receipt(
            ctx,
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            login=login,
            branch=branch,
            commit_sha=commit_sha,
            skill=attempt.skill,
            snapshot_hash=attempt.snapshot_hash,
            ruleset_sha256=str(attempt.scanner.get("ruleset_sha256") or ""),
        )
    except SkillPublishGitHubError as exc:
        if result.ok:
            # The mutator succeeded and only the read-only settlement failed: say so
            # before the settlement's own hint, or "retry" would risk a duplicate PR.
            raise SkillPublishGitHubError(
                exc.reason_code,
                "gh pr create reported success but the pull request could not be settled; "
                "inspect the recorded branch and commit on GitHub before retrying. " + exc.repair_hint,
                status=exc.status, detail=exc.detail, http_status=exc.http_status, operation=exc.operation,
            ) from exc
        # Retain the mutator's evidence if the read-only settlement also failed.
        settled = None
    if settled is None and not result.ok:
        raise SkillPublishGitHubError(
            "pr_open_indeterminate",
            github_repair_hint(result, operation="pr create", repository=repository, branch=branch,
                               default="Inspect the recorded branch and commit before deciding whether to retry."),
            detail=result.text, http_status=result.http_status, operation="pr create",
        )
    return settled


__all__ = [
    "SkillPublishGitHubError",
    "commit_payload",
    "create_pr_receipt",
    "ensure_branch",
    "fetch_branch_head",
    "fetch_tree_paths",
    "fetch_upstream_catalog",
    "fetch_upstream_file",
    "github_login",
    "github_repair_hint",
    "prepare_publish_repository",
]
