"""Read Cursor composer usage from its local globalStorage database.

Cursor's built-in models are billed by the backend, and current builds persist
bubble tokenCount fields as zeros. A present database therefore does not imply
that usable token counts exist locally; the UI reports that state explicitly.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .base import Adapter, SyncResult
from ..models import UsageEvent
from ..utils import as_int, epoch_from_any


def cursor_state_dbs() -> list[Path]:
    override = os.environ.get("TOKENOTE_CURSOR_DB")
    if override:
        candidates = [Path(override).expanduser()]
    else:
        roots = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "Cursor")
        config_home = os.environ.get("XDG_CONFIG_HOME")
        roots.extend([
            Path.home() / "Library" / "Application Support" / "Cursor",
            (Path(config_home).expanduser() if config_home else Path.home() / ".config") / "Cursor",
        ])
        candidates = []
        for root in roots:
            candidates.append(root / "User" / "globalStorage" / "state.vscdb")
            profiles = root / "User" / "profiles"
            if profiles.is_dir():
                candidates.extend(sorted(profiles.glob("*/globalStorage/state.vscdb")))
    return [path for path in dict.fromkeys(candidates) if path.is_file()]


def cursor_state_db() -> Path | None:
    paths = cursor_state_dbs()
    return paths[0] if paths else None


class CursorAdapter(Adapter):
    key = "cursor"
    label = "Cursor"

    def _state_dbs(self) -> list[Path]:
        return cursor_state_dbs()

    def sync(self, db) -> SyncResult:
        paths = self._state_dbs()
        result = SyncResult(self.key, scanned=len(paths), availability="ready" if paths else "not_found")
        for path in paths:
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as con:
                    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "cursorDiskKV" not in tables:
                        result.error = "已找到 state.vscdb，但缺少 cursorDiskKV 表"
                        continue
                    composers = self._composers(con)
                    events, skipped = self._events(con, composers, path)
                result.imported += db.replace_source(str(path), events, self.key)
                result.skipped += skipped
            except (OSError, sqlite3.Error, ValueError) as exc:
                result.error = str(exc)
        return result

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any] | None:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if not isinstance(value, str):
            return None
        try:
            obj = json.loads(value)
        except (TypeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def _composers(self, con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        composers = {}
        for key, value in con.execute("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            composer_id = key.partition(":")[2]
            data = self._json_object(value)
            if composer_id and data is not None:
                composers[composer_id] = data
        return composers

    def _events(self, con: sqlite3.Connection, composers: dict[str, dict[str, Any]],
                path: Path) -> tuple[list[UsageEvent], int]:
        events = []
        skipped = 0
        for key, value in con.execute("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            parts = key.split(":", 2)
            data = self._json_object(value)
            if len(parts) != 3 or data is None:
                skipped += 1
                continue
            _, composer_id, bubble_id = parts
            # User bubbles can mirror the prompt; only assistant bubbles represent
            # model responses, so one turn is not recorded twice.
            if as_int(data.get("type")) != 2:
                skipped += 1
                continue
            counts = data.get("tokenCount")
            if not isinstance(counts, dict):
                skipped += 1
                continue
            fresh, output = as_int(counts.get("inputTokens")), as_int(counts.get("outputTokens"))
            if fresh <= 0 and output <= 0:
                skipped += 1
                continue
            composer = composers.get(composer_id) or {}
            bubble_model = data.get("modelInfo")
            model = None
            if isinstance(bubble_model, dict):
                model = bubble_model.get("modelName") or bubble_model.get("modelId")
            model = model or self._composer_model(composer)
            occurred_at = epoch_from_any(
                data.get("createdAt") or data.get("startedAtMs")
                or composer.get("lastUpdatedAt") or composer.get("createdAt")
            )
            event_key = f"cursor:{path}:{composer_id}:{bubble_id}"
            events.append(UsageEvent(
                event_key=event_key, app_type=self.key, model=model, session_id=composer_id,
                turn_id=bubble_id, request_id=str(data.get("requestId") or bubble_id), occurred_at=occurred_at,
                input_tokens=fresh, output_tokens=output, total_tokens=fresh + output,
                input_token_semantics="cursor_reported_tokens", source_kind="cursor_state_db",
                source_path=str(path), source_event_id=event_key, capture_mode="sqlite_snapshot",
                raw_usage_json=UsageEvent.json_usage({"inputTokens": fresh, "outputTokens": output}),
            ))
        return events, skipped

    @staticmethod
    def _composer_model(composer: dict[str, Any]) -> str | None:
        config = composer.get("modelConfig")
        if not isinstance(config, dict):
            return None
        names = [config.get("modelName")]
        for item in config.get("selectedModels") or []:
            if isinstance(item, dict):
                names.append(item.get("modelId") or item.get("modelName"))
        for name in names:
            if isinstance(name, str) and name.strip() and name.strip().lower() != "default":
                return name.strip()
        return None
