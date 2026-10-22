from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models import UsageEvent
from ..utils import as_int, deep_get, epoch_from_any, find_key, model_name, stable_hash
from .base import Adapter, SyncResult, read_jsonl_delta


class WorkBuddyAdapter(Adapter):
    key = "workbuddy"
    label = "WorkBuddy"

    def _roots(self) -> list[Path]:
        home = Path.home()
        return [home / ".workbuddy" / "projects", home / ".workbuddy-ai" / "projects"]

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for root in self._roots():
            if root.exists():
                files.extend(root.rglob("*.jsonl"))
        return sorted(set(files), key=lambda p: str(p).lower())

    @staticmethod
    def _usage(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        for path, kind in [
            (["providerData", "usage"], "providerData.usage"),
            (["usage"], "usage"),
            (["message", "usage"], "message.usage"),
        ]:
            value = deep_get(obj, path)
            if isinstance(value, dict) and any(k in value for k in ("inputTokens", "outputTokens", "input_tokens", "output_tokens")):
                return value, kind
        raw = deep_get(obj, ["providerData", "rawUsage"])
        if isinstance(raw, dict) and any("token" in str(k).lower() for k in raw):
            return raw, "providerData.rawUsage"
        return None, ""

    @staticmethod
    def _cached(usage: dict[str, Any]) -> int:
        details = usage.get("inputTokensDetails") or usage.get("input_tokens_details") or []
        if isinstance(details, dict):
            details = [details]
        total = 0
        for item in details if isinstance(details, list) else []:
            if isinstance(item, dict):
                total += as_int(item.get("cached_tokens") or item.get("cachedTokens"))
        return total or as_int(usage.get("cached_tokens") or usage.get("cache_read_input_tokens"))

    def sync(self, db) -> SyncResult:
        result = SyncResult(self.key)
        for path in self._files():
            result.scanned += 1
            entries = read_jsonl_delta(db, f"workbuddy:{path}", "workbuddy_jsonl", path)
            events: list[UsageEvent] = []
            for offset, obj in entries:
                usage, usage_path = self._usage(obj)
                if not usage:
                    continue
                input_tokens = as_int(usage.get("inputTokens") or usage.get("input_tokens") or usage.get("prompt_tokens"))
                output_tokens = as_int(usage.get("outputTokens") or usage.get("output_tokens") or usage.get("completion_tokens"))
                cache_read = self._cached(usage)
                cache_creation = as_int(usage.get("cache_creation_input_tokens") or usage.get("cacheCreationTokens"))
                reasoning = as_int(usage.get("reasoningTokens") or usage.get("reasoning_tokens"))
                total = as_int(usage.get("totalTokens") or usage.get("total_tokens")) or input_tokens + output_tokens
                event_key = f"workbuddy:{path}:{offset}"
                model = model_name(deep_get(obj, ["providerData", "model"])) or model_name(obj.get("model")) or model_name(find_key(obj, {"modelId", "modelID", "model_name"}))
                session = find_key(obj, {"sessionId", "session_id", "conversationId", "conversation_id"})
                request = find_key(obj, {"requestId", "request_id", "messageId", "message_id", "id"})
                occurred_at = epoch_from_any(find_key(obj, {"createdAt", "created_at", "timestamp", "time"}))
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=model,
                    session_id=str(session) if session else path.stem,
                    request_id=str(request) if request else event_key,
                    occurred_at=occurred_at,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_creation,
                    reasoning_tokens=reasoning,
                    total_tokens=total,
                    input_token_semantics="provider_reported",
                    source_kind="workbuddy_jsonl",
                    source_path=str(path),
                    source_offset=offset,
                    source_event_id=event_key,
                    capture_mode="file_jsonl",
                    raw_usage_json=UsageEvent.json_usage({"path": usage_path, "usage": usage}),
                ))
            result.imported += db.upsert_events(events)
        return result
