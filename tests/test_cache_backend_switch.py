"""Explicit backend changes retain current records and fail without stale authority."""

import contextlib
import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
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


class CacheBackendSwitchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.database = self.cache / studio._CACHE_DATABASE_NAME
        self.marker = self.cache / studio._CACHE_BACKEND_MARKER
        self.main = self.cache / "sentence_cache.json"
        self.main.write_text('{"stale":{}}', encoding="utf-8")
        self.addCleanup(self.cleanup_owner)

    def cleanup_owner(self):
        entry = studio._SQLITE_CACHE_POOL.get(str(self.database))
        if entry is not None:
            index = entry["index"]
            index._blocked_keys.clear()
            index._unidentified_recovery_error = None
            while str(self.database) in studio._SQLITE_CACHE_POOL:
                studio.release_sqlite_cache_index(index)

    def activate_fixture(self, data=None):
        with studio.SQLiteCacheIndex(self.database) as index:
            for key, metadata in (data or {}).items():
                index.upsert(key, metadata)
            index._request("set_meta", "activation", "active")
        studio._write_cache_json_durable(self.marker,
            {"schema": 1, "backend": "sqlite", "database": studio._CACHE_DATABASE_NAME,
             "storage_policy": "confirmed_local", "synchronous": "FULL"})

    def test_switch_exports_current_json_closes_owner_and_preserves_files_with_backup(self):
        data = {"current": {"file_name": "a" * 32 + "_" + "b" * 32 + ".ogg",
                            "unknown": {"text": "Ёж", "list": [None, True]}, "usage_count": 8}}
        self.activate_fixture(data)
        audio = self.cache / "audio" / data["current"]["file_name"]
        audio.parent.mkdir()
        audio.write_bytes(b"unchanged audio")
        backup = self.cache / "sentence_cache.json.bak"
        backup.write_bytes(b"retained original backup")
        result = studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(result["backend"], "json")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(json.loads(self.main.read_text()), data)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertNotIn(str(self.database), studio._SQLITE_CACHE_POOL)
        self.assertTrue(self.database.is_file())
        self.assertEqual(audio.read_bytes(), b"unchanged audio")
        self.assertEqual(backup.read_bytes(), b"retained original backup")
        snapshot = Path(result["backup_path"])
        with contextlib.closing(sqlite3.connect(snapshot)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(json.loads(connection.execute("SELECT record_json FROM entries").fetchone()[0]), data["current"])
        receipt = json.loads(snapshot.with_name(snapshot.name + ".receipt.json").read_text())
        self.assertEqual(receipt["reason"], "backend_switch")
        self.assertFalse(list(self.cache.glob("*.retired")))
        studio._CacheWriterLease(str(self.database) + ".writer.lock").close()

    def test_any_held_reference_refuses_switch_before_export_or_backup(self):
        self.activate_fixture({"current": {}})
        index = studio.acquire_sqlite_cache_index(self.cache)
        with mock.patch.object(studio, "create_cache_index_backup") as backup:
            with self.assertRaisesRegex(RuntimeError, "читателей"):
                studio.switch_sqlite_cache_to_json(self.cache)
        backup.assert_not_called()
        self.assertEqual(self.main.read_text(), '{"stale":{}}')
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(index.count(), 1)

    def test_other_process_writer_lease_refuses_without_changing_authority(self):
        self.activate_fixture({"current": {}})
        with studio.SQLiteCacheIndex(self.database):
            with self.assertRaisesRegex(RuntimeError, "экземпляром"):
                studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.main.read_text(), '{"stale":{}}')

    def test_backup_export_and_close_failures_leave_sqlite_selected(self):
        self.activate_fixture({"current": {"value": 2}})
        for operation in ("backup", "export_json_snapshot", "close"):
            with self.subTest(operation=operation):
                original = getattr(studio.SQLiteCacheIndex, operation)
                failed = [False]

                def fail_once(index, *args, **kwargs):
                    if not failed[0]:
                        failed[0] = True
                        raise OSError("operation failed")
                    return original(index, *args, **kwargs)

                with mock.patch.object(studio.SQLiteCacheIndex, operation, fail_once):
                    with self.assertRaisesRegex(OSError, "operation failed"):
                        studio.switch_sqlite_cache_to_json(self.cache)
                self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
                with studio.SQLiteCacheIndex(self.database) as index:
                    self.assertEqual(index.get("current"), {"value": 2})

    def test_marker_removal_and_directory_fsync_failures_restore_sqlite_authority(self):
        self.activate_fixture({"current": {}})
        real_replace = studio.os.replace
        real_fsync = studio._fsync_cache_directory
        for stage in ("marker_rename", "directory_fsync"):
            with self.subTest(stage=stage):
                renamed = [False]

                def rename(source, destination):
                    if Path(source) == self.marker:
                        if stage == "marker_rename":
                            raise OSError("marker failure")
                        renamed[0] = True
                    return real_replace(source, destination)

                def fsync(path):
                    if stage == "directory_fsync" and renamed[0] and Path(path) == self.cache:
                        renamed[0] = False
                        raise OSError("marker failure")
                    return real_fsync(path)

                with mock.patch.object(studio.os, "replace", side_effect=rename), \
                     mock.patch.object(studio, "_fsync_cache_directory", side_effect=fsync):
                    with self.assertRaisesRegex(OSError, "marker failure"):
                        studio.switch_sqlite_cache_to_json(self.cache)
                self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
                self.assertNotIn(str(self.database), studio._SQLITE_CACHE_POOL)
                self.assertEqual(json.loads(self.main.read_text()), {"current": {}})
        studio._CacheWriterLease(str(self.database) + ".writer.lock").close()

    def test_persistent_close_failure_retains_owner_without_a_phantom_reference_and_allows_retry(self):
        self.activate_fixture({"current": {"value": "retained"}})
        with mock.patch.object(studio.SQLiteCacheIndex, "close", side_effect=OSError("persistent close failure")):
            with self.assertRaisesRegex(OSError, "persistent close failure"):
                studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        entry = studio._SQLITE_CACHE_POOL[str(self.database)]
        self.assertEqual(entry["references"], 0)
        self.assertFalse(entry["index"]._closed)
        self.assertEqual(entry["index"].get("current"), {"value": "retained"})
        result = studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(result["backend"], "json")
        self.assertNotIn(str(self.database), studio._SQLITE_CACHE_POOL)
        self.assertEqual(json.loads(self.main.read_text()), {"current": {"value": "retained"}})

    def test_damaged_recovery_receipt_blocks_switch_and_is_retained(self):
        self.activate_fixture({"current": {}})
        intents = self.cache / "publication_intents"
        intents.mkdir()
        receipt = intents / ("1_" + "a" * 32 + ".json")
        receipt.write_text("[]", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(studio.CacheRecoveryError):
                studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.main.read_text(), '{"stale":{}}')
        self.assertEqual(receipt.read_text(), "[]")

    def test_rebuild_requires_confirmation_json_authority_and_valid_source(self):
        self.activate_fixture({"current": {}})
        with self.assertRaisesRegex(ValueError, "Подтвердите"):
            studio.reset_sqlite_cache_for_migration(self.cache)
        with self.assertRaisesRegex(ValueError, "Рабочий SQLite"):
            studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        studio.switch_sqlite_cache_to_json(self.cache)
        self.main.write_text("bad", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON"):
            studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertTrue(self.database.is_file())

    def test_reset_retained_database_keeps_json_audio_and_user_backups(self):
        self.activate_fixture({"current": {}})
        studio.switch_sqlite_cache_to_json(self.cache)
        audio = self.cache / "audio" / "user.ogg"
        audio.parent.mkdir()
        audio.write_bytes(b"audio")
        user_backup = self.cache / "index_backups" / "keep-this.sqlite"
        user_backup.write_bytes(b"user backup")
        original = self.main.read_bytes()
        result = studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertTrue(result["removed"])
        self.assertFalse(self.database.exists())
        self.assertEqual(self.main.read_bytes(), original)
        self.assertEqual(audio.read_bytes(), b"audio")
        self.assertEqual(user_backup.read_bytes(), b"user backup")
        self.assertTrue(Path(result["backup_path"]).is_file())

    def test_reset_backup_close_and_remove_failure_keep_json_authoritative_and_database_readable(self):
        self.activate_fixture({"current": {}})
        studio.switch_sqlite_cache_to_json(self.cache)
        original_json = self.main.read_bytes()
        for operation in ("backup", "close", "unlink"):
            with self.subTest(operation=operation):
                failed = [False]
                if operation == "unlink":
                    original = Path.unlink

                    def fail_database_removal(path, *args, **kwargs):
                        if path == self.database and not failed[0]:
                            failed[0] = True
                            raise OSError("reset failed")
                        return original(path, *args, **kwargs)

                    patch = mock.patch.object(Path, "unlink", fail_database_removal)
                else:
                    original = getattr(studio.SQLiteCacheIndex, operation)

                    def fail_once(index, *args, **kwargs):
                        if not failed[0]:
                            failed[0] = True
                            raise OSError("reset failed")
                        return original(index, *args, **kwargs)

                    patch = mock.patch.object(studio.SQLiteCacheIndex, operation, fail_once)
                with patch:
                    with self.assertRaisesRegex(OSError, "reset failed"):
                        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
                self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
                self.assertEqual(self.main.read_bytes(), original_json)
                with studio.SQLiteCacheIndex(self.database) as index:
                    self.assertEqual(index.count(), 1)

    def test_reset_inactive_candidate_is_explicit_and_retained_receipt_blocks_it(self):
        with studio.SQLiteCacheIndex(self.database) as index:
            index.upsert("candidate", {})
        intents = self.cache / "publication_intents"
        intents.mkdir()
        receipt = intents / ("1_" + "a" * 32 + ".json")
        receipt.write_text("[]")
        with self.assertRaises(studio.CacheRecoveryError):
            studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertTrue(self.database.is_file())
        receipt.unlink()
        result = studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertTrue(result["removed"])
        self.assertEqual(json.loads(self.main.read_text()), {"stale": {}})

    def test_persistent_reset_close_failure_retains_standalone_owner_for_retry(self):
        self.activate_fixture({"current": {}})
        studio.switch_sqlite_cache_to_json(self.cache)
        with mock.patch.object(studio.SQLiteCacheIndex, "close", side_effect=OSError("reset close failure")):
            with self.assertRaisesRegex(OSError, "reset close failure"):
                studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        entry = studio._SQLITE_CACHE_POOL[str(self.database)]
        self.assertEqual(entry["references"], 0)
        self.assertFalse(entry["index"].runtime)
        self.assertFalse(entry["index"]._closed)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        result = studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        self.assertTrue(result["removed"])
        self.assertNotIn(str(self.database), studio._SQLITE_CACHE_POOL)
        self.assertFalse(self.database.exists())

    def test_writer_lease_remains_exclusive_until_marker_change(self):
        self.activate_fixture({"current": {}})
        real_replace = studio.os.replace
        checked = []

        def rename(source, destination):
            if Path(source) == self.marker:
                with self.assertRaises(RuntimeError):
                    studio._CacheWriterLease(str(self.database) + ".writer.lock")
                self.assertNotIn(str(self.database), studio._SQLITE_CACHE_POOL)
                checked.append(True)
            return real_replace(source, destination)

        with mock.patch.object(studio.os, "replace", side_effect=rename):
            studio.switch_sqlite_cache_to_json(self.cache)
        self.assertEqual(checked, [True])
        studio._CacheWriterLease(str(self.database) + ".writer.lock").close()

    def test_old_active_database_is_not_resumed_as_an_inactive_candidate(self):
        self.activate_fixture({"current": {}})
        report = self.cache / "sentence_cache.migration.json"
        report.write_text(json.dumps({"activation": "inactive", "destination": str(self.database)}))
        studio.switch_sqlite_cache_to_json(self.cache)
        with self.assertRaisesRegex(ValueError, "уже использовался"):
            studio.read_prepared_sqlite_cache_migration(self.cache)

    def test_json_sqlite_json_sqlite_roundtrip_imports_latest_json_instead_of_retained_db(self):
        self.main.write_text('{"first":{"normalized_text":"первый"}}', encoding="utf-8")
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        index = studio.acquire_sqlite_cache_index(self.cache)
        index.upsert("second", {"normalized_text": "второй"})
        studio.release_sqlite_cache_index(index)
        studio.switch_sqlite_cache_to_json(self.cache)
        data = json.loads(self.main.read_text())
        data["third"] = {"normalized_text": "третий"}
        self.main.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        studio.reset_sqlite_cache_for_migration(self.cache, confirmed=True)
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        index = studio.acquire_sqlite_cache_index(self.cache)
        try:
            self.assertEqual(index.count(), 3)
            self.assertEqual(index.get("third"), {"normalized_text": "третий"})
        finally:
            studio.release_sqlite_cache_index(index)

    def test_generation_audio_is_a_cache_hit_after_switch_without_http_or_renaming(self):
        try:
            import test_regressions as regression
        except ModuleNotFoundError:
            from tests import test_regressions as regression
        cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        cfg.update(cache_dir=str(self.cache), input_dir=str(self.root / "input"),
                   output_dir=str(self.root / "output"), auto_trim_silence=False)
        text = "Совместимая запись."
        probe = studio.TTSProcessor(cfg, shared_processing_statuses={})
        key = probe.get_hash(text)
        probe.close_cache()
        generation = key + "_" + "a" * 32 + ".ogg"
        metadata = {"file_name": generation, "speaker": cfg["speaker"], "audio_codec": "opus",
                    "original_text": text, "normalized_text": text, "usage_count": 1}
        self.activate_fixture({key: metadata})
        audio = self.cache / "audio" / generation
        audio.write_bytes(regression.CacheBehaviorTests.complete_cached_opus())
        before = audio.read_bytes()
        studio.switch_sqlite_cache_to_json(self.cache)
        processor = studio.TTSProcessor(cfg, shared_processing_statuses={})
        self.addCleanup(processor.close_cache)
        with mock.patch.object(processor.session, "post", side_effect=AssertionError("HTTP forbidden")):
            self.assertEqual(processor.synthesize_sentence(text, text), (audio, True))
        self.assertEqual(audio.read_bytes(), before)
        self.assertEqual(processor.cache[key]["file_name"], generation)


if __name__ == "__main__":
    unittest.main()
