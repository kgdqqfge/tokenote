from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..models import UsageEvent
from ..utils import as_int, epoch_from_any, model_name
from .base import Adapter, SyncResult


class OpenCodeAdapter(Adapter):
    key = "opencode"
    label = "OpenCode"

    def _paths(self) -> list[Path]:
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        localappdata = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        candidates = [
            home / ".local" / "share" / "opencode" / "opencode.db",
            appdata / "opencode" / "opencode.db",
            localappdata / "opencode" / "opencode.db",
        ]
        configured = os.environ.get("TOKENOTE_OPENCODE_DB")
        if configured:
            candidates.insert(0, Path(configured))
        return [p for p in dict.fromkeys(candidates) if p.exists()]

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)

    @staticmethod
    def _model(value: Any) -> str | None:
        if isinstance(value, dict):
            return model_name(value.get("id") or value.get("modelID") or value)
        if isinstance(value, str):
            try:
                return OpenCodeAdapter._model(json.loads(value))
            except Exception:
                return value
        return model_name(value)

    @staticmethod
    def _tokens(data: dict[str, Any]) -> dict[str, int] | None:
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        input_tokens = as_int(tokens.get("input"))
        reasoning = as_int(tokens.get("reasoning"))
        output_tokens = as_int(tokens.get("output")) + reasoning
        cache_read = as_int(cache.get("read"))
        cache_write = as_int(cache.get("write"))
        if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
            return None
        return {
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": reasoning,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "total": as_int(tokens.get("total")) or input_tokens + output_tokens + cache_read + cache_write,
        }

    @staticmethod
    def _time(data: dict[str, Any], fallback: Any) -> int | None:
        time_data = data.get("time")
        if isinstance(time_data, dict):
            return epoch_from_any(time_data.get("completed") or time_data.get("created"))
        return epoch_from_any(fallback)

    def _message_events(self, path: Path, con: sqlite3.Connection) -> tuple[list[UsageEvent], set[str]]:
        events: list[UsageEvent] = []
        session_ids: set[str] = set()
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "session_message" in tables:
            rows = con.execute(
                "SELECT id, session_id, time_created, data FROM session_message "
                "WHERE type = 'assistant' ORDER BY time_created"
            )
            for message_id, session_id, created_at, raw in rows:
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                tokens = self._tokens(data)
                if not tokens:
                    continue
                session_ids.add(str(session_id))
                event_key = f"opencode:{path}:session_message:{message_id}"
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=self._model(data.get("model")),
                    session_id=str(session_id),
                    request_id=str(message_id),
                    occurred_at=self._time(data, created_at),
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    cache_read_tokens=tokens["cache_read"],
                    cache_creation_tokens=tokens["cache_write"],
                    reasoning_tokens=tokens["reasoning"],
                    total_tokens=tokens["total"],
                    input_token_semantics="provider_input_plus_cache_buckets",
                    source_kind="opencode_message",
                    source_path=str(path),
                    source_event_id=event_key,
                    capture_mode="sqlite_snapshot",
                    raw_usage_json=UsageEvent.json_usage({"table": "session_message", "tokens": tokens, "model": data.get("model")}),
                ))
        if "message" in tables:
            rows = con.execute("SELECT id, session_id, time_created, data FROM message ORDER BY time_created")
            for message_id, session_id, created_at, raw in rows:
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if data.get("role") not in (None, "assistant"):
                    continue
                if str(session_id) in session_ids:
                    continue
                tokens = self._tokens(data)
                if not tokens:
                    continue
                event_key = f"opencode:{path}:message:{message_id}"
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=self._model(data.get("model") or data.get("modelID")),
                    session_id=str(session_id),
                    request_id=str(message_id),
                    occurred_at=self._time(data, created_at),
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    cache_read_tokens=tokens["cache_read"],
                    cache_creation_tokens=tokens["cache_write"],
                    reasoning_tokens=tokens["reasoning"],
                    total_tokens=tokens["total"],
                    input_token_semantics="provider_input_plus_cache_buckets",
                    source_kind="opencode_message",
                    source_path=str(path),
                    source_event_id=event_key,
                    capture_mode="sqlite_snapshot",
                    raw_usage_json=UsageEvent.json_usage({"table": "message", "tokens": tokens, "model": data.get("model") or data.get("modelID")}),
                ))
        session_ids.update(str(event.session_id) for event in events)
        return events, session_ids

    def _session_fallback_events(self, path: Path, con: sqlite3.Connection, represented: set[str]) -> list[UsageEvent]:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = "session_v2" if "session_v2" in tables else "session"
        if table not in tables:
            return []
        columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        required = {"id", "model", "time_updated", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write"}
        if not required.issubset(columns):
            return []
        events: list[UsageEvent] = []
        query = f'SELECT id, model, time_updated, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write FROM "{table}"'
        for session_id, model, updated, input_tokens, output_tokens, reasoning, cache_read, cache_write in con.execute(query):
            if str(session_id) in represented:
                continue
            input_tokens, output_tokens = as_int(input_tokens), as_int(output_tokens)
            reasoning, cache_read, cache_write = as_int(reasoning), as_int(cache_read), as_int(cache_write)
            if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
                continue
            event_key = f"opencode:{path}:session_fallback:{session_id}"
            total = input_tokens + output_tokens + cache_read + cache_write
            events.append(UsageEvent(
                event_key=event_key, app_type=self.key, model=self._model(model), session_id=str(session_id),
                request_id=event_key, occurred_at=epoch_from_any(updated), input_tokens=input_tokens,
                output_tokens=output_tokens, cache_read_tokens=cache_read, cache_creation_tokens=cache_write,
                reasoning_tokens=reasoning, total_tokens=total, input_token_semantics="session_cumulative_fallback",
                source_kind="opencode_session_fallback", source_path=str(path), source_event_id=event_key,
                capture_mode="sqlite_snapshot", raw_usage_json=UsageEvent.json_usage({"table": table, "session_id": session_id}),
            ))
        return events

    def sync(self, db) -> SyncResult:
        result = SyncResult(self.key)
        # Replace the old session aggregate rows with per-assistant-message rows.
        for path in self._paths():
            result.scanned += 1
            con = None
            try:
                con = self._connect(path)
                events, represented = self._message_events(path, con)
                events.extend(self._session_fallback_events(path, con, represented))
                result.imported += db.replace_source(str(path), events, self.key)
            except (OSError, sqlite3.Error) as exc:
                result.error = str(exc)
            finally:
                if con is not None:
                    con.close()
        return result
