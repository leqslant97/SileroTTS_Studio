"""Повреждённые кэшированные паузы восстанавливаются до склейки звука."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    _import_directory = tempfile.TemporaryDirectory(prefix="silence-studio-import-")
    _module_path = Path(_import_directory.name) / "SileroTTS_Studio.py"
    _module_path.write_bytes(
        (Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py").read_bytes()
    )
    _previous_cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, _module_path)
        studio = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = studio
        spec.loader.exec_module(studio)
    finally:
        os.chdir(_previous_cwd)
else:
    studio = sys.modules[MODULE_NAME]


class SilenceCacheRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture_directory.cleanup)
        fixture = Path(cls.fixture_directory.name) / "silence.ogg"
        try:
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=mono", "-t", "1",
                "-c:a", "libopus", str(fixture),
            ], check=True, capture_output=True)
        except FileNotFoundError:
            raise unittest.SkipTest("FFmpeg отсутствует")
        cls.valid_silence = fixture.read_bytes()
        cls.truncated_silence = cls.valid_silence[:47]
        studio._validate_complete_ogg_audio_file(fixture)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_dir = Path(self.directory.name)
        self.silence_path = self.cache_dir / "silences" / "silence_1000ms.ogg"
        self.silence_path.parent.mkdir()
        self.processor = self.make_processor()

    def make_processor(self, *, lock=None):
        processor = object.__new__(studio.TTSProcessor)
        processor.cache_dir = self.cache_dir
        processor.cache_lock = lock if lock is not None else threading.RLock()
        return processor

    def assert_real_second_of_silence(self, path):
        self.assertEqual(studio._inspect_ogg_audio_header(path), ("opus", 1))
        studio._validate_complete_ogg_audio_file(path)
        decoded = subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(path),
            "-ar", "48000", "-ac", "1", "-f", "s16le", "-",
        ], check=True, capture_output=True)
        self.assertEqual(decoded.stderr, b"")
        self.assertEqual(len(decoded.stdout), 48000 * 2)
        self.assertEqual(decoded.stdout, b"\x00" * (48000 * 2))
        self.assertEqual(list(path.parent.glob(".*.tmp.ogg")), [])

    def assert_warm_hit_does_not_scan_or_encode(self, processor=None):
        processor = processor or self.processor
        with mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                               side_effect=AssertionError("Повторное чтение Ogg")), \
             mock.patch.object(studio.AudioSegment, "export",
                               side_effect=AssertionError("Повторное кодирование")), \
             mock.patch.object(studio.subprocess, "Popen",
                               side_effect=AssertionError("Новый FFmpeg/ffprobe")):
            self.assertEqual(processor._get_silence_file(1000), self.silence_path)

    def test_truncated_opus_header_is_regenerated_and_published_into_memo(self):
        self.silence_path.write_bytes(self.truncated_silence)
        self.assertEqual(studio._detect_ogg_audio_codec(self.silence_path), "opus")
        result = self.processor._get_silence_file(1000)
        self.assertEqual(result, self.silence_path)
        self.assert_real_second_of_silence(result)
        with studio._OGG_VALIDATION_CACHE_LOCK:
            self.assertIn(studio._ogg_audio_file_fingerprint(result),
                          studio._OGG_VALIDATION_CACHE)
        self.assert_warm_hit_does_not_scan_or_encode()

    def test_payload_corruption_invalidates_previous_memo_and_is_regenerated(self):
        self.silence_path.write_bytes(self.valid_silence)
        studio._validate_cached_ogg_audio_file(self.silence_path)
        corrupt = bytearray(self.valid_silence)
        corrupt[-4] ^= 1
        self.silence_path.write_bytes(corrupt)
        with self.assertRaises(ValueError):
            studio._validate_complete_ogg_audio_file(self.silence_path)
        result = self.processor._get_silence_file(1000)
        self.assert_real_second_of_silence(result)
        self.assert_warm_hit_does_not_scan_or_encode()

    def test_existing_valid_silence_is_checked_once_without_ffmpeg_or_ffprobe(self):
        self.silence_path.write_bytes(self.valid_silence)
        with mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                               wraps=studio._validate_complete_ogg_audio_file) as validate, \
             mock.patch.object(studio.AudioSegment, "export",
                               side_effect=AssertionError("Повторное кодирование")), \
             mock.patch.object(studio.subprocess, "Popen",
                               side_effect=AssertionError("Новый FFmpeg/ffprobe")):
            for _ in range(3):
                self.assertEqual(self.processor._get_silence_file(1000),
                                 self.silence_path)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(self.silence_path.read_bytes(), self.valid_silence)

    def test_incomplete_encoder_output_does_not_replace_old_file_or_leave_temp(self):
        self.silence_path.write_bytes(self.truncated_silence)

        def broken_export(segment, out_path, *args, **kwargs):
            Path(out_path).write_bytes(self.truncated_silence)
            return mock.Mock()

        with mock.patch.object(studio.AudioSegment, "export", autospec=True,
                               side_effect=broken_export), \
             mock.patch.object(studio.os, "replace", wraps=studio.os.replace) as replace:
            with self.assertRaises(ValueError):
                self.processor._get_silence_file(1000)
        replace.assert_not_called()
        self.assertEqual(self.silence_path.read_bytes(), self.truncated_silence)
        self.assertEqual(list(self.silence_path.parent.glob(".*.tmp.ogg")), [])
        with studio._OGG_VALIDATION_CACHE_LOCK:
            self.assertNotIn(studio._ogg_audio_file_fingerprint(self.silence_path),
                             studio._OGG_VALIDATION_CACHE)

    def test_parallel_processors_publish_once_under_shared_cache_lock(self):
        lock = threading.RLock()
        processors = [self.make_processor(lock=lock), self.make_processor(lock=lock)]
        encoded = threading.Barrier(2)
        real_replace = studio.os.replace
        real_remember = studio._remember_validated_ogg_audio_file

        def export(segment, out_path, *args, **kwargs):
            # Оба кодировщика работают вне cache_lock и заканчивают до публикации.
            self.assertFalse(lock._is_owned())
            Path(out_path).write_bytes(self.valid_silence)
            encoded.wait(timeout=5)
            return mock.Mock()

        def replace(source, target):
            self.assertTrue(lock._is_owned())
            return real_replace(source, target)

        def remember(path, **kwargs):
            self.assertTrue(lock._is_owned())
            return real_remember(path, **kwargs)

        with mock.patch.object(studio.AudioSegment, "export", autospec=True,
                               side_effect=export), \
             mock.patch.object(studio.os, "replace", side_effect=replace) as publish, \
             mock.patch.object(studio, "_remember_validated_ogg_audio_file",
                               side_effect=remember) as memo, \
             mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                               wraps=studio._validate_complete_ogg_audio_file) as validate:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(processor._get_silence_file, 1000)
                           for processor in processors]
                self.assertEqual([future.result(timeout=10) for future in futures],
                                 [self.silence_path, self.silence_path])
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(memo.call_count, 1)
        self.assertEqual(validate.call_count, 2)
        self.assert_real_second_of_silence(self.silence_path)
        for processor in processors:
            self.assert_warm_hit_does_not_scan_or_encode(processor)


if __name__ == "__main__":
    unittest.main()
