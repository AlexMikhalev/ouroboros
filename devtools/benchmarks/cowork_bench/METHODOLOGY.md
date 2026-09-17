# Cowork Bench methodology

## Protocol and comparison

The dataset, runner and evaluator are pinned to
[`0717376/cowork_bench@d943e75bc0fc8e3b27141979300cd8cbcd1e890d`](https://github.com/0717376/cowork_bench/tree/d943e75bc0fc8e3b27141979300cd8cbcd1e890d).
A full run measures pass@1 over **all 496 tasks**, with denominator 496 even when
some tasks fail or cannot start. A smoke subset is qualification evidence only;
report its selected IDs and count separately. This document defines the setup
and does not claim a completed Ouroboros score.

The pinned upstream README reports Kimi K3 at **363/496 (73.2%)**, using its
`parallel` runner. This campaign does not run a paired reference agent. The
published row does not identify the exact serving provider or quantization, so
a difference against it cannot establish a causal improvement from Ouroboros's
agent loop alone.

Each task uses the official `run_parallel.sh` lifecycle: fresh PostgreSQL state,
an agent container and a separate evaluator phase. The runner script and task
evaluators are unchanged. A run-scoped `BASH_ENV` script changes only Docker
executable discovery so the official script uses the resource wrapper despite
resetting `PATH`. The run manifest records this environment and applied limits.
The task prompt is the benchmark's system prompt plus task text with workspace
substitution; the adapter adds no task-specific answer hints.

## Agent configuration

The accepted campaign configuration is:

| Setting | Value |
|---|---|
| Model | `moonshotai/kimi-k3` in all model and review slots |
| Provider routing | OpenRouter default routing; no provider pin |
| Reasoning | High effort |
| Agent loop | Single agent, no scheduled subagents or external coding delegation |
| Acceptance | Required, blocking, three same-model reviewer slots; two review cycles allow one rework |
| Round bound | 100 Ouroboros rounds; not a promise of identical tool-call counts to other engines |
| Workspace and memory | External task workspace, empty task memory |
| Runtime / safety | `pro`; LLM safety pass `off` in the disposable benchmark environment |
| Native web | Disabled, including native browser/search tools; benchmark-required MCP tools remain available |
| Post-task evolution | Disabled |
| Qualification timeout | 3600 seconds per agent phase; final full-run timeout chosen after smoke |

The acceptance panel is part of the measured agent, not the official scorer.
Safety-off is a benchmark-specific departure from the usual light-mode template;
it avoids adding a separate safety-model request to the mock office operations.
These settings do not alter the user's live installation. The committed model
roster is serialized for provenance; disabled subagent scheduling means that its
presence is not evidence that subagents ran.

The benchmark's MCP tools are exposed with Ouroboros server prefixes. Native
shell/file/context tools perform the roles of the reference engine's local
Python and context helpers; the adapter does not emulate those four helpers as
identically named tools. Document this capability difference when comparing
engines.

## Container and dependency disclosures

Ouroboros starts its ordinary server inside the task container from a clean
committed seed. Its dependency environment is separate from the benchmark's.
`mcp-proxy==0.12.0` with `mcp==1.30.0` holds the task's stdio MCP sessions alive
behind local HTTP endpoints. This preserves presentation and browser state
across Ouroboros calls without changing its core MCP client.

The derived image also repairs reproducibility failures in the pinned upstream
build. Vendored servers which resolve incompatible MCP 2.x are repinned to
`mcp==1.26.0`; `psycopg2-binary==2.9.10` is installed in the two finance-server
environments whose local PostgreSQL shims require it. Chromium is installed at
the revision selected by the vendored Playwright dependency and its presence is
checked during build. When running as root inside the disposable container,
Playwright receives `--no-sandbox`; the outer Docker limits remain in force.
These dependency and launch changes must accompany any result report.

Every container created through the official runner, including database,
evaluator and helper containers, receives a limit of **4 CPUs, 16 GiB memory,
no swap and 512 PIDs**. These are per-container limits, not an aggregate run
quota. Exact run labels scope cleanup. Image building is a separate preparation
step and is not covered by these task-container limits. Resource admission
checks a configurable free-space reserve on the heavy-storage filesystem
(default 200 GiB), and the supervisor also checks the root filesystem (default
40 GiB). Another user's writes can still consume shared storage between checks.

## Spending and run custody

A paid invocation requires a shared campaign file. Its spending is the selected
OpenRouter key's cumulative usage minus one durable baseline, plus any recorded
prior spending. The same file spans qualification, smoke, the full run and
infrastructure retries. Concurrent ownership is locked. A changed key, changed
ceiling, decreased usage counter or unsettled prior run requires reconciliation.
Unrelated spending on the same key counts conservatively toward the campaign.

For the owner-approved example campaign, the requested total ceiling is **$1000**
and an invocation's default spending bound is **$150**. The supervisor preserves
an in-flight allowance equal to the larger of the configured reserve (default
$100) and concurrency times the per-task bound (default $25). It stops when the
campaign remainder reaches that allowance, when the invocation bound is reached,
or when the meter or disk reserve becomes unavailable. These are configurable
operator bounds. Billing can be delayed and paid calls may already be in flight;
the monitor is **not a provider-enforced hard dollar cap**.

Start qualification at concurrency 1; the approved campaign allows at most 2
after measuring resource use. If smoke projects the full dataset above the
remaining campaign budget, pause for an owner decision rather than changing the
model, effort, configuration or budget. Reconcile delayed charges before another
paid phase. Preserve every run in a new directory outside the source and live
runtime data, including aborted runs. No score is inferred from launcher exit 0.

## Outcomes and evidence

A voluntarily completed Ouroboros task maps to the reference engine's `success`;
only the official evaluator decides pass or fail. Runtime round, budget and
deadline termination remain disclosed truncations. Provider/transport failures
and adapter setup failures are infrastructure outcomes. A wall-clock timeout
after model work is a genuine failed attempt, not a new attempt entitlement.
The result ledger retains every selected ID, including `not_attempted` entries.
Infrastructure retries use new roots and the identical configuration and seed;
settled successes and genuine failures are not repeated for best-of selection.
Any final scoring overlay must retain provenance to the original attempts.

Phase-aware mounts omit the task's evaluator and ground-truth workspace from the
agent's task view. Ouroboros settings and provider credentials remain outside the
shared dump directory; the run-local credential file is mode 0600 and is cleared
on launcher completion. Sanitize and inspect artifacts before publication.
Task dumps are shared across the run, and native shell/Python can potentially
access PostgreSQL directly instead of using MCP, as can reference agents. Native
web is disabled because benchmark answers are public, but this is not proof of
complete network isolation or absence of contamination.

The offline audit reports token-bearing usage records, MCP activity, reported
capability omissions, known versus unknown cost, and argument references to
answer sources, evaluator artifacts or direct database clients. Findings contain
log coordinates for manual inspection, never copied answers, and do not change
scores. No findings are not proof of a clean trace. Missing logs and prices remain
unknown. `llm_usage` amounts are compatibility accounting evidence; the campaign
meter is the spending check. A billing provider such as `openrouter` does not
identify the upstream endpoint. `response_provider` observations are reported
only when present, with incomplete coverage disclosed; successful-call endpoint
evidence may be unavailable in the copied logs.

A result report therefore needs the exact seed and image/benchmark pins, selected
IDs, applied settings, official evaluator outputs, complete denominator,
infrastructure and truncation disclosures, audit disposition, measured cost and
duration, and these protocol differences. Inventory or build success alone does
not demonstrate an end-to-end benchmark result.
