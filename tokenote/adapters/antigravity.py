"""Read Antigravity per-generation usage from its conversation SQLite databases.

Antigravity stores each cascade as ``<conversation-id>.db`` with ``steps``
(metadata is protobuf) and ``gen_metadata`` (model and request metadata). The
model response step carries a usage message, and its generation index points at
the matching ``gen_metadata`` row.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import UsageDatabase
from ..models import UsageEvent
from ..utils import utc_now
from .base import Adapter, SyncResult


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise ValueError("protobuf varint out of range")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _fields(data: bytes) -> list[tuple[int, int, Any]]:
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
            if len(value) != 8:
                raise ValueError("truncated protobuf fixed64")
        elif wire == 2:
            length, offset = _varint(data, offset)
            value = data[offset:offset + length]
            offset += length
            if len(value) != length:
                raise ValueError("truncated protobuf bytes")
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
            if len(value) != 4:
                raise ValueError("truncated protobuf fixed32")
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _first(fields: list[tuple[int, int, Any]], number: int, wire: int = 2) -> Any:
    for field, kind, value in fields:
        if field == number and kind == wire:
            return value
    return None


def _numbers(fields: list[tuple[int, int, Any]]) -> dict[int, int]:
    return {field: value for field, kind, value in fields if kind == 0}


def _text(value: bytes | None) -> str | None:
    if not value:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _printable(value: bytes | None) -> str | None:
    text = _text(value)
    if not text or any(not 0x20 <= ord(char) < 0x7F for char in text):
        return None
    return text


@dataclass(slots=True)
class _StepUsage:
    occurred_at: int | None
    generation: int | None
    fresh: int
    cache_read: int
    output: int
    reasoning: int
    visible: int
    request_key: str | None


def _step_usage(index: int, metadata: bytes | None) -> _StepUsage | None:
    if not metadata:
        return None
    fields = _fields(metadata)
    usage_data = _first(fields, 9)
    if not usage_data:
        return None
    usage = _numbers(_fields(usage_data))
    occurred_at = None
    stamp = _first(fields, 1)
    if stamp:
        seconds = _numbers(_fields(stamp)).get(1)
        if seconds and seconds > 0:
            occurred_at = seconds
    generation = None
    request_key = None
    context = _first(fields, 20)
    if context:
        context_fields = _fields(context)
        numbers = _numbers(context_fields)
        # Field 2 echoes the step index; field 3 is the generation index and is
        # omitted for generation 0. Field 1 is the trajectory id.
        if numbers.get(2) == index:
            generation = numbers.get(3, 0)
        request_key = _printable(_first(context_fields, 1))
    return _StepUsage(
        occurred_at=occurred_at,
        generation=generation,
        fresh=usage.get(2, 0),
        cache_read=usage.get(5, 0),
        output=usage.get(3, 0),
        reasoning=usage.get(9, 0),
        visible=usage.get(10, 0),
        request_key=request_key,
    )


def _generation_models(connection: sqlite3.Connection) -> dict[int, str]:
    models: dict[int, str] = {}
    for index, data in connection.execute("SELECT idx, data FROM gen_metadata"):
        if not isinstance(data, bytes):
            continue
        try:
            fields = _fields(data)
            detail = _first(fields, 1)
            if not detail:
                continue
            model = _text(_first(_fields(detail), 19))
        except ValueError:
            continue
        if model:
            models[int(index)] = model
    return models


class AntigravityAdapter(Adapter):
    key = "antigravity"
    label = "Antigravity"
    semantics = "antigravity_exclusive_cache_v1"
    source_kind = "antigravity_conversation"

    def _conversation_dbs(self) -> list[Path]:
        configured = os.environ.get("TOKENOTE_ANTIGRAVITY_DIR") or os.environ.get("ANTIGRAVITY_HOME")
        if configured:
            roots = [Path(configured).expanduser()]
        else:
            roots = [Path.home() / ".gemini" / name
                     for name in ("antigravity", "antigravity-ide", "antigravity-cli")]
        files: list[Path] = []
        for root in roots:
            folder = root if root.name == "conversations" else root / "conversations"
            if folder.is_dir():
                files.extend(sorted(folder.glob("*.db"), key=lambda path: str(path).lower()))
        return list(dict.fromkeys(path.resolve() for path in files if path.is_file()))

    @staticmethod
    def _stat_identity(path: Path) -> tuple[str, int]:
        parts: list[str] = []
        modified = 0
        for candidate in (path, path.with_name(path.name + "-wal")):
            try:
                stat = candidate.stat()
            except OSError:
                parts.append("-")
            else:
                parts.append(f"{stat.st_size}:{stat.st_mtime_ns}")
                modified = max(modified, stat.st_mtime_ns)
        return "|".join(parts), modified

    def _read_events(self, path: Path) -> list[UsageEvent]:
        conversation = path.stem
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            models = _generation_models(connection)
            rows = connection.execute(
                "SELECT idx, metadata FROM steps WHERE step_type = 15 ORDER BY idx"
            ).fetchall()
        events: list[UsageEvent] = []
        for index, metadata in rows:
            try:
                usage = _step_usage(int(index), metadata)
            except ValueError:
                continue
            if usage is None or not any((usage.fresh, usage.cache_read, usage.output)):
                continue
            output = max(usage.output, usage.reasoning + usage.visible)
            request_id = usage.request_key or f"{conversation}:{index}"
            model = models.get(usage.generation) if usage.generation is not None else None
            event_key = f"antigravity:{conversation}:{index}"
            events.append(UsageEvent(
                event_key=event_key,
                app_type=self.key,
                model=model,
                session_id=conversation,
                turn_id=str(index),
                request_id=request_id,
                occurred_at=usage.occurred_at,
                input_tokens=usage.fresh,
                output_tokens=output,
                cache_read_tokens=usage.cache_read,
                reasoning_tokens=usage.reasoning,
                total_tokens=usage.fresh + usage.cache_read + output,
                input_token_semantics=self.semantics,
                source_kind=self.source_kind,
                source_path=str(path),
                source_offset=index,
                source_event_id=event_key,
                capture_mode="sqlite_snapshot",
                raw_usage_json=UsageEvent.json_usage({
                    "input_tokens": usage.fresh,
                    "cached_input_tokens": usage.cache_read,
                    "output_tokens": output,
                    "reasoning_tokens": usage.reasoning,
                    "visible_output_tokens": usage.visible,
                    "generation": usage.generation,
                }),
            ))
        return events

    def sync(self, db: UsageDatabase) -> SyncResult:
        paths = self._conversation_dbs()
        result = SyncResult(self.key, scanned=len(paths), availability="ready" if paths else "not_found")
        errors: list[str] = []
        for path in paths:
            try:
                identity, modified = self._stat_identity(path)
                source_id = f"antigravity:{path}"
                cursor = db.get_cursor(source_id)
                if (cursor and cursor["file_identity"] == identity
                        and cursor["last_logical_cursor"] == self.semantics):
                    result.skipped += 1
                    continue
                events = self._read_events(path)
                result.imported += db.upsert_events(events)
                db.save_cursor({
                    "source_id": source_id,
                    "source_kind": self.source_kind,
                    "file_path": str(path),
                    "file_identity": identity,
                    "last_byte_offset": path.stat().st_size,
                    "last_line_offset": 0,
                    "last_modified": modified,
                    "last_tail_fingerprint": None,
                    "last_logical_cursor": self.semantics,
                    "updated_at": utc_now(),
                })
            except (OSError, sqlite3.Error, ValueError) as exc:
                errors.append(f"{path.name}：{exc}")
        if errors:
            result.error = "；".join(errors)
        return result
