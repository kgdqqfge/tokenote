from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.openclaw import OpenClawAdapter
from tokenote.db import UsageDatabase
from tokenote.ui.app import TokenoteApp


class OpenClawTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_root = self.root / ".openclaw-autoclaw"
        self.db = UsageDatabase(self.root / "usage.db")
        self.adapter = OpenClawAdapter()
        self.adapter._roots = lambda: [self.state_root]
        self.now = int(datetime.now().timestamp()) * 1000

    def message(self, identity="m1", model="glm-test", timestamp=None, **overrides):
        usage = {"input": 20, "output": 10, "cacheRead": 70, "cacheWrite": 5, "reasoningTokens": 4,
                 "cost": {"total": 0}}
        usage.update(overrides)
        usage.setdefault("totalTokens", sum(usage[key] for key in ("input", "output", "cacheRead", "cacheWrite")))
        return {"type": "message", "id": identity, "timestamp": datetime.now().astimezone().isoformat(),
                "message": {"role": "assistant", "model": model, "timestamp": timestamp or self.now,
                            "content": [{"type": "text", "text": "private content must not be stored"}], "usage": usage}}

    def write(self, records, session="session-1", agent="main", suffix=".jsonl"):
        folder = self.state_root / "agents" / agent / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (session + suffix)
        rows = [{"type": "session", "version": 3, "id": session, "timestamp": self.now}, *records]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    @staticmethod
    def append(path, records):
        with path.open("a", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")

    def test_normalizes_canonical_cache_and_reasoning_without_reading_chat(self):
        self.write([self.message()])
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 1)
        summary = self.db.summary(self.adapter.key)
        self.assertEqual((summary["input"], summary["fresh_input"], summary["output"], summary["cache"], summary["total"]),
                         (95, 20, 10, 70, 105))
        model = self.db.model_totals(self.adapter.key)[0]
        self.assertEqual((model["fresh_input"], model["output"], model["cache"]), (20, 10, 75))
        event = self.db.recent(self.adapter.key)[0]
        self.assertEqual(TokenoteApp._fresh_input(event), 20)
        self.assertEqual(event["reasoning_tokens"], 4)
        self.assertNotIn("private content", event["raw_usage_json"])
        self.assertEqual(self.db.daily_totals(self.adapter.key, days=1)[0]["total"], 105)
        self.assertEqual(self.adapter.sync(self.db).skipped, 1)
        self.assertEqual(self.db.summary()["total"], 105)

    def test_normalizes_openai_and_anthropic_usage_aliases(self):
        cases = [
            ({"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 5},
              "output_tokens_details": {"reasoning_tokens": 10}, "total_tokens": 120}, (100, 20, 80, 5, 10, 120)),
            ({"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 5},
             (95, 10, 70, 5, 0, 105)),
            ({"input": 20, "output": 10, "cached_input_tokens": 80}, (100, 10, 80, 0, 0, 110)),
            ({"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 80}, (100, 10, 80, 0, 0, 110)),
            ({"totalTokens": 200}, (0, 0, 0, 0, 0, 200)),
            ({"input": 10, "output": 5, "totalTokens": 20}, (10, 5, 0, 0, 0, 20)),
        ]
        for usage, expected in cases:
            with self.subTest(usage=usage):
                self.assertEqual(self.adapter._normalize(usage), expected)

    def test_append_carries_model_context_across_sync_and_updates_same_message(self):
        path = self.write([{"type": "model_change", "modelId": "model-a"}, self.message(model=None)])
        self.adapter.sync(self.db)
        self.append(path, [self.message("m2", model=None, timestamp=self.now + 1000),
                           {"type": "custom", "customType": "model-snapshot", "data": {"modelId": "model-b"}},
                           self.message("m3", model=None, timestamp=self.now + 2000)])
        second = self.adapter.sync(self.db)
        self.assertEqual(second.imported, 2)
        models = {row["model"]: row["requests"] for row in self.db.model_totals(self.adapter.key)}
        self.assertEqual(models, {"model-a": 2, "model-b": 1})
        self.append(path, [self.message("m3", model=None, timestamp=self.now + 2000, output=20)])
        final = self.adapter.sync(self.db)
        self.assertEqual((final.imported, final.updated), (0, 1))
        self.assertEqual(self.db.summary()["requests"], 3)
        self.assertEqual(self.db.summary()["total"], 325)

    def test_archive_rename_and_copies_do_not_double_count(self):
        path = self.write([self.message()])
        self.adapter.sync(self.db)
        archived = path.with_name(path.name + ".reset.2026-10-20T00-00-00Z")
        path.rename(archived)
        first = self.adapter.sync(self.db)
        self.assertEqual((first.imported, first.updated), (0, 0))
        shutil.copyfile(archived, path)
        shutil.copyfile(archived, path.with_name(path.name + ".deleted.2026-10-20"))
        (path.parent / "sessions.json").write_text(json.dumps({"totalTokens": 999999}), encoding="utf-8")
        self.adapter.sync(self.db)
        self.assertEqual(self.db.summary()["requests"], 1)
        self.assertEqual(self.db.summary()["total"], 105)

    def test_forked_or_profile_copies_deduplicate_but_new_calls_count(self):
        original = self.message()
        self.write([original])
        self.write([original, self.message("fork-new", timestamp=self.now + 1000)], session="forked", agent="child")
        self.adapter.sync(self.db)
        self.assertEqual(self.db.summary()["requests"], 2)
        self.assertEqual(self.db.summary()["total"], 210)

    def test_reads_all_agents_reset_and_deleted_history(self):
        self.write([self.message("a")], session="current", agent="main")
        self.write([self.message("b")], session="reset", suffix=".jsonl.reset.2026-10-20")
        self.write([self.message("c")], session="deleted", agent="worker", suffix=".jsonl.deleted.2026-10-20")
        result = self.adapter.sync(self.db)
        self.assertEqual((result.scanned, result.imported), (3, 3))
        self.assertEqual(self.db.summary()["total"], 315)

    def test_incomplete_line_waits_for_completion(self):
        path = self.write([self.message()])
        tail = (json.dumps(self.message("second", timestamp=self.now + 1000)) + "\n").encode("utf-8")
        with path.open("ab") as handle:
            handle.write(tail[:-4])
        first = self.adapter.sync(self.db)
        self.assertIsNone(first.error)
        self.assertEqual(first.imported, 1)
        with path.open("ab") as handle:
            handle.write(tail[-4:])
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        self.assertEqual(self.db.summary()["requests"], 2)

    def test_failed_database_write_keeps_cursor_unadvanced_and_retry_works(self):
        path = self.write([self.message()])
        with patch.object(self.db, "upsert_events", side_effect=OSError("simulated write failure")):
            failed = self.adapter.sync(self.db)
        self.assertIsNotNone(failed.error)
        self.assertEqual(failed.imported, 0)
        self.assertIsNone(self.db.get_cursor(f"openclaw:{path.resolve()}"))
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        self.assertEqual(self.db.summary()["requests"], 1)

    def test_bad_file_does_not_discard_previous_usage_or_block_other_sessions(self):
        path = self.write([self.message()])
        self.adapter.sync(self.db)
        path.write_bytes(b"{broken header}\n")
        self.write([self.message("other")], session="valid")
        result = self.adapter.sync(self.db)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.db.summary()["requests"], 2)

    def test_rewritten_larger_file_resets_context_and_cursor(self):
        path = self.write([{"type": "model_change", "modelId": "old"}, self.message("before", model=None)])
        self.adapter.sync(self.db)
        self.write([{"type": "model_change", "modelId": "new"},
                    {"type": "message", "message": {"role": "user", "content": "private " * 500}},
                    self.message("after", model=None, timestamp=self.now + 1000)])
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        models = {row["model"] for row in self.db.model_totals(self.adapter.key)}
        self.assertEqual(models, {"old", "new"})
        self.assertEqual(self.db.summary()["requests"], 2)

    def test_zero_unavailable_user_and_tool_messages_are_not_billed(self):
        zero = self.message("zero", input=0, output=0, cacheRead=0, cacheWrite=0, reasoningTokens=0)
        unavailable = self.message("provisional", usageReport={"state": "unavailable"})
        user = self.message("user")
        user["message"]["role"] = "user"
        tool = self.message("tool")
        tool["message"]["role"] = "toolResult"
        self.write([zero, unavailable, user, tool, self.message("failed-with-usage")])
        result = self.adapter.sync(self.db)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.db.summary()["requests"], 1)

    def test_missing_source_and_state_root_discovery(self):
        self.assertEqual(self.adapter.sync(self.db).availability, "not_found")
        (self.root / ".openclaw").mkdir()
        (self.root / ".openclaw-autoclaw").mkdir()
        custom = self.root / "custom-state"
        custom.mkdir()
        with patch("tokenote.adapters.openclaw.Path.home", return_value=self.root), \
             patch.dict(os.environ, {"OPENCLAW_HOME": str(self.root), "OPENCLAW_STATE_DIR": str(custom),
                                     "TOKENOTE_OPENCLAW_DIR": str(custom)}, clear=False):
            roots = OpenClawAdapter()._roots()
        self.assertIn((self.root / ".openclaw").resolve(), roots)
        self.assertIn((self.root / ".openclaw-autoclaw").resolve(), roots)
        self.assertEqual(roots.count(custom.resolve()), 1)


if __name__ == "__main__":
    unittest.main()
