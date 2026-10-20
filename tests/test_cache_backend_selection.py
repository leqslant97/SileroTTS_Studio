"""Новая папка использует SQLite; существующий индекс не теряется без JSON-указателя."""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class CacheBackendSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.database = self.cache / studio._CACHE_DATABASE_NAME
        self.marker = self.cache / studio._CACHE_BACKEND_MARKER
        self.main = self.cache / "sentence_cache.json"
        self.addCleanup(self.close_owner)

    def close_owner(self):
        entry = studio._SQLITE_CACHE_POOL.get(str(self.database))
        if entry is not None:
            studio.release_sqlite_cache_index(entry["index"])

    def database_fixture(self, *, activation="active", records=None):
        with studio.SQLiteCacheIndex(self.database) as index:
            for key, value in (records or {}).items():
                index.upsert(key, value)
            index._request("set_meta", "activation", activation)

    def test_empty_directory_selects_sqlite_without_creating_files(self):
        with mock.patch.object(studio, "SQLiteCacheIndex", side_effect=AssertionError("status opens owner")):
            self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_missing_directory_selects_sqlite_without_creating_it(self):
        directory = self.root / "not-created"
        self.assertEqual(studio.get_active_cache_backend(directory), "sqlite")
        self.assertFalse(directory.exists())

    def test_new_sqlite_opens_without_json_or_migration(self):
        with mock.patch.object(studio, "prepare_sqlite_cache_migration", side_effect=AssertionError("migration")):
            index = studio.acquire_sqlite_cache_index(self.cache)
            self.assertEqual(index.count(), 0)
            index.upsert("preserved", {"original_text": "Ёж", "unknown": [1, None]})
            studio.release_sqlite_cache_index(index)
        self.assertFalse(self.main.exists())
        self.assertFalse((self.cache / "sentence_cache.migration.json").exists())
        reopened = studio.acquire_sqlite_cache_index(self.cache)
        self.assertEqual(reopened.get("preserved"), {"original_text": "Ёж", "unknown": [1, None]})

    def test_json_main_and_backup_each_preserve_json_backend(self):
        for path in (self.main, self.cache / "sentence_cache.json.bak"):
            with self.subTest(path=path.name):
                path.write_text("{}")
                self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
                path.unlink()

    def test_broken_json_is_not_replaced_by_a_new_sqlite(self):
        self.main.write_text("{broken")
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertFalse(self.database.exists())
        self.assertEqual(self.main.read_text(), "{broken")

    def test_unmarked_sqlite_reopens_after_all_control_json_files_are_deleted(self):
        self.database_fixture(records={"newer": {"usage_count": 7}})
        with mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("JSON read")):
            self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
            index = studio.acquire_sqlite_cache_index(self.cache)
            self.assertEqual(index.get("newer"), {"usage_count": 7})

    def test_empty_inactive_sqlite_without_receipt_is_usable(self):
        self.database_fixture(activation="inactive")
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        index = studio.acquire_sqlite_cache_index(self.cache)
        self.assertEqual(index.count(), 0)

    def test_existing_json_wins_over_unmarked_retained_sqlite(self):
        self.database_fixture(records={"old": {}})
        self.main.write_text('{"current":{}}')
        before = self.database.read_bytes()
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.database.read_bytes(), before)

    def test_explicit_sqlite_marker_still_wins_over_json_snapshot(self):
        self.database_fixture(records={"current": {}})
        studio._write_cache_json_durable(self.marker, {
            "schema": 1, "backend": "sqlite", "database": studio._CACHE_DATABASE_NAME,
            "storage_policy": "confirmed_local", "synchronous": "FULL",
        })
        self.main.write_text('{"stale":{}}')
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")

    def test_corrupt_or_foreign_sqlite_is_not_replaced_or_read_as_json(self):
        self.database.write_bytes(b"broken SQLite")
        before = self.database.read_bytes()
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            studio.get_active_cache_backend(self.cache)
        self.assertEqual(self.database.read_bytes(), before)
        self.database.unlink()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("CREATE TABLE user_data (value TEXT)")
        finally:
            connection.close()
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            studio.get_active_cache_backend(self.cache)
        self.assertFalse(self.main.exists())

    def test_orphan_sqlite_sidecars_do_not_initialize_an_empty_index(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.database) + suffix)
                sidecar.write_bytes(b"recovery data")
                with self.assertRaises(ValueError):
                    studio.get_active_cache_backend(self.cache)
                self.assertFalse(self.database.exists())
                self.assertEqual(sidecar.read_bytes(), b"recovery data")
                sidecar.unlink()

    def test_database_symlink_is_rejected(self):
        target = self.root / "target.sqlite"
        with studio.SQLiteCacheIndex(target):
            pass
        self.database.symlink_to(target)
        with self.assertRaises(ValueError):
            studio.get_active_cache_backend(self.cache)

    def test_prepared_empty_migration_remains_pending_until_confirmation(self):
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(studio.read_prepared_sqlite_cache_migration(self.cache), receipt)
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")

    def test_new_sqlite_startup_failure_keeps_directory_retryable(self):
        with mock.patch.object(studio.SQLiteCacheIndex, "_open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                studio.acquire_sqlite_cache_index(self.cache)
        self.assertFalse(self.main.exists())
        self.assertFalse(self.database.exists())
        self.assertFalse(self.marker.exists())
        index = studio.acquire_sqlite_cache_index(self.cache)
        self.assertEqual(index.count(), 0)

    def test_empty_default_sqlite_can_be_explicitly_switched_to_json(self):
        result = studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(result["backend"], "json")
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.main.read_text().strip(), "{}")
        self.assertFalse(self.marker.exists())

    def test_json_appearing_during_new_database_creation_is_preserved(self):
        original = studio.SQLiteCacheIndex

        def create_and_publish_json(*args, **kwargs):
            index = original(*args, **kwargs)
            self.main.write_text('{"newer":{"original_text":"Новая запись"}}')
            return index

        with mock.patch.object(studio, "SQLiteCacheIndex", side_effect=create_and_publish_json):
            with self.assertRaises(ValueError):
                studio.acquire_sqlite_cache_index(self.cache)
        self.assertFalse(self.database.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")

    def test_unmarked_sqlite_archive_includes_restored_backend_marker(self):
        self.database_fixture(records={"saved": {"original_text": "Сохранённая запись."}})
        index = studio.acquire_sqlite_cache_index(self.cache)
        self.assertTrue(self.marker.is_file())
        archive = self.root / "cache.zip"
        studio.create_cache_archive(self.cache, archive)
        import zipfile
        with zipfile.ZipFile(archive) as saved:
            self.assertTrue(any(name.endswith(studio._CACHE_BACKEND_MARKER) for name in saved.namelist()))

    def test_new_folder_status_is_read_in_worker_without_migration_or_dialog(self):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = {"cache_dir": str(self.cache)}
        app._cache_operation = None
        app._cache_backend_status_request_id = 0
        app.cache_backend_choice = mock.Mock(get=mock.Mock(return_value=""))
        app.lbl_cache_backend_status = mock.Mock()
        app.lbl_settings_cache_backend = mock.Mock()
        done = threading.Event()
        callbacks = []
        threads = []
        original = studio.get_active_cache_backend

        def inspect(path):
            threads.append(threading.get_ident())
            return original(path)

        def post(callback, *args):
            callbacks.append((callback, args))
            done.set()

        app._post_to_ui = post
        with mock.patch.object(studio, "get_active_cache_backend", side_effect=inspect), \
                mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("JSON")):
            app.refresh_cache_backend_status()
            self.assertTrue(done.wait(3))
        callback, args = callbacks[0]
        callback(*args)
        self.assertNotEqual(threads, [threading.get_ident()])
        self.assertEqual(app._cache_backend_status, "sqlite")
        app.cache_backend_choice.set.assert_called_with("SQLite")
        self.assertEqual(list(self.cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
