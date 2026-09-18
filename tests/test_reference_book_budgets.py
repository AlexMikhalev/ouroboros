"""Per-chapter byte budgets for the two reference books.

Official-CI ``size_ratchet`` lane only (local runs exclude the marker, like the repository size
gates): a chapter may grow past its budget only in a diff that raises the number here and says
why. The budget is the chapter's size after the compression pass plus roughly ten percent, so
ordinary maintenance fits and accretion does not. The same maintenance margin applies to unchanged registry chapters.
"""
from __future__ import annotations

import pathlib

import pytest

from ouroboros.reference_books import BOOK_ENTRYPOINTS, load_reference_book

REPO = pathlib.Path(__file__).resolve().parents[1]

# UTF-8 bytes of each chapter source. Raise a value in the same diff that needs it, with a reason.
CHAPTER_BYTE_BUDGETS: dict[str, int] = {
    "docs/architecture/01-high-level-architecture.md": 161453,
    "docs/architecture/02-startup-onboarding-flow.md": 15517,
    "docs/architecture/03-web-ui-pages-and-buttons.md": 97435,
    "docs/architecture/04-server-api-endpoints.md": 26833,
    "docs/architecture/05-supervisor-loop.md": 27137,
    "docs/architecture/06-agent-core.md": 286850,
    "docs/architecture/07-configuration.md": 36991,
    "docs/architecture/08-git-branching-ci-and-build.md": 18947,
    "docs/architecture/09-shutdown-and-process-cleanup.md": 12405,
    "docs/architecture/10-key-invariants.md": 17655,
    "docs/architecture/11-frozen-contracts-v1.md": 24194,
    "docs/architecture/12-host-service-companions-and-chat-ids.md": 11007,
    "docs/architecture/13-external-skills-layer.md": 7764,
    "docs/development/01-role-and-authority.md": 2437,
    "docs/development/02-naming-and-boundaries.md": 36372,
    "docs/development/03-module-size-and-complexity.md": 22873,
    "docs/development/04-core-governance-artifacts.md": 16431,
    "docs/development/05-review-and-commit-protocol.md": 12956,
    "docs/development/06-rules-by-change-class.md": 94197,
    "docs/development/07-managed-update-rule.md": 4166,
    "docs/development/08-mutation-attribution-rule.md": 2899,
    "docs/development/09-process-custody-rule.md": 10028,
    "docs/development/10-platform-abstraction-rule.md": 3316,
    "docs/development/11-design-system.md": 27103,
    "docs/development/12-mcp-client-integration.md": 3313,
    "docs/development/13-gateway-boundary-pattern.md": 2228,
    "docs/development/14-build-and-ci.md": 14958,
}


@pytest.mark.size_ratchet
def test_every_chapter_has_a_budget_and_stays_inside_it():
    seen = set()
    for book_id in BOOK_ENTRYPOINTS:
        for chapter in load_reference_book(REPO, book_id).chapters:
            seen.add(chapter.source_path)
            budget = CHAPTER_BYTE_BUDGETS.get(chapter.source_path)
            assert budget is not None, f"{chapter.source_path}: add a byte budget for the new chapter"
            size = len(chapter.raw)
            assert size <= budget, (
                f"{chapter.source_path}: {size} bytes exceeds its budget {budget}; replace the description "
                "you touched instead of appending, or raise the budget in this diff with a reason"
            )
    stale = set(CHAPTER_BYTE_BUDGETS) - seen
    assert not stale, f"budgets for chapters that no longer exist: {sorted(stale)}"
