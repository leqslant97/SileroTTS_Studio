"""Целостность чистого ZIP кэша, восстановления и атомарной публикации."""

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
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


class CleanCacheArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "Кэш аудио"
        self.cache.mkdir()
        self.destination = self.root / "Резервная копия.zip"

    def write_file(self, name, payload):
        path = self.cache / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_archive_sync_uses_a_writable_descriptor_before_publication(self):
        self.write_file("chapter.ogg", b"audio")
        sync = studio.os.fsync
        checked = []

        def require_write_access(descriptor):
            # На POSIX синхронизация rb проходит; запись пустого блока проверяет
            # доступ так же строго, как _commit в Windows, не меняя содержимое.
            studio.os.write(descriptor, b"")
            checked.append(descriptor)
            return sync(descriptor)

        with mock.patch.object(studio.os, "fsync", side_effect=require_write_access), \
                mock.patch.object(studio, "_fsync_cache_directory"):
            studio.create_zip_archive_atomic(self.cache, self.destination)
        self.assertTrue(checked)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read(self.cache.name + "/chapter.ogg"), b"audio")

    def test_clean_archive_restores_all_payload_and_has_no_unix_extra_fields(self):
        entries = {
            "hash": {
                "file_name": "глава.ogg",
                "original_text": "Точная глава.",
                "normalized_text": "Точная глава.",
                "speaker": "arthas",
            }
        }
        payloads = {
            "sentence_cache.json": json.dumps(entries, ensure_ascii=False).encode(),
            "sentence_cache.json.bak": b'{"old": {}}',
            "audio/глава.ogg": bytes(range(256)) * 400,
            ".settings.json": b'{"keep": true}',
            "exports/._notes.json.backup": b"metadata sidecar is excluded",
            "exports/.DS_Store_notes.json": b"legitimate similar name",
            "exports/__MACOSX_notes.json": b"legitimate similar name",
            "ordinary/__MACOSX": b"ordinary file with reserved directory name",
            "ordinary/.AppleDouble": b"ordinary file with reserved directory name",
        }
        for name, payload in payloads.items():
            self.write_file(name, payload)
        for name in [
            ".DS_Store", "audio/.DS_Store", "._sentence_cache.json",
            "audio/._глава.ogg", "__MACOSX/cache/._index.json",
            "exports/__MACOSX/metadata.json", "._folder/payload.json",
            ".AppleDouble/index.json", "audio/.AppleDouble/chapter.ogg",
        ]:
            self.write_file(name, b"macOS metadata")
        (self.cache / "empty").mkdir()
        # AppleDouble files are metadata regardless of their suffix.
        del payloads["exports/._notes.json.backup"]
        expected = {
            f"{self.cache.name}/{name}": hashlib.sha256(data).hexdigest()
            for name, data in payloads.items()
        }

        result = studio.create_zip_archive_atomic(self.cache, self.destination)
        self.assertEqual(result, self.destination.resolve())
        restore = self.root / "restored"
        with zipfile.ZipFile(result) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(
                {info.filename: hashlib.sha256(archive.read(info)).hexdigest()
                 for info in archive.infolist() if not info.is_dir()},
                expected,
            )
            self.assertIn(self.cache.name + "/", archive.namelist())
            self.assertIn(self.cache.name + "/empty/", archive.namelist())
            self.assertTrue(all(info.extra == b"" for info in archive.infolist()))
            archive.extractall(restore)
        self.assertEqual(
            json.loads((restore / self.cache.name / "sentence_cache.json").read_text()),
            entries,
        )
        for name, payload in payloads.items():
            self.assertEqual((restore / self.cache.name / name).read_bytes(), payload)
        self.assertFalse(list(self.root.glob(".*.tmp.zip")))

    def test_write_or_publish_failure_preserves_previous_archive_and_cleans_temp(self):
        self.write_file("audio/chapter.ogg", b"chapter payload")
        previous = b"previous backup stays byte-for-byte"
        for phase in ["write", "publish"]:
            with self.subTest(phase=phase):
                self.destination.write_bytes(previous)
                if phase == "write":
                    original_write = zipfile.ZipFile.write

                    def fail_audio(archive, filename, *args, **kwargs):
                        if Path(filename).name == "chapter.ogg":
                            raise OSError("fixture: read failed")
                        return original_write(archive, filename, *args, **kwargs)

                    patch = mock.patch.object(zipfile.ZipFile, "write", fail_audio)
                else:
                    patch = mock.patch.object(studio.os, "replace", side_effect=OSError("fixture: publication failed"))
                with patch, self.assertRaises(OSError):
                    studio.create_zip_archive_atomic(self.cache, self.destination)
                self.assertEqual(self.destination.read_bytes(), previous)
                self.assertFalse(list(self.root.glob(".*.tmp.zip")))

    def test_archive_inside_cache_excludes_its_previous_and_temporary_outputs(self):
        self.write_file("audio/chapter.ogg", bytes(range(256)) * 400)
        destination = self.cache / "backups" / "cache.zip"
        destination.parent.mkdir()
        destination.write_bytes(b"previous backup")
        result = studio.create_zip_archive_atomic(self.cache, destination)
        with zipfile.ZipFile(result) as archive:
            self.assertIsNone(archive.testzip())
            files = [info.filename for info in archive.infolist() if not info.is_dir()]
            self.assertEqual(files, [self.cache.name + "/audio/chapter.ogg"])
            self.assertEqual(archive.read(files[0]), bytes(range(256)) * 400)
        self.assertFalse(list(destination.parent.glob(".*.tmp.zip")))

    def test_directory_symlinks_are_not_traversed_and_file_links_keep_previous_semantics(self):
        external = self.root / "external"
        external.mkdir()
        secret = external / "outside.json"
        secret.write_bytes(b"outside data")
        self.write_file("audio/chapter.ogg", b"current audio")
        try:
            (self.cache / "linked_directory").symlink_to(external, target_is_directory=True)
            (self.cache / "linked_file.json").symlink_to(secret)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        result = studio.create_zip_archive_atomic(self.cache, self.destination)
        with zipfile.ZipFile(result) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(self.cache.name + "/linked_directory/", archive.namelist())
            self.assertNotIn(self.cache.name + "/linked_directory/outside.json", archive.namelist())
            self.assertEqual(archive.read(self.cache.name + "/linked_file.json"), secret.read_bytes())
            self.assertEqual(archive.read(self.cache.name + "/audio/chapter.ogg"), b"current audio")


if __name__ == "__main__":
    unittest.main()
