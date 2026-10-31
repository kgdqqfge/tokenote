from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

CREDIT_COLUMNS = (
    "event_key", "app_type", "model", "session_id", "request_id", "occurred_at", "credits",
    "original_credits", "billable", "token_total", "source_kind", "source_path", "source_offset", "raw_usage_json",
)
CREDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_usage_event (
    event_key TEXT PRIMARY KEY,
    app_type TEXT NOT NULL,
    model TEXT,
    session_id TEXT,
    request_id TEXT,
    occurred_at INTEGER,
    credits REAL,
    original_credits REAL,
    billable INTEGER CHECK (billable IS NULL OR billable IN (0,1)),
    token_total INTEGER,
    source_kind TEXT NOT NULL,
    source_path TEXT,
    source_offset INTEGER,
    raw_usage_json TEXT,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_credit_app_time ON credit_usage_event(app_type, occurred_at DESC);
"""


@dataclass(slots=True)
class CreditEvent:
    event_key: str
    app_type: str
    model: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    occurred_at: int | None = None
    credits: float | None = None
    original_credits: float | None = None
    billable: bool | None = None
    token_total: int | None = None
    source_kind: str = "qoder_session"
    source_path: str | None = None
    source_offset: int | None = None
    raw_usage_json: str | None = None

    def as_db_tuple(self) -> tuple:
        return tuple(getattr(self, key) for key in CREDIT_COLUMNS)


class CreditDatabaseMixin:
    """Credits remain separate from Token accounting, including unknown values."""

    def upsert_credit_events(self, events: Iterable[CreditEvent]) -> int:
        rows = [event.as_db_tuple() for event in events]
        if not rows:
            return 0
        columns = ",".join(CREDIT_COLUMNS)
        update_fields = ("model", "occurred_at", "credits", "original_credits", "billable", "token_total")
        updates = ",".join(f"{key}=COALESCE(excluded.{key},credit_usage_event.{key})" for key in update_fields)
        with self.connection() as con:
            con.executemany(
                f"INSERT INTO credit_usage_event ({columns}) VALUES ({','.join('?' for _ in CREDIT_COLUMNS)}) "
                f"ON CONFLICT(event_key) DO UPDATE SET {updates}, raw_usage_json=excluded.raw_usage_json, updated_at=unixepoch()", rows)
        return len(rows)

    @staticmethod
    def _credit_filter(app_type: str | None) -> tuple[str, tuple]:
        return ("WHERE app_type=?", (app_type,)) if app_type and app_type != "all" else ("", ())

    def credit_summary(self, app_type: str | None = None) -> dict[str, Any]:
        where, args = self._credit_filter(app_type)
        with self.connection() as con:
            row = con.execute(
                "SELECT COUNT(*) requests, SUM(credits) credits, SUM(original_credits) original_credits, "
                "SUM(CASE WHEN billable=1 THEN credits WHEN billable=0 THEN 0 END) billed_credits, "
                "COUNT(credits) credit_reports, COUNT(billable) billing_reports, "
                "COALESCE(SUM(CASE WHEN token_total IS NOT NULL THEN 1 ELSE 0 END),0) token_requests "
                f"FROM credit_usage_event {where}", args).fetchone()
        return dict(row)

    def credit_model_totals(self, app_type: str | None = None, limit: int = 8, offset: int = 0,
                            since: int | None = None):
        where, args = self._credit_filter(app_type)
        args = list(args)
        if since is not None:
            clause = "occurred_at IS NOT NULL AND occurred_at >= ?"
            where = f"{where} AND {clause}" if where else f"WHERE {clause}"
            args.append(int(since))
        args.extend((limit, offset))
        with self.connection() as con:
            return con.execute(
                "SELECT model_id(model) model, MIN(app_type) app_type, COUNT(*) requests, SUM(credits) credits, "
                "COALESCE(SUM(credits),0) total, SUM(original_credits) original_credits, COUNT(credits) credit_reports, "
                "SUM(CASE WHEN billable=1 THEN credits WHEN billable=0 THEN 0 END) billed_credits "
                f"FROM credit_usage_event {where} GROUP BY model_id(model) ORDER BY total DESC,model_id(model) LIMIT ? OFFSET ?",
                args).fetchall()

    def credit_app_totals(self, app_type: str | None = None):
        where, args = self._credit_filter(app_type)
        with self.connection() as con:
            return con.execute(
                "SELECT app_type, COUNT(*) requests, COUNT(credits) credit_reports, COALESCE(SUM(credits),0) total "
                f"FROM credit_usage_event {where} GROUP BY app_type ORDER BY total DESC", args).fetchall()

    def credit_daily_totals(self, app_type: str | None = None, days: int = 14):
        days = max(1, min(days, 366))
        first = date.today() - timedelta(days=days - 1)
        where = "WHERE occurred_at IS NOT NULL AND date(occurred_at,'unixepoch','localtime') BETWEEN ? AND ?"
        args: list[Any] = [first.isoformat(), date.today().isoformat()]
        if app_type and app_type != "all":
            where += " AND app_type=?"
            args.append(app_type)
        with self.connection() as con:
            rows = con.execute(
                "SELECT date(occurred_at,'unixepoch','localtime') day, COALESCE(SUM(credits),0) total, COUNT(*) requests, COUNT(credits) credit_reports "
                f"FROM credit_usage_event {where} GROUP BY day ORDER BY day", args).fetchall()
        lookup = {row["day"]: dict(row) for row in rows}
        return [lookup.get(day.isoformat(), {"day": day.isoformat(), "total": 0, "requests": 0, "credit_reports": 0})
                for day in (first + timedelta(days=index) for index in range(days))]

    def credit_daily_totals_all(self, app_type: str | None = None):
        where, args = self._credit_filter(app_type)
        with self.connection() as con:
            rows = con.execute(
                "SELECT date(occurred_at,'unixepoch','localtime') day, COALESCE(SUM(credits),0) total, "
                "COUNT(*) requests, COUNT(credits) credit_reports "
                f"FROM credit_usage_event {where} GROUP BY day ORDER BY day", args).fetchall()
        if not rows:
            return []
        first = date.fromisoformat(rows[0]["day"])
        last = max(date.today(), date.fromisoformat(rows[-1]["day"]))
        lookup = {row["day"]: dict(row) for row in rows}
        return [lookup.get(day.isoformat(), {"day": day.isoformat(), "total": 0, "requests": 0, "credit_reports": 0})
                for day in (first + timedelta(days=index) for index in range((last - first).days + 1))]

    def credit_recent(self, app_type: str | None = None, limit: int = 100):
        where, args = self._credit_filter(app_type)
        with self.connection() as con:
            return con.execute(
                f"SELECT * FROM credit_usage_event {where} ORDER BY COALESCE(occurred_at,0) DESC,event_key DESC LIMIT ?",
                (*args, limit)).fetchall()

    def export_credits_csv(self, path: Path, app_type: str | None = None) -> None:
        rows = self.credit_recent(app_type, limit=1_000_000)
        with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            headers = list(CREDIT_COLUMNS)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row[key] for key in headers])

    def credit_recent_with_tokens(self, app_type: str, limit: int = 100):
        with self.connection() as con:
            return con.execute(
                "SELECT c.*, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens, u.input_token_semantics "
                "FROM credit_usage_event c LEFT JOIN usage_event u ON c.event_key=u.event_key "
                "WHERE c.app_type=? ORDER BY COALESCE(c.occurred_at,0) DESC,c.event_key DESC LIMIT ?",
                (app_type, limit)).fetchall()

    def export_credit_tokens_csv(self, path: Path, app_type: str) -> None:
        rows = self.credit_recent_with_tokens(app_type, limit=1_000_000)
        headers = ("occurred_at", "app_type", "model", "request_id", "input_tokens", "output_tokens",
                   "cache_read_tokens", "cache_creation_tokens", "token_total", "credits", "billable")
        with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row[key] for key in headers])
