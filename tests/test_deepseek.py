from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.deepseek import DeepSeekHarnessAdapter
from tokenote.db import UsageDatabase
from tokenote.ui.app import TokenoteApp

try:
    from compression import zstd
except ImportError:
    try:
        from backports import zstd
    except ImportError:
        zstd = None


class DeepSeekHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.db = UsageDatabase(self.root / "meter.db")
        self.adapter = DeepSeekHarnessAdapter()
        self.adapter._session_roots = lambda: [self.sessions]
        self.now = int(datetime.now().timestamp()) * 1000

    def tearDown(self):
        self.temp.cleanup()

    def record(self, kind, **data):
        return {"type": kind, "data": data}

    def usage(self, turn=1, step=1, **overrides):
        usage = {"inputTokens": 20, "outputTokens": 30, "cacheReadTokens": 100,
                 "cacheWriteTokens": 5, "reasoningTokens": 7}
        usage.update(overrides)
        return self.record("assistant/chunk", turn=turn, step=step, chunk={"type": "usage", "usage": usage})

    def transcript(self, events, session_id="session-1", **header_fields):
        header = {"type": "session", "version": 0, "id": session_id,
                  "createdAt": self.now, "delegationDepth": 0, **header_fields}
        return [header, *[{"seq": seq, "time": self.now + seq * 1000, **event}
                         for seq, event in enumerate(events)]]

    @staticmethod
    def encode(rows):
        return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8")

    def write(self, rows, session_id="session-1", compressed=False):
        directory = self.sessions / "--project--" / session_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("session.jsonl.zstd" if compressed else "session.jsonl")
        payload = self.encode(rows)
        path.write_bytes(zstd.compress(payload) if compressed else payload)
        return path

    def test_normalizes_usage_and_replaces_stream_sample_with_final(self):
        usage = self.usage()["data"]["chunk"]["usage"]
        self.write(self.transcript([
            self.record("request/context", model="deepseek-v4-pro"),
            self.record("user/message", content="private prompt must never enter the meter"),
            self.usage(),
            self.record("assistant/message", turn=1, step=1, usage=usage, message={"content": "private answer"}),
            # Identical usage for a different step is a separate billed call.
            self.usage(step=2),
        ]))
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 2)
        summary = self.db.summary(self.adapter.key)
        self.assertEqual((summary["input"], summary["output"], summary["cache"], summary["total"]), (250, 60, 200, 310))
        self.assertEqual(summary["fresh_input"], 40)
        model = self.db.model_totals(self.adapter.key)[0]
        self.assertEqual((model["model"], model["fresh_input"], model["output"], model["cache"]),
                         ("deepseek-v4-pro", 40, 60, 210))
        row = self.db.recent(self.adapter.key)[0]
        self.assertEqual(TokenoteApp._fresh_input(row), 20)
        self.assertEqual(row["reasoning_tokens"], 7)
        self.assertNotIn("private", row["raw_usage_json"])
        self.assertEqual(self.db.daily_totals(self.adapter.key, days=1)[0]["total"], 310)
        repeated = self.adapter.sync(self.db)
        self.assertEqual((repeated.imported, repeated.updated, repeated.skipped), (0, 0, 1))

    def test_later_final_usage_updates_request_without_moving_initial_time(self):
        rows = self.transcript([self.record("request/context", model="deepseek-v4-flash"), self.usage(outputTokens=1)])
        path = self.write(rows)
        self.adapter.sync(self.db)
        timestamp = self.db.recent()[0]["occurred_at"]
        final = self.record("assistant/message", turn=1, step=1,
                            usage={"inputTokens": 25, "outputTokens": 40, "cacheReadTokens": 120})
        rows.append({"seq": 2, "time": self.now + 10_000, **final})
        path.write_bytes(self.encode(rows))
        result = self.adapter.sync(self.db)
        self.assertEqual((result.imported, result.updated), (0, 1))
        summary = self.db.summary(self.adapter.key)
        self.assertEqual((summary["requests"], summary["total"]), (1, 185))
        self.assertEqual(self.db.recent()[0]["occurred_at"], timestamp)

    def test_tracks_context_and_header_model_switches(self):
        self.write(self.transcript([
            self.record("request/context", model="deepseek-v4-flash"), self.usage(),
            self.record("request/header", header={"config": {"model": "deepseek-v4-pro"}}),
            self.usage(step=2, inputTokens=50),
        ]))
        self.adapter.sync(self.db)
        models = {row["model"]: row["total"] for row in self.db.model_totals(self.adapter.key)}
        self.assertEqual(models, {"deepseek-v4-pro": 185, "deepseek-v4-flash": 155})

    def test_ignores_inherited_fork_usage_but_keeps_resumed_usage(self):
        self.write(self.transcript([
            self.record("request/context", model="deepseek-v4-pro"), self.usage(),
            self.record("session/end-seed"), self.usage(step=2),
        ], seedLength=3))
        result = self.adapter.sync(self.db)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.db.summary()["total"], 155)
        self.write(self.transcript([
            self.record("request/context", model="deepseek-v4-pro"), self.usage(),
            self.record("session/end-seed"), self.usage(step=2),
        ], session_id="resumed"), session_id="resumed")
        self.adapter.sync(self.db)
        self.assertEqual(self.db.summary()["requests"], 3)

    def test_plain_incomplete_tail_is_counted_only_when_complete(self):
        rows = self.transcript([self.record("request/context", model="deepseek-v4-pro"), self.usage(), self.usage(step=2)])
        complete = self.encode(rows[:-1])
        tail = self.encode(rows[-1:])
        path = self.write(rows[:-1])
        path.write_bytes(complete + tail[:-4])
        first = self.adapter.sync(self.db)
        self.assertIsNone(first.error)
        self.assertEqual(self.db.summary()["requests"], 1)
        with path.open("ab") as handle:
            handle.write(tail[-4:])
        second = self.adapter.sync(self.db)
        self.assertEqual(second.imported, 1)
        self.assertEqual(self.db.summary()["requests"], 2)

    @unittest.skipIf(zstd is None, "Zstandard decoder unavailable")
    def test_concatenated_zstd_frames_and_incomplete_tail(self):
        rows = self.transcript([self.record("request/context", model="deepseek-v4-pro"), self.usage(), self.usage(step=2)])
        path = self.write(rows[:1], compressed=True)
        first = zstd.compress(self.encode(rows[1:3]))
        final = zstd.compress(self.encode(rows[3:]))
        with path.open("ab") as handle:
            handle.write(first + final[:-3])
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 1)
        with path.open("ab") as handle:
            handle.write(final[-3:])
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        self.assertEqual(self.db.summary()["total"], 310)
        self.assertEqual(self.adapter.sync(self.db).imported, 0)

    @unittest.skipIf(zstd is None, "Zstandard decoder unavailable")
    def test_corrupt_snapshot_keeps_old_data_and_other_sessions_continue(self):
        path = self.write(self.transcript([self.usage()]), compressed=True)
        self.adapter.sync(self.db)
        path.write_bytes(b"corrupted zstd data")
        self.write(self.transcript([self.usage()], session_id="valid-session"), session_id="valid-session")
        result = self.adapter.sync(self.db)
        self.assertIsNotNone(result.error)
        self.assertEqual(self.db.summary()["total"], 310)
        self.assertEqual(self.db.summary()["requests"], 2)

    @unittest.skipIf(zstd is None, "Zstandard decoder unavailable")
    def test_plain_and_compressed_variants_share_one_transcript(self):
        rows = self.transcript([self.record("request/context", model="deepseek-v4-pro"), self.usage()])
        plain = self.write(rows)
        self.adapter.sync(self.db)
        compressed = self.write(rows, compressed=True)
        os.utime(compressed, ns=(plain.stat().st_mtime_ns + 1_000_000, plain.stat().st_mtime_ns + 1_000_000))
        self.assertEqual(self.adapter._files(), [compressed])
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual((result.imported, self.db.summary()["requests"]), (0, 1))
        self.assertEqual(self.db.summary()["total"], 155)

    def test_zero_reports_and_packed_content_do_not_create_usage(self):
        self.write(self.transcript([
            {"type": "text-chunks", "seq0": 0, "time0": self.now, "deltas": []},
            self.usage(inputTokens=0, outputTokens=0, cacheReadTokens=0, cacheWriteTokens=0, reasoningTokens=0),
        ]))
        result = self.adapter.sync(self.db)
        self.assertEqual(result.availability, "ready")
        self.assertEqual(self.db.summary()["requests"], 0)

    def test_failed_database_write_does_not_advance_cursor_or_report_imports(self):
        path = self.write(self.transcript([self.usage()]))
        with patch.object(self.db, "replace_source", side_effect=OSError("simulated write failure")):
            failed = self.adapter.sync(self.db)
        self.assertIsNotNone(failed.error)
        self.assertEqual((failed.imported, failed.updated), (0, 0))
        self.assertIsNone(self.db.get_cursor(f"deepseek-harness:{path}"))
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        self.assertEqual(self.db.summary()["requests"], 1)

    def test_missing_source_and_environment_discovery(self):
        self.assertEqual(self.adapter.sync(self.db).availability, "not_found")
        custom = self.root / "custom-home"
        configured = custom / "sessions"
        configured.mkdir(parents=True)
        adapter = DeepSeekHarnessAdapter()
        with patch.dict(os.environ, {"DSH_HOME": str(custom), "TOKENOTE_DEEPSEEK_HARNESS_DIR": str(configured)}):
            roots = adapter._session_roots()
        self.assertIn(configured.resolve(), roots)
        self.assertEqual(roots.count(configured.resolve()), 1)


if __name__ == "__main__":
    unittest.main()
