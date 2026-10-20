"""Полный просмотр использует реальный план и не применяет черновик."""

import ast
import copy
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class Value:
    def __init__(self, value):
        self.value = value
        self.writes = []

    def get(self):
        return self.value

    def set(self, value):
        self.writes.append(value)
        self.value = value


class TemplateFullPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.groups = {
            f"g{number}": {"name": f"Том {number}", "album": "Книга", "artist": "Автор", "merge": True, "pause": 2000}
            for number in range(1, 3)
        }
        self.children = {group: [f"{group}_{number}" for number in range(1, 7)] for group in self.groups}
        self.files = {
            file_id: {"path": str(self.base / (file_id + ".mp3")), "title": f"Глава {number}", "duration": 10.25}
            for group, members in self.children.items()
            for number, file_id in enumerate(members, 1)
        }

    def export_arguments(self, targets=None):
        return {
            "items": tuple(self.groups), "group_children": self.children,
            "export_groups": self.groups, "export_files": self.files,
            "targets": targets or [{"format": "mp3", "assembly_mode": "files"}],
            "base_dir": str(self.base / "output"),
        }

    def test_all_export_paths_match_actual_multi_target_plan_and_keep_drafts(self):
        arguments = self.export_arguments([
            {"format": "mp3", "assembly_mode": "files", "filename_template": "MP3-{filename}"},
            {"format": "opus", "assembly_mode": "files", "filename_template": "старое"},
        ])
        snapshot = {"export_plan": arguments, "edited_target_index": 1}
        before = copy.deepcopy(snapshot)
        template = "Том {group_index}-Глава {file_index:1:02d}"
        with mock.patch.object(studio.requests.Session, "request", side_effect=AssertionError("unexpected API")):
            rows = studio.template_helper_full_preview_rows("output_file", template, snapshot)
        actual = copy.deepcopy(arguments)
        actual["targets"][1]["filename_template"] = template
        expected = studio.plan_export_target_paths(**actual)
        self.assertEqual([row["path"] for row in rows], [str(record["path"]) for record in expected if record["target_index"] == 1])
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[-1]["result"], "Том 2-Глава 06.opus")
        self.assertEqual(snapshot, before)
        self.assertFalse((self.base / "output").exists())
        self.assertEqual(len(studio.render_template_helper_preview("output_file", template, preview_data=snapshot)), 3)
        self.assertEqual(len(studio.render_template_helper_preview("output_file", template, preview_data=snapshot, limit=None)), 12)

    def test_all_source_paths_match_plan_with_virtual_groups_and_multiple_formats(self):
        files = [{"id": key, "path": str(self.base / "texts" / (key + ".txt"))} for key in self.files]
        arguments = {
            "source_root": str(self.base / "texts"), "output_root": str(self.base / "audio"),
            "files": files, "book_name": "Книга",
            "source_m4b_groups": [{"id": group, "name": values["name"], "file_ids": self.children[group]} for group, values in self.groups.items()],
            "targets": [{"format": "mp3", "assembly_mode": "files"}, {"format": "m4b", "assembly_mode": "merge"}],
        }
        snapshot = {"source_plan": arguments, "edited_target_index": 0}
        before = copy.deepcopy(snapshot)
        template = "{name}-{file_index:03d}"
        rows = studio.template_helper_full_preview_rows("output_file", template, snapshot)
        actual = copy.deepcopy(arguments)
        actual["targets"][0]["filename_template"] = template
        expected = studio.plan_source_synthesis_target_paths(**actual)
        self.assertEqual([row["path"] for row in rows], [str(record["path"]) for record in expected if record["target_index"] == 0])
        self.assertEqual(len(rows), len(files))
        self.assertEqual(snapshot, before)
        self.assertFalse((self.base / "audio").exists())

    def test_all_chapters_keep_full_series_counts_for_each_m4b_target(self):
        arguments = self.export_arguments([
            {"format": "m4b", "max_chapters": 3, "bitrate": "64k"},
            {"format": "m4b", "max_chapters": 3, "bitrate": "96k"},
        ])
        records = studio.plan_export_target_paths(**arguments)
        groups = studio._template_helper_export_chapter_groups(records, self.groups, self.files, self.children)
        self.assertTrue(all(group["chapter_count"] == 12 for group in groups))
        selected = studio._template_helper_visible_records(groups, {"edited_target_index": 1})
        rows = studio.template_helper_full_preview_rows("m4b_chapter", "Том {part}. {global_index}/{chapter_count}: {name}", {"chapter_groups": selected})
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["result"], "Том 1. 1/12: Глава 1")
        self.assertEqual(rows[-1]["result"], "Том 4. 12/12: Глава 6")
        self.assertEqual(rows[-1]["source"], "Глава 6")

    def test_invalid_template_and_collisions_do_not_create_outputs_or_change_snapshot(self):
        snapshot = {"export_plan": self.export_arguments()}
        before = copy.deepcopy(snapshot)
        for template in ("{unknown}", "одинаковое имя", "{name.__class__}"):
            with self.subTest(template=template), self.assertRaises((ValueError, studio.TemplateError)):
                studio.template_helper_full_preview_rows("output_file", template, snapshot)
            self.assertEqual(snapshot, before)
            self.assertFalse((self.base / "output").exists())

    def test_long_group_name_keeps_complete_volume_count_in_row_details(self):
        self.groups["g1"]["name"] = "Очень длинное название серии с подзаголовком. Том первый"
        arguments = self.export_arguments([{"format": "m4b", "max_chapters": 3}])
        rows = studio.template_helper_full_preview_rows("m4b_part", "{group}-{part}", {"export_plan": arguments})
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0]["group"].endswith("том 1/4"))
        self.assertIn("Группа / том: " + rows[0]["group"], rows[0]["detail"])
        self.assertIn("Группа / том: " + rows[-1]["group"], rows[-1]["detail"])

    def test_focus_filters_after_global_numbering_without_truncating(self):
        snapshot = {"export_plan": self.export_arguments(), "preview_item_ids": tuple(self.children["g2"])}
        rows = studio.template_helper_full_preview_rows("output_file", "{group_index}-{file_index}", snapshot)
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["result"] for row in rows], [f"2-{number}.mp3" for number in range(1, 7)])

    def test_album_preview_respects_local_override_clear_global_tag_and_disabled_tags(self):
        contexts = [{"book": "Книга", "group": f"Часть {index}", "part": index, "parts": 5, "metadata_overrides": {}} for index in range(1, 6)]
        contexts[1]["metadata_overrides"] = {"album": "Локальный альбом"}
        contexts[2]["metadata_overrides"] = {"album": ""}
        snapshot = {"album_contexts": contexts, "metadata_config": {"apply_output_tags": True, "tag_album": "Общий альбом", "synthesis_m4b_album_template": "{book}"}}
        before = copy.deepcopy(snapshot)
        rows = studio.template_helper_full_preview_rows("m4b_album", "{book}-{part:02d}", snapshot)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["source"], "Общий альбом")
        self.assertEqual(rows[0]["result"], "Книга-01")
        self.assertEqual(rows[1]["result"], "Локальный альбом")
        self.assertEqual(rows[2]["result"], "(тег не записывается)")
        self.assertEqual(snapshot, before)
        default = studio.template_helper_full_preview_rows("m4b_album", "{book}", snapshot)
        self.assertEqual(default[0]["result"], "Общий альбом")
        snapshot["metadata_config"]["apply_output_tags"] = False
        disabled = studio.template_helper_full_preview_rows("m4b_album", "{book}-{part}", snapshot)
        self.assertTrue(all(row["result"] == "(тег не записывается)" for row in disabled))

    def test_no_synthetic_full_result_without_real_sources_or_extracted_chapters(self):
        for context in ("output_file", "m4b_part", "m4b_chapter", "source_group", "m4b_album", "book_import", "group_sequence"):
            with self.subTest(context=context), self.assertRaises(studio.TemplateError):
                studio.template_helper_full_preview_rows(context, "{name}", {})

    def nested_function(self, name, namespace):
        tree = ast.parse(Path(studio.__file__).read_text())
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSApp")
        dialog = next(node for node in app.body if isinstance(node, ast.FunctionDef) and node.name == "open_output_targets_dialog")
        function = next(node for node in dialog.body if isinstance(node, ast.FunctionDef) and node.name == name)
        code = compile(ast.Module(body=[function], type_ignores=[]), "<target-draft-preview>", "exec")
        scope = dict(vars(studio), **namespace)
        exec(code, scope)
        return scope[name]

    def test_collecting_preview_uses_unsaved_directory_and_omits_only_edited_template(self):
        row = {
            "format": Value("mp3"), "enabled": Value(True), "directory": Value("новая папка"),
            "assembly": Value(studio.OUTPUT_TARGET_ASSEMBLY_LABELS["files"]),
            "profile": Value("Сохранённые параметры"), "filename_template": Value("{unfinished"),
            "chapter_title_template": Value(""), "include_track_metadata": Value(True),
            "timecodes_enabled": Value(False), "timecodes_preset": Value("Оглавление"),
            "timecodes_directory": Value(""),
            "timecodes_filename_template": Value(studio.TIMECODE_DEFAULT_FILENAME_TEMPLATE),
            "timecodes_line_template": Value("{start:hms} {title}"),
            "old": {"format": "mp3", "bitrate": "192k", "filename_template": "старое", "output_dir": "старая папка"},
        }
        before = {key: value.get() for key, value in row.items() if isinstance(value, Value)}
        collect = self.nested_function("collect_targets", {"controls": [row], "source_context": False})
        targets = collect(read_only=True, preview_row=row, preview_field="filename_template")
        self.assertEqual(targets[0]["output_dir"], "новая папка")
        self.assertEqual(targets[0]["filename_template"], "")
        self.assertEqual(targets[0]["bitrate"], "192k")
        self.assertEqual(before, {key: value.get() for key, value in row.items() if isinstance(value, Value)})
        self.assertTrue(all(not value.writes for value in row.values() if isinstance(value, Value)))


class TemplateFullPreviewWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app._center_popup = lambda window, width, height, **kwargs: (window.geometry(f"{width}x{height}"), window.deiconify())
        self.app.get_status_color = lambda status: "black"
        self.app._is_dark_appearance = False
        self.pending = queue.Queue()
        self.app._post_to_ui = lambda callback, *args: self.pending.put((callback, args))

    def pump(self, condition, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while not self.pending.empty():
                callback, args = self.pending.get_nowait()
                callback(*args)
            self.root.update()
            if condition():
                return
            time.sleep(0.005)
        self.fail("preview did not finish")

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_all_rows_arrive_in_batches_and_close_does_not_apply(self):
        rows = tuple({"source": f"Источник {index}", "result": f"Результат {index}", "group": "Группа", "path": "", "detail": ""} for index in range(1200))
        untouched = {"template": "старое", "settings": {"enabled": True}}
        before = copy.deepcopy(untouched)
        with mock.patch.object(studio, "template_helper_full_preview_rows", return_value=rows):
            window = self.app._open_template_full_preview("output_file", "новое", {}, owner=self.root)
            table = next(widget for widget in self.descendants(window) if isinstance(widget, studio.ttk.Treeview))
            ticks = []
            self.root.after(1, lambda: ticks.append(True))
            self.pump(lambda: len(table.get_children()) == 1200)
            self.assertTrue(ticks)
            self.assertEqual(table.item("1199", "values")[1], "Результат 1199")
            self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
            self.assertFalse(window.winfo_exists())
            self.assertEqual(untouched, before)

    def test_closing_during_calculation_discards_late_result(self):
        release = threading.Event()
        entered = threading.Event()

        def delayed(*args):
            entered.set()
            release.wait(2)
            return ()

        with mock.patch.object(studio, "template_helper_full_preview_rows", side_effect=delayed):
            window = self.app._open_template_full_preview("output_file", "новое", {}, owner=self.root)
            self.assertTrue(entered.wait(1))
            self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
            release.set()
            self.pump(lambda: not self.pending.empty() or not window.winfo_exists())
            end = time.monotonic() + 1
            while self.pending.empty() and time.monotonic() < end:
                time.sleep(0.005)
            self.assertFalse(self.pending.empty())
            callback, args = self.pending.get_nowait()
            callback(*args)
            self.assertFalse(window.winfo_exists())

    def test_search_finds_late_results_and_registry_removes_closed_window(self):
        rows = tuple({"source": f"Источник {index}", "result": f"Результат {index}", "group": "Группа", "path": "", "detail": ""} for index in range(800))
        with mock.patch.object(studio, "template_helper_full_preview_rows", return_value=rows):
            window = self.app._open_template_full_preview("output_file", "новое", {}, owner=self.root)
            widgets = tuple(self.descendants(window))
            table = next(widget for widget in widgets if isinstance(widget, studio.ttk.Treeview))
            search = next(widget for widget in widgets if isinstance(widget, studio.ttk.Entry))
            self.pump(lambda: len(table.get_children()) == 800)
            self.assertIn(window, self.app._appearance_popup_windows)
            search.insert(0, "Источник 799")
            self.pump(lambda: table.get_children() == ("799",))
            self.assertEqual(table.item("799", "values")[1], "Результат 799")
            self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
            self.assertNotIn(window, self.app._appearance_popup_windows)


if __name__ == "__main__":
    unittest.main()
