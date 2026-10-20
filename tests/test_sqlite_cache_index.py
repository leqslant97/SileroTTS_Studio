"""Bounded SQLite index, ordered mutations and backup contracts without HTTP."""

import concurrent.futures
import contextlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "prototype.sqlite"

    def index(self, **kwargs):
        index = studio.SQLiteCacheIndex(self.path, **kwargs)
        self.addCleanup(index.close)
        return index

    def raw_count(self):
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute("SELECT count(*) FROM entries").fetchone()[0]

    def test_exact_roundtrip_unknown_fields_historical_keys_and_detached_reads(self):
        index = self.index()
        metadata = {"file_name": "Глава.ogg", "speaker": "еЛеНа", "steps": None,
                    "last_accessed": "historical value", "usage_count": -5,
                    "unknown": {"текст": [1, True, None, 1.25, "\x00"]}}
        for key in ("", "manual:key", "historical\x00key", "0" * 32):
            index.upsert(key, metadata)
            metadata["unknown"]["текст"].append("caller mutation")
            read = index.get(key)
            self.assertNotEqual(read, metadata)
            metadata["unknown"]["текст"].pop()
            self.assertEqual(read, metadata)
            read["unknown"]["текст"].clear()
            self.assertEqual(index.get(key), metadata)
        self.assertEqual(self.raw_count(), 0)
        self.assertEqual(index.flush(), 4)
        index.close()
        reopened = self.index()
        self.assertEqual(reopened.count(), 4)
        for key in ("", "manual:key", "historical\x00key", "0" * 32):
            self.assertEqual(reopened.get(key), metadata)

    def test_touch_delete_upsert_order_and_statistics_are_aggregated(self):
        index = self.index()
        sequences = [index.upsert("a", {"usage_count": 4, "last_accessed": 20})]
        sequences.extend(index.touch("a", timestamp, increment=2) for timestamp in (18, 23, 21))
        self.assertEqual(index.get("a"), {"usage_count": 10, "last_accessed": 23})
        self.assertEqual(len(index._pending), 1)
        sequences.append(index.delete("a"))
        self.assertIsNone(index.touch("a", 100, 1))
        self.assertIsNone(index.get("a"))
        sequences.append(index.upsert("a", {"usage_count": 1, "last_accessed": 2}))
        sequences.append(index.touch("a", 3))
        self.assertEqual(sequences, sorted(set(sequences)))
        index.flush()
        self.assertEqual(index.get("a"), {"usage_count": 2, "last_accessed": 3})

    def test_concurrent_touches_do_not_lose_increments(self):
        index = self.index(max_pending_operations=17)
        index.upsert("a", {"usage_count": 0})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda value: index.touch("a", value), range(200)))
        self.assertEqual(index.get("a")["usage_count"], 200)
        self.assertEqual(index.get("a")["last_accessed"], 199)
        index.close()
        self.assertEqual(self.index().get("a")["usage_count"], 200)

    def test_checked_touch_preserves_pending_metadata_and_publication_version(self):
        index = self.index()
        index.upsert("a", {"file_name": "old.ogg", "usage_count": 4,
                           "last_accessed": 20, "audio_codec": "vorbis", "unknown": ["keep"]})
        version = index._pending_versions["a"]
        index.touch("a", 18, expected_file_name="old.ogg", audio_codec="opus")
        self.assertEqual(index._pending_versions["a"], version)
        expected = {"file_name": "old.ogg", "usage_count": 5, "last_accessed": 20,
                    "audio_codec": "opus", "unknown": ["keep"]}
        self.assertEqual(index.get("a"), expected)
        index.close()
        self.assertEqual(self.index().get("a"), expected)

    def test_checked_touch_does_not_credit_replacement_or_resurrect_deletion(self):
        index = self.index()
        index.upsert("a", {"file_name": "old.ogg", "usage_count": 4})
        index.flush()
        replacement = {"file_name": "new.ogg", "usage_count": 0, "audio_codec": "vorbis"}
        index.upsert("a", replacement)
        self.assertIsNone(index.touch("a", 100, expected_file_name="old.ogg", audio_codec="opus"))
        self.assertEqual(index.get("a"), replacement)
        index.delete("a")
        self.assertIsNone(index.touch("a", 100, expected_file_name="new.ogg", audio_codec="opus"))
        self.assertIsNone(index.get("a"))
        index.flush()
        self.assertEqual(self.raw_count(), 0)

    def test_checked_touch_keeps_concurrent_usage_and_recovery_guard(self):
        index = self.index(max_pending_operations=17)
        index.upsert("a", {"file_name": "same.ogg", "usage_count": 0})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda value: index.touch(
                "a", value, expected_file_name="same.ogg", audio_codec="opus"), range(200)))
        self.assertEqual(index.get("a")["usage_count"], 200)
        self.assertEqual(index.get("a")["last_accessed"], 199)
        index._blocked_keys["a"] = "unresolved publication"
        with self.assertRaises(studio.CacheRecoveryError):
            index.touch("a", 201, expected_file_name="same.ogg", audio_codec="opus")
        index._blocked_keys.clear()

    def test_request_arriving_during_commit_is_retained_after_prefix(self):
        index = self.index()
        index.upsert("a", {"value": "old"})
        entered, release = threading.Event(), threading.Event()
        original = index._commit_transaction

        def blocked_commit(connection):
            entered.set()
            self.assertTrue(release.wait(5))
            original(connection)

        with mock.patch.object(index, "_commit_transaction", side_effect=blocked_commit):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                flushing = executor.submit(index.flush)
                self.assertTrue(entered.wait(5))
                late = executor.submit(index.upsert, "a", {"value": "late"})
                release.set()
                self.assertEqual(flushing.result(), 1)
                late.result()
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            committed = json.loads(connection.execute("SELECT record_json FROM entries").fetchone()[0])
        self.assertEqual(committed, {"value": "old"})
        self.assertEqual(index.get("a"), {"value": "late"})
        self.assertEqual(index.flush(), 1)

    def test_count_and_query_read_pending_overlay_without_early_main_commit(self):
        index = self.index()
        index.upsert('a', {'original_text': 'первый'})
        index.upsert('b', {'original_text': 'второй'})
        self.assertEqual(index.count(), 2)
        self.assertEqual([key for key, _ in index.query_page()['rows']], ['a', 'b'])
        self.assertEqual(self.raw_count(), 0)
        index.flush()
        index.delete('a')
        index.upsert('b', {'original_text': 'обновлённый'})
        index.upsert('c', {'original_text': 'новый'})
        page = index.query_page(search='нов')
        self.assertEqual([key for key, _ in page['rows']], ['b', 'c'])
        self.assertEqual(index.count(), 2)
        self.assertEqual(self.raw_count(), 2)

    def test_actual_guarded_page_sql_merges_primary_key_ranges_without_full_sort(self):
        index = self.index()
        statements = []
        original_dispatch = index._dispatch
        plans = []
        instructions = []
        def traced(connection, operation, args):
            if operation == 'query_page':
                connection.set_trace_callback(statements.append)
                connection.set_progress_handler(lambda: instructions.append(None) or 0, 1)
                try:
                    result = original_dispatch(connection, operation, args)
                    statement = next(sql for sql in reversed(statements) if sql.startswith('SELECT CASE WHEN length'))
                    plans.extend(connection.execute('EXPLAIN QUERY PLAN ' + statement).fetchall())
                    return result
                finally:
                    connection.set_trace_callback(None)
                    connection.set_progress_handler(None, 0)
            return original_dispatch(connection, operation, args)
        for number in range(10000):
            index.upsert(f'{number:04}', {'original_text': 'sample'})
        index.flush()
        index.query_page(cursor='0020', limit=5, max_scan_rows=10)
        with mock.patch.object(index, '_dispatch', side_effect=traced):
            page = index.query_page(cursor='0020', limit=5, max_scan_rows=10)
        details = [row[3] for row in plans]
        self.assertTrue(any('MERGE (UNION ALL)' in item for item in details), details)
        self.assertTrue(any('SEARCH entries USING PRIMARY KEY (key>?)' in item for item in details), details)
        # Старые SQLite могут сортировать уже ограниченную внутренним LIMIT
        # страницу. Число инструкций отличает её от сортировки всего остатка.
        self.assertLess(len(instructions), 1000, (len(instructions), details))
        self.assertEqual([key for key, _ in page['rows']], ['0021', '0022', '0023', '0024', '0025'])

    def test_failed_flush_and_close_preserve_pending_and_allow_retry(self):
        index = self.index()
        index.upsert("a", {"value": 1})
        with mock.patch.object(index, "_commit_transaction", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk full"):
                index.flush()
            self.assertEqual(self.raw_count(), 0)
            with self.assertRaises(sqlite3.OperationalError):
                index.close()
            self.assertEqual(index.get("a"), {"value": 1})
        self.assertEqual(index.flush(), 1)
        self.assertEqual(self.raw_count(), 1)

    def test_pending_backpressure_flushes_and_failed_backpressure_rejects_new_event(self):
        index = self.index(max_pending_operations=2, max_pending_bytes=300)
        index.upsert("a", {"value": "x" * 40})
        index.upsert("b", {"value": "x" * 40})
        with mock.patch.object(index, "_commit_transaction", side_effect=sqlite3.OperationalError("busy")):
            with self.assertRaises(sqlite3.OperationalError):
                index.upsert("c", {"value": "x" * 40})
        self.assertIsNone(index.get("c"))
        self.assertEqual(index.get("a")["value"], "x" * 40)
        index.upsert("c", {"value": "x" * 40})
        self.assertEqual(self.raw_count(), 2)
        self.assertLessEqual(index._pending_bytes, 300)

    def test_invalid_and_oversized_payloads_fail_before_mutation(self):
        index = self.index(max_record_bytes=64, max_pending_bytes=100)
        invalid = ({1: "x"}, {"x": (1, 2)}, {"x": float("nan")}, {"x": object()}, {"x": "a" * 100})
        for metadata in invalid:
            with self.assertRaises((ValueError, TypeError)):
                index.upsert("a", metadata)
        with self.assertRaises(ValueError):
            index.upsert("x" * 65, {})
        with self.assertRaises(ValueError):
            index.upsert("x" * 50, {"v": "a" * 20})
        self.assertIsNone(index.get("a"))
        self.assertEqual(index.flush(), 0)

    def test_keyset_pages_scan_and_byte_limits_unicode_and_literal_search(self):
        index = self.index()
        for number, text in enumerate(("ЕЖИК", "ЁЖ", "100%_literal", "100XXliteral", "ёж")):
            index.upsert(str(number), {"original_text": text, "speaker": "a"})
        cursor, keys = None, []
        while True:
            page = index.query_page(cursor=cursor, limit=2, max_scan_rows=2, search="ёж")
            self.assertLessEqual(page["scanned"], 2)
            self.assertLessEqual(len(page["rows"]), 2)
            self.assertNotEqual(page["cursor"], cursor)
            keys.extend(key for key, _ in page["rows"])
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(keys, ["1", "4"])
        self.assertEqual([key for key, _ in index.query_page(search="%_")["rows"]], ["2"])
        page = index.query_page(limit=100, max_bytes=65)
        self.assertLessEqual(page["bytes"], 65)
        self.assertTrue(page["has_more"])
        with self.assertRaisesRegex(ValueError, "max_bytes"):
            index.query_page(max_bytes=1)
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT key,record_json FROM entries "
                                      "WHERE key > ? ORDER BY key LIMIT ?", ("2", 100)).fetchall()
        self.assertTrue(any("SEARCH" in row[3] and "PRIMARY KEY" in row[3] for row in plan), plan)

    def test_projection_does_not_modify_exact_json_or_make_unsafe_filename_usable(self):
        index = self.index()
        records = [{"file_name": "same.ogg", "speaker": "Нина", "last_accessed": 10},
                   {"file_name": "same.ogg", "speaker": "Нина", "last_accessed": 11},
                   {"file_name": "../outside.ogg", "speaker": {"unknown": True}, "last_accessed": "yesterday"}]
        for number, record in enumerate(records):
            index.upsert(str(number), record)
        index.flush()
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            rows = connection.execute("SELECT record_json,file_name,speaker,last_accessed FROM entries ORDER BY key").fetchall()
        self.assertEqual([json.loads(row[0]) for row in rows], records)
        self.assertEqual([row[1] for row in rows], ["same.ogg", "same.ogg", None])
        self.assertEqual(rows[-1][2:], (None, None))

    def test_backup_includes_pending_wal_and_reopens_without_sidecars(self):
        index = self.index(synchronous="NORMAL")
        index.upsert("a", {"unknown": [1, "Глава"]})
        backup = index.backup(self.root / "backup.sqlite")
        self.assertEqual(index.integrity_check(), "ok")
        self.assertFalse(Path(str(backup) + "-wal").exists())
        with studio.SQLiteCacheIndex(backup) as restored:
            self.assertEqual(restored.get("a"), {"unknown": [1, "Глава"]})
            self.assertEqual(restored.integrity_check(), "ok")
        with self.assertRaises(ValueError):
            index.backup(self.path)

    def test_backup_rejects_existing_destinations_symlinks_and_sidecars(self):
        index = self.index()
        destination = self.root / "existing.sqlite"
        destination.write_bytes(b"valuable existing data")
        with self.assertRaises(FileExistsError):
            index.backup(destination)
        self.assertEqual(destination.read_bytes(), b"valuable existing data")
        symbolic = self.root / "symbolic.sqlite"
        symbolic.symlink_to(destination)
        with self.assertRaises(ValueError):
            index.backup(symbolic)
        with self.assertRaises(ValueError):
            studio.SQLiteCacheIndex(symbolic)
        sidecar_destination = self.root / "with-sidecar.sqlite"
        Path(str(sidecar_destination) + "-wal").write_bytes(b"owned by other process")
        with self.assertRaises(FileExistsError):
            index.backup(sidecar_destination)

    def test_huge_historical_numeric_metadata_is_preserved_and_touch_is_safe(self):
        index = self.index()
        historical = {"last_accessed": 10 ** 400, "unknown": 10 ** 400}
        index.upsert("a", historical)
        self.assertEqual(index.get("a"), historical)
        index.touch("a", 20)
        self.assertEqual(index.get("a"), {"last_accessed": 20, "usage_count": 1, "unknown": 10 ** 400})
        with self.assertRaises(ValueError):
            index.touch("a", 10 ** 400)

    def test_reopening_with_lower_record_bound_rejects_oversized_stored_values(self):
        index = self.index()
        index.upsert("a", {"payload": "x" * 10000})
        index.close()
        bounded = self.index(max_record_bytes=1000)
        with self.assertRaisesRegex(ValueError, "stored metadata"):
            bounded.get("a")
        with self.assertRaisesRegex(ValueError, "stored metadata"):
            bounded.query_page()

    def test_externally_modified_oversized_projections_are_rejected_before_scan(self):
        index = self.index(max_record_bytes=100)
        index.upsert("safe", {})
        index.flush()
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO entries VALUES (?, '{}', NULL, NULL, NULL)", ("x" * 10000,))
        with self.assertRaisesRegex(ValueError, "byte bound"):
            index.query_page(search="no match")

    def test_all_sql_runs_on_one_owner_thread_and_closed_requests_fail(self):
        threads = []
        original = studio.SQLiteCacheIndex._open

        def traced_open(index):
            connection = original(index)
            threads.append(threading.get_ident())
            connection.set_trace_callback(lambda statement: threads.append(threading.get_ident()))
            return connection

        with mock.patch.object(studio.SQLiteCacheIndex, "_open", traced_open):
            index = self.index()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda value: index.upsert(str(value), {"x": value}), range(20)))
            index.count()
            index.query_page()
            index.get("1")
            index.close()
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        with self.assertRaises(RuntimeError):
            index.get("1")
        index.close()

    def test_foreign_database_and_unknown_schema_are_not_modified(self):
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE foreign_data(value)")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.index()
        self.path.unlink()
        index = self.index()
        index.close()
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.index()


if __name__ == "__main__":
    unittest.main()
