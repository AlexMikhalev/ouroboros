# Process Custody Rule

This chapter owns the spawn chokepoint every long-lived child process must pass and its custody scopes, the single worker tree-kill seam, the rules a change to the daemon stop must keep, and the harvest cadence and disclosed residuals of the failed-start latch. It exists because an unledgered process survives the death of its owner invisibly, and because a command-line-class match would let one instance reap another's children.

Long-lived OS processes (anything `subprocess.Popen`-ed or `mp.Process`-ed
without a bounded wait in the same call) MUST be spawned through
`ouroboros.process_custody.spawn_supervised(cmd, drive_root=..., purpose=...,
scope=...)` — or, when an existing manager owns the Popen call, registered
via `record_process(...)` write-through immediately after spawn. An unledgered process may evade durable generation-aware reaping (the ledger,
the reaper's strict fingerprint and the skill-companion exception:
`docs/architecture/01-high-level-architecture.md` § "Runtime topology"). Scopes
are `task`, `session`, and `daemon`. Never add command-line-class matching to
the reaper — it would let a dev instance reap a packaged instance's processes. `tests/test_process_custody.py` enforces the
chokepoint with an explicit allowlist for bounded synchronous helpers.
An installation-owned daemon uses `daemon` scope, with legacy purpose retention
provided by its lifecycle owner and checked against the existing ledger identity;
server-generation changes alone must not kill it. A worker's process tree is
killed only through `supervisor.worker_pool_lifecycle.kill_worker_tree` — pool
shutdown and restart, the managed-update fence, unready-slot replacement, cancel
and timeout custody alike — which spares the ledger's live `daemon`-scope roots
(`process_custody.live_daemon_root_pids`, including the owner's retained legacy
purposes) and, for one task's cancel or timeout only, the kept services; a direct `kill_pid_tree` on a worker anywhere else in
`supervisor/` is a defect. The explicit stop (`OwnedClaudexorDaemon.stop_outcome`, used by Panic and
Restart) separates authenticated cooperative shutdown from forced signalling;
its protocol — what proves an exit, what forced fallback may signal, what never
grants that authority — is
`docs/architecture/09-shutdown-and-process-cleanup.md`. The rules a change must
keep: read-only resolution must not ensure or install a runtime, start a
daemon, probe accounts or inherit another home's socket override; pure operator
commands select the existing exact Node with
`resolve_cli_command(require_npm=False)` so ordinary stop works on Windows and
bundled-only installs; RPC acknowledgement and lease release alone are never
physical exit proof; a matching error-code string alone gives no right to
signal; manager-lock acquisition stays bounded with preparation/network/exit
waits outside it, and Stop retires delayed spawns; the operator CLI and
captured-exit bounds are `config.CLAUDEXOR_OPERATOR_STOP_TIMEOUT_SEC` and
`config.CLAUDEXOR_STOP_EXIT_WAIT_SEC`, defined once in runtime_limits. Join
existing purpose-filtered startup custody before and after runtime preparation;
caller wait expiry never kills it or replaces engine writer election. Keep
startup and normal admission waits independent, identify current PID/build/log
interval rather than an old log tail, and preserve existing malformed/foreign
ownership markers. Publish a missing marker atomically only after revalidating
the home under the shared JSON lock.

A failed owned-daemon start latches on the TYPED exit fact only
(`ExitFact.failed_without_control`), and `claudexor_startup_failure.py`
classifies the child's own log interval for the diagnostic label and the one
supervisor row, never for behaviour (BIBLE P5); the classes, the rows and the
releases of the latch are ARCHITECTURE §9. Harvest the exit fact at every spawn decision, at attach and at stop
(`_settle_exited_child`), never only on a caller's wait expiry: with the real
crash cadence (V8 dies after the 20 s startup window) the waiting caller gets
`daemon_starting` and the child dies with nobody waiting, and the next caller,
attach or owner stop must still record the row and the latch. Take the latch
under the same lock as the reap, before reading the log, so a concurrent
same-process caller meets the latch, not a free spawn slot; `_spawn` re-checks
the latch under its own lock and never replaces an exited, unsettled child (the
next settle owns that exit fact). Two residuals are
disclosed, not closed: the descriptor identity is sampled at the first
observation of the exit, not at the exit itself, so a foreign publisher in
that window reads as written and costs at most one extra spawn; and between
the sweep's release and its retry an ordinary caller can pass the refusal and
become the spawner, in which case the retry joins that same live child (one
spawn either way, only who pays the startup wait differs). Do not add a
backoff machine, a retry counter, a cooldown constant, host-side heap sizing
or writer-lease handling, and do not add a third retrier: the periodic
sweep, the owner's explicit Refresh, a live attach, or a new manager (a task
worker's manager is its own instance with its own latch) are the only releases
(each named in ARCHITECTURE §9), and ordinary callers never make the retry; `NODE_OPTIONS` passthrough is the operator escape
hatch (ARCHITECTURE §9).

Ordinary close preserves the shared daemon on every platform, including forced
worker/server/stray cleanup. Exclusions protect the whole subtree, not merely a
ledger row. The launcher Job allows explicit breakaway only when the daemon asks;
ordinary generation children remain covered. Do not apply this lifetime to all
skill companions or broaden reserved-port/service ancestry rules. Windows birth,
command, Job and explicit prior-generation stop require native Windows evidence;
psutil is a main Windows-only dependency, and both embedded and frozen imports
need packaging proof. A portable fixture does not establish those claims. An old
immutable launcher keeps its disclosed limitation until its package is updated.
Platform-lock/ABI/fingerprint rationale is specified once in ARCHITECTURE §1
"Platform substrate"; code keeps the local invariant and that pointer.

Explicit stop and next-start runtime selection remain separate contracts: a
newer engine pin is never hot-swapped and the daemon's next start selects it; a
planned restart whose landed checkout pins another engine version or build ends
the serving daemon in the lifespan teardown through
`server_restart._stop_owned_daemon_for_new_pin` (`load_runtime_pin` from the
checkout against `read_owned_gateway`, then the shared attested stop — never a
veto), while an unchanged, unreadable or unpublished pin and an unreachable daemon
leave the handoff untouched (`tests/test_planned_restart_engine_pin.py`). Service quiescence excludes
zombie-only groups, but checks every member before releasing a writer fence
(`tests/test_claudexor_custody_lifetime.py`, `tests/test_process_custody_liveness.py`).
The owner's manual Restart keeps its checkout-first order: the update gate and
`safe_restart` refuse before anything is stopped, and `server_restart._stop_owned_work`
runs only after the durable no-resume flags. Reuse the `request_cancel` ingress,
`kill_workers` with `reconcile_delegate_custody=False`, `reconcile_orphaned_runs` over
`read_owned_gateway`, and the typed `OwnedClaudexorDaemon.stop_outcome`; never call
`ensure_owned_gateway` between the cancel intents and the daemon stop, and never read
the manager's private error state to tell "nothing to stop" from "unconfirmed".
Past the gate an unconfirmed step is a critical diagnostic with custody retained,
never a deferral or a veto — do not add a "deferred" state, a drain, a lease, a
census or a new custody event kind for it; the next generation's startup sweep is
the recovery. The lifespan warmup is one background `ensure_owned_gateway` for a
provisioned home and needs no invalidation of its own: a Stop retires it through the
start generation (`tests/test_manual_restart_execution.py`).

Out-of-process extension HTTP responses execute their standard Starlette ASGI
response in the child. The runner owns staging, Popen registration and cleanup;
`extension_route_stream` owns only portable pipe frames and ASGI delivery. Preserve
ordered headers, HEAD/Range and background actions. Consumer backpressure is not
an idle failure and no total/pre-header response deadline applies. Bind cancellation
to the existing loaded bundle, before spawning, and detach on completion. A
cancellation during startup retains the worker future and process context until
it exits; move context entry/exit, spawn registration, pipe shutdown and termination
off the ASGI event loop. Static captured module sources spawn no child. Chunk size
and post-response cleanup grace come from the runtime-limits owner via config.py
and are not stream deadlines. Bound each frame before reading its payload, not
the cumulative response; retain sanitized bounded stderr and the actual exit
code after draining a child that dies abnormally.
Failed final sends remain delivery failures; background failure after a successful final
body is a separate diagnostic. Widget pull credits bound transport buffering;
large URL downloads use the existing native/browser file owner, never an automatic
HTTP-stream-to-Blob conversion.

## Android platform development

Android keeps the common source/update/review authority. Its runtime layout,
launcher/seed distinction, native artifact receipt and lifecycle owners live in
ARCHITECTURE "Android host (experimental)"; the user procedure is
`docs/ANDROID_INSTALL.md`. Keep experiments described as rooted ARM64 Android,
physically tested on Pixel 10a only. Do not turn that tested model into a runtime
allowlist or treat the manifest's minimum SDK as a proven support matrix.

Native changes use ordinary Android source, reviewed commits and the persistent
per-install signing identity. Never copy the publisher private key, runtime
credentials, memory or a live rootfs into release assets. Verify official bytes
before trusting their source; distinguish a publisher-signed reference APK from
the locally built installed APK, and record each artifact's own identity. Restore
the original personal key after loss; do not silently generate another identity
for an installed package. Preserve source/data/key on installer retries and use
the existing managed Git merge to retain local evolution during official updates.

Dependency changes must reach the same source-selected hook: `ensure_platform`
serves first installation and later updates, with package, Java SDK, native AAPT,
common Node and browser input groups in the existing SDK receipt. Include tracked
recipes and patches; retain `platform_preparing` until completion so interruption
and Git rollback cannot reuse a partially changed SDK as current. Verify installed
output hashes and desired-source stability before native success. Use the existing
verified download cache, Node manager and Playwright installer; do not start a
second daemon. A legacy receipt is prepared once; Java SDK-only changes skip AAPT
compilation. The common `EXTERNAL_PLATFORM_UPDATE_TIMEOUT_SEC` constant is 3600
seconds in `runtime_limits.py`, re-exported by `config.py`, with the existing
ProcessContainer cleanup. Start the HTTP readiness clock only after server spawn, not while native preparation runs. Propagate launcher shutdown into the hook and await its owned cleanup before exiting, including a later generation. Preparation completion is not HTTP or native-success evidence. This bound is not an environment setting or a harness deadline.
The Ubuntu Base archive remains initial seed provenance. Same-Noble apt recipes
can evolve, with actual installed package versions recorded; Git rollback does
not promise package removal/downgrade or a major distribution migration.

Exercise actual source → dependency preparation → build → install → readback → restart behavior before
claiming native adoption. Cover failed native builds alongside a still-usable
core, older immutable seed with newer source, local APK modification followed by
an upstream source merge, bootstrap-only changes, rollback to older native source
with a newer versionCode, bridge loss/recovery, and Panic versus automatic entry.
Core HTTP health is separate from native artifact and bridge readiness; checks
must use their existing owners and preserve incomplete outcomes.

Each Available-subagents `agent_session` row may select `access=full` for its
mutating assignments; omitted access stays `workspace_write` on every platform.
API-model rows cannot request full. Preserve the choice in the existing canonical
configuration fingerprint and immutable task snapshot, and consume that snapshot
at dispatch preflight and fresh start. Omitted defaults must preserve existing
fingerprints. Both profiles use the same snapshot, retry, capture and
persistent-registration owners.
Only a fresh full request can create an absent Claudexor trust grant for its stable
project root through the existing API; an explicit false grant remains a refusal.
Retries use their stored access and exact request, without granting or widening.
Full means broader native process powers, not a different assignment or implicit
patch integration. Read-only tasks retain Ask/readonly; an Android Codex sandbox
failure must not be hidden as success or an automatic full-access retry.

On Android, `enter-linux` restores ordinary OOM selection for its own process and
descendants without removing root. The existing
`OUROBOROS_PREFLIGHT_TEST_WORKERS` operator lever defaults to 2 and
`OUROBOROS_PREFLIGHT_TIMEOUT_SEC` to 3600 seconds at this entry, preserving explicit
overrides. Other installs retain the upstream 1800-second total test budget.
Standalone preflight/advisory ToolEntry bounds add that resolved test total to the
existing plan-style task/transport settlement envelope and finalization grace;
they must not expire before tests and the critic can settle. This outer bound
creates no new cognitive deadline; inner critic/owner deadlines, test containment
and the reviewed commit's terminal wait remain unchanged. These settings change
test concurrency/time, not test content, review models or context. Measure memory/swap and confirm process cleanup before
running full preflight on a phone; do not deliberately reproduce a kernel panic.
Keep reusable large downloads in the installer's durable cache.

`android-test` explicitly collects `android/tests`; ordinary `pytest tests/` does
not cover that directory. Portable source/transport fixtures and host compilation
are separate from physical root, boot, permissions, hardware and battery evidence.
The same-key instrumentation under `android/tests/device` owns a temporary SDK
bridge and an uncommitted PackageInstaller session. The emulator job runs for Android source changes and tags, and executes
session readback on API 26/29/30/33/36 and accepts its explicit PASS only after
abandon and bridge cleanup. It requires neither root nor a provisioned Linux
core; it does not certify the phone bootstrap or owner consent UI.
Android release source/APK SBOMs describe those shipped bytes; installed dependency
pins/package inventories describe the provisioned phone. Neither invents the other.
The trusted tag-only `android-build` job reads `ANDROID_KEYSTORE_BASE64`,
`ANDROID_KEYSTORE_PASSWORD`, and `ANDROID_KEY_ALIAS` from repository secrets;
the branch/PR Android jobs use a disposable key and never publish it. A PR is
therefore source/build evidence, not a publisher-signed release claim.

