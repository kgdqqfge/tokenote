from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.antigravity import AntigravityAdapter
from tokenote.db import UsageDatabase


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _vint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _usage(fresh: int, cache: int, output: int, thoughts: int = 0, visible: int = 0) -> bytes:
    return b''.join([
        _vint(1, 1400),
        _vint(2, fresh),
        *([_vint(3, output)] if output else []),
        *([_vint(5, cache)] if cache else []),
        _vint(6, 24),
        *([_vint(9, thoughts)] if thoughts else []),
        *([_vint(10, visible)] if visible else []),
    ])


def _step_metadata(index: int, seconds: int, usage: bytes, generation: int | None = None) -> bytes:
    context = b''.join([
        _bytes_field(1, b'trajectory-1'),
        _vint(2, index),
        *([_vint(3, generation)] if generation else []),
        _bytes_field(4, b'cascade-1'),
    ])
    return b''.join([
        _bytes_field(1, _vint(1, seconds)),
        _bytes_field(9, usage),
        _bytes_field(20, context),
    ])


def _gen_metadata(model: str) -> bytes:
    return _bytes_field(1, b''.join([
        _bytes_field(19, model.encode('utf-8')),
        _bytes_field(20, b''.join([
            _bytes_field(1, b'last_step_index'),
            _bytes_field(2, b'2'),
        ])),
    ]))


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.antigravity = self.root / '.gemini' / 'antigravity'
        self.conversations = self.antigravity / 'conversations'
        self.database = self.conversations / 'conversation-1.db'
        self.meter = UsageDatabase(self.root / 'meter.db')
        self.adapter = AntigravityAdapter()
        env = patch.dict(os.environ, {'TOKENOTE_ANTIGRAVITY_DIR': str(self.antigravity)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, steps=(), gens=()):
        self.conversations.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS steps (
                    idx integer PRIMARY KEY,
                    step_type integer NOT NULL DEFAULT 0,
                    status integer NOT NULL DEFAULT 0,
                    metadata blob,
                    step_payload blob
                );
                CREATE TABLE IF NOT EXISTS gen_metadata (
                    idx integer PRIMARY KEY,
                    data blob,
                    size integer NOT NULL DEFAULT 0
                );
            """)
            con.executemany('INSERT INTO steps (idx, step_type, metadata) VALUES (?, ?, ?)', steps)
            con.executemany(
                'INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)',
                [(index, data, len(data)) for index, data in gens],
            )
            con.commit()

    def test_reads_usage_and_maps_generation_model(self):
        self.write(
            steps=[
                (0, 14, None),
                (1, 15, _step_metadata(1, 1_793_422_125, _usage(1000, 0, 300, thoughts=120, visible=180), 0)),
                (2, 132, None),
                (3, 15, _step_metadata(3, 1_793_422_130, _usage(200, 1300, 50, visible=50), 1)),
            ],
            gens=[(0, _gen_metadata('gemini-test')), (1, _gen_metadata('claude-test'))],
        )
        result = self.adapter.sync(self.meter)
        self.assertIsNone(result.error)
        self.assertEqual((result.scanned, result.imported), (1, 2))
        summary = self.meter.summary('antigravity')
        self.assertEqual(
            (summary['requests'], summary['input'], summary['fresh_input'], summary['output'],
             summary['cache'], summary['total']),
            (2, 2500, 1200, 350, 1300, 2850),
        )
        models = self.meter.model_totals('antigravity')
        self.assertEqual([(row['model'], row['total']) for row in models],
                         [('claude-test', 1550), ('gemini-test', 1300)])
        recent = self.meter.recent('antigravity')
        self.assertEqual(recent[0]['occurred_at'], 1_793_422_130)
        self.assertEqual(recent[0]['reasoning_tokens'], 0)
        self.assertEqual(recent[1]['reasoning_tokens'], 120)
        self.assertEqual(self.adapter.sync(self.meter).skipped, 1)
        self.assertEqual(self.meter.summary('antigravity')['requests'], 2)

    def test_zero_usage_and_broken_metadata_are_ignored(self):
        self.write(
            steps=[
                (1, 15, _step_metadata(1, 1_793_422_125, _usage(0, 0, 0))),
                (3, 15, b'\xff\xff'),
            ],
            gens=[(0, _gen_metadata('gemini-test'))],
        )
        result = self.adapter.sync(self.meter)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 0)
        self.assertEqual(self.meter.summary('antigravity')['requests'], 0)

    def test_missing_generation_row_keeps_usage_without_model(self):
        self.write(steps=[(1, 15, _step_metadata(1, 1_793_422_125, _usage(10, 0, 5), 7))])
        self.assertEqual(self.adapter.sync(self.meter).imported, 1)
        self.assertEqual(self.meter.summary('antigravity')['total'], 15)
        self.assertEqual(self.meter.model_totals('antigravity')[0]['model'], '未知模型')

    def test_new_step_in_existing_database_is_resynced(self):
        self.write(steps=[(1, 15, _step_metadata(1, 1_793_422_125, _usage(10, 0, 5), 0))],
                   gens=[(0, _gen_metadata('gemini-test'))])
        self.adapter.sync(self.meter)
        self.write(steps=[(3, 15, _step_metadata(3, 1_793_422_130, _usage(20, 0, 7), 1))],
                   gens=[(1, _gen_metadata('gemini-test'))])
        result = self.adapter.sync(self.meter)
        self.assertEqual((result.imported, result.skipped), (2, 0))
        self.assertEqual(self.meter.summary('antigravity')['total'], 42)

    def test_configured_conversations_folder_is_accepted(self):
        self.write(steps=[], gens=[])
        with patch.dict(os.environ, {'TOKENOTE_ANTIGRAVITY_DIR': str(self.conversations)}):
            paths = self.adapter._conversation_dbs()
        self.assertEqual([path.name for path in paths], ['conversation-1.db'])

    def test_missing_source_reports_not_found(self):
        result = self.adapter.sync(self.meter)
        self.assertEqual(result.availability, 'not_found')
        self.assertEqual(result.scanned, 0)


if __name__ == '__main__':
    unittest.main()
