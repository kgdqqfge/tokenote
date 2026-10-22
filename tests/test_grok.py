from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.grok import GrokAdapter
from tokenote.db import UsageDatabase


class GrokTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.db = UsageDatabase(self.root / "usage.db")

    def adapter(self):
        adapter = GrokAdapter()
        adapter._root = lambda: self.root
        return adapter

    def write_session(self, session_id="session-1", cwd="project", summary=None, updates=None):
        folder = self.sessions / cwd / session_id
        folder.mkdir(parents=True, exist_ok=True)
        if summary is not None:
            (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        if updates is not None:
            (folder / "updates.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in updates), encoding="utf-8")
        return folder

    @staticmethod
    def usage(input_tokens=1000, output_tokens=100, cache_read=400, cache_creation=0,
              reasoning=20, total_tokens=None, models=None):
        value = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens if total_tokens is not None else input_tokens + output_tokens,
            "cachedReadTokens": cache_read,
            "cacheCreationTokens": cache_creation,
            "reasoningTokens": reasoning,
        }
        if models is not None:
            value["modelUsage"] = models
        return value

    @staticmethod
    def update(prompt_id="prompt-1", usage=None, session_id="session-1", event_id="event-1",
               timestamp_ms=1792563972843):
        row = {
            "timestamp": timestamp_ms // 1000,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed", "prompt_id": prompt_id, "stop_reason": "end_turn"},
                "_meta": {"eventId": event_id, "agentTimestampMs": timestamp_ms},
            },
        }
        if usage is not None:
            row["params"]["update"]["usage"] = usage
        return row

    def test_turn_usage_uses_inclusive_input_and_ignores_chat(self):
        chat = {"timestamp": 1792563972, "method": "session/update", "params": {
            "sessionId": "session-1", "update": {"sessionUpdate": "user_message_chunk",
                                                 "content": {"type": "text", "text": "private prompt"}}}}
        self.write_session(updates=[chat, self.update(
            usage=self.usage(models={"grok-build": {
                "inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100,
                "cachedReadTokens": 400, "cacheCreationTokens": 0, "reasoningTokens": 20,
            }}))])
        result = self.adapter().sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.availability, "ready")
        self.assertEqual(result.imported, 1)
        summary = self.db.summary("grok")
        self.assertEqual((summary["requests"], summary["input"], summary["fresh_input"],
                          summary["output"], summary["cache"], summary["total"]),
                         (1, 1000, 600, 100, 400, 1100))
        row = self.db.recent("grok")[0]
        self.assertEqual(row["model"], "grok-build")
        self.assertEqual(row["session_id"], "session-1")
        self.assertEqual(row["turn_id"], "prompt-1")
        self.assertEqual(row["occurred_at"], 1792563972)
        self.assertEqual(row["reasoning_tokens"], 20)
        self.assertNotIn("private prompt", row["raw_usage_json"])

    def test_each_model_usage_row_is_imported_once(self):
        models = {
            "grok-build": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110,
                           "cachedReadTokens": 40, "cacheCreationTokens": 0},
            "grok-fast": {"inputTokens": 200, "outputTokens": 20, "totalTokens": 220,
                          "cachedReadTokens": 80, "cacheCreationTokens": 0},
        }
        self.write_session(updates=[self.update(
            usage=self.usage(input_tokens=300, output_tokens=30, cache_read=120,
                             reasoning=0, total_tokens=330, models=models))])
        result = self.adapter().sync(self.db)
        self.assertEqual(result.imported, 2)
        rows = self.db.recent("grok")
        self.assertEqual({row["model"] for row in rows}, {"grok-build", "grok-fast"})
        self.assertEqual(self.db.summary("grok")["total"], 330)
        self.assertEqual(self.db.summary("grok")["requests"], 2)

    def test_repeat_and_rewritten_turn_do_not_double_count(self):
        folder = self.write_session(updates=[self.update(
            usage=self.usage(models={"grok-build": {"inputTokens": 1000, "outputTokens": 100,
                                                     "totalTokens": 1100}}))])
        adapter = self.adapter()
        self.assertEqual(adapter.sync(self.db).imported, 1)
        self.assertEqual(adapter.sync(self.db).imported, 0)
        with (folder / "updates.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.update(event_id="event-2", usage=self.usage(
                models={"grok-build": {"inputTokens": 1000, "outputTokens": 200,
                                       "totalTokens": 1200}}))) + "\n")
        self.assertEqual(adapter.sync(self.db).imported, 1)
        summary = self.db.summary("grok")
        self.assertEqual((summary["requests"], summary["total"]), (1, 1200))

    def test_subagent_session_is_not_counted_twice(self):
        parent_usage = self.usage(input_tokens=3000, output_tokens=300, cache_read=1200,
                                  reasoning=60, total_tokens=3300,
                                  models={"mock-model": {"inputTokens": 3000, "outputTokens": 300,
                                                         "totalTokens": 3300}})
        child_usage = self.usage(models={"mock-model": {"inputTokens": 1000, "outputTokens": 100,
                                                        "totalTokens": 1100}})
        parent = self.write_session("parent", updates=[self.update(
            prompt_id="parent-prompt", session_id="parent", usage=parent_usage)])
        self.write_session("child", updates=[self.update(
            prompt_id="child-prompt", session_id="child", usage=child_usage)])
        meta_dir = parent / "subagents" / "child"
        meta_dir.mkdir(parents=True)
        (meta_dir / "meta.json").write_text(json.dumps({
            "subagent_id": "child", "parent_session_id": "parent", "child_session_id": "child",
        }), encoding="utf-8")
        result = self.adapter().sync(self.db)
        self.assertEqual((result.scanned, result.imported, result.skipped), (2, 1, 1))
        summary = self.db.summary("grok")
        self.assertEqual((summary["requests"], summary["total"]), (1, 3300))

    def test_model_falls_back_to_session_summary(self):
        self.write_session(summary={"current_model_id": "grok-4.5"},
                           updates=[self.update(usage=self.usage(models=None))])
        result = self.adapter().sync(self.db)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.db.recent("grok")[0]["model"], "grok-4.5")

    def test_missing_usage_or_root_is_reported_without_import(self):
        self.write_session(updates=[self.update(usage=None)])
        result = self.adapter().sync(self.db)
        self.assertEqual((result.imported, result.scanned), (0, 1))
        self.assertEqual(self.db.summary("grok")["requests"], 0)
        missing = self.adapter()
        missing._root = lambda: self.root / "absent"
        result = missing.sync(self.db)
        self.assertEqual(result.availability, "not_found")
        self.assertEqual(result.scanned, 0)

    def test_grok_home_environment_override(self):
        with patch.dict("os.environ", {"GROK_HOME": str(self.root), "TOKENOTE_GROK_DIR": ""}, clear=False):
            self.assertEqual(GrokAdapter()._root(), self.root)


if __name__ == "__main__":
    unittest.main()
