"""Adapters based on documented upstream schemas; absent sources stay inactive."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .base import Adapter, SyncResult, read_jsonl_delta
from ..models import UsageEvent
from ..utils import as_int, epoch_from_any, stable_hash


def unique_existing(paths):
    return [path for path in dict.fromkeys(Path(p).expanduser() for p in paths) if path.is_file()]


class ClaudeCodeAdapter(Adapter):
    key = "claude"
    label = "Claude Code"

    def files(self):
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
        return sorted(root.rglob("*.jsonl")) if root.exists() else []

    def sync(self, db):
        files = self.files()
        result = SyncResult(self.key, scanned=len(files), availability="ready" if files else "not_found")
        for path in files:
            events = {}
            for offset, obj in read_jsonl_delta(db, f"claude:{path}", "claude_jsonl", path):
                msg = obj.get("message") or {}
                if obj.get("type") != "assistant" or not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                identity = f"{obj.get('requestId', '')}:{msg.get('id') or str(path) + ':' + str(offset)}"
                key = f"claude:{identity}"
                fresh, output = as_int(usage.get("input_tokens")), as_int(usage.get("output_tokens"))
                read, write = as_int(usage.get("cache_read_input_tokens")), as_int(usage.get("cache_creation_input_tokens"))
                event = UsageEvent(
                    event_key=key, app_type=self.key, model=msg.get("model"), session_id=obj.get("sessionId"),
                    request_id=str(obj.get("requestId") or msg.get("id") or key),
                    occurred_at=epoch_from_any(obj.get("timestamp")), input_tokens=fresh, output_tokens=output,
                    cache_read_tokens=read, cache_creation_tokens=write, total_tokens=fresh+output+read+write,
                    input_token_semantics="anthropic_exclusive_cache", source_kind="claude_jsonl", source_path=str(path),
                    source_offset=offset, source_event_id=key, capture_mode="file_jsonl",
                    raw_usage_json=UsageEvent.json_usage(usage),
                )
                current = events.get(key)
                if current is None or event.output_tokens >= current.output_tokens:
                    events[key] = event
            # Streaming fragments may repeat one message across successive polls.
            selected = []
            with db.connection() as con:
                for event in events.values():
                    previous = con.execute("SELECT output_tokens FROM usage_event WHERE event_key=?", (event.event_key,)).fetchone()
                    if previous is None or event.output_tokens >= previous[0]:
                        selected.append(event)
            result.imported += db.upsert_events(selected)
        return result


class PiAdapter(Adapter):
    key = "pi"
    label = "Pi agents"

    def files(self):
        root = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))) / "sessions"
        return sorted(root.rglob("*.jsonl")) if root.exists() else []

    def sync(self, db):
        files = self.files()
        result = SyncResult(self.key, scanned=len(files), availability="ready" if files else "not_found")
        for path in files:
            events = []
            for offset, obj in read_jsonl_delta(db, f"pi:{path}", "pi_jsonl", path):
                message = obj.get("message") or {}
                if obj.get("type") == "usage":
                    message = obj
                elif obj.get("type") != "message" or message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = f"pi:{path.stem}:{obj.get('id') or offset}"
                fresh, output = as_int(usage.get("input")), as_int(usage.get("output"))
                read, write = as_int(usage.get("cacheRead")), as_int(usage.get("cacheWrite"))
                events.append(UsageEvent(
                    event_key=key, app_type=self.key, model=message.get("model"), session_id=path.stem,
                    request_id=str(obj.get("id") or key), occurred_at=epoch_from_any(message.get("timestamp") or obj.get("timestamp")),
                    input_tokens=fresh, output_tokens=output, cache_read_tokens=read, cache_creation_tokens=write,
                    reasoning_tokens=as_int(usage.get("reasoning")), total_tokens=as_int(usage.get("totalTokens")) or fresh+output+read+write,
                    input_token_semantics="pi_exclusive_cache", source_kind="pi_jsonl", source_path=str(path),
                    source_offset=offset, source_event_id=key, capture_mode="file_jsonl", raw_usage_json=UsageEvent.json_usage(usage),
                ))
            result.imported += db.upsert_events(events)
        return result


class SQLiteUsageAdapter(Adapter):
    table = ""
    columns = set()

    def paths(self):
        return []

    def parse(self, row, path):
        raise NotImplementedError

    def sync(self, db):
        paths = self.paths()
        result = SyncResult(self.key, scanned=len(paths), availability="ready" if paths else "not_found")
        for path in paths:
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as con:
                    con.row_factory = sqlite3.Row
                    columns = {row[1] for row in con.execute(f'PRAGMA table_info("{self.table}")')}
                    if not self.columns.issubset(columns):
                        result.error = f"已找到数据文件，但缺少兼容的 {self.table} 字段"
                        continue
                    events = []
                    for row in con.execute(f'SELECT * FROM "{self.table}"'):
                        event = self.parse(dict(row), path)
                        if event is not None:
                            events.append(event)
                result.imported += db.replace_source(str(path), events, self.key)
            except (OSError, sqlite3.Error, ValueError) as exc:
                result.error = str(exc)
        return result


class ZCodeAdapter(SQLiteUsageAdapter):
    key = "zcode"
    label = "ZCode"
    table = "model_usage"
    columns = {"id", "model_id", "input_tokens", "output_tokens", "cache_read_input_tokens"}

    def paths(self):
        base = Path(os.environ.get("ZCODE_DATA_BASE_DIR", str(Path.home())))
        paths = [base / ".zcode" / "cli" / "db" / "db.sqlite"]
        if os.environ.get("TOKENOTE_ZCODE_DB"):
            paths.insert(0, Path(os.environ["TOKENOTE_ZCODE_DB"]))
        return unique_existing(paths)

    def parse(self, row, path):
        reported_input, output = as_int(row.get("input_tokens")), as_int(row.get("output_tokens"))
        read, write = as_int(row.get("cache_read_input_tokens")), as_int(row.get("cache_creation_input_tokens"))
        if not any((reported_input, output, read, write)):
            return None
        total = as_int(row.get("computed_total_tokens"))
        provider_total = as_int(row.get("provider_total_tokens")) or total
        input_total = reported_input if reported_input > 0 else read+write
        # Mirror the upstream reader: AI SDK v6 input includes caches, while older
        # stored rows can be fresh input. Choose the interpretation nearest the raw total.
        if reported_input > 0 and read+write > 0 and provider_total > 0:
            inclusive_distance = abs(provider_total - (reported_input+output))
            exclusive_distance = abs(provider_total - (reported_input+read+write+output))
            if exclusive_distance < inclusive_distance:
                input_total = reported_input+read+write
        key = f"zcode:{path}:{row['id']}"
        return UsageEvent(
            event_key=key, app_type=self.key, model=row["model_id"], session_id=row.get("session_id"), turn_id=row.get("turn_id"),
            request_id=str(row.get("logical_request_id") or row["id"]), occurred_at=epoch_from_any(row.get("completed_at") or row.get("started_at")),
            input_tokens=input_total, output_tokens=output, cache_read_tokens=read, cache_creation_tokens=write,
            reasoning_tokens=as_int(row.get("reasoning_tokens")), total_tokens=total or input_total+output,
            input_token_semantics="zcode_total_input", source_kind="zcode_model_usage", source_path=str(path),
            source_event_id=key, capture_mode="sqlite_snapshot", raw_usage_json=UsageEvent.json_usage({k:row[k] for k in row if 'token' in k or k in ('status','id')}),
        )


class MiniMaxCodeAdapter(SQLiteUsageAdapter):
    key = "minimax"
    label = "MiniMax Code"
    table = "local_runtime_token_usage"
    columns = {"id", "session_id", "ts", "input_tokens", "output_tokens", "cache_read_tokens"}

    def paths(self):
        roots = [Path.home() / ".minimax"]
        roots += list(Path.home().glob(".minimax-*"))
        for name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR"):
            if os.environ.get(name):
                roots.insert(0, Path(os.environ[name]))
        paths = [root / "v2" / "sqlite" / "runtime-state.sqlite" for root in roots]
        if os.environ.get("TOKENOTE_MINIMAX_DB"):
            paths.insert(0, Path(os.environ["TOKENOTE_MINIMAX_DB"]))
        return unique_existing(paths)

    def parse(self, row, path):
        fresh, reasoning = as_int(row.get("input_tokens")), as_int(row.get("reasoning_tokens"))
        output = as_int(row.get("output_tokens")) + reasoning
        read, write = as_int(row.get("cache_read_tokens")), as_int(row.get("cache_write_tokens"))
        key = f"minimax:{path}:{row['id']}"
        return UsageEvent(
            event_key=key, app_type=self.key, model=row.get("model"), session_id=row.get("session_id"), turn_id=row.get("turn_id"),
            request_id=str(row["id"]), occurred_at=epoch_from_any(row.get("ts")), input_tokens=fresh, output_tokens=output,
            cache_read_tokens=read, cache_creation_tokens=write, reasoning_tokens=reasoning, total_tokens=fresh+output+read+write,
            input_token_semantics="minimax_exclusive_cache", source_kind="minimax_token_usage", source_path=str(path),
            source_event_id=key, capture_mode="sqlite_snapshot", raw_usage_json=UsageEvent.json_usage({k:row[k] for k in row if 'token' in k or k in ('ts','id')}),
        )


class HermesAdapter(SQLiteUsageAdapter):
    key = "hermes"
    label = "Hermes"
    table = "session_model_usage"
    columns = {"session_id", "model", "input_tokens", "output_tokens", "cache_read_tokens"}

    def paths(self):
        roots = [Path.home() / ".hermes", Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "hermes"]
        if os.environ.get("HERMES_HOME"):
            roots.insert(0, Path(os.environ["HERMES_HOME"]))
        expanded = list(roots)
        for root in roots:
            expanded += list((root / "profiles").glob("*")) if (root / "profiles").exists() else []
        paths = [root / "state.db" for root in expanded]
        if os.environ.get("TOKENOTE_HERMES_DB"):
            paths.insert(0, Path(os.environ["TOKENOTE_HERMES_DB"]))
        return unique_existing(paths)

    def parse(self, row, path):
        identity = {k:row.get(k) for k in ('session_id','model','billing_provider','billing_base_url','billing_mode','task')}
        key = f"hermes:{path}:{stable_hash(identity)}"
        fresh, output = as_int(row.get("input_tokens")), as_int(row.get("output_tokens"))
        read, write = as_int(row.get("cache_read_tokens")), as_int(row.get("cache_write_tokens"))
        # This is a per-session/model aggregate. Keep the time NULL: assigning last_seen
        # would invent a daily breakdown for historic calls across multiple dates.
        return UsageEvent(
            event_key=key, app_type=self.key, model=row.get("model"), session_id=row.get("session_id"),
            request_id=key, occurred_at=None, input_tokens=fresh, output_tokens=output,
            cache_read_tokens=read, cache_creation_tokens=write, reasoning_tokens=as_int(row.get("reasoning_tokens")),
            total_tokens=fresh+output+read+write, input_token_semantics="hermes_exclusive_cache_aggregate",
            source_kind="hermes_session_aggregate", source_path=str(path), source_event_id=key,
            capture_mode="sqlite_snapshot", raw_usage_json=UsageEvent.json_usage({k:row[k] for k in row if 'token' in k or k in ('api_call_count','first_seen','last_seen')}),
        )
