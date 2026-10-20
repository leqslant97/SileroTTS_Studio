"""Inactive exact metadata staging, preserving source cache and audio."""

import importlib.util
import json
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cache = self.root / "source-cache"
        cache.mkdir()
        self.source = cache / "sentence_cache.json"
        self.backup = self.source.with_suffix(".json.bak")
        self.destination = self.root / "candidate" / "metadata.sqlite"

    def write(self, data, path=None):
        (path or self.source).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_exact_metadata_roundtrip_does_not_normalize_or_require_audio(self):
        data = {"historical/key": {"file_name": "missing.ogg", "last_accessed": "unknown", "usage_count": -1,
                                   "unknown": {"future": [None, "Ёж", 1]}},
                "": {"file_name": "../unsafe.ogg"}, "\x00": {}}
        self.write(data)
        before = self.source.read_bytes()
        receipt = studio.stage_sqlite_cache_metadata(self.source, self.destination, batch_size=1)
        self.assertEqual(receipt["activation"], "inactive")
        self.assertEqual(receipt["record_count"], 3)
        self.assertEqual(receipt["normalization"], "none")
        self.assertFalse(receipt["audio_verified"])
        self.assertTrue(receipt["database_reopened"])
        self.assertFalse(receipt["backup_reopened"])
        with studio.SQLiteCacheIndex(self.destination) as index:
            self.assertEqual({key: index.get(key) for key in data}, data)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.source.parent.iterdir()), {self.source})
        self.assertFalse(list(self.destination.parent.glob("*.verified-backup.sqlite")))

    def test_backup_fallback_is_explicit_and_main_does_not_merge_backup(self):
        self.source.write_text("invalid", encoding="utf-8")
        self.write({"backup": {}}, self.backup)
        receipt = studio.stage_sqlite_cache_metadata(self.source, self.destination)
        self.assertEqual(receipt["source_kind"], "backup")
        self.assertEqual(len(receipt["source_errors"]), 1)
        second = self.destination.with_name("second.sqlite")
        self.write({"main": {}})
        receipt = studio.stage_sqlite_cache_metadata(self.source, second)
        self.assertEqual(receipt["source_kind"], "main")
        with studio.SQLiteCacheIndex(second) as index:
            self.assertIsNotNone(index.get("main"))
            self.assertIsNone(index.get("backup"))

    def test_both_bad_duplicate_and_nonfinite_json_never_publish_destination(self):
        for payload in ('{"key":{"x":1,"x":2}}', '{"key":{"value":NaN}}',
                        '{"key":{"value":1e999}}', '{"key":"old path"}', '[]'):
            self.source.write_text(payload, encoding="utf-8")
            self.backup.write_text("bad", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no valid"):
                studio.stage_sqlite_cache_metadata(self.source, self.destination)
            self.assertFalse(self.destination.exists())

    def test_existing_target_sidecar_and_source_directory_are_not_changed(self):
        self.write({"key": {}})
        with self.assertRaises(ValueError):
            studio.stage_sqlite_cache_metadata(self.source, self.source.parent / "metadata.sqlite")
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            studio.stage_sqlite_cache_metadata(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"existing")
        self.destination.unlink()
        sidecar = Path(str(self.destination) + "-wal")
        sidecar.write_bytes(b"live sidecar")
        with self.assertRaises(FileExistsError):
            studio.stage_sqlite_cache_metadata(self.source, self.destination)
        self.assertEqual(sidecar.read_bytes(), b"live sidecar")

    def test_failed_import_keeps_inactive_stage_without_publishing_target(self):
        self.write({"record": {"large": "x" * (1024 * 1024)}})
        with self.assertRaisesRegex(ValueError, "max_record_bytes"):
            studio.stage_sqlite_cache_metadata(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        stages = list(self.destination.parent.glob("*.stage.sqlite"))
        self.assertEqual(len(stages), 1)
        with contextlib.closing(sqlite3.connect(stages[0])) as connection:
            self.assertEqual(connection.execute("SELECT value FROM meta WHERE key='activation'").fetchone()[0], "inactive")


if __name__ == "__main__":
    unittest.main()
