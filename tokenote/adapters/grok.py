"""Read Grok Build turn usage from its local session update log."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models import UsageEvent
from ..utils import as_int, epoch_from_any
from .base import Adapter, SyncResult, read_jsonl_delta


def _value(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


class GrokAdapter(Adapter):
    key = "grok"
    label = "Grok Build"

    def _root(self) -> Path:
        configured = os.environ.get("TOKENOTE_GROK_DIR") or os.environ.get("GROK_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".grok"

    def _sessions_root(self) -> Path:
        return self._root() / "sessions"

    def _update_files(self) -> list[Path]:
        root = self._sessions_root()
        if not root.is_dir():
            return []
        return sorted(root.glob("*/*/updates.jsonl"), key=lambda path: str(path).lower())

    def _subagent_ids(self) -> set[str]:
        root = self._sessions_root()
        ids: set[str] = set()
        if not root.is_dir():
            return ids
        for folder in root.glob("*/*/subagents/*"):
            if folder.is_dir():
                ids.add(folder.name)
        for meta_path in root.glob("*/*/subagents/*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            child = _value(meta, "child_session_id", "childSessionId") if isinstance(meta, dict) else None
            if child:
                ids.add(str(child))
        return ids

    @staticmethod
    def _session_model(session_dir: Path) -> str | None:
        try:
            summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        if not isinstance(summary, dict):
            return None
        model = _value(summary, "current_model_id", "currentModelId")
        return str(model) if model else None

    @staticmethod
    def _usage_rows(usage: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
        model_usage = usage.get("modelUsage")
        if isinstance(model_usage, dict):
            rows = [(str(key), value) for key, value in model_usage.items() if isinstance(value, dict)]
            if rows:
                return rows
        return [(None, usage)]

    def sync(self, db) -> SyncResult:
        root = self._sessions_root()
        result = SyncResult(self.key, availability="ready" if root.is_dir() else "not_found")
        if not root.is_dir():
            return result
        child_ids = self._subagent_ids()
        model_cache: dict[Path, str | None] = {}
        for path in self._update_files():
            result.scanned += 1
            if path.parent.name in child_ids:
                result.skipped += 1
                continue
            entries = read_jsonl_delta(db, f"grok:{path}", "grok_session_update", path)
            events: list[UsageEvent] = []
            for offset, obj in entries:
                params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                if update.get("sessionUpdate") != "turn_completed":
                    continue
                usage = update.get("usage")
                if not isinstance(usage, dict):
                    continue
                session_id = str(params.get("sessionId") or path.parent.name)
                prompt_id = str(_value(update, "prompt_id", "promptId") or "")
                meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                occurred_at = epoch_from_any(meta.get("agentTimestampMs") or obj.get("timestamp"))
                model_usage = usage.get("modelUsage")
                has_model_rows = isinstance(model_usage, dict) and any(
                    isinstance(value, dict) for value in model_usage.values())
                fallback_model = None
                if not has_model_rows:
                    if path.parent not in model_cache:
                        model_cache[path.parent] = self._session_model(path.parent)
                    fallback_model = model_cache[path.parent]
                for model_row, row in self._usage_rows(usage):
                    input_tokens = as_int(_value(row, "inputTokens", "input_tokens", "input"))
                    output_tokens = as_int(_value(row, "outputTokens", "output_tokens", "output"))
                    cache_read = as_int(_value(row, "cachedReadTokens", "cache_read_input_tokens", "cache_read_tokens"))
                    cache_creation = as_int(_value(row, "cacheCreationTokens", "cache_creation_input_tokens"))
                    reasoning = as_int(_value(row, "reasoningTokens", "reasoning_tokens"))
                    total = as_int(_value(row, "totalTokens", "total_tokens"))
                    if total <= 0:
                        total = input_tokens + output_tokens
                    if not any((input_tokens, output_tokens, cache_read, cache_creation, total)):
                        continue
                    model = model_row or fallback_model
                    turn_key = prompt_id or str(meta.get("eventId") or offset)
                    event_key = f"grok:{session_id}:{turn_key}:{model or 'unknown'}"
                    events.append(UsageEvent(
                        event_key=event_key,
                        app_type=self.key,
                        model=model,
                        session_id=session_id,
                        turn_id=prompt_id or None,
                        request_id=prompt_id or None,
                        occurred_at=occurred_at,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        cache_creation_tokens=cache_creation,
                        reasoning_tokens=reasoning,
                        total_tokens=total,
                        input_token_semantics="grok_inclusive_input",
                        source_kind="grok_session_update",
                        source_path=str(path),
                        source_offset=offset,
                        source_event_id=str(meta.get("eventId") or event_key),
                        capture_mode="file_jsonl",
                        raw_usage_json=UsageEvent.json_usage(row),
                    ))
            result.imported += db.upsert_events(events)
        return result
