"""Длительность виртуальных глав одинакова в плане, дереве и реальном M4B."""

import array
import copy
import json
import math
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from tests.test_regressions import studio


class ExportClippedDurationTests(unittest.TestCase):
    def test_clip_bounds_take_priority_over_missing_or_stale_duration(self):
        for duration in (None, 0, "bad", 20, True):
            with self.subTest(duration=duration):
                record = {"path": "book.m4b", "clip_start": 1.25,
                          "clip_end": 2.75, "duration": duration}
                self.assertEqual(studio.export_file_duration_seconds(record), 1.5)

    def test_whole_file_unknown_duration_remains_unknown_without_io(self):
        for duration in (None, 0, -1, "bad", math.nan, math.inf, True, [], {}):
            with self.subTest(duration=duration):
                record = {"path": "missing.wav", "duration": duration,
                          "clip_start": None, "clip_end": None}
                self.assertEqual(studio.export_file_duration_seconds(record), 0)
        self.assertEqual(studio.export_file_duration_seconds({"duration": "1.25"}), 1.25)

    def test_invalid_explicit_range_cannot_become_a_whole_file(self):
        for bounds in ({"clip_start": 1}, {"clip_end": 2},
                       {"clip_start": -1, "clip_end": 2},
                       {"clip_start": 2, "clip_end": 1},
                       {"clip_start": 0, "clip_end": math.inf},
                       {"clip_start": math.nan, "clip_end": 1}):
            with self.subTest(bounds=bounds):
                record = {"path": "book.m4b", "duration": 20, **bounds}
                with self.assertRaises(ValueError):
                    studio.export_file_duration_seconds(record)
                with self.assertRaises(ValueError):
                    studio.normalize_export_file_record(record)

    def test_tree_split_accepts_known_ranges_without_cached_duration(self):
        files = {"a": {"path": "book.m4b", "clip_start": 0, "clip_end": 1},
                 "b": {"path": "book.m4b", "clip_start": 1, "clip_end": 2,
                       "duration": None}}
        children = {"g": ("a", "b")}
        groups = {"g": {"name": "Книга", "merge": True}}
        target = {"format": "m4b", "max_duration_seconds": 1.5}
        plan = studio.plan_export_m4b_tree_split(
            ("g",), children, groups, files, target,
        )
        self.assertFalse(plan["errors"])
        self.assertFalse(plan["oversized"])
        self.assertEqual([part["file_ids"] for part in plan["groups"]["g"]],
                         [("a",), ("b",)])

    def test_two_unknown_clips_with_pauses_fit_in_one_real_m4b(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "whole.wav"
            samples = array.array("h", (
                int(10000 * math.sin(2 * math.pi * 440 * index / 48000))
                for index in range(96000)
            ))
            with wave.open(str(source), "wb") as stream:
                stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(samples.tobytes())
            groups = {"g": {"name": "Книга", "merge": True, "album": "Книга",
                            "pause": 100, "pause_start": 100, "pause_end": 100}}
            children = {"g": ("a", "b")}
            files = {"a": {"path": str(source), "title": "Первый", "duration": None,
                           "clip_start": 0.0, "clip_end": 0.5},
                     "b": {"path": str(source), "title": "Второй", "duration": 0,
                           "clip_start": 0.5, "clip_end": 1.0}}
            before = copy.deepcopy((groups, children, files))
            target = {"format": "m4b", "max_duration_seconds": 1.5,
                      "filename_template": "Книга-{part}-{parts}",
                      "chapter_title_template": "{global_index}/{chapter_count}: {name}"}
            app = studio.TTSApp.__new__(studio.TTSApp)
            app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
            app._run_output_target_set(
                ("g",), children, groups, files, [target], str(base / "output"),
                max_workers=2,
            )
            outputs = list((base / "output").glob("*.m4b"))
            self.assertEqual(len(outputs), 1)
            data = json.loads(subprocess.run([
                studio.get_ffprobe_path(), "-v", "error", "-show_streams",
                "-show_chapters", "-of", "json", str(outputs[0]),
            ], check=True, capture_output=True, text=True).stdout)
            audio = next(stream for stream in data["streams"]
                         if stream["codec_type"] == "audio")
            self.assertAlmostEqual(float(audio["duration"]), 1.3, delta=0.001)
            self.assertEqual([chapter["tags"]["title"] for chapter in data["chapters"]],
                             ["1/2: Первый", "2/2: Второй"])
            self.assertAlmostEqual(float(data["chapters"][0]["end_time"]), 0.7, delta=0.001)
            self.assertAlmostEqual(float(data["chapters"][1]["end_time"]), 1.3, delta=0.001)
            self.assertEqual((groups, children, files), before)


if __name__ == "__main__":
    unittest.main()
