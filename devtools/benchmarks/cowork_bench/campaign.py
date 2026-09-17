"""Provider-metered campaign budget shared by every Cowork invocation.

This is an operator spending bound, not a model-price table. The same provider key's
lifetime usage is compared with one durable baseline across smoke and recovery runs.
Other spending on that key counts conservatively; a changed key or reset counter needs
explicit reconciliation. The caller holds the existing cross-platform file lock for
the whole run, so two launchers cannot independently spend the same remaining budget.
"""

from __future__ import annotations

import json
import math
import pathlib
import time
import urllib.request
from contextlib import contextmanager
from typing import Iterator

from devtools.benchmarks.common.manifests import write_json


def key_usage(api_key: str) -> float:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    value = float(payload["data"]["usage"])
    if not math.isfinite(value) or value < 0:
        raise ValueError("provider returned an invalid usage counter")
    return value


class CampaignBudget:
    """A locked, monotonic account of the owner's campaign spending limit."""

    def __init__(self, path: pathlib.Path, *, fingerprint: str, ceiling: float,
                 usage: float, prior_spend: float = 0.0) -> None:
        self.path = path
        if not all(math.isfinite(x) for x in (ceiling, usage, prior_spend)):
            raise ValueError("campaign amounts must be finite")
        if ceiling <= 0 or usage < 0 or prior_spend < 0:
            raise ValueError("campaign ceiling must be positive; usage cannot be negative")
        if path.exists():
            self.record = json.loads(path.read_text(encoding="utf-8"))
            if self.record["fingerprint"] != fingerprint or self.record["ceiling_usd"] != ceiling:
                raise ValueError("campaign key/ceiling changed; reconcile the existing campaign first")
            if prior_spend:
                raise ValueError("prior spending can only be set when creating a campaign")
            if self.record.get("active_run"):
                raise ValueError("previous campaign run has unsettled custody; inspect its processes and containers")
        else:
            self.record = {
                "schema": "ouroboros.cowork_bench.campaign.v1",
                "fingerprint": fingerprint, "ceiling_usd": ceiling,
                "usage_baseline": usage, "prior_spend_usd": prior_spend,
                "last_usage": usage, "created_at": time.time(), "runs": [],
            }
        self.observe(usage)

    @property
    def spent(self) -> float:
        return self.record["prior_spend_usd"] + self.record["last_usage"] - self.record["usage_baseline"]

    @property
    def remaining(self) -> float:
        return self.record["ceiling_usd"] - self.spent

    def save(self) -> None:
        self.record.update({"spent_usd": self.spent, "remaining_usd": self.remaining,
                            "observed_at": time.time()})
        write_json(self.path, self.record)

    def observe(self, usage: float) -> None:
        if not math.isfinite(usage) or usage < self.record["last_usage"]:
            raise ValueError("provider usage counter decreased or is invalid; spending is unknown")
        self.record["last_usage"] = usage
        self.save()

    def start(self, run_root: pathlib.Path) -> None:
        self.record["active_run"] = str(run_root)
        self.save()

    def finish(self, run_root: pathlib.Path, *, outcome: str, meter_error: str = "") -> None:
        self.record["runs"].append({"run_root": str(run_root), "outcome": outcome,
                                    "spent_usd": self.spent, "meter_error": meter_error,
                                    "finished_at": time.time()})
        self.record.pop("active_run", None)
        self.save()


@contextmanager
def campaign_lock(path: pathlib.Path) -> Iterator[None]:
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = acquire_exclusive_file_lock(lock_path, timeout_sec=0.2, owner_aware_stale=True)
    if fd is None:
        raise RuntimeError("another launcher owns this campaign budget")
    try:
        yield
    finally:
        release_exclusive_file_lock(lock_path, fd)
