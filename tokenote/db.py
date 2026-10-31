from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import UsageEvent
from .credits import CREDIT_SCHEMA, CreditDatabaseMixin
from .utils import display_model_id

EXCLUSIVE_INPUT_SQL = (
    "input_token_semantics IN ('provider_input_plus_cache_buckets','session_cumulative_fallback',"
    "'anthropic_exclusive_cache','pi_exclusive_cache','hermes_exclusive_cache_aggregate',"
    "'minimax_exclusive_cache','zcode_exclusive_cache','antigravity_exclusive_cache_v1')"
)
FRESH_INPUT_SQL = (
    f"CASE WHEN {EXCLUSIVE_INPUT_SQL} THEN input_tokens "
    "ELSE MAX(0, input_tokens - cache_read_tokens - cache_creation_tokens) END"
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS source_cursor (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_identity TEXT,
    last_byte_offset INTEGER NOT NULL DEFAULT 0,
    last_line_offset INTEGER NOT NULL DEFAULT 0,
    last_modified INTEGER,
    last_tail_fingerprint TEXT,
    last_logical_cursor TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_event (
    event_key TEXT PRIMARY KEY,
    app_type TEXT NOT NULL,
    model TEXT,
    session_id TEXT,
    turn_id TEXT,
    request_id TEXT,
    occurred_at INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    input_token_semantics TEXT,
    source_kind TEXT NOT NULL,
    source_path TEXT,
    source_offset INTEGER,
    source_event_id TEXT,
    capture_mode TEXT NOT NULL,
    raw_usage_json TEXT,
    imported_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_usage_event_app_time ON usage_event(app_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_event_model_time ON usage_event(model, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_event_source ON usage_event(source_kind, source_path);
"""


class UsageDatabase(CreditDatabaseMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.create_function("model_id", 1, display_model_id, deterministic=True)
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextmanager
    def connection(self):
        con = self.connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _initialize(self) -> None:
        with self.connection() as con:
            con.executescript(SCHEMA + CREDIT_SCHEMA)

    def get_cursor(self, source_id: str) -> sqlite3.Row | None:
        with self.connection() as con:
            return con.execute("SELECT * FROM source_cursor WHERE source_id = ?", (source_id,)).fetchone()

    def save_cursor(self, cursor: dict[str, Any]) -> None:
        columns = [
            "source_id", "source_kind", "file_path", "file_identity",
            "last_byte_offset", "last_line_offset", "last_modified",
            "last_tail_fingerprint", "last_logical_cursor", "updated_at",
        ]
        values = [cursor.get(column) for column in columns]
        with self.connection() as con:
            con.execute(
                f"INSERT INTO source_cursor ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in columns[1:]),
                values,
            )

    def upsert_events(self, events: Iterable[UsageEvent]) -> int:
        rows = [event.as_db_tuple() for event in events]
        if not rows:
            return 0
        sql = """
        INSERT INTO usage_event (
            event_key, app_type, model, session_id, turn_id, request_id,
            occurred_at, input_tokens, output_tokens, cache_read_tokens,
            cache_creation_tokens, reasoning_tokens, total_tokens,
            input_token_semantics, source_kind, source_path, source_offset,
            source_event_id, capture_mode, raw_usage_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            model=excluded.model,
            occurred_at=excluded.occurred_at,
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            cache_read_tokens=excluded.cache_read_tokens,
            cache_creation_tokens=excluded.cache_creation_tokens,
            reasoning_tokens=excluded.reasoning_tokens,
            total_tokens=excluded.total_tokens,
            input_token_semantics=excluded.input_token_semantics,
            source_event_id=excluded.source_event_id,
            raw_usage_json=excluded.raw_usage_json,
            updated_at=unixepoch()
        """
        with self.connection() as con:
            con.executemany(sql, rows)
        return len(rows)

    def replace_source(self, source_path: str, events: list[UsageEvent], app_type: str) -> int:
        with self.connection() as con:
            con.execute("DELETE FROM usage_event WHERE app_type=? AND source_path=?", (app_type, source_path))
            if events:
                columns = "event_key,app_type,model,session_id,turn_id,request_id,occurred_at,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,input_token_semantics,source_kind,source_path,source_offset,source_event_id,capture_mode,raw_usage_json"
                con.executemany(f"INSERT INTO usage_event ({columns}) VALUES ({','.join('?' for _ in range(20))})", [event.as_db_tuple() for event in events])
        return len(events)

    def delete_events(self, app_type: str | None = None, source_kinds: tuple[str, ...] = ()) -> int:
        clauses = []
        args: list[Any] = []
        if app_type:
            clauses.append("app_type = ?")
            args.append(app_type)
        if source_kinds:
            placeholders = ",".join("?" for _ in source_kinds)
            clauses.append(f"source_kind IN ({placeholders})")
            args.extend(source_kinds)
        if not clauses:
            return 0
        with self.connection() as con:
            cursor = con.execute("DELETE FROM usage_event WHERE " + " AND ".join(clauses), args)
            return cursor.rowcount

    def delete_cursors(self, prefix: str) -> int:
        with self.connection() as con:
            cursor = con.execute("DELETE FROM source_cursor WHERE source_id LIKE ?", (prefix + "%",))
            return cursor.rowcount

    def summary(self, app_type: str | None = None) -> dict[str, int]:
        where = "WHERE app_type = ?" if app_type and app_type != "all" else ""
        args = (app_type,) if where else ()
        # Some sources report fresh input while OpenAI-style sources include cache reads.
        inclusive = f"CASE WHEN {EXCLUSIVE_INPUT_SQL} THEN input_tokens + cache_read_tokens + cache_creation_tokens ELSE input_tokens END"
        with self.connection() as con:
            row = con.execute(
                f"SELECT COUNT(*) requests, COALESCE(SUM({inclusive}),0) input, "
                f"COALESCE(SUM({FRESH_INPUT_SQL}),0) fresh_input, "
                "COALESCE(SUM(output_tokens),0) output, COALESCE(SUM(cache_read_tokens),0) cache, "
                f"COALESCE(SUM(total_tokens),0) total FROM usage_event {where}", args,
            ).fetchone()
        return dict(row)

    def app_totals(self) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute(
                "SELECT app_type, COUNT(*) requests, COALESCE(SUM(input_tokens),0) input, "
                "COALESCE(SUM(output_tokens),0) output, COALESCE(SUM(cache_read_tokens),0) cache, "
                "COALESCE(SUM(total_tokens),0) total FROM usage_event GROUP BY app_type ORDER BY total DESC"
            ).fetchall()

    def model_totals(self, app_type: str | None = None, limit: int = 8, offset: int = 0,
                     since: int | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        args: list[Any] = []
        if app_type and app_type != "all":
            clauses.append("app_type = ?")
            args.append(app_type)
        if since is not None:
            clauses.append("occurred_at IS NOT NULL AND occurred_at >= ?")
            args.append(int(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.extend((limit, offset))
        with self.connection() as con:
            return con.execute(
                "SELECT model_id(model) model, MIN(app_type) app_type, COUNT(*) requests, "
                f"COALESCE(SUM({FRESH_INPUT_SQL}),0) fresh_input, "
                "COALESCE(SUM(output_tokens),0) output, "
                "COALESCE(SUM(cache_read_tokens + cache_creation_tokens),0) cache, "
                "COALESCE(SUM(total_tokens),0) total FROM usage_event "
                f"{where} GROUP BY model_id(model) ORDER BY total DESC, model_id(model) LIMIT ? OFFSET ?", args,
            ).fetchall()

    def daily_totals(self, app_type: str | None = None, days: int = 14) -> list[dict[str, Any]]:
        from datetime import date, timedelta
        days = max(1, min(days, 366))
        first = date.today() - timedelta(days=days-1)
        where = "WHERE occurred_at IS NOT NULL AND date(occurred_at, 'unixepoch', 'localtime') BETWEEN ? AND ?"
        args: list[Any] = [first.isoformat(), date.today().isoformat()]
        if app_type and app_type != "all":
            where += " AND app_type = ?"
            args.append(app_type)
        with self.connection() as con:
            rows = con.execute(
                "SELECT date(occurred_at, 'unixepoch', 'localtime') day, "
                "COALESCE(SUM(total_tokens),0) total, COUNT(*) requests "
                f"FROM usage_event {where} GROUP BY day ORDER BY day", args,
            ).fetchall()
        totals = {row["day"]: dict(row) for row in rows}
        return [totals.get((first+timedelta(days=i)).isoformat(), {"day":(first+timedelta(days=i)).isoformat(), "total":0, "requests":0}) for i in range(days)]

    def daily_totals_all(self, app_type: str | None = None) -> list[dict[str, Any]]:
        from datetime import date, timedelta
        where = "WHERE occurred_at IS NOT NULL"
        args: list[Any] = []
        if app_type and app_type != "all":
            where += " AND app_type = ?"
            args.append(app_type)
        with self.connection() as con:
            rows = con.execute(
                "SELECT date(occurred_at, 'unixepoch', 'localtime') day, "
                "COALESCE(SUM(total_tokens),0) total, COUNT(*) requests "
                f"FROM usage_event {where} GROUP BY day ORDER BY day", args,
            ).fetchall()
        if not rows:
            return []
        first = date.fromisoformat(rows[0]["day"])
        last = max(date.today(), date.fromisoformat(rows[-1]["day"]))
        totals = {row["day"]: dict(row) for row in rows}
        return [totals.get((first + timedelta(days=index)).isoformat(),
                           {"day": (first + timedelta(days=index)).isoformat(), "total": 0, "requests": 0})
                for index in range((last - first).days + 1)]

    def recent(self, app_type: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        where = "WHERE app_type = ?" if app_type and app_type != "all" else ""
        args: tuple[Any, ...] = (app_type, limit) if where else (limit,)
        with self.connection() as con:
            return con.execute(
                "SELECT * FROM usage_event "
                f"{where} ORDER BY COALESCE(occurred_at,0) DESC, imported_at DESC LIMIT ?", args,
            ).fetchall()

    def source_status(self) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute(
                "SELECT source_kind, COUNT(*) events, MAX(updated_at) updated_at "
                "FROM usage_event GROUP BY source_kind ORDER BY source_kind"
            ).fetchall()

    def export_csv(self, path: Path, app_type: str | None = None) -> None:
        rows = self.recent(app_type, limit=1_000_000)
        if not rows:
            headers = ["event_key"]
        else:
            headers = rows[0].keys()
        with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row[h] for h in headers])



