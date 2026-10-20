"""Подготовка последовательности M4B не растёт с каждым дочерним заданием."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class M4BSequencePreparationTests(unittest.TestCase):
    def test_split_group_uses_only_first_file_metadata_once(self):
        files = {
            f"file-{index}": {"path": f"chapter-{index}.mp3", "title": str(index), "album": "Книга", "artist": "Автор"}
            for index in range(2000)
        }
        records = [
            {
                "kind": "m4b", "target_index": "0", "item_id": "group",
                "part": studio.M4BPart(number, 40, number, number, 1, [{"duration": 1}]),
            }
            for number in range(1, 41)
        ]
        with mock.patch.object(studio, "normalize_export_file_record", wraps=studio.normalize_export_file_record) as normalize:
            volumes, offsets, totals = studio._export_m4b_sequence_metadata(
                records, {"group": tuple(files)}, {"group": {"name": "Группа"}}, files,
            )
        normalize.assert_called_once_with(files["file-0"])
        self.assertEqual(volumes[(0, "group", 40)], (40, 40))
        self.assertEqual(offsets[(0, "group", 40)], 39)
        self.assertEqual(set(totals.values()), {40})

    def test_parallel_children_reuse_one_complete_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups = {"group": {"name": "Книга", "album": "Книга", "merge": True}}
            files = {}
            for index in range(12):
                path = root / f"{index}.mp3"
                path.write_bytes(b"fixture")
                files[str(index)] = {"path": str(path), "duration": 1, "title": str(index)}
            app = object.__new__(studio.TTSApp)
            app.config = dict(studio.DEFAULT_CONFIG)
            target = studio.OutputTarget(format="m4b", max_chapters=1).to_dict()
            with mock.patch.object(studio, "_export_m4b_sequence_metadata", wraps=studio._export_m4b_sequence_metadata) as prepare, \
                 mock.patch.object(studio, "_export_m4b_ffmpeg") as encode:
                app._run_output_target_set(
                    ("group",), {"group": tuple(files)}, groups, files, [target],
                    root / "output", max_workers=4,
                )
            prepare.assert_called_once()
            self.assertEqual(encode.call_count, 12)
            self.assertEqual(
                sorted((call.kwargs["disk_number"], call.kwargs["disk_total"]) for call in encode.call_args_list),
                [(index, 12) for index in range(1, 13)],
            )

    def test_duplicate_legacy_records_and_private_groups_keep_numbering(self):
        groups = {"one": {"name": "Same"}, "two": {"name": "Same"}}
        part = studio.M4BPart(1, 1, 1, 2, 2, [{"duration": 1}] * 2)
        records = [
            {"kind": "m4b", "target_index": "0", "item_id": group, "part": part}
            for group in ("one", "one", "two")
        ]
        volumes, offsets, totals = studio._export_m4b_sequence_metadata(records, {}, groups, {})
        self.assertEqual(volumes, {(0, "one", 1): (1, 1), (0, "two", 1): (1, 1)})
        self.assertEqual(set(offsets.values()), {0})
        self.assertEqual(set(totals.values()), {2})

    def test_parallel_parts_do_not_rematerialize_complete_group_or_change_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for index in range(100):
                path = root / f"{index}.mp3"
                path.write_bytes(b"fixture")
                files[str(index)] = {
                    "path": str(path), "duration": 1, "title": str(index),
                    "album": "Книга", "artist": "Автор" if index == 0 else "Другой",
                    "cover": "first.png" if index == 0 else "later.png",
                }
            groups = {"group": {"name": "Книга", "merge": True}}
            children = {"group": tuple(files)}
            app = object.__new__(studio.TTSApp)
            app.config = dict(studio.DEFAULT_CONFIG)
            target = studio.OutputTarget(format="m4b", max_chapters=1).to_dict()
            with mock.patch.object(studio, "normalize_export_file_record", wraps=studio.normalize_export_file_record) as normalize, \
                 mock.patch.object(studio, "plan_m4b_parts", wraps=studio.plan_m4b_parts) as split, \
                 mock.patch.object(studio, "_export_m4b_ffmpeg") as encode:
                app._run_output_target_set(
                    ("group",), children, groups, files, [target], root / "output", max_workers=4,
                )
            self.assertLess(normalize.call_count, 600, "Каждая часть заново обходит группу")
            split.assert_called_once()
            self.assertEqual(encode.call_count, 100)
            for call in encode.call_args_list:
                self.assertEqual(call.kwargs["tags"]["artist"], "Автор")
                self.assertEqual(call.kwargs["cover"], "first.png")
                self.assertEqual(len(call.args[0]), 1)

    def test_incomplete_private_plan_reverts_to_normal_part_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for index in range(2):
                source = root / f"{index}.mp3"
                source.write_bytes(b"fixture")
                files[str(index)] = {"path": str(source), "duration": 1, "title": str(index)}
            target = studio.OutputTarget(format="m4b", max_chapters=1, output_dir=str(root / "output")).to_dict()
            invalid_part = studio.M4BPart(1, 2, 1, 100, 1, [{"duration": 1}])
            record = {"target_index": 0, "item_id": "group", "kind": "m4b", "part": invalid_part,
                      "path": root / "output" / "first.m4b"}
            app = object.__new__(studio.TTSApp)
            app.config = dict(studio.DEFAULT_CONFIG)
            with mock.patch.object(studio, "plan_m4b_parts", wraps=studio.plan_m4b_parts) as split, \
                 mock.patch.object(studio, "_export_m4b_ffmpeg") as encode:
                app._run_output_target_set(
                    ("group",), {"group": tuple(files)}, {"group": {"name": "Книга", "merge": True}},
                    files, [target], root / "output", planned_records=[record],
                    _reuse_planned_records=True, _m4b_job_filter={(0, "group", 1)},
                )
            split.assert_called_once()
            encode.assert_called_once()
            self.assertEqual(tuple(encode.call_args.args[0]), (str(root / "0.mp3"),))


if __name__ == "__main__":
    unittest.main()
