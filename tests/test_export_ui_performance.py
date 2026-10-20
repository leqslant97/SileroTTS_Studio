"""Точность итоговых длительностей и работа с большими проектами экспорта."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class DurationTree:
    def __init__(self, groups, records):
        self.groups = groups
        self.durations = {key: "00:01" for key in records}
        self.durations.update({key: "00:00" for key in groups})
        self.exists_calls = 0
        self.duration_reads = 0

    def exists(self, item):
        self.exists_calls += 1
        return item in self.durations

    def get_children(self, parent=""):
        return tuple(self.groups.get(parent, ()))

    def set(self, item, column, value=None):
        if value is None:
            self.duration_reads += 1
            return self.durations[item]
        self.durations[item] = value


class ExportUIPerformanceTests(unittest.TestCase):
    def make_app(self, groups, records):
        app = object.__new__(studio.TTSApp)
        app.export_files = records
        app.export_tree = DurationTree(groups, records)
        app.lbl_export_total_time = mock.Mock()
        return app

    def test_group_and_project_round_only_after_adding_fractional_seconds(self):
        app = self.make_app({"group": ["a", "b", "c"]}, {
            "a": {"duration": 1.9}, "b": {"duration": 1.9}, "c": {"duration": 1.9},
        })
        app.update_group_duration("group")
        self.assertEqual(app.export_tree.durations["group"], "00:05")
        app.lbl_export_total_time.config.assert_called_once_with(text="Общее время: 00:05")
        self.assertEqual(app.export_tree.duration_reads, 0)

    def test_legacy_and_invalid_durations_do_not_break_total(self):
        records = {"legacy": {}, "invalid": {"duration": float("nan")},
                   "missing": {"duration": 9999}, "valid": {"duration": 0.5}}
        app = self.make_app({"group": ["legacy", "invalid", "valid"]}, records)
        del app.export_tree.durations["missing"]
        app.export_tree.durations.update(legacy="01:02:03", invalid="not a duration")
        app.update_group_duration("group")
        self.assertEqual(app.export_tree.durations["group"], "01:02:03")
        app.lbl_export_total_time.config.assert_called_once_with(text="Общее время: 01:02:03")

    def test_bulk_group_refresh_visits_project_once(self):
        groups = {f"g{g}": [f"f{g}_{f}" for f in range(20)] for g in range(200)}
        records = {f: {"duration": 1.25} for files in groups.values() for f in files}
        app = self.make_app(groups, records)
        for group in groups:
            app.update_group_duration(group, update_total=False)
        app.update_total_export_duration()
        self.assertEqual(app.export_tree.exists_calls, len(records))
        self.assertEqual(app.export_tree.duration_reads, 0)
        self.assertTrue(all(app.export_tree.durations[g] == "00:25" for g in groups))
        app.lbl_export_total_time.config.assert_called_once_with(text="Общее время: 01:23:20")

    def test_shared_cover_is_persisted_once_per_snapshot_and_rechecked_later(self):
        app = self.make_app({"": ["group"], "group": ["a", "b"]}, {})
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cover = folder / "temporary" / "cover.jpg"
            cover.parent.mkdir()
            cover.write_bytes(b"first cover")
            app.export_groups = {"group": {"name": "Book", "cover": str(cover)}}
            app.export_files = {
                key: {"path": str(folder / (key + ".mp3")), "cover": str(cover), "duration": 1.5}
                for key in ("a", "b")
            }
            app.export_tree.durations.update(group="00:03", a="00:01", b="00:01")
            original = copy.deepcopy(app.export_files)
            with (
                mock.patch.object(studio, "SESSION_TEMP_DIR", cover.parent),
                mock.patch.object(studio, "EXPORT_PROJECT_COVERS_DIR", folder / "saved"),
                mock.patch.object(app, "_persistent_export_cover", wraps=app._persistent_export_cover) as persist,
            ):
                first = app._build_export_project_snapshot()
                self.assertEqual(persist.call_count, 1)
                first_cover = first["items"][0]["settings"]["cover"]
                self.assertTrue(Path(first_cover).is_file())
                self.assertTrue(all(f["cover"] == first_cover for f in first["items"][0]["files"]))
                cover.write_bytes(b"updated cover")
                second = app._build_export_project_snapshot()
                self.assertEqual(persist.call_count, 2)
                self.assertNotEqual(first_cover, second["items"][0]["settings"]["cover"])
                self.assertEqual(app.export_files, original)

    def test_move_multiple_selected_rows_preserves_order_and_reads_siblings_once(self):
        for direction, selection, expected in (
            (-1, ("b", "c"), ("b", "c", "a", "d")),
            (1, ("b", "c"), ("a", "d", "b", "c")),
            (-1, ("a", "b"), ("b", "a", "c", "d")),
            (1, ("c", "d"), ("a", "b", "d", "c")),
        ):
            with self.subTest(direction=direction, selection=selection):
                app = object.__new__(studio.TTSApp)
                items = ["a", "b", "c", "d"]
                tree = mock.Mock()
                tree.selection.return_value = selection
                tree.parent.return_value = "group"
                tree.get_children.side_effect = lambda parent: tuple(items)

                def move(item, parent, index):
                    items.remove(item)
                    items.insert(index, item)

                tree.move.side_effect = move
                app.export_tree = tree
                app._refresh_export_tree_action_states = mock.Mock()
                app._schedule_export_project_save = mock.Mock()
                app.move_export_item(direction)
                self.assertEqual(tuple(items), expected)
                tree.get_children.assert_called_once_with("group")
                app._schedule_export_project_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
