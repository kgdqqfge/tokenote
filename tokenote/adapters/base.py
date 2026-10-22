from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ..db import UsageDatabase
from ..utils import fingerprint, utc_now


@dataclass(slots=True)
class SyncResult:
    adapter: str
    imported: int = 0
    updated: int = 0
    scanned: int = 0
    skipped: int = 0
    error: str | None = None
    availability: str = "ready"


class Adapter:
    key = "unknown"
    label = "未知来源"

    def sync(self, db: UsageDatabase) -> SyncResult:
        raise NotImplementedError

    @staticmethod
    def save_file_cursor(db: UsageDatabase, source_id: str, source_kind: str, path: Path,
                         offset: int, line_offset: int, previous: Any = None,
                         logical_cursor: str | None = None) -> None:
        try:
            stat = path.stat()
            with path.open("rb") as handle:
                handle.seek(max(0, stat.st_size - 256))
                tail = handle.read()
            identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}" if hasattr(stat, "st_ino") else f"{stat.st_size}"
            db.save_cursor({
                "source_id": source_id,
                "source_kind": source_kind,
                "file_path": str(path),
                "file_identity": identity,
                "last_byte_offset": offset,
                "last_line_offset": line_offset,
                "last_modified": stat.st_mtime_ns,
                "last_tail_fingerprint": fingerprint(tail),
                "last_logical_cursor": logical_cursor,
                "updated_at": utc_now(),
            })
        except OSError:
            pass


def read_jsonl_delta(db: UsageDatabase, source_id: str, source_kind: str, path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Read only complete appended JSONL lines and persist the cursor."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cursor = db.get_cursor(source_id)
    offset = int(cursor["last_byte_offset"] if cursor else 0)
    line_offset = int(cursor["last_line_offset"] if cursor else 0)
    if offset > size:
        offset, line_offset = 0, 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    if not data:
        Adapter.save_file_cursor(db, source_id, source_kind, path, offset, line_offset, cursor)
        return []
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return []
    complete = data[: last_newline + 1]
    entries: list[tuple[int, dict[str, Any]]] = []
    cursor_pos = offset
    for raw_line in complete.splitlines(keepends=True):
        start = cursor_pos
        cursor_pos += len(raw_line)
        text = raw_line.strip()
        if not text:
            line_offset += 1
            continue
        try:
            obj = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            line_offset += 1
            continue
        if isinstance(obj, dict):
            entries.append((start, obj))
        line_offset += 1
    Adapter.save_file_cursor(db, source_id, source_kind, path, cursor_pos, line_offset, cursor)
    return entries
