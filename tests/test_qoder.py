from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tokenote.adapters.qoder import QoderAdapter
from tokenote.credits import CreditEvent
from tokenote.db import SCHEMA, UsageDatabase
from tokenote.models import UsageEvent


class QoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.qoder = self.root / '.qoder-cn'
        self.db = UsageDatabase(self.root / 'meter.db')
        self.adapter = QoderAdapter()
        self.adapter._roots = lambda: [self.qoder]
        self.now = datetime.now().astimezone().isoformat()

    def row(self, request='r1', credits=1.25, billable=False, **counts):
        usage = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0,
                 'cache_creation_input_tokens': 0, 'request_id': request, 'context_usage_ratio': 0.4}
        if credits is not None:
            usage.update(credits=credits, original_credits=credits)
        if billable is not None:
            usage['billable'] = billable
        usage.update(counts)
        return {'type': 'assistant', 'uuid': 'fragment-' + request, 'sessionId': 's1', 'timestamp': self.now,
                'message': {'id': 'message-' + request, 'role': 'assistant', 'model': 'dfmodel',
                            'content': 'private message must not be copied', 'usage': usage}}

    def write(self, rows, name='session'):
        path = self.qoder / 'projects' / 'project' / (name + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return path

    def test_first_party_credits_do_not_create_fake_token_records(self):
        self.write([self.row()])
        result = self.adapter.sync(self.db)
        self.assertIsNone(result.error)
        self.assertEqual(result.imported, 1)
        report = self.db.credit_summary('qoder')
        self.assertEqual((report['requests'], report['credits'], report['billed_credits'], report['token_requests']), (1, 1.25, 0, 0))
        self.assertEqual((self.db.summary()['requests'], self.db.summary()['total']), (0, 0))
        row = self.db.credit_recent('qoder')[0]
        self.assertIsNone(row['token_total'])
        self.assertNotIn('private message', row['raw_usage_json'])
        self.assertNotIn('context_usage_ratio', self.db.summary())
        self.assertEqual(self.adapter.sync(self.db).skipped, 1)

    def test_actual_byok_tokens_remain_separate_from_optional_credits(self):
        self.write([self.row(credits=None, billable=None, input_tokens=20, output_tokens=10,
                             cache_read_input_tokens=70, cache_creation_input_tokens=5, reasoning_tokens=4)])
        self.adapter.sync(self.db)
        summary = self.db.summary('qoder')
        self.assertEqual((summary['input'], summary['fresh_input'], summary['output'], summary['cache'], summary['total']),
                         (95, 20, 10, 70, 105))
        report = self.db.credit_summary('qoder')
        self.assertIsNone(report['credits'])
        self.assertIsNone(report['billed_credits'])
        self.assertEqual(report['token_requests'], 1)
        joined = self.db.credit_recent_with_tokens('qoder')[0]
        self.assertEqual((joined['input_tokens'], joined['output_tokens'], joined['token_total']), (95, 10, 105))

    def test_request_id_deduplicates_fragments_copies_and_result_totals(self):
        first = self.row()
        repeated = self.row()
        repeated['uuid'] = 'different-fragment'
        repeated['message']['id'] = 'different-message'
        self.write([first, repeated, {'type': 'result', 'total_credits': 100, 'usage': {'credits': 100}}])
        self.write([first], name='copied-session')
        self.adapter.sync(self.db)
        self.assertEqual((self.db.credit_summary()['requests'], self.db.credit_summary()['credits']), (1, 1.25))

    def test_later_credit_or_billing_correction_replaces_existing_request(self):
        path = self.write([self.row(credits=1, billable=None)])
        self.adapter.sync(self.db)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(self.row(credits=0.5, billable=True)) + '\n')
        result = self.adapter.sync(self.db)
        self.assertEqual((result.imported, result.updated), (0, 1))
        summary = self.db.credit_summary('qoder')
        self.assertEqual((summary['requests'], summary['credits'], summary['billed_credits']), (1, 0.5, 0.5))

    def test_zero_credits_are_known_while_missing_credit_or_billable_is_unknown(self):
        self.write([self.row('free', credits=0, billable=False), self.row('paid', credits=0.5, billable=True),
                    self.row('unspecified', credits=0.25, billable=None)])
        self.adapter.sync(self.db)
        report = self.db.credit_summary('qoder')
        self.assertEqual((report['requests'], report['credits'], report['billed_credits'], report['billing_reports']), (3, 0.75, 0.5, 2))
        missing = CreditEvent('missing', 'qoder', credits=None, billable=None)
        self.db.upsert_credit_events([missing])
        self.assertEqual(self.db.credit_summary('qoder')['credit_reports'], 3)
        self.assertIsNone(self.db.credit_recent('qoder')[-1]['token_total'])

    def test_incomplete_tail_waits_and_failed_write_does_not_advance_cursor(self):
        path = self.write([self.row()])
        extra = (json.dumps(self.row('r2', credits=0.5)) + '\n').encode('utf-8')
        with path.open('ab') as handle:
            handle.write(extra[:-3])
        with patch.object(self.db, 'upsert_credit_events', side_effect=OSError('simulated write failure')):
            failed = self.adapter.sync(self.db)
        self.assertIsNotNone(failed.error)
        self.assertIsNone(self.db.get_cursor(f'qoder:{path.resolve()}'))
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        with path.open('ab') as handle:
            handle.write(extra[-3:])
        self.assertEqual(self.adapter.sync(self.db).imported, 1)
        self.assertEqual(self.db.credit_summary()['credits'], 1.75)

    def test_raw_openai_aliases_and_context_do_not_estimate_tokens(self):
        row = self.row(credits=None, billable=None, prompt_tokens=100, completion_tokens=20,
                       prompt_tokens_details={'cached_tokens': 80}, total_tokens=120)
        row['message']['model'] = None
        self.write([{'type': 'runtime-config', 'sessionId': 's1', 'model': 'custom-model'}, row])
        self.adapter.sync(self.db)
        summary = self.db.summary('qoder')
        self.assertEqual((summary['input'], summary['fresh_input'], summary['total']), (100, 20, 120))
        self.assertEqual(self.db.model_totals('qoder')[0]['model'], 'custom-model')

    def test_credit_queries_and_exports_preserve_unknown_token_counts(self):
        self.write([self.row('r1', credits=0.25, billable=False), self.row('r2', credits=0.5, billable=True)])
        self.adapter.sync(self.db)
        self.assertEqual(self.db.credit_daily_totals('qoder', days=1)[0]['total'], 0.75)
        model = self.db.credit_model_totals('qoder')[0]
        self.assertEqual((model['model'], model['requests'], model['total']), ('dfmodel', 2, 0.75))
        self.assertEqual(self.db.credit_app_totals('qoder')[0]['total'], 0.75)
        credits_path = self.root / 'credits.csv'
        token_path = self.root / 'tokens.csv'
        self.db.export_credits_csv(credits_path, 'qoder')
        self.db.export_credit_tokens_csv(token_path, 'qoder')
        with credits_path.open(encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row['token_total'] == '' for row in rows))
        self.assertEqual({row['billable'] for row in rows}, {'0', '1'})
        with token_path.open(encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row['input_tokens'] == row['output_tokens'] == row['token_total'] == '' for row in rows))

    def test_credit_model_breakdown_recent_window(self):
        self.db.upsert_credit_events([
            CreditEvent('recent', 'qoder', model='recent-model', occurred_at=2_000, credits=1.5),
            CreditEvent('stale', 'qoder', model='stale-model', occurred_at=1_000, credits=9),
        ])
        rows = self.db.credit_model_totals('qoder', since=1_500)
        self.assertEqual([(row['model'], row['total'], row['credit_reports']) for row in rows],
                         [('recent-model', 1.5, 1)])

    def test_migration_keeps_existing_token_data_and_totals(self):
        old_path = self.root / 'old.db'
        with closing(sqlite3.connect(old_path)) as con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO usage_event (event_key,app_type,input_tokens,output_tokens,total_tokens,source_kind,capture_mode) "
                        "VALUES ('existing','codex',100,20,120,'test','jsonl')")
            con.commit()
        old = UsageDatabase(old_path)
        self.assertEqual(old.summary()['total'], 120)
        old.upsert_credit_events([CreditEvent('credits', 'qoder', credits=100)])
        reopened = UsageDatabase(old_path)
        self.assertEqual((reopened.summary()['requests'], reopened.summary()['total']), (1, 120))
        self.assertEqual(reopened.credit_summary('qoder')['credits'], 100)

    def test_bad_file_does_not_block_other_sources_or_delete_previous_results(self):
        path = self.write([self.row()])
        self.adapter.sync(self.db)
        path.write_text('{broken}\n', encoding='utf-8')
        self.write([self.row('valid')], name='other')
        result = self.adapter.sync(self.db)
        self.assertIsNotNone(result.error)
        self.assertEqual(self.db.credit_summary()['requests'], 2)

    def test_no_data_reports_not_found_and_empty_token_placeholders_are_not_usage(self):
        self.assertEqual(self.adapter.sync(self.db).availability, 'not_found')
        self.write([self.row(credits=None, billable=None)])
        self.assertEqual(self.adapter.sync(self.db).imported, 0)
        self.assertEqual(self.db.credit_summary()['requests'], 0)


if __name__ == '__main__':
    unittest.main()
