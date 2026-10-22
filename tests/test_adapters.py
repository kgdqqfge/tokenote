import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tokenote.db import UsageDatabase
from tokenote.adapters.base import read_jsonl_delta
from tokenote.adapters.codex import CodexAdapter
from tokenote.adapters.workbuddy import WorkBuddyAdapter
from tokenote.adapters.kimi import KimiAdapter


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = UsageDatabase(self.root / "db.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def write_jsonl(self, name, objects):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x) + "\n" for x in objects), encoding="utf-8")
        return path

    def test_incremental_reader(self):
        path = self.write_jsonl("events.jsonl", [{"a": 1}])
        first = read_jsonl_delta(self.db, "test:file", "test", path)
        self.assertEqual(len(first), 1)
        self.assertEqual(read_jsonl_delta(self.db, "test:file", "test", path), [])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"a": 2}) + "\n")
        self.assertEqual(len(read_jsonl_delta(self.db, "test:file", "test", path)), 1)

    def test_codex_token_count(self):
        path = self.write_jsonl("rollout-1.jsonl", [
            {"type": "turn_context", "payload": {"type": "turn_context", "model": "gpt-test"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 12, "total_tokens": 112
            }}}},
        ])
        adapter = CodexAdapter()
        adapter._files = lambda: [path]
        result = adapter.sync(self.db)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.db.summary()["total"], 112)

    def test_workbuddy_usage(self):
        path = self.write_jsonl("projects/p/session.jsonl", [{
            "model": "test-model",
            "providerData": {"usage": {"inputTokens": 100, "outputTokens": 20, "inputTokensDetails": [{"cached_tokens": 80}]}},
        }])
        adapter = WorkBuddyAdapter()
        adapter._files = lambda: [path]
        result = adapter.sync(self.db)
        self.assertEqual(result.imported, 1)
        row = self.db.recent()[0]
        self.assertEqual(row["cache_read_tokens"], 80)

    def test_kimi_usage_record(self):
        path = self.write_jsonl("sessions/x/agents/main/wire.jsonl", [{
            "type": "usage.record", "model": "kimi-test",
            "usage": {"inputOther": 100, "output": 10, "inputCacheRead": 50, "inputCacheCreation": 2}
        }])
        adapter = KimiAdapter()
        adapter._wire_files = lambda: [path]
        adapter._server_files = lambda: []
        result = adapter.sync(self.db)
        self.assertEqual(result.imported, 1)
        row = self.db.recent()[0]
        self.assertEqual(row["input_tokens"], 152)
        self.assertEqual(row["cache_creation_tokens"], 2)

    def test_codex_repeat_does_not_move_timestamp(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "total_tokens": 110}
        path = self.write_jsonl("codex-live.jsonl", [
            {"type":"turn_context", "payload":{"model":"gpt-test"}},
            {"timestamp":"2026-10-20T00:00:00Z", "type":"event_msg", "payload":{"type":"token_count", "info":{"last_token_usage":usage,"total_token_usage":usage}}},
        ])
        adapter=CodexAdapter(); adapter._files=lambda:[path]
        adapter.sync(self.db)
        first=self.db.recent()[0]
        with path.open("a",encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp":"2026-10-20T00:01:00Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":usage,"total_token_usage":usage}}})+"\n")
        adapter.sync(self.db)
        self.assertEqual(self.db.summary()["requests"],1)
        self.assertEqual(self.db.recent()[0]["occurred_at"],first["occurred_at"])
        self.assertEqual(first["model"],"gpt-test")
        self.assertIsNotNone(first["occurred_at"])

    def test_opencode_message_cache_and_reasoning(self):
        from tokenote.adapters.opencode import OpenCodeAdapter
        path=self.root/"opencode.db"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE session_message(id TEXT, session_id TEXT, time_created INTEGER, type TEXT, data TEXT)")
            data={"model":{"id":"space-bunny-free"},"time":{"created":1792450000000},"tokens":{"input":20,"output":10,"reasoning":5,"cache":{"read":180,"write":0}}}
            con.execute("INSERT INTO session_message VALUES(?,?,?,?,?)",("msg1","ses1",1792450000000,"assistant",json.dumps(data)))
        con.close()
        adapter=OpenCodeAdapter(); adapter._paths=lambda:[path]
        adapter.sync(self.db); adapter.sync(self.db)
        self.assertEqual(self.db.summary("opencode")["requests"],1)
        self.assertEqual(self.db.summary("opencode")["input"],200)
        self.assertEqual(self.db.summary("opencode")["total"],215)
        self.assertEqual(self.db.recent()[0]["output_tokens"],15)

    def test_cache_denominator_respects_source_semantics(self):
        from tokenote.models import UsageEvent
        self.db.upsert_events([
            UsageEvent("inclusive","codex",input_tokens=100,cache_read_tokens=80,total_tokens=100,input_token_semantics="provider_total_input_dedup_v1"),
            UsageEvent("exclusive","opencode",input_tokens=20,cache_read_tokens=180,total_tokens=200,input_token_semantics="provider_input_plus_cache_buckets"),
        ])
        summary=self.db.summary()
        self.assertEqual(summary["input"],300)
        self.assertEqual(summary["fresh_input"],40)
        self.assertEqual(summary["cache"],260)

    def test_model_breakdown_normalizes_cache_semantics_before_grouping(self):
        from tokenote.models import UsageEvent
        self.db.upsert_events([
            UsageEvent("inclusive-model", "codex", model="openai/shared-model", input_tokens=100,
                       output_tokens=20, cache_read_tokens=60, cache_creation_tokens=10, total_tokens=120,
                       input_token_semantics="provider_total_input_dedup_v1"),
            UsageEvent("exclusive-model", "opencode", model="provider::shared-model", input_tokens=30,
                       output_tokens=15, cache_read_tokens=40, cache_creation_tokens=5, total_tokens=90,
                       input_token_semantics="provider_input_plus_cache_buckets"),
            UsageEvent("uncached-model", "codex", model="other-model", input_tokens=10,
                       output_tokens=5, total_tokens=15),
            UsageEvent("empty-model", "kimi", model="empty-model"),
        ])
        rows = self.db.model_totals()
        self.assertEqual([row["model"] for row in rows], ["shared-model", "other-model", "empty-model"])
        shared = rows[0]
        self.assertEqual((shared["fresh_input"], shared["output"], shared["cache"], shared["total"]),
                         (60, 35, 115, 210))
        self.assertEqual(shared["fresh_input"] + shared["output"] + shared["cache"], shared["total"])
        codex = self.db.model_totals("codex")[0]
        self.assertEqual((codex["fresh_input"], codex["output"], codex["cache"], codex["total"]),
                         (30, 20, 70, 120))
        self.assertEqual(self.db.model_totals(limit=1, offset=1)[0]["model"], "other-model")
        self.assertEqual(self.db.model_totals("claude"), [])
        empty = rows[-1]
        self.assertEqual((empty["fresh_input"], empty["output"], empty["cache"], empty["total"]), (0, 0, 0, 0))

    def test_model_breakdown_recent_window_excludes_old_and_undated(self):
        from tokenote.models import UsageEvent
        self.db.upsert_events([
            UsageEvent("recent", "codex", model="recent-model", occurred_at=2_000, total_tokens=100),
            UsageEvent("stale", "codex", model="stale-model", occurred_at=1_000, total_tokens=500),
            UsageEvent("undated", "codex", model="undated-model", total_tokens=700),
        ])
        rows = self.db.model_totals("codex", since=1_500)
        self.assertEqual([(row["model"], row["total"]) for row in rows], [("recent-model", 100)])
        self.assertEqual({row["model"] for row in self.db.model_totals("codex")},
                         {"recent-model", "stale-model", "undated-model"})

    def test_new_sqlite_schemas(self):
        from tokenote.adapters.additional import ZCodeAdapter, MiniMaxCodeAdapter, HermesAdapter
        path=self.root/"source.sqlite"
        z=ZCodeAdapter().parse({"id":"z1","model_id":"glm-test","input_tokens":100,"output_tokens":2,"cache_read_input_tokens":90,"completed_at":1792450000000},path)
        m=MiniMaxCodeAdapter().parse({"id":1,"session_id":"s1","model":"MiniMax-test","ts":1792450000000,"input_tokens":10,"output_tokens":2,"reasoning_tokens":3,"cache_read_tokens":90},path)
        h=HermesAdapter().parse({"session_id":"s1","model":"hermes-test","input_tokens":10,"output_tokens":2,"cache_read_tokens":90},path)
        self.assertEqual(z.total_tokens,102)
        self.assertEqual(m.total_tokens,105)
        self.assertIsNone(h.occurred_at)
        self.assertEqual(h.source_kind,"hermes_session_aggregate")


if __name__ == "__main__":
    unittest.main()
