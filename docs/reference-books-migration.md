# Reference books: chapter migration transfer table

This is the operator record of the physical split of the two reference books
(`docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`) at base commit `5585133db86419c1a28673e498de4fb13c6b2d1e`.
It is a docs file and deliberately NOT a book member: it lives directly under
`docs/`, so `validate_reference_books` never sees it in either book's chapter
population.

Every row below is a **verbatim move**. The moved bytes are every byte of the
old `##` section AFTER its heading line, including its `###`/`####`
sub-headings at the levels they already had. Nothing was merged, rewritten,
summarized, reordered or deleted, and no section title was renamed — the
rename column is empty for every row. Semantic work (merging duplicate
explanations, retiring obsolete history) is a separate later package and will
add its own dispositions here.

## What changed, exactly

Per chapter file, the only new bytes are a two-line prologue:

1. `# <the old section title>` — the old `## N. Title` text at H1, numbering
   text kept so every cross-reference in the corpus still reads;
2. one authored introductory paragraph (2–4 sentences: what the chapter owns
   and why it exists), the only new prose in this migration.

Everything after that prologue is the old section body, byte for byte.

Per entrypoint, the body is replaced by an ordered `## Chapters` membership
list; the H1 line stays byte-identical in both books.

## Byte proof

`tests/test_reference_book_migration.py` reverses the prologue of every chapter
(drop the H1 line, drop the one introductory paragraph, re-prefix `## ` to the
H1 text), concatenates the results in membership order, and requires the
SHA-256 below. It also compares against `git show <base>:<path>` whenever the
base commit is reachable, so the recorded digest cannot be a fiction.

| Book | Old file | Old bytes | Old SHA-256 | Preamble bytes (replaced) | Moved-body bytes | Moved-body SHA-256 |
|---|---|---|---|---|---|---|
| architecture | `docs/ARCHITECTURE.md` | 724691 | `5db278f8ef5060c4aff5ee1e8743c279661ddd975a311a9bafb5858f32b080de` | 610 | 724081 | `f1f054c700a15533687e0cf81cf19ac53ffcf022eb179f84c0cbccacbb1d305e` |
| development | `docs/DEVELOPMENT.md` | 275551 | `50eb460602f1501915195e3ad1918366312e6292b0dccec6f7ef53f5a5302f3b` | 60 | 275491 | `bffc00227bc5e91f054b38eaed63acd256c2ddf111e231754bfbe92c7dd122e3` |

## `docs/ARCHITECTURE.md`

Entrypoint preamble before: `# Ouroboros v7.0.0 — Architecture & Reference` (byte-identical, the release version carrier `release_sync.VERSION_CARRIER_SPANS` writes), then two paragraphs (`This file is NOT a changelog…` and `This is the present-tense operational map…`) and a `---` rule.

Entrypoint preamble after: the same H1, then ONE merged paragraph carrying both original paragraphs' claims (present-tense map in three layers, not a changelog, WHY stays in the book, rationale self-contained), then `## Chapters`. The `---` rule is dropped.

| Old `##` section | Lines at base | Section bytes | Destination chapter | Disposition | Title rename |
|---|---|---|---|---|---|
| `1. High-Level Architecture` | 9–659 | 188782 | `docs/architecture/01-high-level-architecture.md` | verbatim move | — |
| `2. Startup / Onboarding Flow` | 660–695 | 14555 | `docs/architecture/02-startup-onboarding-flow.md` | verbatim move | — |
| `3. Web UI Pages & Buttons` | 696–868 | 88631 | `docs/architecture/03-web-ui-pages-and-buttons.md` | verbatim move | — |
| `4. Server API Endpoints` | 869–1031 | 23636 | `docs/architecture/04-server-api-endpoints.md` | verbatim move | — |
| `5. Supervisor Loop` | 1032–1082 | 32383 | `docs/architecture/05-supervisor-loop.md` | verbatim move | — |
| `6. Agent Core` | 1083–1871 | 258915 | `docs/architecture/06-agent-core.md` | verbatim move | — |
| `7. Configuration (ouroboros/config.py)` | 1872–2084 | 33538 | `docs/architecture/07-configuration.md` | verbatim move | — |
| `8. Git Branching, CI, and Build` | 2085–2140 | 17850 | `docs/architecture/08-git-branching-ci-and-build.md` | verbatim move | — |
| `9. Shutdown & Process Cleanup` | 2141–2156 | 12956 | `docs/architecture/09-shutdown-and-process-cleanup.md` | verbatim move | — |
| `10. Key Invariants` | 2157–2235 | 15834 | `docs/architecture/10-key-invariants.md` | verbatim move | — |
| `11. Frozen Contracts v1 (`ouroboros/contracts/`)` | 2236–2326 | 19130 | `docs/architecture/11-frozen-contracts-v1.md` | verbatim move | — |
| `12. Host Service, Companion Processes, and Chat IDs` | 2327–2371 | 10594 | `docs/architecture/12-host-service-companions-and-chat-ids.md` | verbatim move | — |
| `13. External Skills Layer` | 2372–2386 | 7277 | `docs/architecture/13-external-skills-layer.md` | verbatim move | — |

## `docs/DEVELOPMENT.md`

Entrypoint preamble before: `# DEVELOPMENT.md — Development Principles & Module Guide` (byte-identical), then `## Role and authority` directly, with no introductory paragraph of its own.

Entrypoint preamble after: the same H1, then ONE newly authored orientation paragraph (what the handbook is, how the chapters are ordered, read the chapter for the class of change in hand), then `## Chapters`. No moved prose.

| Old `##` section | Lines at base | Section bytes | Destination chapter | Disposition | Title rename |
|---|---|---|---|---|---|
| `Role and authority` | 3–32 | 1707 | `docs/development/01-role-and-authority.md` | verbatim move | — |
| `Naming and boundaries` | 33–526 | 35291 | `docs/development/02-naming-and-boundaries.md` | verbatim move | — |
| `Module Size & Complexity` | 527–849 | 21621 | `docs/development/03-module-size-and-complexity.md` | verbatim move | — |
| `Core Governance Artifacts` | 850–1121 | 20584 | `docs/development/04-core-governance-artifacts.md` | verbatim move | — |
| `Review & Commit Protocol` | 1122–1351 | 15585 | `docs/development/05-review-and-commit-protocol.md` | verbatim move | — |
| `Rules by change class` | 1352–2963 | 118624 | `docs/development/06-rules-by-change-class.md` | verbatim move | — |
| `Managed Update Rule` | 2964–3022 | 3732 | `docs/development/07-managed-update-rule.md` | verbatim move | — |
| `Mutation Attribution Rule` | 3023–3059 | 2289 | `docs/development/08-mutation-attribution-rule.md` | verbatim move | — |
| `Process Custody Rule` | 3060–3203 | 10776 | `docs/development/09-process-custody-rule.md` | verbatim move | — |
| `Platform Abstraction Rule` | 3204–3247 | 2514 | `docs/development/10-platform-abstraction-rule.md` | verbatim move | — |
| `Design System` | 3248–3557 | 22543 | `docs/development/11-design-system.md` | verbatim move | — |
| `MCP Client Integration` | 3558–3605 | 3454 | `docs/development/12-mcp-client-integration.md` | verbatim move | — |
| `Gateway Boundary Pattern` | 3606–3635 | 1986 | `docs/development/13-gateway-boundary-pattern.md` | verbatim move | — |
| `Build & CI` | 3636–3886 | 14785 | `docs/development/14-build-and-ci.md` | verbatim move | — |

## Chapter granularity

One chapter per old `##` section, with no merges. Two adjacent pairs were
under the ~60-line merge threshold on both sides — Architecture §12/§13 and
Development "MCP Client Integration"/"Gateway Boundary Pattern" — but neither
pair shares a subject (a host callback boundary is not the external skills
plane; an outbound MCP client is not the inbound browser boundary), so the
default 1:1 mapping was kept. It also keeps every existing cross-reference of
the form `ARCHITECTURE "8. Git Branching, CI, and Build"` resolving to exactly
one chapter.
