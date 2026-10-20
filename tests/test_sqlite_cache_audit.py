"""Bounded maintenance, read-only reopening and migration race regressions."""
import importlib.util
import contextlib
import json
import sqlite3
import sys
import tempfile
import tracemalloc
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


class InstrumentedIndex(studio.SQLiteCacheIndex):
    def _open(self):
        connection = super()._open()
        self.open_changes = connection.total_changes
        wal = Path(str(self.path) + '-wal')
        self.open_wal_bytes = wal.stat().st_size if wal.exists() else 0
        return connection

    def _dispatch(self, connection, operation, args):
        if operation == 'seed_audit_fixture':
            rows = ((f'{number:08d}', json.dumps({'last_accessed': number,
                    'created_at': 1}), None, None, number) for number in range(args[0]))
            connection.executemany('INSERT INTO entries VALUES (?,?,?,?,?)', rows)
            connection.commit()
            return None
        if operation == 'inspect_audit_operation':
            target, parameters = args
            statements, steps = [], [0]
            connection.set_trace_callback(statements.append)
            connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
            try:
                value = super()._dispatch(connection, target, parameters)
            finally:
                connection.set_progress_handler(None, 0)
                connection.set_trace_callback(None)
            return value, statements, steps[0]
        return super()._dispatch(connection, operation, args)


class SQLiteCacheAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def standalone(self):
        index = InstrumentedIndex(self.root / 'index.sqlite')
        self.addCleanup(index.close)
        return index

    def active_cache(self):
        cache = self.root / 'cache'
        cache.mkdir()
        database = cache / studio._CACHE_DATABASE_NAME
        with studio.SQLiteCacheIndex(database):
            pass
        studio._write_cache_json_durable(cache / studio._CACHE_BACKEND_MARKER,
            {'schema': 1, 'backend': 'sqlite', 'database': studio._CACHE_DATABASE_NAME,
             'storage_policy': 'confirmed_local', 'synchronous': 'FULL'})
        return cache, database

    def test_count_reconciles_only_pending_keys_without_committing_or_full_scan(self):
        index = self.standalone()
        index._request('seed_audit_fixture', 20000)
        value, _, steps = index._request('inspect_audit_operation', 'count', ())
        self.assertEqual(value, 20000)
        self.assertLess(steps, 1000)
        index.upsert('new', {'unchanged_unknown': [None, True]})
        index.upsert('00000001', {'replacement': True})
        index.delete(['00000000', 'not-present'])
        index.delete('new')
        index.upsert('another-new', {})
        value, statements, steps = index._request('inspect_audit_operation', 'count', ())
        self.assertEqual(value, 20000)
        self.assertLess(steps, 1000)
        self.assertFalse(any(sql.startswith(('COMMIT', 'INSERT', 'UPDATE', 'DELETE')) for sql in statements))
        with contextlib.closing(sqlite3.connect(index.path)) as source:
            self.assertEqual(source.execute('SELECT count(*) FROM entries').fetchone()[0], 20000)
            self.assertIsNotNone(source.execute('SELECT 1 FROM entries WHERE key=?', ('00000000',)).fetchone())
            self.assertIsNone(source.execute('SELECT 1 FROM entries WHERE key=?', ('another-new',)).fetchone())
        self.assertGreater(index.pending_count, 0)

    def test_lru_batches_seek_the_access_index_and_keep_created_fallback_order(self):
        index = self.standalone()
        index._request('seed_audit_fixture', 20000)
        first = index._request('expire_lru', 0, 5)
        value, statements, steps = index._request('inspect_audit_operation', 'expire_lru', (0, 5))
        self.assertEqual([record['last_accessed'] for record in first + value], list(range(10)))
        self.assertLess(steps, 5000)
        self.assertFalse(any(sql.startswith('CREATE INDEX') for sql in statements))
        statement = next(sql for sql in statements if sql.startswith('SELECT CASE WHEN length') and 'entries.key LIMIT' in sql)
        with contextlib.closing(sqlite3.connect(index.path)) as source:
            plan = [row[3] for row in source.execute('EXPLAIN QUERY PLAN ' + statement)]
        self.assertTrue(any('entries_lru_access' in row for row in plan), plan)
        self.assertFalse(any('TEMP B-TREE' in row for row in plan), plan)
        other = studio.SQLiteCacheIndex(self.root / 'fallback.sqlite')
        self.addCleanup(other.close)
        for key, metadata in {'a': {'created_at': 5}, 'b': {'last_accessed': 'invalid', 'created_at': 3},
                              'c': {'last_accessed': 4, 'created_at': 1}, 'd': {}}.items():
            other.upsert(key, {'key_for_assertion': key, **metadata})
        removed = [record['key_for_assertion'] for batch in other.expire_cache(max_entries=0, batch_size=1) for record in batch]
        self.assertEqual(removed, ['d', 'b', 'c', 'a'])

    def test_warm_numeric_pages_do_not_repeat_schema_ddl(self):
        index = self.standalone()
        index.upsert('a', {'usage_count': 1})
        index.upsert('b', {'usage_count': 2})
        first = index.query_page(sort_by='usage_count', limit=1)
        parameters = (first['cursor'], 1, 1024 * 1024, '', None, 1000, 'usage_count', False)
        value, statements, _ = index._request('inspect_audit_operation', 'query_page', parameters)
        self.assertEqual([key for key, _ in value['rows']], ['b'])
        self.assertFalse(any(sql.startswith('CREATE INDEX') for sql in statements))
        self.assertEqual(index.pending_count, 2)

    def test_external_oversized_records_are_guarded_before_python_allocation(self):
        index = self.standalone()
        index.upsert('a', {'file_name': 'shared.ogg', 'last_accessed': 1})
        index.flush()
        record = json.dumps({'file_name': 'shared.ogg', 'last_accessed': 1,
                             'external': 'x' * (8 * 1024 * 1024)})
        with contextlib.closing(sqlite3.connect(index.path)) as source:
            source.execute('UPDATE entries SET record_json=? WHERE key=?', (record, 'a'))
            source.commit()
        del record
        for operation in (lambda: index.query_file_page('shared.ogg'),
                          lambda: next(index.expire_cache(max_entries=0))):
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(ValueError, 'byte bound'):
                    operation()
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 1024 * 1024)
        self.assertEqual(index.count(), 1)

    def test_lru_return_batches_obey_byte_cap_in_addition_to_row_cap(self):
        index = self.standalone()
        for number in range(5):
            index.upsert(str(number), {'last_accessed': number, 'payload': 'x' * 750000})
        batches = list(index.expire_cache(max_entries=0, batch_size=256))
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual([record['last_accessed'] for batch in batches for record in batch], list(range(5)))
        self.assertEqual(index.count(), 0)

    def test_runtime_reopen_writes_neither_activation_nor_wal_frames(self):
        cache, database = self.active_cache()
        with InstrumentedIndex(database, runtime=True, cache_dir=cache) as index:
            self.assertEqual(index._request('get_meta', 'activation'), 'active')
        with InstrumentedIndex(database, runtime=True, cache_dir=cache) as index:
            self.assertEqual(index.open_changes, 0)
            self.assertEqual(index.open_wal_bytes, 0)
            self.assertEqual(index.count(), 0)

    def test_invalid_timeout_fails_before_acquiring_a_writer_lease(self):
        with mock.patch.object(studio, '_CacheWriterLease') as lease:
            for timeout in (object(), float('nan'), float('inf'), -1):
                with self.assertRaises(ValueError):
                    studio.SQLiteCacheIndex(self.root / 'bad.sqlite', timeout=timeout)
            lease.assert_not_called()

    def test_candidate_appearing_before_migration_lease_is_preserved(self):
        cache = self.root / 'cache'
        cache.mkdir()
        candidate = cache / studio._CACHE_DATABASE_NAME
        report = cache / 'sentence_cache.migration.json'
        original = studio._CacheWriterLease
        def acquire_and_publish(path):
            lease = original(path)
            candidate.write_bytes(b'existing candidate from a completed earlier attempt')
            report.write_text('{"existing":"report"}', encoding='utf-8')
            return lease
        with mock.patch.object(studio, '_CacheWriterLease', side_effect=acquire_and_publish):
            with self.assertRaises(FileExistsError):
                studio.prepare_sqlite_cache_migration(cache, confirmed_local_storage=True)
        self.assertEqual(candidate.read_bytes(), b'existing candidate from a completed earlier attempt')
        self.assertEqual(json.loads(report.read_text()), {'existing': 'report'})
        # A surviving invalid database must not be hidden by an empty JSON view.
        with self.assertRaises(sqlite3.DatabaseError):
            studio.get_active_cache_backend(cache)
        # The failed attempt must release its lease even without owning a DB.
        original(cache / '.sentence_cache.migration.lock').close()

    def test_archive_refuses_an_unidentified_damaged_publication_receipt(self):
        cache, _ = self.active_cache()
        intents = cache / 'publication_intents'
        intents.mkdir()
        receipt = intents / ('1_' + 'a' * 32 + '.json')
        receipt.write_text('[]', encoding='utf-8')
        destination = self.root / 'existing.zip'
        destination.write_bytes(b'previous archive')
        with mock.patch.object(studio.requests.Session, 'request', side_effect=AssertionError('HTTP forbidden')):
            with self.assertLogs(level='ERROR'):
                index = studio.acquire_sqlite_cache_index(cache)
            try:
                self.assertEqual(index._blocked_keys, {})
                self.assertTrue(index._unidentified_recovery_error)
                with self.assertRaises(studio.CacheRecoveryError):
                    studio.create_cache_archive(cache, destination)
            finally:
                studio.release_sqlite_cache_index(index)
        self.assertEqual(destination.read_bytes(), b'previous archive')
        self.assertEqual(receipt.read_text(), '[]')


if __name__ == '__main__':
    unittest.main()
