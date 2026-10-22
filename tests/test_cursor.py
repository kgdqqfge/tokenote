from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.cursor import CursorAdapter
from tokenote.db import UsageDatabase


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = UsageDatabase(self.root / 'meter.db')

    def state_db(self, rows, name='state.vscdb'):
        path = self.root / name
        with closing(sqlite3.connect(path)) as con:
            con.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)')
            con.executemany('INSERT INTO cursorDiskKV VALUES (?,?)',
                            [(key, json.dumps(value)) for key, value in rows])
            con.commit()
        return path

    def adapter(self, *paths):
        adapter = CursorAdapter()
        adapter._state_dbs = lambda: list(paths)
        return adapter

    @staticmethod
    def composer(model='grok-4.6', created=1792548800000):
        config = {'modelName': 'default', 'selectedModels': [{'modelId': model}]} if model else {}
        return {'createdAt': created, 'modelConfig': config}

    @staticmethod
    def bubble(kind=2, input_tokens=0, output_tokens=0, **fields):
        value = {'type': kind, 'tokenCount': {'inputTokens': input_tokens, 'outputTokens': output_tokens}}
        value.update(fields)
        return value

    def test_assistant_bubble_counts_and_composer_model_fallback(self):
        path = self.state_db([
            ('composerData:c1', self.composer()),
            ('bubbleId:c1:u1', self.bubble(kind=1, input_tokens=999, text='private message must not be copied')),
            ('bubbleId:c1:b1', self.bubble(input_tokens=100, output_tokens=20, createdAt='2026-10-21T02:24:06.055Z',
                                           text='private message must not be copied')),
            ('bubbleId:c1:b2', self.bubble(input_tokens=30, output_tokens=5, createdAt='2026-10-21T02:26:11.024Z')),
        ])
        result = self.adapter(path).sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.availability, 'ready')
        self.assertEqual(result.imported, 2)
        summary = self.db.summary('cursor')
        self.assertEqual((summary['requests'], summary['input'], summary['fresh_input'], summary['output'], summary['total']),
                         (2, 130, 130, 25, 155))
        rows = self.db.recent('cursor')
        self.assertTrue(all('private message' not in row['raw_usage_json'] for row in rows))
        self.assertEqual(rows[0]['model'], 'grok-4.6')
        self.assertEqual(rows[0]['session_id'], 'c1')
        self.assertEqual(rows[0]['source_kind'], 'cursor_state_db')
        self.assertEqual(rows[0]['capture_mode'], 'sqlite_snapshot')
        self.assertEqual(rows[0]['occurred_at'],
                         int(datetime.fromisoformat('2026-10-21T02:26:11.024+00:00').timestamp()))

    def test_bubble_model_wins_over_composer(self):
        path = self.state_db([
            ('composerData:c1', self.composer(model='composer-model')),
            ('bubbleId:c1:b1', self.bubble(input_tokens=10, output_tokens=2,
                                           modelInfo={'modelName': 'bubble-model'}, createdAt='2026-10-21T02:00:00Z')),
        ])
        self.adapter(path).sync(self.db)
        self.assertEqual(self.db.model_totals('cursor')[0]['model'], 'bubble-model')

    def test_zero_counts_and_incomplete_bubbles_create_no_records(self):
        path = self.state_db([
            ('composerData:c1', self.composer()),
            ('bubbleId:c1:u1', self.bubble(kind=1)),
            ('bubbleId:c1:b1', self.bubble()),
            ('bubbleId:c1:b2', {'type': 2}),
            ('bubbleId:broken', {'type': 2, 'tokenCount': {'inputTokens': 100, 'outputTokens': 10}}),
            ('bubbleId:bad-json', 'not-an-object'),
        ])
        result = self.adapter(path).sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.skipped, 5)
        self.assertEqual(self.db.summary('cursor')['requests'], 0)

    def test_latest_turn_usage_is_not_used_without_bubble_counts(self):
        composer = self.composer()
        composer['turnTokenUsage'] = {'inputTokens': 500, 'outputTokens': 50,
                                      'cacheReadTokens': 400, 'cacheWriteTokens': 0}
        composer['usageData'] = {'inputTokens': 500, 'outputTokens': 50}
        path = self.state_db([
            ('composerData:c1', composer),
            ('bubbleId:c1:b1', self.bubble()),
        ])
        self.adapter(path).sync(self.db)
        self.assertEqual(self.db.summary('cursor')['requests'], 0)

    def test_repeat_sync_replaces_instead_of_double_counting(self):
        path = self.state_db([
            ('composerData:c1', self.composer()),
            ('bubbleId:c1:b1', self.bubble(input_tokens=100, output_tokens=20, createdAt='2026-10-21T02:00:00Z')),
        ])
        adapter = self.adapter(path)
        adapter.sync(self.db)
        adapter.sync(self.db)
        self.assertEqual((self.db.summary('cursor')['requests'], self.db.summary('cursor')['total']), (1, 120))
        with closing(sqlite3.connect(path)) as con:
            con.execute("UPDATE cursorDiskKV SET value=? WHERE key='bubbleId:c1:b1'",
                        (json.dumps(self.bubble()),))
            con.commit()
        adapter.sync(self.db)
        self.assertEqual(self.db.summary('cursor')['requests'], 0)

    def test_missing_source_and_missing_table(self):
        self.assertEqual(self.adapter().sync(self.db).availability, 'not_found')
        path = self.root / 'empty.vscdb'
        with closing(sqlite3.connect(path)) as con:
            con.execute('CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)')
            con.commit()
        result = self.adapter(path).sync(self.db)
        self.assertEqual(result.availability, 'ready')
        self.assertIn('cursorDiskKV', result.error)

    def test_schema_error_keeps_previous_results(self):
        path = self.state_db([
            ('composerData:c1', self.composer()),
            ('bubbleId:c1:b1', self.bubble(input_tokens=100, output_tokens=20, createdAt='2026-10-21T02:00:00Z')),
        ])
        adapter = self.adapter(path)
        adapter.sync(self.db)
        path.unlink()
        with closing(sqlite3.connect(path)) as con:
            con.execute('CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)')
            con.commit()
        self.assertIsNotNone(adapter.sync(self.db).error)
        self.assertEqual(self.db.summary('cursor')['total'], 120)

    def test_composer_timestamp_is_only_a_fallback(self):
        path = self.state_db([
            ('composerData:c1', self.composer(created=1792548800000)),
            ('bubbleId:c1:b1', self.bubble(input_tokens=10, output_tokens=2)),
        ])
        self.adapter(path).sync(self.db)
        self.assertEqual(self.db.recent('cursor')[0]['occurred_at'], 1792548800)

    def test_state_db_environment_override(self):
        path = self.state_db([
            ('composerData:c1', self.composer()),
            ('bubbleId:c1:b1', self.bubble(input_tokens=10, output_tokens=2, createdAt='2026-10-21T02:00:00Z')),
        ], name='custom.vscdb')
        with patch.dict('os.environ', {'TOKENOTE_CURSOR_DB': str(path)}):
            adapter = CursorAdapter()
            self.assertEqual(adapter._state_dbs(), [path])
            self.assertEqual(adapter.sync(self.db).imported, 1)


if __name__ == '__main__':
    unittest.main()
