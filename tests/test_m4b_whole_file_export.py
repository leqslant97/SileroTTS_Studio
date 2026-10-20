"""Одиночный M4B принимает целый файл и сохраняет границы явного фрагмента."""

import array
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    _import_directory = tempfile.TemporaryDirectory(prefix="whole-m4b-studio-import-")
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


class M4BWholeFileExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.source = self.base / "source.wav"
        samples = array.array("h", (
            round(10000 * math.sin(2 * math.pi * 650 * index / 48000))
            for index in range(24000)
        ))
        with wave.open(str(self.source), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(samples.tobytes())
        self.app = studio.TTSApp.__new__(studio.TTSApp)
        self.app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.output_dir = self.base / "output"
        self.setting = {
            "path": str(self.source), "title": "Одиночная глава",
            "artist": "Тестовый автор", "duration": 0.5,
        }

    def run_root(self, setting, **options):
        self.app._run_output_target_set(
            ("root",), {}, {}, {"root": setting},
            [{"format": "m4b"}], str(self.output_dir), **options,
        )

    def assert_real_m4b(self, expected_duration):
        files = list(self.output_dir.rglob("*.m4b"))
        self.assertEqual(len(files), 1)
        result = subprocess.run([
            studio.get_ffprobe_path(), "-v", "error", "-show_streams",
            "-show_chapters", "-of", "json", str(files[0]),
        ], check=True, capture_output=True, text=True)
        metadata = json.loads(result.stdout)
        audio = next(stream for stream in metadata["streams"]
                     if stream["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertEqual(int(audio["sample_rate"]), 48000)
        self.assertEqual(int(audio["channels"]), 1)
        self.assertAlmostEqual(float(audio["duration"]), expected_duration, delta=0.001)
        self.assertEqual(len(metadata["chapters"]), 1)
        chapter = metadata["chapters"][0]
        self.assertEqual(float(chapter["start_time"]), 0)
        self.assertAlmostEqual(float(chapter["end_time"]), expected_duration,
                               delta=0.00051)
        self.assertEqual(chapter["tags"]["title"], self.setting["title"])
        decoded = subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(files[0]),
            "-map", "0:a:0", "-ar", "48000", "-ac", "1", "-f", "s16le", "-",
        ], check=True, capture_output=True)
        self.assertEqual(decoded.stderr, b"")
        sample_count = len(decoded.stdout) // 2
        expected_samples = round(expected_duration * 48000)
        self.assertGreaterEqual(sample_count, expected_samples)
        self.assertLess(sample_count - expected_samples, 1024)
        self.assertGreater(max(array.array("h", decoded.stdout)), 1000)
        self.assertEqual(list(self.output_dir.rglob(".*.tmp*")), [])

    def test_whole_root_file_without_clip_fields_exports_real_m4b(self):
        original = copy.deepcopy(self.setting)
        with mock.patch.object(studio, "_export_m4b_ffmpeg",
                               wraps=studio._export_m4b_ffmpeg) as exporter:
            self.run_root(self.setting)
        self.assertEqual(exporter.call_args.kwargs["source_ranges"], (None,))
        self.assertEqual(self.setting, original)
        self.assert_real_m4b(0.5)

    def test_clipped_root_file_keeps_range_and_actual_chapter_duration(self):
        self.setting.update(clip_start=0.1, clip_end=0.4, duration=0.3)
        original = copy.deepcopy(self.setting)
        with mock.patch.object(studio, "_export_m4b_ffmpeg",
                               wraps=studio._export_m4b_ffmpeg) as exporter:
            self.run_root(self.setting)
        source_range, = exporter.call_args.kwargs["source_ranges"]
        self.assertEqual((source_range["clip_start"], source_range["clip_end"]),
                         (0.1, 0.4))
        self.assertEqual(self.setting, original)
        self.assert_real_m4b(0.3)

    def test_defensive_nonmerge_group_branch_accepts_whole_file(self):
        # M4B в пользовательском плане всегда merge; проверяем оставшуюся
        # защитную ветку напрямую, не меняя эту политику приложения.
        with mock.patch.object(studio, "output_target_merges_group", return_value=False), \
             mock.patch.object(studio, "_export_m4b_ffmpeg",
                               wraps=studio._export_m4b_ffmpeg) as exporter:
            self.app._run_output_target_set(
                ("group",), {"group": ("root",)},
                {"group": {"name": "Группа", "merge": False}},
                {"root": self.setting}, [{"format": "m4b"}],
                str(self.output_dir),
            )
        self.assertEqual(exporter.call_args.kwargs["source_ranges"], (None,))
        self.assert_real_m4b(0.5)

    def test_explicit_none_clip_fields_mean_whole_file(self):
        self.setting.update(clip_start=None, clip_end=None)
        with mock.patch.object(studio, "_export_m4b_ffmpeg") as exporter:
            self.run_root(self.setting)
        self.assertEqual(exporter.call_args.kwargs["source_ranges"], (None,))

    def test_partial_clip_still_fails_validation_instead_of_exporting_whole_file(self):
        for bounds in ({"clip_start": 0.1}, {"clip_end": 0.4},
                       {"clip_start": None, "clip_end": 0.4},
                       {"clip_start": 0.1, "clip_end": None}):
            with self.subTest(bounds=bounds):
                setting = dict(self.setting, **bounds)
                with self.assertRaisesRegex(ValueError, "границы аудиоисточника"):
                    self.run_root(setting)
                self.assertEqual(list(self.output_dir.rglob("*.m4b")), [])

    def test_missing_source_path_remains_a_validation_error(self):
        setting = dict(self.setting, path=None)
        with mock.patch.object(studio, "_export_m4b_ffmpeg") as exporter:
            with self.assertRaises(ValueError):
                self.run_root(setting)
        exporter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
