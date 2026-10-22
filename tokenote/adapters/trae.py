from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR
from ..models import UsageEvent
from ..preferences import get as preference
from ..trae_extractor import (
    default_db_path,
    extract_key_from_memory,
    find_trae_pid,
    has_cryptography,
    read_request_records,
    verify_key,
)
from ..utils import as_int, epoch_from_any, stable_hash
from .base import Adapter, SyncResult


class TraeAdapter(Adapter):
    key = "trae"
    label = "Trae CN"

    def __init__(self) -> None:
        configured = os.environ.get("TOKENOTE_TRAE_DB")
        self.db_path = Path(configured) if configured else default_db_path()

    @property
    def key_file(self) -> Path:
        return DATA_DIR / "trae.key"

    def cached_key_files(self) -> list[Path]:
        candidates = [
            Path(tempfile.gettempdir()) / "trae_key.bin",
            self.key_file,
        ]
        configured = os.environ.get("TOKENOTE_TRAE_KEY")
        if configured:
            candidates.insert(0, Path(configured))
        return list(dict.fromkeys(path for path in candidates
                                  if path.exists() and path.stat().st_size == 32))

    def _cached_key(self) -> bytes | None:
        for path in self.cached_key_files():
            key = path.read_bytes()
            if verify_key(self.db_path, key):
                return key
        return None

    @staticmethod
    def _memory_consent() -> bool:
        return bool(preference("trae_memory_consent", not getattr(sys, "frozen", False)))

    def sync(self, db) -> SyncResult:
        result = SyncResult(self.key)
        if not self.db_path.exists():
            result.availability = "not_found"
            return result
        if not has_cryptography():
            result.error = "缺少 cryptography，无法读取 Trae 用量"
            return result

        result.scanned = 1
        key = self._cached_key()
        if key is None:
            if not self._memory_consent():
                result.error = "尚未确认读取 Trae 密钥的提示"
                return result
            if find_trae_pid() is None:
                result.error = "Trae CN 未运行，无法获取数据库密钥"
                return result
            try:
                key = extract_key_from_memory(self.db_path)
            except RuntimeError as exc:
                result.error = str(exc)
                return result
            if key is None:
                result.error = "未能在 Trae 进程中找到数据库密钥"
                return result
            try:
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                self.key_file.write_bytes(key)
            except OSError:
                pass

        try:
            records = read_request_records(self.db_path, key)
            events: list[UsageEvent] = []
            for record in records:
                occurred = record.get("time")
                try:
                    occurred_at = int(datetime.fromisoformat(str(occurred)).timestamp()) if occurred else None
                except ValueError:
                    occurred_at = epoch_from_any(occurred)
                signature = {
                    "time": occurred,
                    "model": record.get("model"),
                    "prompt": record.get("prompt_tokens"),
                    "output": record.get("output_tokens"),
                    "cache": record.get("cache_read_tokens"),
                    "total": record.get("total_tokens"),
                    "workspace": record.get("workspace"),
                }
                event_key = f"trae:{stable_hash(signature)}"
                events.append(UsageEvent(
                    event_key=event_key,
                    app_type=self.key,
                    model=record.get("model"),
                    session_id=record.get("workspace"),
                    occurred_at=occurred_at,
                    input_tokens=as_int(record.get("prompt_tokens")),
                    output_tokens=as_int(record.get("output_tokens")),
                    cache_read_tokens=as_int(record.get("cache_read_tokens")),
                    cache_creation_tokens=as_int(record.get("cache_write_tokens")),
                    reasoning_tokens=as_int(record.get("reasoning_tokens")),
                    total_tokens=as_int(record.get("total_tokens")),
                    input_token_semantics=record.get("semantics") or "provider_raw_usage",
                    source_kind="trae_server_history",
                    source_path=str(self.db_path),
                    source_event_id=event_key,
                    capture_mode="sqlcipher_extractor",
                    raw_usage_json=UsageEvent.json_usage(record),
                ))
            result.imported += db.upsert_events(events)
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
            result.error = str(exc)
        return result
