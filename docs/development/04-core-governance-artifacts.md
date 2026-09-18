# Core Governance Artifacts

The **core governance artifacts** — `BIBLE.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md` — are the constitutional, architectural and procedural ground truth. This chapter owns their availability in every reasoning flow: the delivery registry, plan review's structural tiering, exact planning premises, earned compaction and disclosed, model-only truncation. A reviewer without the architecture map lacks full context; a required artifact that cannot fit is a typed refusal with its disclosure, an optional one a named omission — never a quietly shrunk pack.

### Invariant: Full availability in reasoning flows

Any flow that requires architectural, constitutional, or procedural reasoning
MUST include these artifacts as **first-class context sections** — not as
optional or opportunistic inclusions via touched-file packs.

Plan review is the one flow whose governance pack is tiered, by ONE structural
fact — whether a declared `affected_paths` target resolves under the Ouroboros
system repository, never prose or a plan-kind taxonomy, which keeps
classification un-gameable. Tiering is not omission: the subject is an
INTENTION before any work exists, and nothing is silently omitted (P1). Only a
REQUIRED governance pack that cannot be assembled stops the review, as a typed
assembly failure (`PlanPacketError`); evidence the policy cannot attach is a
named absence the panel still judges with (`[reviewer-requested]` omission row,
head cut `truncated_to_<N>`), and a re-asked locator stays `need_evidence`
(`need_evidence_repeat`) without new request memory or paid cycles.
Classification, packet composition, bounds and wave/replay mechanics: ARCHITECTURE §6 "Plan
construction and review", `ouroboros/tools/plan_packet.py`, `plan_spec.py`.

Exact-wave custody is fail-closed: the evidence continuation uses a fresh
full-packet dispatch only when no exact artifact reference exists; an unreadable
referenced artifact returns `plan_review_exact_artifact_unavailable` and never
mints replacement authority.

The context-delivery registry:

| Flow | BIBLE.md | ARCHITECTURE.md | DEVELOPMENT.md |
|------|----------|-----------------|----------------|
| Main task context (`context.py`) | full tier-0 | full composition in Max, a subagent child excepted (issue #1026); book navigation in Low/Nano and for every subagent child | book navigation in Low/Nano and for a subagent child; in Max full when the active binding targets the system repo (evolution/self-body work, `workspace="none"`, a project-room turn with no external binding), else a visible on-demand pointer (external workspace, API/CLI/scheduled surface) |
| Triad review (`tools/review.py`) | ✅ via preamble | ✅ via `load_governance_doc` | ✅ via `load_governance_doc` |
| ↳ Cold-start density rung | — | — | Shared with scope review and the packed deep self-review: a packet that would be refused or degraded for size while its route has no fresh exact-model density witness gets one bounded exact-model probe, then one re-size/rebuild; a budget-refused probe is a typed disclosure and the refusal stands (`capability_evidence.cold_start_density_probe`; ARCHITECTURE §6 "Review stack") |
| ↳ Anti-thrashing | — | — | Open obligations from `review_state` (`load_state(drive_root)` + `make_repo_key(repo_dir)`) injected unconditionally into `_build_review_history_section`; scope the same, best-effort when `drive_root` is available (`scope_review_pack._build_scope_prompt`) |
| Background consciousness wake-up (`consciousness.py` → `handle_wake_direct`) | = Main task context | = Main task context | = Main task context |
| Advisory pre-review (`tools/claude_advisory_review.py`) | both delivery classes retrieve via MANDATORY FULL READ pointers — `api_chat` in a bounded NATIVE inspection episode (host-observed), `agent_session` with its own tools (unobserved); retrieval disclosed | same two delivery classes | same two delivery classes |
| Scope review (`tools/scope_review.py`) | full canonical doc + Atlas accounting; under a cold density cap a size terminal or degradation rung takes the density rung (`density_probe` ladder step) | full canonical doc + Atlas accounting | full canonical doc + Atlas accounting |
| Skill review (`skill_review.py`) | full inline (`api_chat`) / mandatory full source-root read (`agent_session`) | same two classes | same two classes |
| Plan review (`tools/plan_review.py`) | full for a SELF-MODIFICATION plan; otherwise a runtime heading-derived navigation map, never a copy | full for a self-modification plan (`api_chat` inline, `agent_session` mandatory full read); otherwise book navigation + a resolvable pointer | not resident: a named on-demand pointer; a reviewer needing it returns `need_evidence` with an exact `::lines=A-B` range |
| Deep self-review (`deep_self_review.py`) | three deliveries on the `deep_review` row — packed api row: full doc + Atlas accounting, typed `deep_self_review_pack_unfit` refusal, no fallback; native episode / agent session: MANDATORY full read at the repository root, coverage host-observed / `unobserved`; memory inlined byte-exact on every delivery (ARCHITECTURE §6 "Deep self-review") | packed: full composition (Max) / book navigation (Low), + Atlas accounting; retrieving rows: book navigation, chapters read on demand | packed: full composed book + Atlas accounting; retrieving rows: book navigation (CHECKLISTS.md keeps its single-doc `generate_doc_nav_map`) |

Skill review keeps the full stable governance/host prefix cache-friendly on API
rows; a retrieving session reads those canonical files from its
source-repository root and takes only the byte-exact dynamic tail inline, so
payload snapshot and per-chunk quorum stay identical without rebilling or
crowding its window.

Planning resolves targets and evidence against `active_repo_dir_for(ctx)` and
governance against the system repository; never fall back to reviewing the
Ouroboros repo for an external plan. Exact user-managed installed-skill payload
paths are the one data-plane exception, for CLASSIFICATION only: never a
self-modification, never attachable evidence (`denied_path`).

SPEC shape, finding classes, verdicts and closure rules: ARCHITECTURE §6 "Plan construction
and review"; findings are inputs the main agent may accept, reject, or defer.
Outstanding `need_evidence` closes with no second LLM call, through a separate
`plan_task` call carrying only `review_disposition`
(`{review_fingerprint, items: [{finding_id, decision, rationale}]}`, one item per
required finding, exactly once);
duplicate, contradictory, unknown, stale or incomplete dispositions fail closed,
and a mixed or vacuous call is a typed argument error before any attempt is
recorded (a default-empty optional field is ignored, not meaning:
`plan_review._vacuous`). Do not replay the plan envelope merely to disposition findings.
An explicit `review_disposition.author_action` with author disposition and the
actual critic fingerprint is a separate declared operation: it may carry a full
corrected goal/plan/spec. Its exact `current_attempt.author_subject` source stays
separate from the critic wave. Advisory finish may select it without another panel;
Blocking stop preserves it for later work without approving implementation.
`closed_plan_review_wave` still means critic-closed authority; current Advisory
author claims use the existing acceptance owner with `author_plan` provenance.

Force-plan is an LLM-first pre-implementation obligation on the admitted managed
root, not a mechanical permission check. `plan_review_state` owns durable review
facts, `config.get_review_enforcement()` the blocking/advisory value; effective
Cyber authority is separate (BIBLE P0/P3) and never rewrites an old wave to
GREEN. Every submitted envelope reaching `plan_task` supersedes prior authority,
so no newer attempt falls back to an older GREEN. Paid cycles are bounded by the
shared `OUROBOROS_REVIEW_MAX_CYCLES` (`ouroboros/review_cycles.py`).

**Context mode (Nano / Low / Max).** The Main task context row above is each
mode's projection of the two books (ARCHITECTURE §6 "Context fitting, retry, and
compaction"); Max binds `DEVELOPMENT.md` to the active repository — a path fact,
never a guess from message text. Tier-0 identity and constitutional context
stays full in every mode. Predicted Max pressure never swaps in Low documents:
only actual provider overflow may use a task-local Low projection, then at most
one same-route strictly-smaller call, and none of it changes owner mode or P3
commit/scope review. Disclosed residual: an explicit per-task handbook override
(`context_requires_self_body_docs`) wins in Max only: Low and Nano ignore it
(issue #1019), as does a delegated subagent child in every mode (issue #1026); the
sibling `context_requires_development` flag is ignored on the same paths.

### Invariant: Exact premises with explicit source ownership

Plan from the complete retained room — both speakers, options and answers
exact — (`dialogue_evidence.py`, `plan_dialogue.py`; ARCHITECTURE §6 "Plan construction and
review"), with no independent dialogue byte cap, never from the bounded
post-consolidation reader or the acceptance directive ledger that task
acceptance keeps (`review_evidence._accept_owner_directives`). JSONL records
and chat line selectors split on physical LF only, never on valid Unicode
inside a message. Each consumer redacts at its boundary and discloses missing
source or ranges; a replay or earned paid retry of the same author request
keeps its recorded snapshot, disclosing later messages as unreviewed. Follow
ARCHITECTURE's per-delivery context/source contract for delivery, coverage and sizing. Enforcement:
`test_packet_uses_full_dialogue_and_keeps_acceptance_directives`,
`tests/test_plan_dialogue_review_regressions.py`, the acceptance ledger tests
in `tests/test_loop_misc.py`.

### Invariant: Compaction must earn its rewrite

Context compaction is a deficit-requested materializer, not an independent
threshold, timer, route, or retry policy (the no-reclaim-no-mutation rule and
the route+round latch: ARCHITECTURE §6 "Context fitting, retry, and compaction"). For a
non-empty selection, persist the exact actor-visible checkpoint before calling
the summarizer. A replacement publishes only once transcript/unit binding,
complete coverage, checkpoint provenance and a strictly smaller size on the
caller's ContextFit basis are proved, with matching image proxy and density
(raw base64 bytes are not token reclaim). Only typed summarizer context
overflow may split a source; capsules keep host-only provenance metadata, so
recompaction never loses the original provenance union. Enforcement:
`tests/test_compaction.py`, `tests/test_loop_compaction.py`,
`tests/test_loop_compaction_policy.py`.

### Invariant: No silent truncation

If a core governance artifact cannot fit in the available context budget:

- Where the flow REQUIRES it, that is a FAILURE to assemble, not a smaller pack
  (BIBLE P3): a typed entry names the artifact and reason, the review does not
  proceed on the remainder, and disclosure accompanies the refusal, never
  replaces it; adjust the budget/flow or refactor. Elsewhere an omission or cut is
  NAMED where the reader sees it (`Reference book source unavailable: …`,
  `⚠️ OMISSION NOTE`), never silent, so operator and model both know the
  context is incomplete.
- A reviewer or agent operating without ARCHITECTURE.md MUST NOT be treated as
  operating with full context — findings may be incomplete.
- Tools returning multi-model review findings (`commit_reviewed`,
  `skill_review`, scope/advisory review helpers) MUST be in
  `UNTRUNCATED_TOOL_RESULTS` or carry an explicit per-tool limit; the default
  15,000-char `DEFAULT_TOOL_RESULT_LIMIT` is not acceptable for review verdicts.
- Book **navigation** (`context_layout.book_navigation`: per chapter the
  authored introduction, physical path and H2-H4 inclusive complete-subtree
  ranges), a single-doc **navigation map** and a named on-demand pointer are
  visible, lossless representations, NOT silent truncation; Low and Nano use
  them and never apply `[:N]` to a doc.
- Bound strings through the SSOT `utils.truncate_review_artifact` (DISPLAY
  previews) or `utils.truncate_within_limit` (a STRICT wire/prompt bound that
  never exceeds its limit), never a hand-rolled `text[:cap] + marker`, which
  loses the anti-waste floor and can return a value LONGER than its input.
- A LIST obeys the same rule: a `[:N]` slice carries an explicit omitted COUNT
  and, where it touches an identity something downstream compares, a durable
  hash or reference for the full set
  (`_outcome_receipts.receipt_identity_projection`); bounding a set is allowed,
  hiding that you bounded it is the P1 violation.

Disclosed source-read gap: some existing `load_governance_doc` callers continue
with an explicit omission marker (triad and skill review) or a placeholder
(scope review) when a book cannot be loaded. Source unreadability is therefore
not uniformly refused; the required-artifact cannot-fit refusal above does not
certify those loader paths.

Enforcement: `tests/test_tool_capabilities.py` (the `UNTRUNCATED_TOOL_RESULTS`
roster) and the truncation-floor coverage in
`tests/test_owner_facing_honesty.py`.

### Invariant: Owner-facing surfaces show the full text

Disclosed truncation (the `⚠️ OMISSION NOTE` marker) protects **LLM context
budgets**; it never licenses shortening what the owner reads:

- **Owner/UI-bound surfaces** (chat panels, task_results projections, review
  verdicts shown to a person) present the COMPLETE text or a reference to a
  durable full copy (e.g. an observability `response_ref`): reviewer rationale
  is a cognitive artifact (BIBLE P1), so truncating it beside an unreferenced
  full copy in private blobs is partial memory loss. Terminal text asserts only
  recovery facts the round record carries, never a route mechanism the selected
  transport cannot perform.
- **Model-bound projections** (review packs, context sections, tool-result
  transport) keep their disclosed-truncation budgets — real context economics.
- **A cut cheaper than its own marker is forbidden everywhere** (the shared
  primitive's floor). One named exception: single-line identifier fields under
  a 100-char limit (a reflection backlog `kind`) take a plain hard slice, as a
  multi-line marker in a one-line value does worse damage than the cut.

Enforcement: `tests/test_owner_facing_honesty.py`.

### Invariant: No "only if touched" gate for core artifacts

Core governance artifacts reach review/reasoning flows unconditionally, NOT only
when in `touched_paths`: `build_touched_file_pack` serves _changed_ files; core
artifacts load independently. No surface of its own — the per-flow presence
tests below are the mechanical cover; otherwise review-only.

### When adding a new reasoning flow

A new flow that reasons about code structure, system architecture, or
engineering standards MUST:

1. Explicitly load `ARCHITECTURE.md` (and BIBLE.md if constitutional reasoning
   applies).
2. Log a warning if the file is missing or unavailable — never skip silently
   (a REQUIRED artifact that cannot FIT fails assembly — "No silent truncation").
3. Add a test asserting the file is present in the assembled context/prompt.
   That test is the enforcing surface; CHECKLISTS item 11 (`context_building`,
   advisory) backstops the review.

---

