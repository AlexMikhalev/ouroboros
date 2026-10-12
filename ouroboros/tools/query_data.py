"""Typed runtime reads with exact evidence exports, never model-supplied paths.

Selectors use dotted keys, [n] and [*]; wildcards preserve array nesting. JSONL
filters select whole records before tail and projection. A bounded tail window
discloses incomplete scanning. Store documents and multi-id bundles are canonical
JSON reconstructions; single file/blob records retain their original bytes.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re

from ouroboros import query_data_sql as sql, store
from ouroboros.tool_capabilities import RESULT_VIEW_PARAMS
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result

_FILES = {"review_ledger": "state/review_ledger/{id}.json", "advisory_review": "state/advisory_review.json",
          "queue_snapshot": "state/queue_snapshot.json", "queue_dispatch": "state/queue_dispatch.json",
          "skill_state": "state/skills/{id}/{state_file}.json",
          "run": "task_drives/{child}/delegated_runs/{id}.json",
          "verification_receipts": "task_results/artifacts/{id}/verification_receipts.jsonl",
          **{name + "_log": f"logs/{name}.jsonl" for name in ("supervisor", "events", "tools", "chat")}}
_ID_KINDS = {"task_result", "run", "review_ledger", "skill_state", "verification_receipts"}
_STATES = ("review", "review_job", "health", "enabled", "deps", "grants")
_SELECTOR = re.compile(r"(?:[^.\[\]]+|\[(?:\d+|\*)\])(?:\.[^.\[\]]+|\[(?:\d+|\*)\])*")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) or value in {".", ".."}:
        raise ValueError("IDs and artifact names must be plain names, without path components")
    return value


def _select(value, selector):
    if not selector:
        return value
    parts = re.findall(r"[^.\[\]]+|\[(?:\d+|\*)\]", selector)

    def walk(current, index):
        if index == len(parts):
            return current
        part = parts[index]
        if part == "[*]":
            if not isinstance(current, list):
                raise ValueError("[*] requires an array")
            return [walk(item, index + 1) for item in current]
        return walk(current[int(part[1:-1])] if part.startswith("[") else current[part], index + 1)
    return walk(value, 0)


def _path(ctx, root, relative):
    from ouroboros.tools.core_secret_paths import is_runtime_secret_target
    target = (root / relative).resolve()
    if is_runtime_secret_target(target, ctx.repo_dir, data_root=root, ctx=ctx, include_owner_state=False):
        raise PermissionError("runtime secret target refused")
    if not target.is_relative_to(root.resolve()):
        raise PermissionError("source escapes its host-selected root")
    return target


def _child_root(ctx, canonical, child):
    from ouroboros.task_custody import own_child_drives
    caller = _name(ctx.task_id)
    with store.open_store(canonical, write=False) as txn:
        owned = txn.conn.execute("""WITH RECURSIVE lineage(task_id) AS (
            SELECT task_id FROM tasks WHERE task_id=? UNION
            SELECT t.task_id FROM tasks t JOIN lineage p ON t.parent_task_id=p.task_id)
            SELECT task_id FROM lineage WHERE task_id=?""", (caller, child)).fetchone()
    if not owned:
        raise PermissionError("child drive is outside the caller's lineage")
    if child == caller:
        return Path(ctx.drive_root).resolve()
    roots = own_child_drives(canonical, child)
    if not roots:
        raise FileNotFoundError("owned child drive is unavailable")
    return roots[0]


def _jsonl(path, where, tail, cap):
    rows, consumed, partial = deque(maxlen=tail or None), 0, False
    whole = bytearray() if not where and not tail else None
    with path.open("rb") as stream:
        if tail and path.stat().st_size > cap:
            stream.seek(-cap, 2)
            stream.readline(cap + 1)  # discard the possibly partial leading record
            partial = True
        while line := stream.readline(cap - consumed + 1):
            consumed += len(line)
            if consumed > cap:
                raise ValueError("input byte limit exceeded; use tail or a narrower source")
            if whole is not None:
                whole.extend(line)
            if not line.strip():
                continue
            value = json.loads(line)
            if where:
                try:
                    field = _select(value, where["field"])
                except (KeyError, IndexError, TypeError):
                    continue
                if where["op"] == "eq":
                    matches = field == where["value"]
                else:
                    matches = isinstance(field, (str, list, dict)) and where["value"] in field
                if not matches:
                    continue
            rows.append((line, value))
    raw = bytes(whole) if whole is not None else b"".join(row[0] for row in rows)
    return raw, [row[1] for row in rows], partial, consumed


def _record(ctx, root, kind, identity, child, state_file, op, where, tail, cap):
    if kind == "task_result":
        if not _path(ctx, root, store.STORE_REL).is_file():
            raise FileNotFoundError("task store is unavailable in the selected data root")
        with store.open_store(root, write=False) as txn:
            size = txn.conn.execute("SELECT length(CAST(body AS BLOB)) FROM task_bodies WHERE task_id=?",
                                    (identity,)).fetchone()
            if size is None:
                raise FileNotFoundError("task result not found")
            if size[0] > cap:
                raise ValueError("task document exceeds input byte limit")
            value = txn.load(identity)
        raw = _canonical(value)
        return raw, value, True, False, len(raw)
    relative = _FILES[kind].format(id=identity, child=child, state_file=state_file)
    if kind == "run" and root.parts[-2:] == ("task_drives", child):
        relative = f"delegated_runs/{identity}.json"
    path = _path(ctx, root, relative)
    if op == "jsonl":
        raw, value, partial, consumed = _jsonl(path, where, tail, cap)
        return raw, value, False, partial, consumed
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("document exceeds input byte limit")
    return raw, json.loads(raw), False, False, len(raw)


def _query_data(ctx: ToolContext, *, op: str, db="current", query="", kind="", ids=None,
                child_task_id="", state_file="review_job", sha256="", blob_kind="json", select="",
                filter=None, tail=100, max_rows=100, max_bytes=65536, snapshot=None,
                timeout_sec=1, save_as="", format="json", view_head_chars=None, view_tail_chars=None) -> str:
    from ouroboros.artifacts import store_task_artifact_bytes
    from ouroboros.tool_access import canonical_data_root
    from ouroboros.observability import read_blob_ref

    result = {}
    try:
        if op not in {"sql", "json", "jsonl", "blob"} or format not in {"json", "text"}:
            raise ValueError("invalid op or format")
        for value, low, high in ((max_bytes, 256, sql.MAX_BYTES), (max_rows, 1, 10000), (tail, 0, 10000)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("byte/row/tail limit outside the declared bounds")
        if type(timeout_sec) not in (int, float) or not 0 < timeout_sec <= 10:
            raise ValueError("timeout_sec must be greater than zero and at most 10")
        if snapshot is not None and type(snapshot) is not bool:
            raise ValueError("snapshot must be boolean")
        if filter is not None and (op != "jsonl" or not isinstance(filter, dict)
                or set(filter) != {"field", "op", "value"} or filter["op"] not in {"eq", "contains"}
                or not isinstance(filter["field"], str) or not filter["field"]):
            raise ValueError("filter requires field, op=eq|contains and value (JSONL only)")
        for selector in (select, filter["field"] if filter else ""):
            if selector and (not isinstance(selector, str) or len(selector) > 512 or not _SELECTOR.fullmatch(selector)):
                raise ValueError("select/filter fields accept dotted keys, [n] and [*]")
        if op != "sql" and (query or db != "current" or snapshot is not None or max_rows != 100 or timeout_sec != 1):
            raise ValueError("db/query/snapshot/max_rows/timeout_sec are SQL parameters")
        if op in {"sql", "blob"} and (kind or ids is not None) or op != "blob" and (sha256 or blob_kind != "json"):
            raise ValueError("source parameters do not belong to this operation")
        if op != "jsonl" and tail != 100 or kind != "skill_state" and state_file != "review_job":
            raise ValueError("tail is JSONL-only; state_file is skill_state-only")
        canonical = Path((ctx.task_metadata or {}).get("native_data_root") or canonical_data_root(ctx)).resolve()
        root = Path(ctx.drive_root).resolve()
        if child_task_id:
            root = _child_root(ctx, canonical, _name(child_task_id))
        source = {"op": op, "child_task_id": child_task_id}
        reconstructed, partial = False, False
        if op == "sql":
            if db not in {"current", "money", "packs"} or not isinstance(query, str) or not query.strip() or len(query) > 16384:
                raise ValueError("sql requires db=current|money|packs and one query of at most 16384 characters")
            use_snapshot = db == "money" if snapshot is None else snapshot
            if db == "current" and not use_snapshot and timeout_sec > sql.CURRENT_HOLD_SEC:
                raise ValueError(f"a direct db=current read stops at {sql.CURRENT_HOLD_SEC:g} s because task writes wait "
                                 "on it; use snapshot=true for a longer query")
            path = _path(ctx, root, sql.database_path(root, db).relative_to(root))
            absent = not any(path.parent.glob("*.members.json")) if db == "packs" else not path.is_file()
            if absent:
                result = {"status": "unavailable", "code": "no_packs" if db == "packs" else "not_found", "source": {"db": db}}
            else:
                source.update(db=db, query=query, snapshot=use_snapshot)
                result = sql.run_sql(ctx, root, db=db, query=query, max_rows=max_rows, max_bytes=max_bytes,
                                     snapshot=use_snapshot, timeout_sec=timeout_sec)
            value = result.get("data")
            raw, reconstructed = _canonical(value), True
        elif op == "blob":
            if not re.fullmatch(r"[0-9a-f]{64}", sha256) or blob_kind not in {"json", "txt", "bin"}:
                raise ValueError("blob requires a lowercase sha256 and blob_kind=json|txt|bin")
            path = _path(ctx, root, f"observability/blobs/{sha256}.{blob_kind}.gz")
            ref = dict(sha256=sha256, kind=blob_kind, encoding="gzip", path=str(path))
            raw = read_blob_ref(root, ref, expected_kind=blob_kind, max_bytes=sql.MAX_BYTES, raw_bytes=True)
            value = json.loads(raw) if blob_kind == "json" else {"hex": raw.hex()} if blob_kind == "bin" else raw.decode("utf-8")
            source.update(sha256=sha256, blob_kind=blob_kind)
        else:
            if kind not in _FILES and kind != "task_result" or state_file not in _STATES:
                raise ValueError("invalid kind or state_file")
            is_lines = kind.endswith("_log") or kind == "verification_receipts"
            if (op == "jsonl") != is_lines:
                raise ValueError("this kind requires " + ("jsonl" if is_lines else "json"))
            identities = ids if ids is not None else ([ctx.task_id] if kind == "verification_receipts" else [""])
            if not isinstance(identities, list) or not 1 <= len(identities) <= 32:
                raise ValueError("ids must be an array of 1..32 identities")
            if kind in _ID_KINDS:
                identities = [_name(identity) for identity in identities]
            elif identities != [""]:
                raise ValueError("this kind has no IDs; use child_task_id for a child's logs or queue")
            if len(set(identities)) != len(identities):
                raise ValueError("ids must be unique")
            if kind in {"task_result", "review_ledger", "skill_state"}:
                root = canonical
            values, remaining = {}, sql.MAX_BYTES
            for identity in identities:
                raw, value, rebuilt, clipped, consumed = _record(
                    ctx, root, kind, identity, _name(child_task_id or ctx.task_id) if kind == "run" else "",
                    state_file, op, filter, tail, remaining)
                remaining -= consumed
                if remaining < 0:
                    raise ValueError("input byte limit exceeded")
                if select:
                    value = _select(value, select)
                    raw = _canonical(value)
                values[identity] = value
                reconstructed |= rebuilt
                partial |= clipped
            if len(values) > 1:
                value, reconstructed = values, True
                raw = _canonical(value)
            source.update(kind=kind, ids=identities, state_file=state_file if kind == "skill_state" else None)
        if not result or result["status"] == "ok":
            if select:
                if op in {"sql", "blob"}:
                    value = _select(value, select)
                    raw = _canonical(value)
                source["select"] = select
            if op == "jsonl":
                source.update(filter=filter, tail=tail, scan_truncated=partial)
            if format == "text":
                if not isinstance(value, str):
                    raise ValueError("format=text requires exactly one selected string")
                raw = value.encode("utf-8")
            if len(raw) > max_bytes or len(_canonical(value)) > max_bytes:
                raise ValueError("selected bytes exceed max_bytes; narrow select or raise the limit")
            result = dict(status="ok", data=value, source=source, reconstructed=reconstructed)
            if save_as:
                receipt = store_task_artifact_bytes(ctx.drive_root, _name(ctx.task_id), _name(save_as), raw)
                result["saved"] = dict(path=receipt["path"], sha256=receipt["sha256"], size=receipt["bytes"],
                                       source=source, reconstructed=reconstructed)
    except (ValueError, TypeError, KeyError, IndexError, OSError, RuntimeError, RecursionError) as exc:
        code = "access_denied" if isinstance(exc, PermissionError) else "not_found" if isinstance(exc, FileNotFoundError) else "query_failed"
        result = dict(status="blocked" if code == "access_denied" else "unavailable" if code == "not_found" else "error",
                      code=code, message=str(exc))
    text = _canonical(result).decode("utf-8")
    code = {"ok": "OK", "error": "TOOL_ERROR", "blocked": "ACCESS_BLOCKED", "unavailable": "CAPABILITY_UNAVAILABLE"}[result["status"]]
    return _publish_tool_result(ctx, ToolResult(status=result["status"], code=code, text=text))


def get_tools():
    return [ToolEntry("query_data", {"name": "query_data", "description": (
        "Read runtime facts, money, pack indexes, typed JSON/JSONL and verified blobs. No paths accepted. "
        "Current SQL exposes tasks facts; use json/task_result for bodies. Money snapshots default on. "
        f"Current-store reads hold the lock task writes wait on: at most {sql.CURRENT_HOLD_SEC:g} s; longer: snapshot=true. "
        "One allocating SQL function's TIME is not bounded by interrupt (its memory is); the child has a deadline. "
        "save_as retains exact whole-record bytes in the calling task's artifact_store with path/sha256/size/source. "
        "Projections use sorted UTF-8 JSON; store documents and multi-ID bundles are flagged reconstructed. "
        "format=text saves one selected string exactly. Complete tool output is retained before any delivery cut."),
        "parameters": {"type": "object", "required": ["op"], "properties": {
            "op": {"enum": ["sql", "json", "jsonl", "blob"], "type": "string"},
            "db": {"enum": ["current", "money", "packs"], "type": "string", "default": "current"},
            "query": {"type": "string", "maxLength": 16384, "description": "One read-only SQL statement; BLOB cells return {hex:...}."},
            "kind": {"type": "string", "enum": ["task_result", *_FILES]},
            "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32,
                    "description": "Task, run, review-record or skill names; one returns its value, several return an ID map."},
            "child_task_id": {"type": "string", "description": "Owned descendant's execution drive; default caller drive. Task documents/ledger/skills use canonical state."},
            "state_file": {"type": "string", "enum": list(_STATES), "default": "review_job"},
            "sha256": {"type": "string", "description": "Blob SHA-256; the host locates loose or packed bytes."},
            "blob_kind": {"type": "string", "enum": ["json", "txt", "bin"], "default": "json"},
            "select": {"type": "string", "description": "Dotted keys, [n], [*], e.g. primary_output.text or [*].tool. Applied per ID after filter/tail."},
            "filter": {"type": "object", "required": ["field", "op", "value"], "additionalProperties": False,
                       "properties": {"field": {"type": "string"}, "op": {"type": "string", "enum": ["eq", "contains"]}, "value": {}}},
            "tail": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 100,
                     "description": "JSONL: last matching rows; 0 reads from start. An 8 MiB scan window reports scan_truncated."},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 100},
            "max_bytes": {"type": "integer", "minimum": 256, "maximum": sql.MAX_BYTES, "default": 65536},
            "snapshot": {"type": "boolean", "description": "SQL snapshot; default true for money, false otherwise."},
            "timeout_sec": {"type": "number", "exclusiveMinimum": 0, "maximum": 10, "default": 1},
            "save_as": {"type": "string", "description": "One plain immutable artifact filename in the CALLING task."},
            "format": {"type": "string", "enum": ["json", "text"], "default": "json"},
            **RESULT_VIEW_PARAMS,
        }}}, _query_data, timeout_sec=30)]
