"""Cache ZIPs stream audio, keep a current index and preserve failed outputs."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
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


class CacheArchiveStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "sample.ogg"
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=330:sample_rate=48000:duration=0.12",
                "-ac", "1", "-c:a", "libopus", str(audio),
            ], check=True, capture_output=True)
            cls.audio_bytes = audio.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "Кэш"
        (self.cache / "audio").mkdir(parents=True)
        self.destination = self.root / "Копия.zip"
        self.name = "a" * 32 + "_" + "b" * 32 + ".ogg"
        self.audio = self.cache / "audio" / self.name
        self.audio.write_bytes(self.audio_bytes)
        self.record = {"file_name": self.name, "original_text": "Глава.",
                       "normalized_text": "Глава.", "speaker": "ilia", "usage_count": 7}
        (self.cache / "sentence_cache.json").write_text(
            json.dumps({"chapter": self.record}, ensure_ascii=False))
        (self.cache / "sentence_cache.json.bak").write_text('{"older":{}}')
        (self.cache / "glossary.json").write_text('{"Ёж":"еж"}')
        self.index = None

    def activate(self):
        database = self.cache / studio._CACHE_DATABASE_NAME
        with studio.SQLiteCacheIndex(database) as index:
            index.upsert("chapter", self.record)
        studio._write_cache_json_durable(self.cache / studio._CACHE_BACKEND_MARKER,
            {"schema": 1, "backend": "sqlite", "database": studio._CACHE_DATABASE_NAME,
             "storage_policy": "confirmed_local", "synchronous": "FULL"})
        self.index = studio.acquire_sqlite_cache_index(self.cache)
        self.addCleanup(lambda: studio.release_sqlite_cache_index(self.index))

    def service_files(self):
        for name in ("index_backups/manual.json", "publication_intents/not-a-receipt.txt",
                     ".DS_Store", "audio/._sample.ogg", "audio/temp_abandoned.ogg",
                     ".sentence_cache.migration.lock", "sentence_cache.migration.json"):
            path = self.cache / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(b"excluded service file")
        secret = self.root / "outside.txt"
        secret.write_bytes(b"outside file must not be copied")
        (self.cache / "linked.txt").symlink_to(secret)
        (self.cache / "linked-folder").symlink_to(self.root, target_is_directory=True)

    def assert_clean(self, archive):
        names = archive.namelist()
        for excluded in ("index_backups", "publication_intents", ".DS_Store", "._",
                         "temp_abandoned", "linked", "migration.json", "-wal", "-shm", ".writer.lock"):
            self.assertFalse(any(excluded in name for name in names), (excluded, names))
        self.assertEqual(archive.read(f"{self.cache.name}/audio/{self.name}"), self.audio_bytes)
        self.assertIsNone(archive.testzip())

    def test_json_archive_excludes_inactive_database_and_external_links(self):
        self.service_files()
        for suffix in ("", "-wal", "-shm", ".writer.lock"):
            (self.cache / (studio._CACHE_DATABASE_NAME + suffix)).write_bytes(b"inactive SQLite")
        studio.create_cache_archive(self.cache, self.destination)
        with zipfile.ZipFile(self.destination) as archive:
            self.assert_clean(archive)
            self.assertFalse(any(".sqlite" in name for name in archive.namelist()))
            self.assertEqual(json.loads(archive.read(f"{self.cache.name}/sentence_cache.json")),
                             {"chapter": self.record})
            self.assertIn(f"{self.cache.name}/sentence_cache.json.bak", archive.namelist())

    def test_sqlite_archive_reads_audio_in_place_and_restores_latest_pending_metadata(self):
        self.activate()
        self.service_files()
        current = dict(self.record, usage_count=99)
        self.index.upsert("chapter", current)
        orphan = self.cache / "audio" / ("c" * 32 + ".ogg")
        orphan.write_bytes(self.audio_bytes)
        original_copy = studio.shutil.copyfile
        original_write = zipfile.ZipFile.write
        audio_sources = []

        def copy_metadata_only(source, *args, **kwargs):
            self.assertNotEqual(Path(source).parent, self.cache / "audio")
            return original_copy(source, *args, **kwargs)

        def record_source(archive, filename, *args, **kwargs):
            if Path(filename).suffix == ".ogg":
                audio_sources.append(Path(filename))
            return original_write(archive, filename, *args, **kwargs)

        with mock.patch.object(studio.shutil, "copyfile", side_effect=copy_metadata_only), \
                mock.patch.object(zipfile.ZipFile, "write", record_source):
            studio.create_cache_archive(self.cache, self.destination)
        self.assertEqual(audio_sources, [self.audio])
        with zipfile.ZipFile(self.destination) as archive:
            self.assert_clean(archive)
            self.assertFalse(any("sentence_cache.json" in name for name in archive.namelist()))
            self.assertNotIn(f"{self.cache.name}/audio/{orphan.name}", archive.namelist())
            archive.extractall(self.root / "restore")
        restored_dir = self.root / "restore" / self.cache.name
        restored = studio.acquire_sqlite_cache_index(restored_dir)
        try:
            self.assertEqual(restored.get("chapter"), current)
            studio._validate_complete_ogg_audio_file(restored_dir / "audio" / self.name)
        finally:
            studio.release_sqlite_cache_index(restored)
        self.assertFalse(list(self.root.glob(".cache-archive-*")))

    def test_file_change_during_zip_preserves_previous_archive_and_cache(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend):
                if backend == "sqlite":
                    self.activate()
                self.destination.write_bytes(b"previous ZIP")
                self.audio.write_bytes(self.audio_bytes)
                original_write = zipfile.ZipFile.write

                def change_after_read(archive, filename, *args, **kwargs):
                    result = original_write(archive, filename, *args, **kwargs)
                    if Path(filename) == self.audio:
                        self.audio.write_bytes(self.audio_bytes + b"external modification")
                    return result

                with mock.patch.object(zipfile.ZipFile, "write", change_after_read):
                    with self.assertRaisesRegex(RuntimeError, "изменился"):
                        studio.create_cache_archive(self.cache, self.destination)
                self.assertEqual(self.destination.read_bytes(), b"previous ZIP")
                self.assertEqual(studio.get_active_cache_backend(self.cache), backend)
                self.assertFalse(list(self.root.glob(".*.tmp.zip")))
                self.assertFalse(list(self.root.glob(".cache-archive-*")))

    def test_cancellation_before_snapshot_does_not_copy_or_publish(self):
        self.activate()
        self.destination.write_bytes(b"previous ZIP")
        with mock.patch.object(studio.SQLiteCacheIndex, "_backup") as backup:
            with self.assertRaisesRegex(RuntimeError, "отменена"):
                studio.create_cache_archive(self.cache, self.destination, cancelled=lambda: True)
        backup.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"previous ZIP")

    def test_read_failure_preserves_previous_zip_and_cleans_staging(self):
        self.activate()
        self.destination.write_bytes(b"previous ZIP")
        original_write = zipfile.ZipFile.write

        def fail_audio(archive, filename, *args, **kwargs):
            if Path(filename) == self.audio:
                raise OSError("read failed")
            return original_write(archive, filename, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "write", fail_audio):
            with self.assertRaisesRegex(OSError, "read failed"):
                studio.create_cache_archive(self.cache, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"previous ZIP")
        self.assertEqual(self.audio.read_bytes(), self.audio_bytes)
        self.assertEqual(self.index.get("chapter"), self.record)
        self.assertFalse(list(self.root.glob(".*.tmp.zip")))
        self.assertFalse(list(self.root.glob(".cache-archive-*")))


if __name__ == "__main__":
    unittest.main()
