from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenote.instance import SingleInstance, WAIT_OBJECT_0

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import json, sys
from pathlib import Path
from tokenote.instance import SingleInstance
with SingleInstance(Path(sys.argv[1])) as instance:
    print(json.dumps({'acquired': instance.acquired}), flush=True)
"""
HOLDER = PROBE.replace("    print(json.dumps({'acquired': instance.acquired}), flush=True)",
                       "    print(json.dumps({'acquired': instance.acquired}), flush=True)\n    sys.stdin.readline()")


@unittest.skipUnless(sys.platform == "win32", "Windows kernel instance guard")
class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "usage.db"
        self.environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def run_child(self, *arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=15, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def probe(self, db_path=None):
        return json.loads(self.run_child("-c", PROBE, str(db_path or self.db_path)).stdout)["acquired"]

    def test_second_process_is_rejected_and_does_not_release_primary_lock(self):
        with SingleInstance(self.db_path) as owner:
            self.assertTrue(owner.acquired)
            self.assertFalse(self.probe())
            self.assertFalse(self.probe())
        self.assertTrue(self.probe())

    def test_different_database_has_independent_lock(self):
        with SingleInstance(self.db_path):
            self.assertTrue(self.probe(self.db_path.with_name("different.db")))

    def test_path_aliases_and_case_share_one_lock(self):
        with SingleInstance(self.db_path):
            alias = self.db_path.parent / "nested" / ".." / self.db_path.name
            self.assertFalse(self.probe(alias))
            self.assertFalse(self.probe(Path(str(self.db_path).upper())))

    def test_duplicate_gui_signals_owner_before_any_database_is_created(self):
        with SingleInstance(self.db_path) as owner:
            result = self.run_child("app.py", "--db", str(self.db_path))
            self.assertEqual(result.stdout, "")
            self.assertEqual(owner._kernel.WaitForSingleObject(owner._event, 0), WAIT_OBJECT_0)
            self.assertFalse(self.db_path.exists())

    def test_sync_only_is_skipped_when_database_is_in_use(self):
        with SingleInstance(self.db_path):
            result = self.run_child("app.py", "--sync-only", "--db", str(self.db_path))
            self.assertIn("已跳过本次同步", result.stdout)
            self.assertFalse(self.db_path.exists())

    def test_startup_exception_releases_lock(self):
        import app as entry
        with patch.object(sys, "argv", ["app.py", "--db", str(self.db_path)]), \
             patch.object(entry, "ensure_data_dir"), \
             patch.object(entry, "run_gui", side_effect=RuntimeError("simulated startup failure")):
            with self.assertRaisesRegex(RuntimeError, "simulated startup failure"):
                entry.main()
        self.assertTrue(self.probe())

    def test_terminated_owner_allows_abandoned_mutex_to_be_acquired(self):
        child = subprocess.Popen([sys.executable, "-u", "-c", HOLDER, str(self.db_path)], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=self.environment)
        try:
            ready = json.loads(child.stdout.readline())
            self.assertTrue(ready["acquired"])
            with SingleInstance(self.db_path) as rejected:
                self.assertFalse(rejected.acquired)
                child.kill()
                child.communicate(timeout=10)
                with SingleInstance(self.db_path) as replacement:
                    self.assertTrue(replacement.acquired)
                    self.assertFalse(self.probe())
            self.assertTrue(self.probe())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
