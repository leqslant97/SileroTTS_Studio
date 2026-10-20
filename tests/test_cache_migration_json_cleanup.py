"""JSON migration backups are optional; mirror cleanup follows durable activation."""

import importlib.util
import json
import shutil
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


class CacheMigrationJsonCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.main = self.cache / "sentence_cache.json"
        self.backup = self.cache / "sentence_cache.json.bak"
        self.marker = self.cache / studio._CACHE_BACKEND_MARKER
        self.backups = self.cache / "index_backups"
        self.payload = b'{ "current": {"unknown":[true,null], "usage_count":3} }'
        self.main.write_bytes(self.payload)
        self.backup.write_bytes(self.payload)

    def prepare(self):
        return studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)

    def safety_data(self):
        return sorted(path for path in self.backups.glob("safety_*") if not path.name.endswith(".receipt.json"))

    def legacy_prepared(self, receipt, *, owned=True):
        if owned:
            snapshot = studio._create_cache_safety_backup(self.cache, "json", "migration",
                source=receipt["source"], expected_sha256=receipt["source_sha256"])
            receipt.update(source_snapshot=snapshot.name, backup_policy="single_format_switch")
        else:
            self.backups.mkdir(exist_ok=True)
            snapshot = self.backups / ("migration_source_" + "a" * 32 + ".json")
            snapshot.write_bytes(self.payload)
            receipt["source_snapshot"] = snapshot.name
        studio._write_cache_json_durable(self.cache / "sentence_cache.migration.json", receipt)
        return snapshot

    def test_prepare_is_readonly_for_json_and_creates_no_backup_default_activation_removes_mirror(self):
        receipt = self.prepare()
        self.assertEqual(self.main.read_bytes(), self.payload)
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertFalse(self.backups.exists())
        self.assertNotIn("source_snapshot", receipt)
        warnings = []
        result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.main.read_bytes(), self.payload)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.backups.exists())
        self.assertEqual(warnings, [])

    def test_explicit_backup_created_only_at_activation_with_exact_source_bytes(self):
        receipt = self.prepare()
        self.assertFalse(self.backups.exists())
        studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        snapshots = self.safety_data()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), self.payload)
        self.assertFalse(self.backup.exists())

    def test_invalid_backup_option_is_rejected_without_modification(self):
        receipt = self.prepare()
        for value in ("false", None, 1, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=value)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertFalse(self.backups.exists())

    def test_backup_fallback_restores_exact_main_before_removing_mirror(self):
        for main_state in ("corrupt", "absent"):
            with self.subTest(main_state=main_state):
                if main_state == "corrupt":
                    self.main.write_bytes(b"broken")
                else:
                    self.main.unlink(missing_ok=True)
                receipt = self.prepare()
                self.assertEqual(receipt["source"], str(self.backup))
                studio.activate_sqlite_cache_migration(self.cache, receipt)
                self.assertEqual(self.main.read_bytes(), self.payload)
                self.assertFalse(self.backup.exists())
                self.assertFalse(self.backups.exists())
                # Set up the second independent migration fixture in-place.
                self.marker.unlink()
                for suffix in ("", "-wal", "-shm", "-journal", ".writer.lock"):
                    Path(str(self.cache / studio._CACHE_DATABASE_NAME) + suffix).unlink(missing_ok=True)
                (self.cache / "sentence_cache.migration.json").unlink()
                self.backup.write_bytes(self.payload)

    def test_marker_failure_keeps_corrupt_main_and_valid_mirror_and_removes_attempt_copy(self):
        self.main.write_bytes(b"broken")
        receipt = self.prepare()
        original_fsync = studio._fsync_cache_directory
        failed = [False]

        def fail_marker(path):
            if Path(path) == self.cache and self.marker.exists() and not failed[0]:
                failed[0] = True
                raise OSError("marker durability failed")
            return original_fsync(path)

        with mock.patch.object(studio, "_fsync_cache_directory", side_effect=fail_marker):
            with self.assertRaisesRegex(OSError, "marker durability failed"):
                studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.main.read_bytes(), b"broken")
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertEqual(self.safety_data(), [])

    def test_requested_backup_failure_prevents_activation_and_preserves_json(self):
        receipt = self.prepare()
        with mock.patch.object(studio, "_create_cache_safety_backup", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.main.read_bytes(), self.payload)
        self.assertEqual(self.backup.read_bytes(), self.payload)

    def test_modified_mirror_before_activation_is_not_deleted_and_refuses_switch(self):
        receipt = self.prepare()
        self.backup.write_bytes(b'{"new":{}}')
        with self.assertRaisesRegex(ValueError, "изменился"):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual(self.backup.read_bytes(), b'{"new":{}}')

    def test_mirror_changed_during_marker_publication_is_kept_and_warning_reports_success(self):
        receipt = self.prepare()
        original_write = studio._write_cache_json_durable
        warnings = []

        def publish_then_change(path, data):
            result = original_write(path, data)
            if Path(path) == self.marker:
                self.backup.write_bytes(b'{"changed_after_check":{}}')
            return result

        with mock.patch.object(studio, "_write_cache_json_durable", side_effect=publish_then_change):
            result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.backup.read_bytes(), b'{"changed_after_check":{}}')
        self.assertEqual(len(warnings), 1)
        self.assertIn("SQLite активирован", warnings[0])

    def test_unlink_failure_is_warning_after_success_and_keeps_mirror(self):
        receipt = self.prepare()
        original_unlink = Path.unlink
        warnings = []

        def fail_mirror(path, *args, **kwargs):
            if path == self.backup:
                raise OSError("cannot delete")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_mirror):
            result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertEqual(len(warnings), 1)
        self.assertIn("cannot delete", warnings[0])

    def test_cleanup_directory_fsync_failure_reports_success_with_warning(self):
        receipt = self.prepare()
        warnings = []
        original_fsync = studio._fsync_cache_directory
        failed = [False]

        def fail_cleanup(path):
            if Path(path) == self.cache and self.marker.exists() and not self.backup.exists() and not failed[0]:
                failed[0] = True
                raise OSError("cleanup durability failed")
            return original_fsync(path)

        with mock.patch.object(studio, "_fsync_cache_directory", side_effect=fail_cleanup):
            result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertFalse(self.backup.exists())
        self.assertEqual(len(warnings), 1)
        self.assertIn("cleanup durability failed", warnings[0])

    def test_pruning_failure_warns_after_success_and_preserves_requested_copy(self):
        receipt = self.prepare()
        warnings = []
        with mock.patch.object(studio, "_prune_owned_cache_backups", side_effect=OSError("cannot rotate")):
            result = studio.activate_sqlite_cache_migration(
                self.cache, receipt, keep_json_backup=True, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertFalse(self.backup.exists())
        self.assertEqual(len(self.safety_data()), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("cannot rotate", warnings[0])

    def test_post_marker_symlink_mirror_is_kept_without_touching_external_target(self):
        receipt = self.prepare()
        original_write = studio._write_cache_json_durable
        warnings = []
        outside = self.root / "outside.json"
        outside.write_bytes(self.payload)

        def publish_then_link(path, data):
            result = original_write(path, data)
            if Path(path) == self.marker:
                self.backup.unlink()
                self.backup.symlink_to(outside)
            return result

        with mock.patch.object(studio, "_write_cache_json_durable", side_effect=publish_then_link):
            result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(outside.read_bytes(), self.payload)
        self.assertEqual(len(warnings), 1)

    def test_restore_main_failure_preserves_fallback_and_reports_success_warning(self):
        self.main.write_bytes(b"broken")
        receipt = self.prepare()
        warnings = []
        with mock.patch.object(studio, "_copy_cache_index_backup_json", side_effect=OSError("cannot restore")):
            result = studio.activate_sqlite_cache_migration(self.cache, receipt, warning=warnings.append)
        self.assertEqual(result, self.marker)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "sqlite")
        self.assertEqual(self.main.read_bytes(), b"broken")
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertEqual(len(warnings), 1)

    def test_old_prepared_owned_copy_reused_only_when_selected_and_removed_only_after_success(self):
        receipt = self.prepare()
        pending = self.legacy_prepared(receipt)
        with mock.patch.object(studio, "_create_cache_safety_backup", side_effect=AssertionError("extra copy")):
            studio.activate_sqlite_cache_migration(self.cache, receipt, keep_json_backup=True)
        self.assertEqual(self.safety_data(), [pending])
        self.assertEqual(pending.read_bytes(), self.payload)

    def test_old_prepared_default_discards_only_its_pending_copy_and_keeps_unrelated_legacy(self):
        receipt = self.prepare()
        pending = self.legacy_prepared(receipt)
        unrelated = self.backups / "my-previous.json"
        unrelated.write_bytes(b"user")
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertFalse(pending.exists())
        self.assertFalse(pending.with_name(pending.name + ".receipt.json").exists())
        self.assertEqual(unrelated.read_bytes(), b"user")
        self.assertFalse(self.backup.exists())

    def test_old_legacy_migration_source_copy_is_verified_and_preserved(self):
        receipt = self.prepare()
        legacy = self.legacy_prepared(receipt, owned=False)
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(legacy.read_bytes(), self.payload)
        self.assertEqual(self.safety_data(), [])

    def test_old_pending_backup_parent_symlink_refuses_activation_and_preserves_external_copy(self):
        receipt = self.prepare()
        pending = self.legacy_prepared(receipt)
        outside = self.root / "external-backups"
        shutil.move(self.backups, outside)
        self.backups.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "небезопасна"):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")
        self.assertEqual((outside / pending.name).read_bytes(), self.payload)
        self.assertEqual(self.backup.read_bytes(), self.payload)

    def test_symlink_mirror_before_activation_refuses_without_touching_target(self):
        receipt = self.prepare()
        self.backup.unlink()
        outside = self.root / "outside.json"
        outside.write_bytes(self.payload)
        self.backup.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "ссылку"):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(outside.read_bytes(), self.payload)
        self.assertTrue(self.backup.is_symlink())
        self.assertEqual(studio.get_active_cache_backend(self.cache), "json")


if __name__ == "__main__":
    unittest.main()
