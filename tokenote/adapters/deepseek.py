from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator

from ..db import UsageDatabase
from ..models import UsageEvent
from ..utils import as_int, epoch_from_any, model_name, stable_hash, utc_now
from .base import Adapter, SyncResult


class TranscriptReader:
    """Read a fixed snapshot, publishing only complete JSONL lines/Zstd frames."""

    def __init__(self, path: Path, size: int):
        self.path = path
        self.size = size
        self.consumed = 0

    def batches(self) -> Iterator[bytes]:
        if self.path.suffix != ".zstd":
            with self.path.open("rb") as handle:
                while handle.tell() < self.size:
                    line = handle.readline(self.size - handle.tell())
                    if not line.endswith(b"\n"):
                        return
                    self.consumed = handle.tell()
                    yield line
            return
        try:
            from compression import zstd
        except ImportError:
            try:
                from backports import zstd
            except ImportError as exc:
                raise RuntimeError("读取 Harness 压缩日志需要 Python 3.14 或 backports.zstd") from exc
        with self.path.open("rb") as handle:
            pending = b""
            while pending or handle.tell() < self.size:
                decoder = zstd.ZstdDecompressor()
                parts: list[bytes] = []
                while not decoder.eof:
                    if not pending:
                        pending = handle.read(min(65536, self.size - handle.tell()))
                        if not pending:
                            return  # An active writer may not have finished the last frame.
                    parts.append(decoder.decompress(pending))
                    pending = decoder.unused_data if decoder.eof else b""
                frame = b"".join(parts)
                if not frame.endswith(b"\n"):
                    raise ValueError("Harness 压缩帧缺少完整的 JSONL 行")
                self.consumed = handle.tell() - len(pending)
                yield frame

    def rows(self) -> Iterator[dict]:
        for batch in self.batches():
            for line in batch.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("Harness 日志记录必须为 JSON 对象")
                yield obj


class DeepSeekHarnessAdapter(Adapter):
    key = "deepseek_harness"
    label = "DeepSeek Harness"
    source_kind = "deepseek_harness_session"
    semantics = "deepseek_harness_inclusive_input_v1"

    # The CLI writes session.jsonl(.zstd); the desktop app versions the name (session.v4.jsonl.zstd).
    TRANSCRIPT_NAME = re.compile(r"session(?:\.\w+)?\.jsonl(?:\.zstd)?")

    def _session_roots(self) -> list[Path]:
        roots = [Path.home() / ".dsh" / "sessions"]
        if os.environ.get("DSH_HOME"):
            roots.insert(0, Path(os.environ["DSH_HOME"]).expanduser() / "sessions")
        configured = os.environ.get("TOKENOTE_DEEPSEEK_HARNESS_DIR")
        if configured:
            root = Path(configured).expanduser()
            roots.insert(0, root / "sessions" if (root / "sessions").is_dir() else root)
        return list(dict.fromkeys(root.resolve() for root in roots))

    @staticmethod
    def _logical_path(path: Path) -> Path:
        # Compression changes the physical filename, not the logical transcript.
        return path.with_suffix("") if path.suffix == ".zstd" else path

    def _files(self) -> list[Path]:
        files: dict[Path, Path] = {}
        for root in self._session_roots():
            if not root.is_dir():
                continue
            for pattern in ("**/session*.jsonl", "**/session*.jsonl.zstd"):
                for path in root.glob(pattern):
                    if not self.TRANSCRIPT_NAME.fullmatch(path.name):
                        continue
                    path = path.resolve()
                    logical = self._logical_path(path)
                    previous = files.get(logical)
                    if previous is None or path.stat().st_mtime_ns >= previous.stat().st_mtime_ns:
                        files[logical] = path
        return sorted(files.values(), key=lambda path: str(path).lower())

    def _parse(self, rows: Iterator[dict], path: Path) -> list[UsageEvent]:
        header = next(rows, None)
        if header is None:
            return []
        if header.get("type") != "session" or not isinstance(header.get("id"), str) or not header["id"]:
            raise ValueError("Harness 会话头缺失或无效")
        session_id = header["id"]
        seed_length = max(0, as_int(header.get("seedLength")))
        model = model_name(header.get("model"))
        events: dict[tuple[int, int], UsageEvent] = {}
        logical_path = str(self._logical_path(path))
        for row in rows:
            kind, data = row.get("type"), row.get("data")
            if not isinstance(data, dict):
                continue  # Packed text/reasoning/tool-call deltas do not contain usage.
            if kind == "request/header":
                request_header = data.get("header")
                config = request_header.get("config") if isinstance(request_header, dict) else None
                if isinstance(config, dict):
                    model = model_name(config.get("model")) or model
            elif kind == "request/context":
                model = model_name(data.get("model")) or model
            usage = None
            if kind == "assistant/message":
                usage = data.get("usage")
            elif kind == "assistant/chunk":
                chunk = data.get("chunk")
                if isinstance(chunk, dict) and chunk.get("type") == "usage":
                    usage = chunk.get("usage")
            if not isinstance(usage, dict) or "inputTokens" not in usage or "outputTokens" not in usage:
                continue
            # Forks copy historical events. Resume markers alone do not imply a fork.
            if seed_length and as_int(row.get("seq")) < seed_length:
                continue
            turn, step = data.get("turn"), data.get("step")
            if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (turn, step)):
                raise ValueError("Harness 用量记录缺少有效的 turn/step")
            counts = {name: max(0, as_int(usage.get(name))) for name in
                      ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")}
            # Harness input is fresh-only, while output already includes reasoning.
            input_total = counts["inputTokens"] + counts["cacheReadTokens"] + counts["cacheWriteTokens"]
            total = input_total + counts["outputTokens"]
            identity = (turn, step)
            previous = events.get(identity)
            event_key = f"deepseek-harness:{stable_hash([logical_path, session_id, turn, step])}"
            events[identity] = UsageEvent(
                event_key=event_key, app_type=self.key, model=model,
                session_id=session_id, turn_id=str(turn), request_id=event_key,
                occurred_at=(previous.occurred_at if previous else None) or epoch_from_any(row.get("time")),
                input_tokens=input_total, output_tokens=counts["outputTokens"],
                cache_read_tokens=counts["cacheReadTokens"], cache_creation_tokens=counts["cacheWriteTokens"],
                reasoning_tokens=counts["reasoningTokens"], total_tokens=total,
                input_token_semantics=self.semantics, source_kind=self.source_kind,
                source_path=logical_path, source_offset=previous.source_offset if previous else as_int(row.get("seq")),
                source_event_id=event_key, capture_mode="compressed_jsonl_snapshot" if path.suffix == ".zstd" else "jsonl_snapshot",
                raw_usage_json=UsageEvent.json_usage({"usage": counts, "storage_path": str(path)}),
            )
        # Empty reports from failed/unbilled calls must not create usage records.
        return [event for event in events.values() if event.total_tokens > 0]

    def sync(self, db: UsageDatabase) -> SyncResult:
        paths = self._files()
        result = SyncResult(self.key, availability="ready" if paths else "not_found")
        errors = []
        for path in paths:
            result.scanned += 1
            logical_path = str(self._logical_path(path))
            source_id = f"deepseek-harness:{logical_path}"
            try:
                stat = path.stat()
                file_identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}"
                cursor = db.get_cursor(source_id)
                if (cursor and cursor["file_identity"] == file_identity and cursor["last_modified"] == stat.st_mtime_ns
                        and cursor["last_logical_cursor"] == self.semantics + ":" + str(path)):
                    result.skipped += 1
                    continue
                reader = TranscriptReader(path, stat.st_size)
                events = self._parse(reader.rows(), path)
                if reader.consumed == 0:
                    continue
                with db.connection() as con:
                    old = {row["event_key"]: row for row in con.execute(
                        "SELECT * FROM usage_event WHERE app_type=? AND source_path=?", (self.key, logical_path))}
                imported = updated = 0
                for event in events:
                    previous = old.get(event.event_key)
                    if previous is None:
                        imported += 1
                    elif event.as_db_tuple() != tuple(previous[key] for key in (
                        "event_key", "app_type", "model", "session_id", "turn_id", "request_id", "occurred_at",
                        "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens",
                        "total_tokens", "input_token_semantics", "source_kind", "source_path", "source_offset",
                        "source_event_id", "capture_mode", "raw_usage_json")):
                        updated += 1
                if reader.consumed == stat.st_size:
                    db.replace_source(logical_path, events, self.key)
                else:
                    db.upsert_events(events)  # Preserve prior data while the writer finishes a trailing frame/line.
                result.imported += imported
                result.updated += updated
                db.save_cursor({
                    "source_id": source_id, "source_kind": self.source_kind, "file_path": str(path),
                    "file_identity": file_identity, "last_byte_offset": reader.consumed, "last_line_offset": 0,
                    "last_modified": stat.st_mtime_ns, "last_logical_cursor": self.semantics + ":" + str(path),
                    "updated_at": utc_now(),
                })
            except Exception as exc:
                errors.append(f"{path.parent.name}：{exc}")
        if errors:
            result.error = "；".join(errors)
        return result
