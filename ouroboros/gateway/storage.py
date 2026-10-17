"""Dashboard Storage routes over the one ``ouroboros.storage.count`` owner of this server."""

from __future__ import annotations

import pathlib
from typing import Any, List, Optional

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ouroboros import storage

_NO_STORE = {"Cache-Control": "no-store"}


def storage_routes(data_dir: pathlib.Path, *, repo_dir: Optional[Any] = None,
                   owner: Optional[storage.StorageCounter] = None) -> List[Route]:
    counter = owner or storage.count

    def repo() -> pathlib.Path:
        if repo_dir is not None:
            return pathlib.Path(repo_dir)
        from ouroboros.config import REPO_DIR

        return pathlib.Path(REPO_DIR)

    async def api_storage(request: Request) -> JSONResponse:
        """Passive: the saved count, the running count's progress, fresh free space; never
        walks, never writes. ``?count=<finished_at>`` omits a count the client already holds."""
        token = request.query_params.get("count") or None
        return JSONResponse(await run_in_threadpool(counter.projection, data_dir, count_token=token), headers=_NO_STORE)

    async def api_storage_measure(_request: Request) -> JSONResponse:
        """Start one complete count in the child, or join the running one; the body is ignored."""
        started = counter.start(data_dir, repo())
        projection = await run_in_threadpool(counter.projection, data_dir)
        return JSONResponse({**projection, "started": started}, status_code=202, headers=_NO_STORE)

    async def api_storage_measure_cancel(_request: Request) -> JSONResponse:
        """Kill the running count; the saved count stays. Nothing running answers the same way."""
        counter.cancel()
        return JSONResponse(await run_in_threadpool(counter.projection, data_dir), status_code=202, headers=_NO_STORE)

    return [
        Route("/api/storage", endpoint=api_storage, methods=["GET"]),
        Route("/api/storage/measure", endpoint=api_storage_measure, methods=["POST"]),
        Route("/api/storage/measure/cancel", endpoint=api_storage_measure_cancel, methods=["POST"]),
    ]
