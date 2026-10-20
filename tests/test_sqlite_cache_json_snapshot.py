"""Explicit exact SQLite-to-JSON snapshots without audio changes or HTTP."""

import builtins
import concurrent.futures
import contextlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheJsonSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name).resolve() / "cache"
        self.cache.mkdir()
        self.main = self.cache / "sentence_cache.json"
        self.database = self.cache / studio._CACHE_DATABASE_NAME

    def active_index(self):
        with studio.SQLiteCacheIndex(self.database):
            pass
        studio._write_cache_json_durable(self.cache / studio._CACHE_BACKEND_MARKER,
            {"schema": 1, "backend": "sqlite", "database": studio._CACHE_DATABASE_NAME,
             "storage_policy": "confirmed_local", "synchronous": "FULL"})
        index = studio.acquire_sqlite_cache_index(self.cache)
        self.addCleanup(studio.release_sqlite_cache_index, index)
        return index

    def replace_raw_record(self, key, record):
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE entries SET record_json=? WHERE key=?", (record, key))

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.cache.glob(".sentence_cache.json.*.tmp")), [])

    def test_exact_pending_snapshot_preserves_backups_audio_marker_and_unknown_fields(self):
        index = self.active_index()
        marker = self.cache / studio._CACHE_BACKEND_MARKER
        backup = self.cache / "sentence_cache.json.bak"
        migration = self.cache / "sentence_cache.migration.json"
        original = self.cache / "index_backups" / "migration_source_1.json"
        original.parent.mkdir()
        audio = self.cache / "audio" / ("a" * 32 + "_" + "b" * 32 + ".ogg")
        audio.parent.mkdir()
        self.main.write_bytes(b'{"old": {"usage_count": 1}}')
        for path in (backup, migration, original, audio):
            path.write_bytes(b"preserve exactly: " + path.name.encode())
        preserved = {path: path.read_bytes() for path in (marker, backup, migration, original, audio)}
        index.upsert("remove", {"original_text": "удалить"})
        index.upsert("touch", {"usage_count": 2, "last_accessed": 3})
        index.flush()
        index.delete("remove")
        index.touch("touch", 8, increment=4)
        metadata = {"original_text": "Кириллица ё — 😀", "normalized_text": "текст",
                    "file_name": audio.name, "usage_count": -8, "last_accessed": "old-value",
                    "unknown": {"nested": [None, True, 10**40, 1.25, "\x00"]}}
        index.upsert("manual:\x00ключ", metadata)
        self.assertGreater(index.pending_count, 0)
        with mock.patch.object(studio.requests.Session, "request", side_effect=AssertionError("HTTP forbidden")):
            result = studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(result["path"], str(self.main))
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["backend"], "sqlite")
        self.assertTrue(result["includes_pending"])
        self.assertEqual(json.loads(self.main.read_text()), {
            "touch": {"usage_count": 6, "last_accessed": 8}, "manual:\x00ключ": metadata})
        self.assertIn("Кириллица", self.main.read_text())
        self.assertEqual(index.pending_count, 0)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        for path, expected in preserved.items():
            self.assertEqual(path.read_bytes(), expected, path)
        self.assert_no_temporary_files()

    def test_empty_index_publishes_valid_empty_object_and_keeps_existing_backup(self):
        self.active_index()
        self.main.write_bytes(b'{"obsolete": {}}')
        backup = self.cache / "sentence_cache.json.bak"
        backup.write_bytes(b"retained backup")
        result = studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(json.loads(self.main.read_text()), {})
        self.assertEqual(backup.read_bytes(), b"retained backup")
        self.assert_no_temporary_files()

    def test_preserves_exact_numeric_literals_and_does_not_normalize_legacy_metadata(self):
        index = self.active_index()
        index.upsert("precise", {})
        index.flush()
        record = '{"number":1.2345678901234567890123456789,"negative":-0.0,"usage_count":-2,"created_at":"old"}'
        self.replace_raw_record("precise", record)
        studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertIn(record, self.main.read_text())
        self.assertEqual(json.loads(self.main.read_text())["precise"]["created_at"], "old")

    def test_rejects_json_backend_without_creating_database_or_changing_files(self):
        self.main.write_bytes(b'{"original": {}}')
        with self.assertRaisesRegex(ValueError, "SQLite"):
            studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(self.main.read_bytes(), b'{"original": {}}')
        self.assertFalse(self.database.exists())

    def test_rejects_destination_symlink_and_does_not_change_link_target(self):
        index = self.active_index()
        index.upsert("a", {})
        target = self.cache / "outside.json"
        target.write_bytes(b"do not overwrite")
        self.main.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertTrue(self.main.is_symlink())
        self.assertEqual(target.read_bytes(), b"do not overwrite")
        self.assert_no_temporary_files()

    def test_direct_export_cannot_replace_source_database_or_its_sidecars(self):
        index = self.active_index()
        index.upsert("a", {"value": 1})
        index.flush()
        alias_directory = self.cache / "nested"
        alias_directory.mkdir()
        for destination in (self.database, alias_directory / ".." / self.database.name,
                            *(Path(str(self.database) + suffix)
                              for suffix in ("-wal", "-shm", "-journal", ".writer.lock"))):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "database and its sidecars"):
                    index.export_json_snapshot(destination)
        self.assertEqual(index.integrity_check(), "ok")
        self.assertEqual(index.get("a"), {"value": 1})

    def test_rejects_unresolved_recovery_before_publication(self):
        index = self.active_index()
        self.main.write_bytes(b"original main")
        for identified in (True, False):
            with self.subTest(identified=identified):
                index._blocked_keys = {"a": "unresolved"} if identified else {}
                index._unidentified_recovery_error = None if identified else "unidentified"
                try:
                    with self.assertRaises(studio.CacheRecoveryError):
                        studio.export_sqlite_cache_to_json_snapshot(self.cache)
                finally:
                    index._blocked_keys.clear()
                    index._unidentified_recovery_error = None
                self.assertEqual(self.main.read_bytes(), b"original main")
        self.assert_no_temporary_files()

    def test_invalid_external_json_is_never_published_and_owner_remains_usable(self):
        index = self.active_index()
        index.upsert("a", {})
        index.flush()
        self.main.write_bytes(b"original main")
        for record in ('[]', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}',
                       '{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{invalid'):
            with self.subTest(record=record):
                self.replace_raw_record("a", record)
                with self.assertRaises(ValueError):
                    studio.export_sqlite_cache_to_json_snapshot(self.cache)
                self.assertEqual(self.main.read_bytes(), b"original main")
                self.assert_no_temporary_files()
        self.replace_raw_record("a", '{"ok":true}')
        studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(json.loads(self.main.read_text()), {"a": {"ok": True}})

    def test_oversized_external_record_is_rejected_before_python_allocation(self):
        index = self.active_index()
        index.upsert("a", {})
        index.flush()
        self.main.write_bytes(b"original main")
        self.replace_raw_record("a", json.dumps({"payload": "x" * (8 * 1024 * 1024)}))
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(ValueError, "byte bound"):
                studio.export_sqlite_cache_to_json_snapshot(self.cache)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)
        self.assertEqual(self.main.read_bytes(), b"original main")
        self.assert_no_temporary_files()

    def test_fsync_and_replace_failures_keep_previous_json_and_remove_temporary(self):
        index = self.active_index()
        index.upsert("a", {"value": 1})
        index.flush()
        self.main.write_bytes(b"original main")
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                with mock.patch.object(studio.os, operation, side_effect=OSError("disk failure")):
                    with self.assertRaisesRegex(OSError, "disk failure"):
                        studio.export_sqlite_cache_to_json_snapshot(self.cache)
                self.assertEqual(self.main.read_bytes(), b"original main")
                self.assert_no_temporary_files()
        studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(json.loads(self.main.read_text()), {"a": {"value": 1}})

    def test_partial_write_failure_keeps_previous_json_and_removes_temporary(self):
        index = self.active_index()
        index.upsert("a", {"value": 1})
        index.flush()
        self.main.write_bytes(b"original main")
        real_open = builtins.open

        class FailingTarget:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, value):
                self.stream.write(value[:1])
                raise OSError("partial write failure")

        def failing_open(path, mode="r", *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            return FailingTarget(stream) if mode == "x" else stream

        with mock.patch("builtins.open", side_effect=failing_open):
            with self.assertRaisesRegex(OSError, "partial write failure"):
                studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(self.main.read_bytes(), b"original main")
        self.assert_no_temporary_files()

    def test_unresolved_receipt_is_retained_and_blocks_snapshot_even_without_cached_diagnostics(self):
        self.active_index()
        self.main.write_bytes(b"original main")
        intents = self.cache / "publication_intents"
        intents.mkdir()
        receipt = intents / ("1_" + "a" * 32 + ".json")
        receipt.write_bytes(b"unresolved intent")
        with self.assertRaises(studio.CacheRecoveryError):
            studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(self.main.read_bytes(), b"original main")
        self.assertEqual(receipt.read_bytes(), b"unresolved intent")
        self.assert_no_temporary_files()

    def test_oversized_external_key_is_rejected_without_partial_publication(self):
        self.active_index()
        self.main.write_bytes(b"original main")
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO entries VALUES (?,?,?,?,?)", ("x" * 65537, "{}", None, None, None))
        with self.assertRaisesRegex(ValueError, "byte bound"):
            studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(self.main.read_bytes(), b"original main")
        self.assert_no_temporary_files()

    def test_failed_pending_commit_preserves_json_and_pending_changes_for_retry(self):
        index = self.active_index()
        index.upsert("a", {"value": "pending"})
        self.main.write_bytes(b"original main")
        with mock.patch.object(index, "_commit_transaction", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk full"):
                studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(index.pending_count, 1)
        self.assertEqual(self.main.read_bytes(), b"original main")
        studio.export_sqlite_cache_to_json_snapshot(self.cache)
        self.assertEqual(json.loads(self.main.read_text()), {"a": {"value": "pending"}})

    def test_single_read_transaction_excludes_concurrent_external_commit(self):
        index = self.active_index()
        index.upsert("a", {"value": "old-a"})
        index.upsert("b", {"value": "old-b"})
        index.flush()
        entered, resume = threading.Event(), threading.Event()
        real_open = builtins.open

        class PausedTarget:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, value):
                result = self.stream.write(value)
                if value == '{"value":"old-a"}':
                    entered.set()
                    if not resume.wait(5):
                        raise RuntimeError("snapshot test timed out")
                return result

        def paused_open(path, mode="r", *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            return PausedTarget(stream) if mode == "x" else stream

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with mock.patch("builtins.open", side_effect=paused_open):
                future = executor.submit(studio.export_sqlite_cache_to_json_snapshot, self.cache)
                try:
                    self.assertTrue(entered.wait(5))
                    self.replace_raw_record("b", '{"value":"new-b"}')
                finally:
                    resume.set()
                future.result(timeout=5)
        self.assertEqual(json.loads(self.main.read_text()), {
            "a": {"value": "old-a"}, "b": {"value": "old-b"}})
        self.assertEqual(index.get("b"), {"value": "new-b"})

    def test_large_index_is_streamed_without_materializing_all_metadata(self):
        index = self.active_index()
        record = json.dumps({"payload": "x" * 4000, "unknown": [None, True]})
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?)",
                ((f"{number:06}", record, None, None, None) for number in range(5000)))
        tracemalloc.start()
        try:
            result = studio.export_sqlite_cache_to_json_snapshot(self.cache)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result["record_count"], 5000)
        self.assertGreater(self.main.stat().st_size, 20_000_000)
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertEqual(index.count(), 5000)
        self.assert_no_temporary_files()


if __name__ == "__main__":
    unittest.main()
