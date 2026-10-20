"""Старые снимки с неизвестной длительностью не прерывают восстановление UI."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_regressions import studio


class DurationTree:
    def __init__(self):
        self.items = {}

    def insert(self, parent, index, *, iid, **kwargs):
        self.items[iid] = {"parent": parent, **kwargs}
        return iid

    def get_children(self, parent=""):
        return tuple(key for key, value in self.items.items()
                     if value["parent"] == parent)

    def exists(self, item):
        return item in self.items

    def set(self, item, column):
        return self.items[item]["values"][0] if column == "duration" else ""

    def selection(self):
        return ()


class ExportSessionDurationTests(unittest.TestCase):
    def test_unknown_duration_restores_all_rows_and_current_preview(self):
        for raw_duration in (None, "bad", [1], {}, "0.5"):
            for bounds in ({}, {"clip_start": 0.25, "clip_end": 0.75}):
                with self.subTest(duration=raw_duration, bounds=bounds):
                    self.check_restore(raw_duration, bounds)

    def check_restore(self, raw_duration, bounds):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, other = base / "whole.wav", base / "other.wav"
            source.touch()
            other.touch()
            original = {
                "schema_version": studio.EXPORT_PROJECT_SCHEMA_VERSION,
                "items": [
                    {"kind": "file", "settings": {
                        "path": str(source), "title": "Первая глава",
                        "duration": raw_duration, **bounds,
                    }},
                    {"kind": "file", "settings": {
                        "path": str(other), "title": "Следующая запись", "duration": 1.25,
                    }},
                ],
            }
            project = base / "export_project.json"
            project.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            saved_bytes = project.read_bytes()
            app = studio.TTSApp.__new__(studio.TTSApp)
            app.export_groups, app.export_files = {}, {}
            app.export_tree = DurationTree()
            app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
            app.config["export_dir"] = str(base / "output")
            app.update_group_duration = mock.Mock()
            app.update_total_export_duration = mock.Mock()
            app._refresh_export_tree_action_states = mock.Mock()
            with mock.patch.object(studio, "EXPORT_PROJECT_FILE", project):
                app._restore_export_project_session()
            roots = app.export_tree.get_children()
            self.assertEqual(len(roots), 2)
            self.assertEqual([app.export_tree.items[item]["values"][1] for item in roots],
                             ["Первая глава", "Следующая запись"])
            expected = 0.5 if bounds or raw_duration == "0.5" else 0
            self.assertEqual(app._export_file_duration_seconds(roots[0]), expected)
            self.assertEqual(app._export_file_duration_seconds(roots[1]), 1.25)
            snapshot = app._template_helper_export_preview_data("m4b", target={"format": "m4b"})
            rows = studio.template_helper_full_preview_rows("output_file", "{name}", snapshot)
            self.assertEqual([row["source"] for row in rows], ["Первая глава", "Следующая запись"])
            self.assertEqual([row["result"] for row in rows],
                             ["Первая глава.m4b", "Следующая запись.m4b"])
            self.assertFalse(app._restoring_export_project_session)
            app.update_total_export_duration.assert_called_once_with()
            app._refresh_export_tree_action_states.assert_called_once_with()
            self.assertEqual(project.read_bytes(), saved_bytes)
            self.assertFalse((base / "output").exists())


if __name__ == "__main__":
    unittest.main()
