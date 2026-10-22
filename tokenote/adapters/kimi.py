from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..models import UsageEvent
from ..utils import as_int, deep_get, epoch_from_any, find_key, model_name
from .base import Adapter, SyncResult, read_jsonl_delta


class KimiAdapter(Adapter):
    key = "kimi"
    label = "Kimi Code"

    def _root(self) -> Path:
        configured = os.environ.get("KIMI_CODE_HOME")
        return Path(configured) if configured else Path.home() / ".kimi-code"

    def _wire_files(self) -> list[Path]:
        root = self._root()
        return sorted(root.glob("sessions/**/*.jsonl"), key=lambda p: str(p).lower()) if root.exists() else []

    def _server_files(self) -> list[Path]:
        root = self._root() / "server" / "events"
        return sorted(root.glob("session_*.jsonl"), key=lambda p: str(p).lower()) if root.exists() else []

    @staticmethod
    def _session_id(path: Path) -> str:
        match = re.search(r"session_([0-9a-f-]{20,})", str(path), re.I)
        return match.group(1) if match else path.stem

    @staticmethod
    def _state_model(path: Path) -> str | None:
        for parent in [path.parent, *path.parents]:
            state = parent / "state.json"
            if not state.exists():
                continue
            try:
                data = json.loads(state.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            value = find_key(data, {"model", "model_id", "modelID"})
            resolved = model_name(value)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _root_type(obj: dict[str, Any]) -> str | None:
        if isinstance(obj.get("type"), str):
            return obj["type"]
        envelope = obj.get("envelope")
        if isinstance(envelope, dict):
            payload = envelope.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("type"), str):
                return payload["type"]
        return None

    @staticmethod
    def _usage(obj: dict[str, Any], event_type: str) -> dict[str, Any] | None:
        if event_type == "usage.record":
            value = obj.get("usage")
            return value if isinstance(value, dict) else None
        if event_type == "turn.step.completed":
            payload = deep_get(obj, ["envelope", "payload"])
            value = payload.get("usage") if isinstance(payload, dict) else None
            return value if isinstance(value, dict) else None
        return None

    def _sync_files(self, db, files: list[Path], source_kind: str, result: SyncResult) -> None:
        for path in files:
            result.scanned += 1
            entries = read_jsonl_delta(db, f"kimi:{path}", source_kind, path)
            events: list[UsageEvent] = []
            state_model = self._state_model(path)
            for offset, obj in entries:
                event_type = self._root_type(obj)
                usage = self._usage(obj, event_type or "")
                if not usage:
                    continue
                model = model_name(obj.get("model")) or state_model
                input_other = as_int(usage.get("inputOther") or usage.get("input_other"))
                output = as_int(usage.get("output"))
                cache_read = as_int(usage.get("inputCacheRead") or usage.get("input_cache_read"))
                cache_creation = as_int(usage.get("inputCacheCreation") or usage.get("input_cache_creation"))
                total = as_int(usage.get("total") or usage.get("totalTokens")) or input_other + output + cache_read + cache_creation
                event_key = f"kimi:{path}:{offset}"
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=model,
                    session_id=self._session_id(path),
                    request_id=event_key,
                    occurred_at=epoch_from_any(find_key(obj, {"createdAt", "created_at", "timestamp", "time"})),
                    input_tokens=input_other + cache_read + cache_creation,
                    output_tokens=output,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_creation,
                    total_tokens=total,
                    input_token_semantics="provider_buckets_v2",
                    source_kind=source_kind,
                    source_path=str(path),
                    source_offset=offset,
                    source_event_id=event_key,
                    capture_mode="file_jsonl",
                    raw_usage_json=UsageEvent.json_usage(usage),
                ))
            result.imported += db.upsert_events(events)

    def sync(self, db) -> SyncResult:
        result = SyncResult(self.key)
        with db.connection() as con:
            con.execute("UPDATE usage_event SET input_tokens=input_tokens+cache_creation_tokens, total_tokens=input_tokens+cache_creation_tokens+output_tokens, input_token_semantics='provider_buckets_v2' WHERE app_type='kimi' AND input_token_semantics='provider_buckets'")
        wire = self._wire_files()
        self._sync_files(db, wire, "kimi_wire", result)
        wire_sessions = {self._session_id(p) for p in wire}
        server = [p for p in self._server_files() if self._session_id(p) not in wire_sessions]
        self._sync_files(db, server, "kimi_server_event", result)
        return result
