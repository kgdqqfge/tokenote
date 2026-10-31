from __future__ import annotations

import json
import time
from dataclasses import asdict

from .adapters import CodexAdapter, DeepSeekHarnessAdapter, KimiAdapter, OpenCodeAdapter, TraeAdapter, WorkBuddyAdapter
from .adapters.base import SyncResult
from .adapters.openclaw import OpenClawAdapter
from .adapters.qoder import QoderAdapter
from .adapters.grok import GrokAdapter
from .adapters.antigravity import AntigravityAdapter
from .adapters.additional import ClaudeCodeAdapter, PiAdapter, HermesAdapter, MiniMaxCodeAdapter, ZCodeAdapter
from .db import UsageDatabase


class SyncService:
    def __init__(self, db: UsageDatabase):
        self.db = db
        self.adapters = [
            CodexAdapter(),
            TraeAdapter(),
            WorkBuddyAdapter(),
            OpenCodeAdapter(),
            KimiAdapter(),
            ZCodeAdapter(),
            ClaudeCodeAdapter(),
            PiAdapter(),
            HermesAdapter(),
            MiniMaxCodeAdapter(),
            DeepSeekHarnessAdapter(),
            OpenClawAdapter(),
            QoderAdapter(),
            GrokAdapter(),
            AntigravityAdapter(),
        ]

    def _save_status(self, results, started_at, current=None, finished=False):
        status_path = self.db.path.parent / "sync-status.json"
        temp_path = status_path.with_suffix(".tmp")
        status = {
            "started_at": started_at,
            "updated_at": int(time.time()),
            "finished": finished,
            "current": current,
            "results": [asdict(result) for result in results],
        }
        try:
            temp_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(status_path)
        except OSError:
            pass

    def sync_all(self):
        results = []
        started_at = int(time.time())
        for adapter in self.adapters:
            self._save_status(results, started_at, current=adapter.key)
            try:
                results.append(adapter.sync(self.db))
            except Exception as exc:  # one source must not block the others
                results.append(SyncResult(adapter.key, error=str(exc)))
            self._save_status(results, started_at, current=adapter.key)
        self._save_status(results, started_at, finished=True)
        return results
