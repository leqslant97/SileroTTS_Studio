"""Физическое разбиение дерева сохраняет книги, порядок и реальные границы M4B."""

import array
import copy
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import wave

from test_m4b_whole_file_export import studio  # Импорт только временной копии source.


def regroup(items, children, groups, plan):
    """Независимая модель применения результата; исходные словари не меняет."""
    new_items = []
    new_children = copy.deepcopy(children)
    new_groups = copy.deepcopy(groups)
    for item_id in items:
        parts = plan["groups"].get(item_id)
        if not parts:
            new_items.append(item_id)
            continue
        for index, entry in enumerate(parts):
            new_id = item_id if index == 0 else f"{item_id}:part{index + 1}"
            settings = copy.deepcopy(groups[item_id])
            settings["name"] = f"{groups[item_id]['name']} — Том {entry['part'].number}"
            new_groups[new_id] = settings
            new_children[new_id] = entry["file_ids"]
            new_items.append(new_id)
    return tuple(new_items), new_children, new_groups


class ExportM4BTreeSplitPlanningTests(unittest.TestCase):
    def setUp(self):
        self.items = ("root", "bookB", "bookA")
        self.children = {"bookA": ("a1", "a2", "a3"),
                         "bookB": ("b1", "b2", "b3")}
        self.groups = {
            "bookA": {"name": "Книга", "artist": "Автор A", "album": "Серия A",
                      "pause": 100, "pause_start": 100, "pause_end": 200,
                      "cover": "untouched-cover.png", "custom": {"keep": [1, 2]}},
            "bookB": {"name": "Книга", "artist": "Автор B", "album": "Серия B",
                      "pause": 50, "pause_start": 0, "pause_end": 0},
        }
        self.files = {file_id: {
            "path": f"/virtual/{file_id}/Глава.wav", "title": f"Title {file_id}",
            "source_title": f"Original {file_id}", "artist": f"Автор {file_id}",
            "duration": duration, "custom": {"unchanged": [file_id]},
        } for file_id, duration in (("a1", 0.2), ("a2", 0.3), ("a3", 0.4),
                                   ("b1", 0.1), ("b2", 0.15), ("b3", 0.2))}
        self.files["root"] = {"path": "/virtual/root.wav", "title": "Root",
                              "duration": None}
        self.target = {"format": "m4b", "max_duration_seconds": 0.65,
                       "max_chapters": 2, "filename_template": "{filename}"}

    def plan(self, **options):
        return studio.plan_export_m4b_tree_split(
            self.items, self.children, self.groups, self.files,
            self.target, **options,
        )

    def test_order_books_metadata_and_oversized_chapter_are_preserved(self):
        before = copy.deepcopy((self.items, self.children, self.groups, self.files, self.target))
        result = self.plan()
        self.assertEqual(result["errors"], ())
        self.assertEqual(tuple(result["groups"]), ("bookB", "bookA"))
        self.assertEqual([entry["file_ids"] for entry in result["groups"]["bookB"]],
                         [("b1", "b2"), ("b3",)])
        self.assertEqual([entry["file_ids"] for entry in result["groups"]["bookA"]],
                         [("a1",), ("a2",), ("a3",)])
        self.assertEqual(len(result["oversized"]), 1)
        oversized = result["oversized"][0]
        self.assertEqual((oversized["item_id"], oversized["file_ids"]),
                         ("bookA", ("a3",)))
        self.assertAlmostEqual(oversized["duration_seconds"], 0.7)
        self.assertEqual(oversized["limit_seconds"], 0.65)
        self.assertEqual((self.items, self.children, self.groups, self.files, self.target), before)
        result["groups"]["bookA"][0]["part"].chapters[0]["title"] = "Changed copy"
        self.assertEqual(self.files["a1"]["title"], "Title a1")

    def test_regrouped_plan_has_one_output_per_group_and_same_file_order(self):
        result = self.plan()
        items, children, groups = regroup(self.items, self.children, self.groups, result)
        self.assertEqual(items, ("root", "bookB", "bookB:part2",
                                 "bookA", "bookA:part2", "bookA:part3"))
        records = studio.plan_export_target_paths(
            items, children, groups, self.files, [self.target], "/virtual/output",
        )
        grouped = [record for record in records if record["kind"] == "m4b"]
        self.assertEqual(len(grouped), 5)
        self.assertTrue(all(record["part"].number == record["part"].total == 1
                            for record in grouped))
        for original_id, original_children in self.children.items():
            actual = tuple(file_id for item_id in items
                           if item_id == original_id or item_id.startswith(original_id + ":")
                           for file_id in children[item_id])
            self.assertEqual(actual, original_children)
        self.assertNotIn("root", result["groups"])

    def test_no_explicit_limits_and_short_book_is_valid_noop(self):
        self.target = {"format": "m4b"}
        self.assertEqual(self.plan(), {"groups": {}, "oversized": (), "errors": ()})
        self.assertEqual(self.plan(auto_split_long=False),
                         {"groups": {}, "oversized": (), "errors": ()})
        self.assertEqual(studio.plan_export_m4b_tree_split(
            (), {}, {}, {}, self.target),
            {"groups": {}, "oversized": (), "errors": ()})

    def test_unknown_durations_abort_entire_plan_and_collect_all_errors(self):
        for invalid in (None, 0, -1, float("nan"), float("inf"), False, True, "bad"):
            with self.subTest(duration=invalid):
                self.files["a1"]["duration"] = invalid
                self.files["a3"]["duration"] = invalid
                result = self.plan()
                self.assertEqual(result["groups"], {})
                self.assertEqual(result["oversized"], ())
                self.assertEqual([error["file_id"] for error in result["errors"]],
                                 ["a1", "a3"])
        del self.files["a1"]
        self.assertIn("a1", [error["file_id"] for error in self.plan()["errors"]])

    def test_inherited_limits_and_explicit_zero_use_common_planner_contract(self):
        self.target = {"format": "m4b"}
        inherited = self.plan(m4b_max_chapters=1)
        self.assertEqual(len(inherited["groups"]["bookA"]), 3)
        self.target["max_chapters"] = 0
        self.assertEqual(self.plan(m4b_max_chapters=1)["groups"], {})

    def test_automatic_long_book_guard_stays_stable_after_physical_regroup(self):
        self.target = {"format": "m4b"}
        for file_id in self.children["bookA"]:
            self.files[file_id]["duration"] = 43100
        result = self.plan()
        self.assertEqual(len(result["groups"]["bookA"]), 3)
        self.assertNotIn("bookB", result["groups"])
        self.assertEqual(result["oversized"], ())
        items, children, groups = regroup(self.items, self.children, self.groups, result)
        after = studio.plan_export_m4b_tree_split(
            items, children, groups, self.files, self.target,
        )
        self.assertEqual(after, {"groups": {}, "oversized": (), "errors": ()})

    def test_wrong_target_kind_is_rejected(self):
        self.target = {"format": "mp3"}
        with self.assertRaisesRegex(ValueError, "цель M4B"):
            self.plan()

    def test_planning_never_probes_decodes_or_creates_files(self):
        with mock.patch.object(studio, "_probe_audio_duration",
                               side_effect=AssertionError("Probe")), \
             mock.patch.object(studio, "_probe_audio_stream_profile",
                               side_effect=AssertionError("Probe profile")), \
             mock.patch.object(studio.subprocess, "Popen",
                               side_effect=AssertionError("Subprocess")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("Mkdir")):
            self.assertEqual(tuple(self.plan()["groups"]), ("bookB", "bookA"))


class ExportM4BPhysicalTreeAudioTests(unittest.TestCase):
    def test_actual_clipped_books_keep_audio_order_and_one_volume_per_new_group(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            files = {}
            groups = {}
            children = {}
            for label, frequency, start, end in (("A", 650, 100, 150),
                                                   ("B", 870, 20, 30)):
                source = base / f"book{label}.wav"
                samples = array.array("h", (
                    round(12000 * math.sin(2 * math.pi * frequency * index / 48000))
                    for index in range(19200)
                ))
                with wave.open(str(source), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(48000)
                    output.writeframes(samples.tobytes())
                group_id = f"book{label}"
                groups[group_id] = {
                    "name": f"Книга {label}", "merge": True, "pause": 50,
                    "pause_start": start, "pause_end": end,
                    "album": f"Альбом {label}", "artist": f"Автор {label}",
                    "custom": {"unchanged": [label]},
                }
                children[group_id] = tuple(f"{label}{index}" for index in (1, 2))
                for index, file_id in enumerate(children[group_id]):
                    files[file_id] = {
                        "path": str(source), "title": f"Глава {label}{index + 1}",
                        "duration": 0.2, "clip_start": index * 0.2,
                        "clip_end": (index + 1) * 0.2,
                        "source_name": f"Источник {label}",
                    }
            items = tuple(groups)
            original = copy.deepcopy((items, children, groups, files))
            target = {"format": "m4b", "max_chapters": 1}
            plan = studio.plan_export_m4b_tree_split(
                items, children, groups, files, target, str(base / "output"),
            )
            self.assertEqual(plan["errors"], ())
            self.assertEqual(plan["oversized"], ())
            new_items, new_children, new_groups = regroup(items, children, groups, plan)
            records = studio.plan_export_target_paths(
                new_items, new_children, new_groups, files, [target], str(base / "output"),
            )
            self.assertEqual(len(records), 4)
            self.assertTrue(all(record["part"].number == record["part"].total == 1
                                for record in records))
            app = studio.TTSApp.__new__(studio.TTSApp)
            app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
            app._run_output_target_set(
                new_items, new_children, new_groups, files, [target],
                str(base / "output"), max_workers=2,
            )
            for record in records:
                source_id = record["item_id"].split(":")[0]
                expected_file = new_children[record["item_id"]][0]
                expected_duration = 0.45 if source_id == "bookA" else 0.25
                data = json.loads(subprocess.run([
                    studio.get_ffprobe_path(), "-v", "error", "-show_streams",
                    "-show_chapters", "-show_format", "-of", "json", str(record["path"]),
                ], check=True, capture_output=True, text=True).stdout)
                audio = next(stream for stream in data["streams"]
                             if stream["codec_type"] == "audio")
                self.assertAlmostEqual(float(audio["duration"]), expected_duration, delta=0.001)
                self.assertEqual(len(data["chapters"]), 1)
                chapter = data["chapters"][0]
                self.assertEqual(chapter["tags"]["title"], files[expected_file]["title"])
                self.assertEqual(float(chapter["start_time"]), 0)
                self.assertAlmostEqual(float(chapter["end_time"]), expected_duration, delta=0.00051)
                self.assertEqual(data["format"]["tags"]["album"], groups[source_id]["album"])
                decoded = array.array("h", subprocess.run([
                    studio.get_ffmpeg_path(), "-v", "error", "-i", str(record["path"]),
                    "-map", "0:a:0", "-ar", "48000", "-ac", "1", "-f", "s16le", "-",
                ], check=True, capture_output=True).stdout)
                self.assertGreater(max(decoded), 1000)
                start_samples = groups[source_id]["pause_start"] * 48
                speech = decoded[start_samples + 500:start_samples + 6500]
                rising = [index for index in range(1, len(speech))
                          if speech[index - 1] <= 0 < speech[index]]
                frequency = (len(rising) - 1) * 48000 / (rising[-1] - rising[0])
                self.assertAlmostEqual(frequency, 650 if source_id == "bookA" else 870, delta=2)
            self.assertEqual((items, children, groups, files), original)


if __name__ == "__main__":
    unittest.main()
