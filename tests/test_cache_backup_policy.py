"""Explicit metadata exports and one switch-owned safety copy, without HTTP."""

import contextlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import stat
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


class CacheBackupPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.main = self.cache / "sentence_cache.json"
        self.main.write_text('{ "key" : { "raw" : "Ёж", "usage_count" : 2 } }', encoding="utf-8")
        self.backups = self.cache / "index_backups"
        self.database = self.cache / studio._CACHE_DATABASE_NAME
        self.addCleanup(self.cleanup_owner)

    def cleanup_owner(self):
        entry = studio._SQLITE_CACHE_POOL.get(str(self.database))
        if entry is not None:
            index = entry["index"]
            index._blocked_keys.clear()
            index._unidentified_recovery_error = None
            while str(self.database) in studio._SQLITE_CACHE_POOL:
                studio.release_sqlite_cache_index(index)

    def safety_files(self):
        return sorted(self.backups.glob("safety_*.*")) if self.backups.exists() else []

    def activate(self):
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        return receipt

    def test_manual_requires_explicit_external_destination_and_never_creates_retention_folder(self):
        for reason in ("manual", "optimize", "clear", "delete", "transcode"):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "Выберите"):
                studio.create_cache_index_backup(self.cache, reason)
        for destination in (self.main, self.cache / "nested" / "backup.json"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "вне"):
                studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertFalse(self.backups.exists())

    def test_manual_json_keeps_exact_bytes_and_user_controls_all_copies(self):
        original = self.main.read_bytes()
        copied = []
        for number in range(5):
            destination = self.root / "exports" / f"copy-{number}.json"
            copied.append(studio.create_cache_index_backup(self.cache, destination=destination))
        self.assertEqual(len(copied), 5)
        self.assertTrue(all(path.read_bytes() == original for path in copied))
        self.assertFalse(self.backups.exists())
        self.assertFalse(list((self.root / "exports").glob("*.receipt.json")))
        copied[0].write_bytes(b"previous manual copy")
        studio.create_cache_index_backup(self.cache, destination=copied[0])
        self.assertEqual(copied[0].read_bytes(), original)

    def test_manual_json_backup_fallback_and_copy_failure_preserve_existing_target(self):
        self.main.write_bytes(b"invalid")
        backup = self.main.with_suffix(".json.bak")
        backup.write_bytes(b'{"fallback":{"unknown":[true,null]}}')
        destination = self.root / "manual.json"
        destination.write_bytes(b"previous")
        original_replace = studio.os.replace

        def fail_publication(source, target):
            if Path(target) == destination:
                raise OSError("publication failed")
            return original_replace(source, target)

        with mock.patch.object(studio.os, "replace", side_effect=fail_publication):
            with self.assertRaisesRegex(OSError, "publication failed"):
                studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertFalse(list(self.root.glob(".manual.json.*.tmp")))
        studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertEqual(destination.read_bytes(), backup.read_bytes())
        self.assertEqual(self.main.read_bytes(), b"invalid")

    def test_json_source_change_before_copy_never_publishes_validated_wrong_snapshot(self):
        destination = self.root / "manual.json"
        destination.write_bytes(b"previous")
        original_load = studio.json.load

        def changed_source(file, *args, **kwargs):
            result = original_load(file, *args, **kwargs)
            self.main.write_text('{"changed":{}}', encoding="utf-8")
            return result

        with mock.patch.object(studio.json, "load", side_effect=changed_source):
            with self.assertRaisesRegex(ValueError, "JSON изменился"):
                studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertEqual(destination.read_bytes(), b"previous")

    def test_json_directory_fsync_failure_restores_previous_manual_file(self):
        destination = self.root / "manual.json"
        destination.write_bytes(b"previous")
        original_fsync = studio._fsync_cache_directory
        failed = [False]

        def fail_once(path):
            if Path(path) == self.root and not failed[0]:
                failed[0] = True
                raise OSError("directory durability failed")
            return original_fsync(path)

        with mock.patch.object(studio, "_fsync_cache_directory", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "directory durability failed"):
                studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertFalse(list(self.root.glob(".manual.json.*")))

    def test_manual_json_rejects_symlink_paths_and_sqlite_database_or_sidecars(self):
        target = self.root / "target.json"
        target.write_bytes(b"keep")
        alias = self.root / "alias.json"
        alias.symlink_to(target)
        directory_alias = self.root / "alias-directory"
        directory_alias.symlink_to(self.cache, target_is_directory=True)
        for destination in (alias, directory_alias / "copy.json", self.root / "db.sqlite-wal"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                studio.create_cache_index_backup(self.cache, destination=destination)
        database = self.root / "unrelated.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE data(value)")
        before = database.read_bytes()
        with self.assertRaisesRegex(ValueError, "SQLite-базу"):
            studio.create_cache_index_backup(self.cache, destination=database)
        self.assertEqual(database.read_bytes(), before)
        sidecar_target = self.root / "sidecar-target.json"
        Path(str(sidecar_target) + "-wal").write_bytes(b"live")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            studio.create_cache_index_backup(self.cache, destination=sidecar_target)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_manual_sqlite_copies_commit_pending_metadata_without_rotating_or_overwriting(self):
        self.activate()
        index = studio.acquire_sqlite_cache_index(self.cache)
        index.upsert("pending", {"unknown": [True, None], "usage_count": 8})
        try:
            copies = []
            for number in range(4):
                destination = self.root / f"manual-{number}.sqlite"
                copies.append(studio.create_cache_index_backup(self.cache, destination=destination))
            before = copies[0].read_bytes()
            with self.assertRaises(FileExistsError):
                studio.create_cache_index_backup(self.cache, destination=copies[0])
            self.assertEqual(copies[0].read_bytes(), before)
            with contextlib.closing(sqlite3.connect(copies[-1])) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                metadata = connection.execute("SELECT record_json FROM entries WHERE key='pending'").fetchone()[0]
            self.assertEqual(json.loads(metadata), {"unknown": [True, None], "usage_count": 8})
            self.assertEqual(len(copies), 4)
            self.assertEqual(len(self.safety_files()), 2)
        finally:
            studio.release_sqlite_cache_index(index)

    def test_manual_sqlite_prepublication_failure_leaves_no_file_or_temporary_copy(self):
        self.activate()
        destination = self.root / "failed.sqlite"
        with mock.patch.object(studio.os, "link", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                studio.create_cache_index_backup(self.cache, destination=destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".failed.sqlite.*.tmp*")))

    @unittest.skipIf(os.name == "nt", "Directory fsync is not exposed on Windows")
    def test_sqlite_directory_fsync_failure_removes_only_new_manual_copy(self):
        self.activate()
        index = studio.acquire_sqlite_cache_index(self.cache)
        index.flush()
        destination = self.root / "failed.sqlite"
        original_fsync = studio.os.fsync
        failed = [False]

        def fail_once(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed[0]:
                failed[0] = True
                raise OSError("directory durability failed")
            return original_fsync(descriptor)

        try:
            with mock.patch.object(studio.os, "fsync", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "directory durability failed"):
                    studio.create_cache_index_backup(self.cache, destination=destination)
            self.assertFalse(destination.exists())
            self.assertFalse(list(self.root.glob(".failed.sqlite.*.tmp*")))
            self.assertEqual(index.count(), 1)
        finally:
            studio.release_sqlite_cache_index(index)

    def test_one_source_copy_after_activation_no_database_clone_and_legacy_files_retained(self):
        self.backups.mkdir()
        legacy = self.backups / ("index_1_" + "a" * 32 + ".json")
        legacy.write_bytes(b"user copy")
        legacy.with_name(legacy.name + ".receipt.json").write_text('{"schema":1,"reason":"manual"}')
        unknown = self.backups / "keep-me.sqlite"
        unknown.write_bytes(b"unknown")
        with mock.patch.object(studio.SQLiteCacheIndex, "backup", side_effect=AssertionError("redundant database copy")):
            receipt = self.activate()
        self.assertTrue(receipt["database_reopened"])
        self.assertFalse(receipt["backup_reopened"])
        self.assertNotIn("verified_backup", receipt)
        self.assertEqual(len(self.safety_files()), 2)
        snapshot = next(path for path in self.safety_files() if not path.name.endswith(".receipt.json"))
        self.assertEqual(snapshot.read_bytes(), self.main.read_bytes())
        self.assertEqual(legacy.read_bytes(), b"user copy")
        self.assertEqual(unknown.read_bytes(), b"unknown")

    def test_successful_roundtrip_retains_exactly_one_latest_source_copy(self):
        self.activate()
        old_snapshot = next(path for path in self.safety_files() if not path.name.endswith(".receipt.json"))
        result = studio.switch_sqlite_cache_to_json(self.cache)
        sqlite_snapshot = Path(result["backup_path"])
        self.assertFalse(old_snapshot.exists())
        self.assertEqual(len(self.safety_files()), 2)
        self.assertTrue(sqlite_snapshot.is_file())
        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertEqual(len(self.safety_files()), 2)
        self.main.write_text('{"new":{"raw":"актуально"}}', encoding="utf-8")
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(len(self.safety_files()), 2)  # Preparation makes no JSON copy.
        studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(len(self.safety_files()), 2)
        snapshot = next(path for path in self.safety_files() if not path.name.endswith(".receipt.json"))
        self.assertEqual(snapshot.read_bytes(), self.main.read_bytes())

    def test_failed_prepare_and_switch_keep_previous_copy_without_accumulating_failed_attempts(self):
        self.activate()
        studio.switch_sqlite_cache_to_json(self.cache)
        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        previous = {path.name: path.read_bytes() for path in self.safety_files()}
        with mock.patch.object(studio.SQLiteCacheIndex, "iter_pages", side_effect=OSError("inventory failed")):
            with self.assertRaisesRegex(OSError, "inventory failed"):
                studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertFalse(self.database.exists())
        self.assertEqual({path.name: path.read_bytes() for path in self.safety_files()}, previous)
        self.activate()
        previous = {path.name: path.read_bytes() for path in self.safety_files()}
        with mock.patch.object(studio.SQLiteCacheIndex, "export_json_snapshot", side_effect=OSError("export failed")):
            with self.assertRaisesRegex(OSError, "export failed"):
                studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual({path.name: path.read_bytes() for path in self.safety_files()}, previous)

    def test_discard_pending_migration_retains_previous_safety_copy(self):
        self.activate()
        studio.switch_sqlite_cache_to_json(self.cache)
        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        previous = {path.name: path.read_bytes() for path in self.safety_files()}
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(len(self.safety_files()), 2)
        studio.discard_prepared_sqlite_cache_migration(self.cache)
        self.assertFalse(self.database.exists())
        self.assertEqual({path.name: path.read_bytes() for path in self.safety_files()}, previous)
        self.assertNotIn("source_snapshot", receipt)

    def test_changed_safety_copy_refuses_activation_without_deleting_previous_files(self):
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        snapshot = studio._create_cache_safety_backup(self.cache, "json", "migration",
            source=receipt["source"], expected_sha256=receipt["source_sha256"])
        receipt.update(source_snapshot=snapshot.name, backup_policy="single_format_switch")
        studio._write_cache_json_durable(self.cache / "sentence_cache.migration.json", receipt)
        snapshot.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Страховочная"):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(snapshot.read_bytes(), b"changed")

    def test_failed_activation_durability_restores_json_and_preserves_previous_safety_copy(self):
        self.activate()
        studio.switch_sqlite_cache_to_json(self.cache)
        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        previous = {path.name: path.read_bytes() for path in self.safety_files()}
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        marker = self.cache / studio._CACHE_BACKEND_MARKER
        original_fsync = studio._fsync_cache_directory
        failed = [False]

        def fail_marker_once(path):
            if Path(path) == self.cache and marker.exists() and not failed[0]:
                failed[0] = True
                raise OSError("activation durability failed")
            return original_fsync(path)

        with mock.patch.object(studio, "_fsync_cache_directory", side_effect=fail_marker_once):
            with self.assertRaisesRegex(OSError, "activation durability failed"):
                studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(len(self.safety_files()), 2)
        self.assertTrue(all((self.backups / name).read_bytes() == content for name, content in previous.items()))
        studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(len(self.safety_files()), 2)

    def test_clear_json_after_switch_removes_uuid_audio_without_creating_metadata_backup(self):
        self.activate()
        studio.switch_sqlite_cache_to_json(self.cache)
        audio = self.cache / "audio"
        audio.mkdir()
        owned = audio / ("a" * 32 + "_" + "b" * 32 + ".ogg")
        owned.write_bytes(b"owned")
        unknown = audio / "my-recording.ogg"
        unknown.write_bytes(b"user")
        glossary = self.cache / "glossary.json"
        glossary.write_text('{"x":"y"}')
        previous = {path.name: path.read_bytes() for path in self.safety_files()}
        with mock.patch.object(studio, "create_cache_index_backup", side_effect=AssertionError("implicit backup")):
            studio.clear_cache_storage(self.cache)
        self.assertFalse(owned.exists())
        self.assertFalse(self.main.exists())
        self.assertEqual(unknown.read_bytes(), b"user")
        self.assertEqual(glossary.read_text(), '{"x":"y"}')
        self.assertEqual({path.name: path.read_bytes() for path in self.safety_files()}, previous)


if __name__ == "__main__":
    unittest.main()
