from __future__ import annotations

import json
import math
import os
from pathlib import Path

from ..credits import CREDIT_COLUMNS, CreditEvent
from ..db import UsageDatabase
from ..models import UsageEvent
from ..utils import as_int, epoch_from_any, fingerprint, model_name, stable_hash, utc_now
from .base import Adapter, SyncResult


def _credits(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _tokens(value) -> int:
    try:
        return max(0, as_int(value))
    except OverflowError:
        return 0


class QoderAdapter(Adapter):
    key = "qoder"
    label = "Qoder"
    semantics = "qoder_inclusive_input_v1"
    source_kind = "qoder_session"

    def _roots(self) -> list[Path]:
        roots = [Path.home() / ".qoder", Path.home() / ".qoder-cn"]
        for name in ("QODER_CONFIG_DIR", "QODER_HOME", "TOKENOTE_QODER_DIR"):
            if os.environ.get(name):
                roots.insert(0, Path(os.environ[name]).expanduser())
        return list(dict.fromkeys(path.resolve() for path in roots))

    def _files(self) -> list[Path]:
        return sorted({path.resolve() for root in self._roots() for path in (root / "projects").glob("**/*.jsonl")
                       if path.is_file()}, key=lambda path: str(path).lower())

    def _parse(self, data: bytes, path: Path) -> tuple[list[CreditEvent], list[UsageEvent], int]:
        boundary = data.rfind(b"\n")
        if boundary < 0:
            return [], [], 0
        model = None
        session = path.stem
        position = 0
        reports: dict[str, CreditEvent] = {}
        tokens: dict[str, UsageEvent] = {}
        for line in data[:boundary + 1].splitlines(keepends=True):
            offset = position
            position += len(line)
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Qoder 日志记录必须为 JSON 对象")
            session = str(row.get("sessionId") or session)
            if row.get("type") == "runtime-config":
                model = model_name(row.get("model")) or model
                continue
            message = row.get("message")
            if row.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            fresh = _tokens(usage.get("input_tokens"))
            output = _tokens(usage.get("output_tokens"))
            read = _tokens(usage.get("cache_read_input_tokens"))
            write = _tokens(usage.get("cache_creation_input_tokens"))
            if "prompt_tokens" in usage:
                prompt = _tokens(usage["prompt_tokens"])
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    read = _tokens(details.get("cached_tokens", read))
                fresh = max(0, prompt - read - write)
                output = _tokens(usage.get("completion_tokens", output))
            inclusive = fresh + read + write
            total = max(inclusive + output, _tokens(usage.get("total_tokens")))
            credits = _credits(usage.get("credits"))
            original = _credits(usage.get("original_credits"))
            billable = usage.get("billable") if isinstance(usage.get("billable"), bool) else None
            # The first-party SDK can report nonzero Credits while all Token
            # counters are zero. These are unknown Token counts, never estimates.
            if not total and credits is None and original is None and billable is None:
                continue
            request_id = str(usage.get("request_id") or row.get("requestId") or message.get("id") or row.get("uuid") or f"{session}:{offset}")
            key = f"qoder:{stable_hash(request_id)}"
            occurred = epoch_from_any(row.get("timestamp") or message.get("timestamp"))
            current_model = model_name(message.get("model")) or model
            report = CreditEvent(
                event_key=key, app_type=self.key, model=current_model, session_id=session, request_id=request_id,
                occurred_at=occurred, credits=credits, original_credits=original, billable=billable,
                token_total=total if total else None, source_kind=self.source_kind, source_path=str(path),
                source_offset=offset, raw_usage_json=UsageEvent.json_usage(usage),
            )
            previous = reports.get(key)
            if previous:
                for name in ("credits", "original_credits", "billable", "token_total"):
                    if getattr(report, name) is None:
                        setattr(report, name, getattr(previous, name))
            reports[key] = report
            if total:
                tokens[key] = UsageEvent(
                    event_key=key, app_type=self.key, model=current_model, session_id=session, request_id=request_id,
                    occurred_at=occurred, input_tokens=inclusive, output_tokens=output, cache_read_tokens=read,
                    cache_creation_tokens=write, reasoning_tokens=_tokens(usage.get("reasoning_tokens")), total_tokens=total,
                    input_token_semantics=self.semantics, source_kind=self.source_kind, source_path=str(path),
                    source_offset=offset, source_event_id=request_id, capture_mode="file_jsonl", raw_usage_json=report.raw_usage_json,
                )
        return list(reports.values()), list(tokens.values()), boundary + 1

    def sync(self, db: UsageDatabase) -> SyncResult:
        files = self._files()
        result = SyncResult(self.key, scanned=len(files), availability="ready" if files else "not_found")
        errors = []
        for path in files:
            try:
                stat = path.stat()
                identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}"
                source_id = f"qoder:{path}"
                cursor = db.get_cursor(source_id)
                if (cursor and cursor["file_identity"] == identity and cursor["last_modified"] == stat.st_mtime_ns
                        and cursor["last_logical_cursor"] == self.semantics):
                    result.skipped += 1
                    continue
                with path.open("rb") as handle:
                    data = handle.read(stat.st_size)
                reports, token_events, offset = self._parse(data, path)
                if offset == 0:
                    continue
                selected = []
                imported = updated = 0
                with db.connection() as con:
                    for report in reports:
                        old = con.execute("SELECT * FROM credit_usage_event WHERE event_key=?", (report.event_key,)).fetchone()
                        if old is None:
                            imported += 1
                            selected.append(report)
                        elif any(getattr(report, name) is not None and getattr(report, name) != old[name]
                                 for name in CREDIT_COLUMNS if name not in ("source_path", "source_offset", "session_id")):
                            updated += 1
                            selected.append(report)
                db.upsert_events(token_events)
                db.upsert_credit_events(selected)
                result.imported += imported
                result.updated += updated
                db.save_cursor({
                    "source_id": source_id, "source_kind": self.source_kind, "file_path": str(path), "file_identity": identity,
                    "last_byte_offset": offset, "last_line_offset": 0, "last_modified": stat.st_mtime_ns,
                    "last_tail_fingerprint": fingerprint(data[max(0, offset - 256):offset]),
                    "last_logical_cursor": self.semantics, "updated_at": utc_now(),
                })
            except Exception as exc:
                errors.append(f"{path.name}：{exc}")
        if errors:
            result.error = "；".join(errors)
        return result
