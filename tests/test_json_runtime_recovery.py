"""Повреждённый JSON-кэш блокирует синтез без потери исходных индексов."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_regressions as regression

studio = regression.studio


class JsonRuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.primary = self.cache / "sentence_cache.json"
        self.backup = self.cache / "sentence_cache.json.bak"
        self.cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.cfg.update(cache_dir=str(self.cache), input_dir=str(self.root / "input"),
                        output_dir=str(self.root / "output"))
        app_data = mock.patch.object(studio, "APP_DATA_DIR", self.root / "data")
        app_data.start()
        self.addCleanup(app_data.stop)
        http_guard = mock.patch.object(studio.requests.Session, "post", side_effect=AssertionError("real HTTP forbidden"))
        self.http = http_guard.start()
        self.addCleanup(http_guard.stop)

    def make_processor(self, **options):
        cfg = dict(self.cfg, **options)
        processor = studio.TTSProcessor(cfg, shared_processing_statuses={})
        self.addCleanup(processor.cleanup_transient_audio_files)
        self.addCleanup(processor.close_cache)
        self.addCleanup(processor.session.close)
        return processor

    def test_two_unreadable_existing_indexes_stop_before_network_and_preserve_both(self):
        damaged = b"{damaged-primary", b"{damaged-backup"
        self.primary.write_bytes(damaged[0])
        self.backup.write_bytes(damaged[1])
        with mock.patch.object(studio.requests.Session, "close") as close:
            with self.assertRaises(studio.CacheRecoveryError):
                self.make_processor()
            close.assert_called_once_with()
        self.assertEqual(self.primary.read_bytes(), damaged[0])
        self.assertEqual(self.backup.read_bytes(), damaged[1])
        self.http.assert_not_called()

    def test_absent_indexes_remain_a_valid_new_empty_cache(self):
        processor = self.make_processor()
        self.assertEqual(processor.cache_backend, "sqlite")
        self.assertEqual(processor.cache.count(), 0)
        self.assertFalse(self.primary.exists())
        self.assertFalse(self.backup.exists())
        self.http.assert_not_called()

    def test_corrupt_primary_uses_valid_backup_without_erasing_it(self):
        self.primary.write_bytes(b"{damaged")
        metadata = {"file_name": "sample.ogg", "speaker": self.cfg["speaker"],
                    "normalized_text": "Проверка.", "created_at": 1,
                    "last_accessed": 2, "usage_count": 3, "custom": ["ё", 2]}
        self.backup.write_text(json.dumps({"key": metadata}), encoding="utf-8")
        before_backup = self.backup.read_bytes()
        processor = self.make_processor()
        self.assertEqual(processor.cache, {"key": metadata})
        self.assertEqual(self.backup.read_bytes(), before_backup)
        self.assertEqual(self.primary.read_bytes(), b"{damaged")
        self.http.assert_not_called()

    def test_cache_off_does_not_inspect_or_change_corrupt_indexes(self):
        damaged = b"{damaged-primary", b"{damaged-backup"
        self.primary.write_bytes(damaged[0])
        self.backup.write_bytes(damaged[1])
        with mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("index read")):
            processor = self.make_processor(use_cache=False)
            self.assertEqual(processor.cache, {})
            self.assertTrue(processor.flush_cache())
        self.assertEqual(self.primary.read_bytes(), damaged[0])
        self.assertEqual(self.backup.read_bytes(), damaged[1])
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
