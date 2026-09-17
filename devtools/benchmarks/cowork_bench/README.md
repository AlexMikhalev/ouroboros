# Cowork Bench adapter

Run a clean, pinned Ouroboros seed inside the task containers of
[`0717376/cowork_bench`](https://github.com/0717376/cowork_bench/tree/d943e75bc0fc8e3b27141979300cd8cbcd1e890d).
The upstream runner and evaluator remain unchanged. The adapter adds the
Ouroboros entrypoint, persistent MCP sessions, run manifests, resource limits,
and a shared campaign spending record. Read [METHODOLOGY.md](METHODOLOGY.md)
before interpreting a result.

## Prepare once

Use Linux, Bash, Docker with working cgroup limits, and the repository's Python
environment. Select an existing rootless Docker daemon explicitly. Check other
workloads and available storage before building; keep the benchmark checkout,
images and expensive downloads in durable storage. Do not prune a shared daemon.

The seed must contain the committed adapter and have an empty
`git status --porcelain`. Create a detached worktree at the reviewed commit;
`--allow-dirty-seed` is for development diagnostics, not a reportable run.
Substitute your own paths and rootless socket below. Supply
`OPENROUTER_API_KEY` through your existing secret environment, never a command
argument or a checked-in file.

```bash
export DOCKER_HOST=unix:///run/user/UID/docker.sock
COWORK_SEED=/path/to/clean-ouroboros-seed
COWORK_BENCH=/path/to/cowork_bench
COWORK_STORAGE=/path/to/bench-storage
COWORK_CAMPAIGN="$COWORK_STORAGE/campaign.json"
```

Reuse an existing benchmark checkout at the pinned commit, or clone it once:

```bash
git clone https://github.com/0717376/cowork_bench.git "$COWORK_BENCH"
git -C "$COWORK_BENCH" checkout --detach d943e75bc0fc8e3b27141979300cd8cbcd1e890d
```

The official image and PostgreSQL image must already be present before a paid
run; task creation uses `--pull=never`. Build the base once on the selected
daemon, and preserve the images in your durable cache:

```bash
docker build -t cowork-pack:d943e75 "$COWORK_BENCH"
docker pull postgres:15
```

## Qualify, then measure

Start with one task and concurrency 1. Choose the task IDs from the pinned
benchmark, then expand to a representative smoke list only after the tool
inventory and first task work. A smoke subset is not the 496-task benchmark
score. The campaign described in the methodology permits concurrency 2 only
after measuring the first task's resource use.

Create the storage directory first. Every invocation needs a **new** run root,
including dry runs and infrastructure retries. The following dry run builds the
derived image and records the configuration without calling a model:

```bash
COWORK_RUN="$COWORK_STORAGE/qualification-$(date +%s)"
python3 "$COWORK_SEED/devtools/benchmarks/cowork_bench/run_cowork_bench.py" \
  --repo-dir "$COWORK_SEED" --bench-root "$COWORK_BENCH" \
  --docker-host "$DOCKER_HOST" --resource-root "$COWORK_STORAGE" \
  --run-root "$COWORK_RUN" --task TASK_ID --concurrency 1 \
  --task-timeout 3600 --build-image --dry-run
```

For the first paid qualification, use a new root, omit `--dry-run`, and add the
shared campaign record and the agreed spending limits. The following $1000
campaign is an example of the owner-approved campaign, not a universal spending
policy:

```bash
COWORK_RUN="$COWORK_STORAGE/smoke-$(date +%s)"
python3 "$COWORK_SEED/devtools/benchmarks/cowork_bench/run_cowork_bench.py" \
  --repo-dir "$COWORK_SEED" --bench-root "$COWORK_BENCH" \
  --docker-host "$DOCKER_HOST" --resource-root "$COWORK_STORAGE" \
  --run-root "$COWORK_RUN" --task TASK_ID --concurrency 1 \
  --task-timeout 3600 --campaign-file "$COWORK_CAMPAIGN" \
  --campaign-budget-usd 1000 --budget-usd 150 --budget-reserve-usd 100
```

Use `--task-file` for a newline-separated selection. Without `--resume-from`,
omitting task selectors requests the full pinned dataset; recovery preserves the
original selection when selectors are omitted. Choose the full-run timeout and concurrency
from smoke evidence before spending on that run. No provider pin is required.
Reuse the **same campaign file and key** for all paid phases. If earlier probes
predate the first campaign baseline, account for them once with
`--prior-spend-usd` when creating the campaign.

## Monitor, recover and audit

`monitor.json` records progress, key-meter spending, disk headroom and stop
reasons. `run_manifest.json` records the seed, benchmark, immutable image ID,
selected IDs, recovery ancestry and applied configuration; `result_index.jsonl` retains every selected task,
including failures and tasks not started. Task artifacts live below
`bench/dumps/`, with sanitized runtime logs in each task's `ouroboros/` folder.
A launcher exit code is not a task score.

Send SIGINT or SIGTERM to the launcher to stop its runner and clean up resources
with that run's exact Docker label. Never use broad container-name cleanup on a
shared daemon. The launcher refuses a campaign with an unsettled `active_run`.
For manual reconciliation after an ungraceful exit, first verify that the exact
launcher/runner processes have exited and no containers or networks remain with
that run's recorded label. Refresh the selected key's usage and retain an
operator record of the checks. Only then clear the stale `active_run` field;
preserve the campaign file, fingerprint, ceiling, usage baseline, prior spending,
last usage and run history. Never create a replacement baseline to resume.

For infrastructure recovery, add `--resume-from OLD_RUN_ROOT` to a new run with
the same seed, immutable image and configuration. The full recovery ancestry is
checked: settled results, including genuine failures in earlier ancestors, are
skipped. Without new explicit selectors, recovery retains the original task
selection; at most two recovery passes with remaining work are permitted. Missing manifests or ledgers require investigation, not an
assumption that a task was never attempted. Preserve old directories and combine compatible result records only
when scoring; never overwrite or mix configurations to improve a score.

```bash
python3 "$COWORK_SEED/devtools/benchmarks/cowork_bench/audit_cowork_bench.py" \
  --run-dir "$COWORK_RUN" --output "$COWORK_RUN/audit.json"
```

The offline audit reports evidence and references requiring manual review; it
never changes the official verdict. It refuses to overwrite an existing report.
Missing logs, unknown cost and unavailable upstream-provider evidence remain
explicit. Full-campaign claims require the scored artifacts and disclosures
specified in the methodology, not successful installation or inventory checks.
