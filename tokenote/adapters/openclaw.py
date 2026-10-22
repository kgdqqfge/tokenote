from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..db import UsageDatabase
from ..models import UsageEvent
from ..utils import as_int, deep_get, epoch_from_any, fingerprint, model_name, stable_hash, utc_now
from .base import Adapter, SyncResult


def _count(value: Any) -> int:
    try:
        return max(0, as_int(value))
    except OverflowError:
        return 0


def _first(usage: dict, *paths: str) -> Any:
    for path in paths:
        value = deep_get(usage, path.split("."))
        if value is not None:
            return value
    return None


class OpenClawAdapter(Adapter):
    key = "openclaw"
    label = "OpenClaw"
    source_kind = "openclaw_session"
    semantics = "openclaw_inclusive_input_v1"

    def _roots(self) -> list[Path]:
        homes = [Path.home()]
        if os.environ.get("OPENCLAW_HOME"):
            homes.insert(0, Path(os.environ["OPENCLAW_HOME"]).expanduser())
        roots = []
        for home in homes:
            roots.append(home / ".openclaw")
            if home.is_dir():
                roots.extend(path for path in home.glob(".openclaw-*") if path.is_dir())
            roots.extend((home / ".clawdbot", home / ".moltbot"))
        for key in ("OPENCLAW_STATE_DIR", "CLAWDBOT_STATE_DIR", "TOKENOTE_OPENCLAW_DIR"):
            if os.environ.get(key):
                roots.insert(0, Path(os.environ[key]).expanduser())
        return list(dict.fromkeys(root.resolve() for root in roots))

    def _files(self) -> list[Path]:
        files = set()
        for root in self._roots():
            for pattern in ("agents/*/sessions/*.jsonl", "agents/*/sessions/*.jsonl.reset.*",
                            "agents/*/sessions/*.jsonl.deleted.*"):
                files.update(path.resolve() for path in root.glob(pattern) if path.is_file())
        return sorted(files, key=lambda path: str(path).lower())

    @staticmethod
    def _normalize(usage: dict) -> tuple[int, int, int, int, int, int] | None:
        report = usage.get("usageReport")
        if isinstance(report, dict) and report.get("state") == "unavailable":
            return None
        read = _count(_first(usage, "cacheRead", "cache_read", "cache_read_input_tokens", "cached_input_tokens",
                             "cached", "cached_tokens", "input_tokens_details.cached_tokens", "prompt_tokens_details.cached_tokens"))
        write = _count(_first(usage, "cacheWrite", "cache_write", "cache_creation_input_tokens", "cache_write_input_tokens",
                              "input_tokens_details.cache_write_tokens", "prompt_tokens_details.cache_write_tokens",
                              "prompt_tokens_details.cache_creation_input_tokens"))
        fresh = _count(_first(usage, "input", "inputTokens", "input_tokens", "promptTokens", "prompt_tokens"))
        direct_input = usage.get("input") is not None
        read_included = any(deep_get(usage, path.split(".")) is not None for path in (
            "cached_tokens", "input_tokens_details.cached_tokens", "prompt_tokens_details.cached_tokens"))
        cli_read_included = "cached_input_tokens" in usage or "cached" in usage
        write_included = any(deep_get(usage, path.split(".")) is not None for path in (
            "cache_write_input_tokens", "input_tokens_details.cache_write_tokens",
            "prompt_tokens_details.cache_write_tokens", "prompt_tokens_details.cache_creation_input_tokens"))
        if read_included or (not direct_input and cli_read_included):
            fresh -= read
        if not direct_input and write_included:
            fresh -= write
        inclusive = max(0, fresh) + read + write
        output = _count(_first(usage, "output", "outputTokens", "output_tokens", "completionTokens", "completion_tokens"))
        reasoning = _count(_first(usage, "reasoningTokens", "reasoning", "reasoning_tokens",
                                  "completion_tokens_details.reasoning_tokens", "output_tokens_details.reasoning_tokens",
                                  "output_tokens_details.thinking_tokens"))
        total = max(inclusive + output, _count(_first(usage, "total", "totalTokens", "total_tokens")))
        return (inclusive, output, read, write, reasoning, total) if total else None

    def _event(self, row: dict, state: dict, path: Path, offset: int) -> UsageEvent | None:
        message = row.get("message")
        if row.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        usage = message.get("usage")
        counts = self._normalize(usage) if isinstance(usage, dict) else None
        if counts is None:
            return None
        raw_time = message.get("timestamp") or row.get("timestamp")
        message_id = row.get("id") or message.get("id")
        # Reset/deleted transcripts and forked histories can contain copies of
        # the same billed message. Preserve its original identity across files.
        identity = [str(message_id), str(raw_time)] if message_id and raw_time else [state["session"], str(message_id or offset)]
        key = f"openclaw:{stable_hash(identity)}"
        return UsageEvent(
            event_key=key, app_type=self.key, model=model_name(message.get("model")) or state.get("model"),
            session_id=state["session"], request_id=str(message_id or key),
            occurred_at=epoch_from_any(raw_time), input_tokens=counts[0], output_tokens=counts[1],
            cache_read_tokens=counts[2], cache_creation_tokens=counts[3], reasoning_tokens=counts[4], total_tokens=counts[5],
            input_token_semantics=self.semantics, source_kind=self.source_kind, source_path=str(path),
            source_offset=offset, source_event_id=str(message_id or key), capture_mode="file_jsonl",
            raw_usage_json=UsageEvent.json_usage(usage),
        )

    def _read(self, path: Path, size: int, cursor) -> tuple[list[UsageEvent], int, dict, str]:
        state = {}
        offset = 0
        if cursor and cursor["last_logical_cursor"]:
            saved = json.loads(cursor["last_logical_cursor"])
            if saved.get("version") == self.semantics:
                state = saved
                offset = int(cursor["last_byte_offset"])
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if offset:
                handle.seek(max(0, offset - 256))
                prefix = handle.read(min(offset, 256))
                if offset > size or cursor["file_identity"] != identity or fingerprint(prefix) != cursor["last_tail_fingerprint"]:
                    offset, state = 0, {}
            handle.seek(offset)
            data = handle.read(max(0, size - offset))
        boundary = data.rfind(b"\n")
        if boundary < 0:
            return [], offset, state, identity
        events = {}
        position = offset
        for raw in data[:boundary + 1].splitlines(keepends=True):
            line_offset = position
            position += len(raw)
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError("OpenClaw 日志记录必须为 JSON 对象")
            kind = row.get("type")
            if not state:
                if kind != "session" or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ValueError("OpenClaw 会话头缺失或无效")
                state = {"version": self.semantics, "session": row["id"]}
            if kind == "model_change":
                state["model"] = model_name(row.get("modelId")) or state.get("model")
            elif kind == "custom" and row.get("customType") == "model-snapshot" and isinstance(row.get("data"), dict):
                state["model"] = model_name(row["data"].get("modelId")) or state.get("model")
            event = self._event(row, state, path, line_offset)
            if event:
                events[event.event_key] = event
        return list(events.values()), position, state, identity

    def sync(self, db: UsageDatabase) -> SyncResult:
        files = self._files()
        result = SyncResult(self.key, scanned=len(files), availability="ready" if files else "not_found")
        errors = []
        for path in files:
            try:
                stat = path.stat()
                source_id = f"openclaw:{path}"
                cursor = db.get_cursor(source_id)
                if cursor and cursor["last_modified"] == stat.st_mtime_ns and cursor["last_logical_cursor"]:
                    saved = json.loads(cursor["last_logical_cursor"])
                    if (saved.get("version") == self.semantics and saved.get("size") == stat.st_size
                            and cursor["file_identity"] == f"{stat.st_dev}:{stat.st_ino}"):
                        result.skipped += 1
                        continue
                events, offset, state, identity = self._read(path, stat.st_size, cursor)
                if not state:
                    continue
                selected = []
                imported = updated = 0
                fields = ("model", "occurred_at", "input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_creation_tokens", "reasoning_tokens", "total_tokens", "input_token_semantics", "raw_usage_json")
                with db.connection() as con:
                    for event in events:
                        previous = con.execute("SELECT * FROM usage_event WHERE event_key=?", (event.event_key,)).fetchone()
                        if previous is None:
                            imported += 1
                            selected.append(event)
                        elif any(getattr(event, field) != previous[field] for field in fields):
                            updated += 1
                            selected.append(event)
                db.upsert_events(selected)
                result.imported += imported
                result.updated += updated
                # Commit the cursor only after persisting usage; a failed write
                # will retry the same lines and the message key prevents doubles.
                with path.open("rb") as handle:
                    handle.seek(max(0, offset - 256))
                    tail = handle.read(min(offset, 256))
                state["size"] = stat.st_size
                db.save_cursor({
                    "source_id": source_id, "source_kind": self.source_kind, "file_path": str(path),
                    "file_identity": identity, "last_byte_offset": offset, "last_line_offset": 0,
                    "last_modified": stat.st_mtime_ns, "last_tail_fingerprint": fingerprint(tail),
                    "last_logical_cursor": json.dumps(state, ensure_ascii=False), "updated_at": utc_now(),
                })
            except Exception as exc:
                errors.append(f"{path.name}：{exc}")
        if errors:
            result.error = "；".join(errors)
        return result
