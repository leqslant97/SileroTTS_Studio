"""Actual SQLite bulk operations preserve refs and support resume, without HTTP."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache_dir = Path(temporary.name).resolve() / "cache"
        self.audio_dir = self.cache_dir / "audio"
        self.audio_dir.mkdir(parents=True)
        self.index = None

    def generate(self, name, codec="libvorbis"):
        path = self.audio_dir / name
        subprocess.run([studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:sample_rate=48000:duration=0.15", "-ac", "1",
                        "-c:a", codec, str(path)], check=True, capture_output=True)
        return path

    def activate(self, entries):
        (self.cache_dir / "sentence_cache.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        receipt = studio.prepare_sqlite_cache_migration(self.cache_dir, confirmed_local_storage=True)
        studio.activate_sqlite_cache_migration(self.cache_dir, receipt)
        self.index = studio.acquire_sqlite_cache_index(self.cache_dir)
        self.addCleanup(lambda: studio.release_sqlite_cache_index(self.index))
        return self.index

    def test_transcode_filename_snapshot_counts_shared_audio_once_and_preserves_unknown_fields(self):
        original = self.generate("shared.ogg")
        original_bytes = original.read_bytes()
        index = self.activate({"a": {"file_name": original.name, "speaker": "a", "unknown": {"hello": "Ё"}},
                               "b": {"file_name": original.name, "speaker": "b", "steps": 15},
                               "missing": {"file_name": "missing.ogg"}})
        with mock.patch.object(studio.requests.Session, "post", side_effect=AssertionError("HTTP forbidden")):
            stats = studio.transcode_sqlite_cache_entries_to_opus(self.cache_dir, index, max_workers=2)
        self.assertEqual(stats["converted"], 1)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(index.get("a")["unknown"], {"hello": "Ё"})
        self.assertEqual(index.get("b")["steps"], 15)
        for key in ("a", "b"):
            record = index.get(key)
            self.assertEqual(record["audio_codec"], "opus")
            audio = studio.resolve_cache_audio_path(self.cache_dir, record["file_name"])
            studio._validate_complete_ogg_audio_file(audio)
            self.assertEqual(studio._detect_ogg_audio_codec(audio), "opus")
        # Source bytes remain available until the caller's reader barrier allows GC.
        self.assertEqual(original.read_bytes(), original_bytes)
        again = studio.transcode_sqlite_cache_entries_to_opus(self.cache_dir, index, max_workers=2)
        self.assertEqual(again["converted"], 0)
        self.assertEqual(again["failed"], 0)

    def test_cancellation_before_first_file_does_not_modify_audio_or_records(self):
        original = self.generate("chapter.ogg")
        bytes_before = original.read_bytes()
        index = self.activate({"a": {"file_name": original.name, "unknown": "keep"}})
        record_before = index.get("a")
        cancelled = threading.Event()
        cancelled.set()
        stats = studio.transcode_sqlite_cache_entries_to_opus(self.cache_dir, index, cancel_event=cancelled)
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["converted"], 0)
        self.assertEqual(index.get("a"), record_before)
        self.assertEqual(original.read_bytes(), bytes_before)
        self.assertFalse(list(self.audio_dir.glob(".cache-transcode-*")))

    def test_delete_existing_sqlite_rows_keeps_audio_with_other_live_reference(self):
        original = self.generate("shared.ogg", "libopus")
        index = self.activate({"a": {"file_name": original.name}, "b": {"file_name": original.name}})
        app = studio.TTSApp.__new__(studio.TTSApp)
        app._delete_sqlite_cache_rows(self.cache_dir, ["a"], index=index)
        self.assertIsNone(index.get("a"))
        self.assertTrue(original.exists())
        app._delete_sqlite_cache_rows(self.cache_dir, ["b"], index=index)
        self.assertFalse(original.exists())
        self.assertEqual(index.count(), 0)


if __name__ == "__main__":
    unittest.main()
