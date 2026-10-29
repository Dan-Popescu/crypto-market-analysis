"""Bounded MCP presentation and ephemeral, immutable result paging.

Only the MCP boundary uses this module. Engine/CLI calculation objects stay full.
The cache contains canonical JSON bytes, never a database cursor or a file path.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import secrets
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from functools import wraps
from typing import Any

from mcp.types import CallToolResult, TextContent

RESPONSE_BYTE_LIMIT = 32_768
# Reserve room for JSON-RPC framing as well as SDK optional fields.
_RESULT_LIMIT = RESPONSE_BYTE_LIMIT - 768


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def result_message(value: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=canonical_json(value).decode())],
                          structuredContent=value)


def response_bytes(value: dict) -> int:
    """Count both SDK text and structured copies, including JSON escaping."""
    return len(result_message(value).model_dump_json(by_alias=True, exclude_none=True).encode())


@dataclass(frozen=True)
class FrozenResult:
    data: bytes
    selection_hash: str
    content_hash: str
    deadline: float
    expires_at: float


class ResultCache:
    """Per-server TTL/LRU with a strict serialized-data memory and entry cap."""

    def __init__(self, *, ttl: float = 300, max_bytes: int = 64 * 1024 * 1024,
                 max_entries: int = 64, clock=time.monotonic, wall_clock=time.time):
        if ttl <= 0 or max_bytes < 1 or max_entries < 1:
            raise ValueError("Result cache limits must be positive.")
        self.ttl, self.max_bytes, self.max_entries = ttl, max_bytes, max_entries
        self.clock, self.wall_clock = clock, wall_clock
        self._entries: OrderedDict[str, FrozenResult] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def _expire(self):
        now = self.clock()
        for key in list(self._entries):
            if self._entries[key].deadline <= now:
                self._bytes -= len(self._entries.pop(key).data)

    def put(self, value: dict, selection: dict) -> tuple[str, FrozenResult]:
        data = canonical_json(value)
        if len(data) > self.max_bytes:
            raise ValueError("Full result exceeds the frozen-result cache byte capacity; narrow the selection.")
        frozen = FrozenResult(data, _hash(canonical_json(selection)), _hash(data),
                              self.clock() + self.ttl, self.wall_clock() + self.ttl)
        with self._lock:
            self._expire()
            while self._entries and (self._bytes + len(data) > self.max_bytes
                                     or len(self._entries) >= self.max_entries):
                self._bytes -= len(self._entries.popitem(last=False)[1].data)
            handle = secrets.token_urlsafe(24)
            self._entries[handle] = frozen
            self._bytes += len(data)
        return handle, frozen

    def get(self, handle: str, selection_hash: str, content_hash: str) -> FrozenResult:
        if not isinstance(handle, str) or not 1 <= len(handle) <= 100:
            raise ValueError("Invalid result handle.")
        with self._lock:
            self._expire()
            frozen = self._entries.get(handle)
            if frozen is None:
                raise ValueError("Result handle not found, expired or evicted; repeat the original tool call.")
            if selection_hash != frozen.selection_hash or content_hash != frozen.content_hash:
                raise ValueError("Result handle selection/content hash mismatch.")
            self._entries.move_to_end(handle)
            return frozen


def _pointer(parent: str, key: str | int) -> str:
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _shape(value: Any) -> dict:
    return {"type": "array" if isinstance(value, list) else "object" if isinstance(value, dict)
            else "value", "count": len(value) if isinstance(value, (dict, list)) else 1}


def _omitted(value: Any, path: str) -> dict:
    summary = {"_omitted": True, "path": path, **_shape(value)}
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        statuses = Counter(str(row.get("status", "unknown")) for row in value)
        if len(statuses) <= 20:
            summary["status_counts"] = dict(statuses)
    return summary


class MCPResultPresenter:
    """Keep useful small results intact; expose every omitted value by frozen handle."""

    def __init__(self, cache: ResultCache | None = None):
        self.cache = cache or ResultCache()

    def tool(self, server, **options):
        """Reusable decorator preserving FastMCP input signatures and tool names."""
        def register(function):
            signature = inspect.signature(function)

            @wraps(function)
            def presented(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                result = function(*args, **kwargs)
                return result_message(self.present(result, {"tool": function.__name__,
                                                            "arguments": dict(bound.arguments)}))

            return server.tool(**options)(presented)
        return register

    def present(self, result: dict, selection: dict) -> dict:
        handle, frozen = self.cache.put(result, selection)
        # Parsing the frozen representation detaches all later mutations.
        summary = json.loads(frozen.data)
        omitted: list[str] = []
        raw_keys = {"consumed_records", "input_references", "asks", "bids"}

        def compact(value, path=""):
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    child_path = _pointer(path, key)
                    if key in raw_keys and isinstance(child, list) and child:
                        value[key] = _omitted(child, child_path)
                        omitted.append(child_path)
                    elif key == "unknown_inputs" and isinstance(child, list) and len(child) > 10:
                        value[key] = _omitted(child, child_path)
                        omitted.append(child_path)
                    else:
                        compact(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    compact(child, _pointer(path, index))

        compact(summary)
        previews = []
        tool_name = selection.get("tool")
        preview_key = {"get_evaluation": "origins", "get_assessment": "outcomes"}.get(tool_name)
        if preview_key and isinstance(summary.get(preview_key), list) and len(summary[preview_key]) > 20:
            path = _pointer("", preview_key)
            previews.append({"path": path, "returned_count": 20,
                             "total_count": len(summary[preview_key]), "selection": "last 20"})
            summary[preview_key] = summary[preview_key][-20:]
            summary[preview_key + "_truncated"] = True
            omitted.append(path)
        if tool_name == "get_assessment" and "evidence" in summary:
            summary["evidence"] = _omitted(summary["evidence"], "/evidence")
            omitted.append("/evidence")
        metadata = {"handle": handle, "selection_hash": frozen.selection_hash,
                    "content_hash": frozen.content_hash, "expires_at": frozen.expires_at,
                    "full_bytes": len(frozen.data), "response_byte_limit": RESPONSE_BYTE_LIMIT,
                    "page_tool": "get_result_page", "omitted_paths": omitted, "preview_paths": previews,
                    "available_paths": [{"path": _pointer("", key), **_shape(value)}
                                        for key, value in result.items()]}
        summary["_mcp_result"] = metadata
        history_page = tool_name == "get_metric_history"
        while response_bytes(summary) > _RESULT_LIMIT:
            candidates = []

            def candidate(parent, key, child, path, candidates=candidates):
                size = len(canonical_json(child))
                savings = size - len(canonical_json(_omitted(child, path))) - len(canonical_json(path))
                if savings <= 0:
                    return
                # Detailed sequences (trajectories, ledgers and evidence rows)
                # yield before the enclosing financial/status/uncertainty maps.
                # Only after those details are paged may a genuinely oversized
                # scalar or the deepest remaining map need explicit omission.
                priority = 2 if isinstance(child, list) else 1 if isinstance(child, str) else 0
                if history_page and path.startswith("/points/") and key == "metadata":
                    priority = 3
                depth = 0 if isinstance(child, list) else path.count("/")
                candidates.append((priority, depth, savings, parent, key, child, path))

            def visit(value, path=""):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "_mcp_result" or (isinstance(child, dict) and child.get("_omitted") is True):
                            continue
                        child_path = _pointer(path, key)
                        if not (history_page and key == "points" and path == ""):
                            candidate(value, key, child, child_path)
                        visit(child, child_path)
                elif isinstance(value, list):
                    if history_page and path == "/points":
                        # Keep every emitted history row and its source/value/unit/clocks.
                        # Only opaque metadata may be summarized; the history cursor
                        # continues after exactly the rows present in this list.
                        for index, row in enumerate(value):
                            if isinstance(row, dict) and "metadata" in row:
                                child = row["metadata"]
                                if not (isinstance(child, dict) and child.get("_omitted") is True):
                                    child_path = _pointer(_pointer(path, index), "metadata")
                                    candidate(row, "metadata", child, child_path)
                    return

            visit(summary)
            if not candidates:
                if history_page:
                    raise ValueError("History point core values exceed the MCP byte limit; narrow the page limit.")
                # Path metadata can itself be large: root paging remains complete.
                summary = {"_mcp_result": {**metadata, "omitted_paths": [""],
                                          "available_paths": [{"path": "", **_shape(result)}]}}
                break
            _, _, _, parent, key, child, path = max(candidates, key=lambda item: item[:3])
            parent[key] = _omitted(child, path)
            omitted[:] = [old for old in omitted if not old.startswith(path + "/")]
            omitted.append(path)
        if response_bytes(summary) > _RESULT_LIMIT:
            raise ValueError("Result metadata exceeds the MCP response byte limit.")
        return summary

    def page(self, handle: str, selection_hash: str, content_hash: str, path: str = "",
             offset: int = 0, limit: int = 100, fragment_offset: int = 0) -> dict:
        """JSON-pointer pages; fragments concatenate as canonical JSON text before parsing."""
        if any(type(value) is not int for value in (offset, limit, fragment_offset)):
            raise ValueError("Page offsets and limit must be integers.")
        if offset < 0 or fragment_offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Page offsets must be nonnegative and limit must be 1–100.")
        if not isinstance(path, str) or len(path) > 2048 or (path and not path.startswith("/")):
            raise ValueError("path must be a bounded JSON pointer, or empty for the full result.")
        frozen = self.cache.get(handle, selection_hash, content_hash)
        value = json.loads(frozen.data)
        try:
            for part in path.split("/")[1:] if path else []:
                # Reject malformed RFC 6901 escapes rather than aliasing another selection.
                if "~" in part.replace("~0", "").replace("~1", ""):
                    raise KeyError(part)
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not part.isdecimal() or str(int(part)) != part:
                        raise KeyError(part)
                    value = value[int(part)]
                elif isinstance(value, dict):
                    value = value[part]
                else:
                    raise KeyError(part)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("Result path not found in this frozen selection.") from exc
        kind = "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "value"
        rows = value if kind == "array" else list(value.items()) if kind == "object" else [value]
        if offset > len(rows) or (offset == len(rows) and fragment_offset):
            raise ValueError("Page offset is outside this frozen selection.")
        base = {"handle": handle, "selection_hash": selection_hash, "content_hash": content_hash,
                "path": path, "path_hash": _hash(canonical_json(value)), "expires_at": frozen.expires_at,
                "kind": kind, "total_count": len(rows), "offset": offset,
                "response_byte_limit": RESPONSE_BYTE_LIMIT}
        items = []
        for row in rows[offset:offset + limit]:
            item = {"key": row[0], "value": row[1]} if kind == "object" else row
            candidate = {**base, "items": [*items, item], "returned_count": len(items) + 1,
                         "next_offset": offset + len(items) + 1 if offset + len(items) + 1 < len(rows) else None}
            if response_bytes(candidate) > _RESULT_LIMIT:
                break
            items.append(item)
        if items and not fragment_offset or offset == len(rows):
            return {**base, "items": items, "returned_count": len(items),
                    "next_offset": offset + len(items) if offset + len(items) < len(rows) else None}
        if items:
            raise ValueError("fragment_offset is only valid for an oversized row.")
        row = rows[offset]
        item = {"key": row[0], "value": row[1]} if kind == "object" else row
        encoded = canonical_json(item).decode()
        if fragment_offset >= len(encoded):
            raise ValueError("fragment_offset is outside the oversized row.")
        low, high = 1, len(encoded) - fragment_offset
        def fragment(count):
            end = fragment_offset + count
            return {**base, "fragment": encoded[fragment_offset:end], "fragment_encoding": "canonical-json-text",
                    "fragment_offset": fragment_offset, "fragment_total_characters": len(encoded),
                    "next_fragment_offset": end if end < len(encoded) else None,
                    "returned_count": 0, "next_offset": offset if end < len(encoded)
                    else offset + 1 if offset + 1 < len(rows) else None}
        while low < high:
            middle = (low + high + 1) // 2
            if response_bytes(fragment(middle)) <= _RESULT_LIMIT:
                low = middle
            else:
                high = middle - 1
        answer = fragment(low)
        if response_bytes(answer) > _RESULT_LIMIT:
            raise ValueError("Page metadata exceeds the MCP response byte limit.")
        return answer
