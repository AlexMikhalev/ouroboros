"""Reviewed companions attach as meeting peers under a call-scoped direction grant.

A skill token never admits an owner turn. Only Presence's binding admission may
create a new skill-opened call; ordinary attachments name an existing hub.
"""

from __future__ import annotations

import hashlib
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from ouroboros import call_protocol as cp
from ouroboros.call_peers import CallPeerError, get_hub
from ouroboros.gateway.call_contracts import HostCallOpenRequest
from ouroboros.gateway.call_ws import read_call_body, refusal, serve_peer
from ouroboros.gateway.host_auth import HostServiceAuthError, _authenticated, _bounded_host_read
from ouroboros.gateway.schema import validate_ingress

log = logging.getLogger(__name__)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _binding(ctx, skill: str, binding_id: str):
    from ouroboros.presence_bindings import load_presence_binding

    return load_presence_binding(ctx.data_dir, skill, binding_id)


async def _admit_presence_call(ctx, skill: str, binding_id: str) -> dict:
    """Presence owns its gate, ceiling, turn and hub lifetime; the door only asks.

    The Presence lane supplies this async entry point with an admission receipt
    containing ``admitted``, ``call_id`` and ``reason``. It authenticates the
    binding's transport again at the admission transaction.
    """
    from ouroboros.presence_call import admit_presence_call

    return await admit_presence_call(ctx, skill, binding_id)


async def api_host_call_open(request: Request) -> JSONResponse:
    ctx = request.app.state.host_service_context
    raw_token = request.headers.get("x-skill-token", "")
    try:
        skill, _ = await _authenticated(ctx, raw_token, "call_peer")
        payload = await read_call_body(request)
        if validate_ingress(payload, HostCallOpenRequest):
            raise CallPeerError("invalid_call_request")
        call_id, binding_id = payload.get("call_id", ""), payload.get("binding_id", "")
        if not call_id and not binding_id:
            raise CallPeerError("call_or_binding_required")
        if not ctx.rate_limiter.allow(f"{skill}:call"):
            return refusal("call_rate_limited", 429)
        if binding_id:
            await _bounded_host_read(ctx, lambda: _binding(ctx, skill, binding_id))
        if not call_id:
            result = await _admit_presence_call(ctx, skill, binding_id)
            if not result.get("admitted"):
                return refusal(result.get("reason") or "presence_call_refused", 409)
            call_id = result["call_id"]
        hub = get_hub(call_id)
        if hub is None or hub.closed:
            return refusal("call_not_found", 404)
        if binding_id and hub.spec.presence_binding_id != binding_id:
            return refusal("call_binding_mismatch", 403)
        if hub.spec.opened_by == cp.OPENED_BY_SKILL:
            # An already-open Presence call retains the same transport boundary.
            await _bounded_host_read(ctx, lambda: _binding(ctx, skill, hub.spec.presence_binding_id))
        hub.grant_peer(skill, _token_digest(raw_token), mic=payload.get("mic", True),
                       speaker=payload.get("speaker", False))
        return JSONResponse({"call_id": call_id, "peer_path": f"/call/{call_id}/peer"})
    except HostServiceAuthError:
        return refusal("call_peer_forbidden", 403)
    except CallPeerError as exc:
        return refusal(exc.code, 413 if exc.code == "control_too_large" else 400)
    except ImportError:
        return refusal("presence_call_admission_unavailable", 503)
    except (ValueError, KeyError):
        return refusal("call_binding_forbidden", 403)
    except Exception:
        log.exception("Host call admission failed")
        return refusal("call_admission_failed", 503)


async def ws_host_call(websocket: WebSocket) -> None:
    ctx = websocket.app.state.host_service_context
    raw_token = websocket.headers.get("x-skill-token", "")
    try:
        skill, _ = await _authenticated(ctx, raw_token, "call_peer")
        hub = get_hub(websocket.path_params["call_id"])
        if hub is None or hub.closed:
            raise CallPeerError("call_not_found")
        grant = hub.peer_grant(skill, _token_digest(raw_token))
        if hub.spec.opened_by == cp.OPENED_BY_SKILL:
            await _bounded_host_read(ctx, lambda: _binding(ctx, skill, hub.spec.presence_binding_id))
    except (HostServiceAuthError, CallPeerError, ValueError, KeyError):
        await websocket.close(code=1008, reason="call_peer_forbidden")
        return
    await serve_peer(websocket, hub, role=cp.PEER_MEETING, skill=skill,
                     mic=grant.mic, speaker=grant.speaker)


def host_call_routes() -> list:
    return [Route("/call/open", api_host_call_open, methods=["POST"]),
            WebSocketRoute("/call/{call_id}/peer", ws_host_call)]
