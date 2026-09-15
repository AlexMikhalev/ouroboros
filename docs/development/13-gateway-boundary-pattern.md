# Gateway Boundary Pattern

This chapter owns the direction of dependency at the browser boundary: inbound routes enter through `ouroboros/gateway/`, outbound provider and harness adapters live in `ouroboros/gateways/` and carry no domain policy, and the frontend calls the one typed client. It exists so the UI can evolve without importing the agent body and the runtime can evolve without ad-hoc browser contracts.

Browser-facing backend work enters through `ouroboros/gateway/` and frontend
calls go through `web/modules/api_client.js` (structure: ARCHITECTURE
"Gateway Boundary v1"; the endpoint index lives in
`ouroboros/gateway/endpoint_index.py`, re-exported by `contracts.py`).
Outbound provider/harness adapters belong in `ouroboros/gateways/` and carry
no domain policy — do not copy policy into an adapter, promote the
`gateway/host_service.py` callback surface into a general owner/task API,
or require a class where established function owners already preserve the
boundary. Enforcement: CHECKLISTS item 17 (`gateway_parity`) and
`tests/test_gateway_parity.py`.

Named skill chat ingress uses the existing message-bus canonical writer before
enqueue. Its internal callback retains request-owned upload copies once the
canonical write is attempted, even if that write fails with an unknown outcome;
replays do not adopt new copies. The existing settled HTTP-worker wait retains
copy/admission until completion under cancellation, and a local cleanup stack
removes only unaccepted destinations. Accepted attachments remain through an
unknown write/queue outcome or disconnect. Such failure may retain an unused
copy but never claims acceptance. Cancellation support and intent writes must
address the same installation root as the operation being read. The server consumes that internal accepted source without logging a
second row. Operation reads/cancel verify the complete source against actual
task or direct-turn ownership; presentation annotations are discovery hints.
Named waits use that operation's state, while unnamed legacy waits retain their
chat callback. Tests: `test_host_service_operation_identity.py` and
`test_host_service_operations.py`. Successful child WS relay failures travel as
bounded counters through the existing process-facts channel, preserving the
producer result and best-effort `None` API (`test_extension_ws_diagnostics.py`).

