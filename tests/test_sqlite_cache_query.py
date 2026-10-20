"""Literal text search and bounded global numeric ordering with no HTTP."""
import contextlib
import importlib.util
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = 'silero_tts_studio_under_test'
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, Path(__file__).resolve().parents[1] / 'SileroTTS_Studio.py')
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'index.sqlite'
        self.index = studio.SQLiteCacheIndex(self.path)
        self.addCleanup(self.index.close)

    def read_all(self, **kwargs):
        pages = list(self.index.iter_pages(limit=2, max_scan_rows=3, **kwargs))
        return [key for page in pages for key, _ in page['rows']]

    def raw_count(self):
        with contextlib.closing(sqlite3.connect(self.path)) as source:
            return source.execute('SELECT count(*) FROM entries').fetchone()[0]

    def test_search_covers_current_text_and_historical_aliases_unicode_literal(self):
        for key, field, value in [('a', 'original_text', 'СЫРОЙ ЁЖ'), ('b', 'normalized_text', 'Норма ЕЖ'),
                                  ('c', 'raw_text', '100%_сЫрОй'), ('d', 'norm_text', 'ЁЖ НОРМА')]:
            self.index.upsert(key, {field: value, 'unknown': {'kept': 1}})
        self.assertEqual(self.read_all(search='ёж'), ['a', 'd'])
        self.assertEqual(self.read_all(search='норма'), ['b', 'd'])
        self.assertEqual(self.read_all(search='%_'), ['c'])
        self.assertEqual(self.read_all(search='A'), ['a'])
        self.assertEqual(self.raw_count(), 0)
        self.assertTrue(studio.cache_record_matches_search('x', {'normalized_text': 'ЁЖ'}, 'ёж'))
        self.assertFalse(studio.cache_record_matches_search('x', {'unknown': 'ёж'}, 'ёж'))

    def test_global_numeric_sorts_include_ties_and_invalid_values_without_changes(self):
        values = {'b': 5, 'a': 5, 'c': 2, 'd': None, 'e': True, 'f': '9', 'g': -1, 'h': 10 ** 400}
        for key, value in values.items():
            self.index.upsert(key, {field: value for field in ('usage_count', 'created_at', 'last_accessed')})
        expected_asc = ['d', 'e', 'f', 'g', 'h', 'c', 'a', 'b']
        expected_desc = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
        for field in ('usage_count', 'created_at', 'last_accessed'):
            self.assertEqual(self.read_all(sort_by=field), expected_asc)
            self.assertEqual(self.read_all(sort_by=field, descending=True), expected_desc)
        self.assertEqual(self.raw_count(), 0)
        self.assertEqual(self.index.get('h')['usage_count'], 10 ** 400)
        self.assertEqual(studio.cache_record_sort_value({'usage_count': float('inf')}, 'usage_count'), 0)
        self.assertEqual(studio.cache_record_sort_value({'created_at': -5}, 'created_at'), 0)

    def test_pending_touch_upsert_delete_sort_visible_without_main_commit(self):
        self.index.upsert('a', {'usage_count': 1, 'last_accessed': 10})
        self.index.upsert('b', {'usage_count': 2, 'last_accessed': 20})
        self.index.flush()
        self.index.touch('a', 30, increment=5)
        self.index.upsert('c', {'usage_count': 8, 'last_accessed': 25})
        self.index.delete('b')
        self.assertEqual(self.read_all(sort_by='usage_count', descending=True), ['c', 'a'])
        self.assertEqual(self.read_all(sort_by='last_accessed', descending=True), ['a', 'c'])
        self.assertEqual(self.raw_count(), 2)
        self.assertGreater(self.index.pending_count, 0)

    def test_sorted_cursor_is_bound_to_field_direction_and_validated(self):
        self.index.upsert('a', {'usage_count': 1})
        cursor = self.index.query_page(sort_by='usage_count')['cursor']
        for kwargs in ({'sort_by': 'created_at', 'cursor': cursor},
                       {'sort_by': 'usage_count', 'descending': True, 'cursor': cursor},
                       {'sort_by': 'usage_count', 'cursor': ('usage_count', False, float('inf'), 'a')},
                       {'sort_by': 'usage_count', 'cursor': ('usage_count', False, -1, 'a')},
                       {'sort_by': 'usage_count', 'cursor': ['usage_count', False, 1, 'a']},
                       {'sort_by': 'usage_count); DROP TABLE entries; --'},
                       {'sort_by': 'key', 'descending': True}):
            with self.assertRaises(ValueError):
                self.index.query_page(**kwargs)
        self.assertEqual(self.index.get('a'), {'usage_count': 1})

    def test_sorted_byte_bounds_and_sparse_search_advance_cursor(self):
        for number in range(12):
            self.index.upsert(str(number), {'usage_count': number, 'normalized_text': 'x' * 80 + ('match' if number == 1 else '')})
        page = self.index.query_page(sort_by='usage_count', descending=True, max_bytes=140, limit=10)
        self.assertLessEqual(page['bytes'], 140)
        self.assertEqual(page['stop_reason'], 'byte_limit')
        self.assertEqual(self.read_all(sort_by='usage_count', descending=True, search='match'), ['1'])
        with self.assertRaises(ValueError):
            self.index.query_page(sort_by='usage_count', max_bytes=1)

    def test_actual_sorted_query_seeks_bounded_branches_for_deep_duplicate_ties(self):
        for number in range(200):
            self.index.upsert(f'{number:05}', {'usage_count': 0, 'created_at': 1, 'last_accessed': 2})
        self.index.flush()
        self.index.upsert('00075', {'usage_count': 0})
        statements, plans = [], []
        original_dispatch = self.index._dispatch
        def trace(connection, operation, args):
            if operation == 'query_page':
                connection.set_trace_callback(statements.append)
                try:
                    result = original_dispatch(connection, operation, args)
                    statement = next(sql for sql in statements if sql.startswith('SELECT CASE WHEN length'))
                    plans.extend(connection.execute('EXPLAIN QUERY PLAN ' + statement).fetchall())
                    return result
                finally:
                    connection.set_trace_callback(None)
            return original_dispatch(connection, operation, args)
        cursor = ('usage_count', True, 0.0, '00070')
        with mock.patch.object(self.index, '_dispatch', side_effect=trace):
            page = self.index.query_page(sort_by='usage_count', descending=True, cursor=cursor, limit=5, max_scan_rows=8)
        self.assertEqual([key for key, _ in page['rows']], ['00071', '00072', '00073', '00074', '00075'])
        details = [row[3] for row in plans]
        self.assertTrue(any('SEARCH entries USING INDEX entries_sort_usage_count_desc' in item for item in details), details)
        self.assertTrue(any('SEARCH pending_read USING INDEX pending_read_sort_usage_count_desc' in item for item in details), details)
        statement = next(sql for sql in statements if sql.startswith('SELECT CASE WHEN length'))
        self.assertNotIn('OFFSET', statement)
        self.assertEqual(statement.count('LIMIT 8'), 5, statement)
        self.assertEqual(self.raw_count(), 200)

    def test_fifty_thousand_equal_values_use_bounded_vm_steps_at_deep_cursor(self):
        # Populate through the owner, outside the measured read. Equal usage
        # values reproduce the largest remaining tie range of a speech cache.
        original_dispatch = self.index._dispatch
        def seed(connection, operation, args):
            connection.executemany('INSERT INTO entries VALUES (?,?,?,?,?)',
                ((f'{number:05}', '{"usage_count":0}', None, None, None) for number in range(50000)))
            connection.commit()
        with mock.patch.object(self.index, '_dispatch', side_effect=seed):
            self.index._request('seed')
        self.index.query_page(sort_by='usage_count', descending=True, limit=1)
        counts = []
        def measure(connection, operation, args):
            count = 0
            def step():
                nonlocal count
                count += 1
                return 0
            connection.set_progress_handler(step, 1)
            try:
                result = original_dispatch(connection, operation, args)
            finally:
                connection.set_progress_handler(None, 0)
            counts.append(count)
            return result
        with mock.patch.object(self.index, '_dispatch', side_effect=measure):
            first = self.index.query_page(sort_by='usage_count', descending=True,
                cursor=('usage_count', True, 0.0, '00010'), limit=5, max_scan_rows=8)
            deep = self.index.query_page(sort_by='usage_count', descending=True,
                cursor=('usage_count', True, 0.0, '40000'), limit=5, max_scan_rows=8)
        self.assertEqual([key for key, _ in deep['rows']], ['40001', '40002', '40003', '40004', '40005'])
        self.assertTrue(all(count < 5000 for count in counts), counts)
        self.assertLess(max(counts), min(counts) * 2 + 100, counts)


if __name__ == '__main__':
    unittest.main()
