from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models import UsageEvent
from ..utils import as_int, epoch_from_any, model_name, stable_hash
from .base import Adapter, SyncResult, read_jsonl_delta


class CodexAdapter(Adapter):
    key = "codex"
    label = "Codex"
    _semantics = "provider_total_input_dedup_v1"

    def _roots(self) -> list[Path]:
        roots: list[Path] = []
        configured = os.environ.get("CODEX_HOME")
        if configured:
            roots.append(Path(configured))
        roots.append(Path.home() / ".codex")
        runtime_root = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "WorkBuddy" / "chromium-env"
        if runtime_root.exists():
            for child in runtime_root.iterdir():
                roots.append(child / ".codex")
        return list(dict.fromkeys(roots))

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for root in self._roots():
            if root.exists():
                files.extend(root.glob("sessions/**/*.jsonl"))
                files.extend(root.glob("archived_sessions/*.jsonl"))
        return sorted(set(files), key=lambda p: str(p).lower())

    @staticmethod
    def _payload(obj: dict[str, Any]) -> dict[str, Any]:
        payload = obj.get("payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _model_before(path: Path, offset: int) -> str | None:
        model: str | None = None
        if offset <= 0:
            return None
        try:
            with path.open("rb") as handle:
                data = handle.read(offset)
        except OSError:
            return None
        for line in data.splitlines():
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if isinstance(payload, dict) and payload.get("model"):
                model = model_name(payload.get("model")) or model
        return model

    def _migrate_legacy(self, db) -> None:
        # v1.0 originally stored every repeated token_count offset. Remove those rows once,
        # then rebuild from cumulative totals so an unchanged token_count is ignored.
        with db.connection() as con:
            legacy = con.execute(
                "SELECT COUNT(*) FROM usage_event WHERE app_type = 'codex' "
                "AND input_token_semantics = 'provider_reported'"
            ).fetchone()[0]
        if legacy:
            db.delete_events(app_type="codex")
            db.delete_cursors("codex:")

    def sync(self, db) -> SyncResult:
        result = SyncResult(self.key)
        self._migrate_legacy(db)
        for path in self._files():
            result.scanned += 1
            cursor = db.get_cursor(f"codex:{path}")
            previous_offset = int(cursor["last_byte_offset"] if cursor else 0)
            model = self._model_before(path, previous_offset)
            entries = read_jsonl_delta(db, f"codex:{path}", "codex_session", path)
            last_total_signature: str | None = None
            events: list[UsageEvent] = []
            for offset, obj in entries:
                payload = self._payload(obj)
                payload_type = payload.get("type")
                if (obj.get("type") == "turn_context" or payload_type == "turn_context") and payload.get("model"):
                    model = model_name(payload.get("model")) or model
                if payload_type != "token_count":
                    continue
                info = payload.get("info") or {}
                usage = info.get("last_token_usage") or payload.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                total_usage = info.get("total_token_usage") or payload.get("total_token_usage") or {}
                if not isinstance(total_usage, dict):
                    total_usage = {}
                total_signature = stable_hash(total_usage) if total_usage else f"offset:{offset}"
                if total_signature == last_total_signature:
                    continue
                last_total_signature = total_signature
                input_tokens = as_int(usage.get("input_tokens"))
                output_tokens = as_int(usage.get("output_tokens"))
                cache_read = as_int(usage.get("cached_input_tokens"))
                cache_creation = as_int(usage.get("cache_write_input_tokens"))
                reasoning = as_int(usage.get("reasoning_output_tokens"))
                total_tokens = as_int(usage.get("total_tokens")) or input_tokens + output_tokens
                event_key = f"codex:{path}:total:{total_signature}"
                with db.connection() as con:
                    exists = con.execute("SELECT 1 FROM usage_event WHERE event_key=?", (event_key,)).fetchone()
                if exists:
                    continue
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=model,
                    session_id=path.stem.removeprefix("rollout-"),
                    request_id=event_key,
                    occurred_at=epoch_from_any(obj.get("timestamp") or payload.get("timestamp")),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_creation,
                    reasoning_tokens=reasoning,
                    total_tokens=total_tokens,
                    input_token_semantics=self._semantics,
                    source_kind="codex_session",
                    source_path=str(path),
                    source_offset=offset,
                    source_event_id=event_key,
                    capture_mode="file_jsonl",
                    raw_usage_json=UsageEvent.json_usage({"last": usage, "total": total_usage}),
                ))
            result.imported += db.upsert_events(events)
        return result
