"""The wake-up message and the wake's task metadata (Background Consciousness redesign).

A wake-up is an ordinary Main turn nobody typed: ``prompts/CONSCIOUSNESS.md`` is its USER
message (system prompt, memory and tools are Main's own, owner decision В15).
``render_wake_message`` fills its placeholders from existing readers (tasks settled since the last
wake, open owner quiz cards, the count of owner messages) and truncates the event list with an
explicit ``(+N more; see recent_tasks)`` line, never silently (BIBLE P1). ``wake_task_metadata`` is
the wake's origin/authority envelope for ``handle_wake_direct`` (``consciousness_authority``).
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import re
from typing import Any, Dict, List, Optional

from ouroboros.consciousness_authority import (
    CONSCIOUSNESS_CATEGORY, CONSCIOUSNESS_INITIATOR, disabled_tools_for, normalize_level, runtime_mode_cap_for,
)
from ouroboros.context_health import safe_read
from ouroboros.utils import iter_jsonl_objects

PROMPT_REL = pathlib.Path("prompts") / "CONSCIOUSNESS.md"
EVENT_LINES_MAX = 10
CARD_LINES_MAX = 4  # unanswered cards never crowd out what settled since the last wake
CHAT_TAIL_BYTES = 512_000
PLACEHOLDERS = ("reason", "last_wake_ago", "events", "level", "level_line", "withheld_tools",
                "spent_usd", "daily_usd", "running", "max_tasks", "interval")
LEVEL_LINES = {
    "observe": "think, keep memory/knowledge, write to your human; no tasks, no changes in the world",
    "act": "everything your runtime mode allows except editing your own code/prompts, evolution, restart and settings",
    "full": "everything your runtime mode allows, including evolution",
}
_FALLBACK_TEMPLATE = "[Wake-up · {reason}] No one wrote to you: this turn is yours. Since your last wake ({last_wake_ago}): {events}"


def wake_task_metadata(level: Any, reason: str, *, root_cost_ceiling_usd: Optional[float] = None) -> Dict[str, Any]:
    """The wake's ``task_metadata``: origin label, ledger category, level and its consequences.

    ``root_cost_ceiling_usd`` is the wake tree's GRACEFUL ceiling (what is left of the
    allowance, at most the per-task cap): ``task_pacing.resolve_cost_ceiling`` honors it for
    the root itself and the members inherit the resolved number, while the ledger fence stays
    at the owner's per-task cap. Only a strictly positive ceiling is stamped — a wake with
    nothing left of its allowance is skipped by the alarm, never started under a $0 ceiling.
    """
    normalized = normalize_level(level)
    metadata: Dict[str, Any] = {
        "initiator": CONSCIOUSNESS_INITIATOR, "usage_category": CONSCIOUSNESS_CATEGORY,
        "wake_reason": str(reason or "heartbeat"), "consciousness_autonomy": normalized,
        "model_role": "consciousness", "disabled_tools": disabled_tools_for(normalized),
        "runtime_mode_cap": runtime_mode_cap_for(normalized),
    }
    if root_cost_ceiling_usd is not None and float(root_cost_ceiling_usd) > 0:
        metadata["root_cost_ceiling_usd"] = float(root_cost_ceiling_usd)
    return metadata


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).isoformat()


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    return f"{seconds // 3600} h {(seconds % 3600) // 60} min ago"


def wake_events(drive_root: Any, *, since: float, now: float, exclude_task_id: str = "") -> List[str]:
    """One line per fact since ``since``: settled tasks (never direct chat turns), unanswered
    owner cards of ANY task — a wake's own included, and a card its task has already left
    behind (``expired_terminal``) still takes a late answer (В17a) — and the count of owner
    messages; readers fail soft (a gap line, never a crash)."""
    from ouroboros.owner_quiz import STATE_EXPIRED_TERMINAL, STATE_OPEN
    from ouroboros.task_results import list_task_results
    from ouroboros.task_status import SETTLED_STATUSES

    root, since_iso, lines = pathlib.Path(drive_root), _iso(since), []
    try:
        rows = list_task_results(root)
    except Exception as exc:  # a disclosed gap beats a missing wake
        rows, lines = [], [f"- task_results unreadable: {type(exc).__name__}"]
    cards, settled = [], []  # (stamp, line) pairs; both classes are listed newest first
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        quizzes = row.get("owner_quiz") if isinstance(row.get("owner_quiz"), dict) else {}
        for quiz_id, block in quizzes.items():
            if not isinstance(block, dict) or block.get("answered_at"):
                continue
            if block.get("state") in (STATE_OPEN, STATE_EXPIRED_TERMINAL):
                cards.append((str(block.get("asked_at") or ""),
                              f"- open question card {quiz_id} on task {task_id} (no answer yet)"))
        if task_id == exclude_task_id or row.get("_is_direct_chat"):
            continue
        status, stamp = str(row.get("status") or ""), str(row.get("updated_at") or row.get("ts") or "")
        if status in SETTLED_STATUSES and stamp >= since_iso:
            cost = row.get("accounted_upper_bound_usd", row.get("cost_usd"))
            cost_text = f", ${float(cost):.2f}" if isinstance(cost, (int, float)) else ""
            title = str(row.get("description") or row.get("text") or row.get("result") or "")[:80]
            settled.append((stamp, f"- task {task_id} {status}{cost_text}: {title}".rstrip(": ")))
    # The newest unanswered cards first (a bounded share, so a backlog of old cards never
    # starves the settled lines), then what settled — newest first, so the honest
    # truncation below drops the oldest facts, never the ones that just happened.
    lines += [line for _stamp, line in sorted(cards, reverse=True)[:CARD_LINES_MAX]]
    lines += [line for _stamp, line in sorted(settled, reverse=True)]
    owner_messages = 0
    try:
        for entry in iter_jsonl_objects(root / "logs" / "chat.jsonl", tail_bytes=CHAT_TAIL_BYTES):
            if entry.get("direction") == "in" and str(entry.get("ts") or "") >= since_iso:
                owner_messages += 1
    except Exception:
        lines.append("- chat log unreadable")
    if owner_messages:
        lines.append(f"- {owner_messages} message(s) from your human (see Recent chat)")
    return lines


def render_wake_message(drive_root: Any, repo_dir: Any, *, reason: str, last_wake_at: float, since: float,
                        now: float, level: Any, disabled_tools: List[str], spent_usd: Any, daily_usd: Any,
                        running: int, max_tasks: int, interval: int, exclude_task_id: str = "",
                        spent_is_floor: bool = False) -> str:
    """Fill ``prompts/CONSCIOUSNESS.md`` for one wake; every placeholder is substituted."""
    template = safe_read(pathlib.Path(repo_dir) / PROMPT_REL) or _FALLBACK_TEMPLATE
    events = wake_events(drive_root, since=since, now=now, exclude_task_id=exclude_task_id)
    omitted = max(0, len(events) - EVENT_LINES_MAX)
    shown = events[:EVENT_LINES_MAX] + ([f"(+{omitted} more; see recent_tasks)"] if omitted else [])
    normalized = normalize_level(level)
    spent = f"{float(spent_usd):.2f}" if isinstance(spent_usd, (int, float)) else "unknown"
    if spent_is_floor and spent != "unknown":
        spent = f"at least {spent}"  # unmetered rows in the window: the number is a floor
    facts = {
        "reason": str(reason or "heartbeat"),
        "last_wake_ago": _ago(now - last_wake_at) if last_wake_at else "no wake since this process started",
        "events": ("\n" + "\n".join(shown)) if shown else "nothing new",
        "level": normalized, "level_line": LEVEL_LINES[normalized],
        "withheld_tools": ", ".join(disabled_tools) if disabled_tools else "none",
        "spent_usd": spent, "daily_usd": f"{float(daily_usd):.2f}",
        "running": str(int(running)), "max_tasks": str(int(max_tasks)), "interval": str(int(interval)),
    }
    # One pass: a fact (a task title inside {events}) that happens to contain "{daily_usd}"
    # is never substituted again.
    return re.sub(r"\{(\w+)\}", lambda m: facts.get(m.group(1), m.group(0)), template)
