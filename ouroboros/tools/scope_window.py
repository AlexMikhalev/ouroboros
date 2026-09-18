"""Scope-reviewer WINDOW sizing: evidence-typed resolution + honest wording (RS5).

Window size is not a condition of authority (owner decision 2026-09-17, BIBLE
P3): a scope verdict rests on the reviewer's independence, the declared
required-source manifest and its recorded coverage. What this module answers is
the one remaining sizing question — how large an output reserve the row's
request may ask for — plus the provenance wording every diagnostic quotes.
``scope_review`` re-imports these names under their private aliases, so its
tests and callers keep exactly one patch point.
"""

from __future__ import annotations

from ouroboros.config import review_model_uses_local
from ouroboros.provider_models import provider_for_model
from ouroboros.reviewer_window import (
    REVIEWER_FULL_WINDOW,
    ReviewerWindow,
    resolve_reviewer_window as _resolve_reviewer_window,
)

# The conservative sizing fallback for a route with no Capability Evidence:
# SIZING only, never an authority floor. `scope_review` imports it back rather
# than defining a second copy.
SCOPE_SIZING_FALLBACK_WINDOW = 200_000
SCOPE_MODEL_DEFAULT = "openai/gpt-5.6-terra"

# Window provenance vocabulary shared with the diagnostics wording (RS5).
WINDOW_CONFIRMED = "confirmed"
WINDOW_ASSERTED = "asserted"
WINDOW_STALE = "stale_unverifiable"
WINDOW_UNKNOWN = "unknown_conservative"
WINDOW_SENTINEL = "designated_default_sentinel"

# The metadata probe lives with the shared window resolver (`reviewer_window`) and is
# rate-limited by the TTL on the evidence record, keyed by the full ROUTE fingerprint
# rather than the model name: capability is a property of provider+base_url+model, and
# a hot base-URL change must get its own probe rather than silently reusing the old
# verdict. EVERY scope route gets that probe, the shipped default included, so the
# sizing number is a measurement wherever one is reachable; rate-limiting it per
# PROCESS instead of per TTL left an install that outlived the TTL unable to
# RE-source it (v6.87.45).


def is_designated_default_reviewer(model: str) -> bool:
    """True iff ``model`` is the shipped default reviewer (``openai/gpt-5.6-terra``),
    across provider spellings (``openai::gpt-5.6-terra``, ``openrouter::openai/...``).

    SIZING only. This answers "how large a reserve may I ask of an unevidenced
    route", never "may this reviewer block a commit": authority rests on the
    required-source manifest and its recorded coverage, and no window number —
    nor a model name — grants or removes it."""
    def _normalized(m: str) -> str:
        text = str(m or "").strip()
        if text.startswith("openrouter::"):
            text = text[len("openrouter::"):]
        try:
            from ouroboros.provider_models import normalize_model_identity
            return normalize_model_identity(text)
        except Exception:
            return text
    return bool(model) and _normalized(model) == _normalized(SCOPE_MODEL_DEFAULT)


def scope_window(model: str, *, session: bool = False, model_role: str = "",
                 credential_profile_id: str | None = None, model_route: dict | None = None,
                 use_local: bool | None = None) -> ReviewerWindow:
    """The scope reviewer's window as ONE typed, provenance-carrying result.

    Replaces the deleted static per-model window table: a confirmed/asserted probe
    (provider metadata or owner-ack) for the reviewer's REAL active route gives the
    real window.

    With NO evidence the result still carries a SIZING number so the review is
    dispatched rather than declined before it starts — the full-window figure for the
    shipped designated reviewer, a conservative fallback for anything else, matching
    what each can plausibly hold. Neither carries a KNOWN status, and the record says
    so: that is why this returns the whole record instead of a bare int — a number
    alone cannot say where it came from, and the caller that needs to know then
    guesses.

    Every route gets one lazy metadata-only fetch per evidence-TTL period (never
    generative, never a paid call), concurrent resolutions of the same route
    serialized by the per-route lock; inside the TTL the cache answers and the path
    stays hot-path safe. How often the network is re-asked is owned by
    ``capability_evidence.probe``'s record TTL, deliberately NOT by the process
    lifetime (v6.87.45: a per-process memo outlived the 24h record and wedged
    every commit once an install stayed up past the TTL)."""
    model = str(model or "")
    try:
        # Probe the scope slot, not the active main route (which honors USE_LOCAL_MAIN).
        # ``session`` is the ROW's configured delivery: a retrieving row's target is a
        # harness route spec, so it fingerprints under its own provider and the local
        # predicate does not apply to it (see `reviewer_window.reviewer_route`).
        resolved = _resolve_reviewer_window(
            model,
            use_local=None if session else review_model_uses_local(model) if use_local is None else use_local,
            session=session,
            **({"model_role": model_role, "credential_profile_id": credential_profile_id,
                "model_route": model_route} if model_role or credential_profile_id is not None or model_route else {}),
        )
        if int(resolved.window_tokens) > 0 or provider_for_model(model) == "claudexor":
            return resolved
    except Exception:
        pass
    if provider_for_model(model) == "claudexor":
        return ReviewerWindow(model=model)
    return ReviewerWindow(
        window_tokens=(
            REVIEWER_FULL_WINDOW if is_designated_default_reviewer(model)
            else SCOPE_SIZING_FALLBACK_WINDOW
        ),
        model=model,
    )


def scope_window_provenance(window: ReviewerWindow) -> str:
    """Provenance label for the diagnostics wording (RS5)."""
    if window.stale:
        return WINDOW_STALE
    if window.status == "asserted":
        return WINDOW_ASSERTED
    if window.status == "confirmed":
        return WINDOW_CONFIRMED
    if int(window.window_tokens) >= REVIEWER_FULL_WINDOW:
        return WINDOW_SENTINEL
    return WINDOW_UNKNOWN


def window_provenance_phrase(window: int, provenance: str, observed_at: str = "") -> str:
    """Honest five-way wording for a reviewer window (RS5).

    The old single phrasing called every sub-1M window "known", which read as a
    measured fact even when it was the conservative fallback for an unprobed route;
    the stale wording exists because an expired record used to be indistinguishable
    from a live reading in every message the owner ever saw. ``observed_at`` dates the
    expired reading, which is the difference between "the provider blipped an hour ago"
    and "this route has been dead for a week" — the only two situations that produce
    identical wording otherwise (BIBLE P1, provenance)."""
    if provenance == WINDOW_CONFIRMED:
        return f"confirmed {window}-token window"
    if provenance == WINDOW_ASSERTED:
        return f"owner-asserted {window}-token window"
    if provenance == WINDOW_STALE:
        dated = f", last confirmed {observed_at}" if observed_at else ""
        return f"EXPIRED, unverifiable {window}-token window{dated}"
    if provenance == WINDOW_SENTINEL:
        return f"designated-default {window}-token sentinel window"
    return (f"unknown window, conservatively treated as {window} tokens"
            if window else "unknown window")
