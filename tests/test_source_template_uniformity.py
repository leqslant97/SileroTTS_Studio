"""Шаблоны имён целей синтеза сохраняют явные значения во всех форматах."""

import copy
import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
    )
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SourceTemplateUniformityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.output = self.root / "audio"
        self.files = [self.source / f"{index:03d}.txt" for index in range(1, 5)]
        self.groups = [
            {"id": "first", "name": "Том первый", "file_ids": self.files[:2]},
            {"id": "second", "name": "Том второй", "file_ids": self.files[2:]},
        ]

    def plan(self, target, *, groups=None, plan_template="{book}"):
        return studio.plan_source_synthesis_target_paths(
            self.source, self.output, self.files, targets=[target],
            source_m4b_groups=self.groups if groups is None else groups,
            book_name="Серия", m4b_template=plan_template,
        )

    def test_empty_target_inherits_group_and_explicit_target_overrides_all_formats(self):
        for fmt in studio.EXPORT_OUTPUT_FORMATS:
            with self.subTest(format=fmt):
                target = {"format": fmt, "assembly_mode": "merge"}
                inherited = self.plan(target)
                self.assertEqual(
                    [record["path"].name for record in inherited],
                    [f"Том первый.{fmt}", f"Том второй.{fmt}"],
                )
                overridden = self.plan(dict(
                    target,
                    filename_template="{book} {group_index:10:02d} — {filename} — {name}",
                ))
                self.assertEqual(
                    [record["path"].name for record in overridden],
                    [f"Серия 10 — 001 — Том первый.{fmt}",
                     f"Серия 11 — 003 — Том второй.{fmt}"],
                )
                self.assertEqual(self.groups[0]["name"], "Том первый")
                self.assertEqual(self.groups[1]["name"], "Том второй")
                for index, record in enumerate(overridden, 1):
                    self.assertEqual(record["template_context"]["group_index"], index)
                    self.assertEqual(record["template_context"]["file_index"], 1)

    def test_default_looking_explicit_m4b_template_keeps_priority_after_plan_change(self):
        groups = copy.deepcopy(self.groups)
        for index, group in enumerate(groups, 1):
            group.update(
                name=f"Серия Главы {index * 2 - 1}-{index * 2}",
                name_template=studio.DEFAULT_SOURCE_M4B_TEMPLATE,
            )
        target = studio.normalize_output_target({
            "format": "m4b", "filename_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE,
        })
        before = copy.deepcopy(target)
        records = self.plan(target, groups=groups, plan_template="{book}")
        self.assertEqual(
            [record["path"].name for record in records],
            ["Серия Главы 1-2.m4b", "Серия Главы 3-4.m4b"],
        )
        self.assertTrue(all(not record["source_template_owned"] for record in records))
        self.assertEqual(target, before)

    def test_imported_m4b_target_preserves_template_and_sound_settings(self):
        target = studio.normalize_source_synthesis_targets([{
            "format": "m4b", "bitrate": "96k", "sample_rate": "44100",
            "channels": "stereo", "filename_template": "Импорт {part:7:02d}",
        }])[0]
        records = self.plan(target)
        self.assertEqual([record["path"].name for record in records],
                         ["Импорт 07.m4b", "Импорт 08.m4b"])
        for record in records:
            for field in ("bitrate", "sample_rate", "channels", "filename_template"):
                self.assertEqual(record["target"][field], target[field])

    def test_explicit_m4b_template_survives_actual_duration_reflow(self):
        records = self.plan({
            "format": "m4b", "filename_template": "Выход {book} Том {part}",
        })
        reflowed = studio.reflow_source_m4b_target_records(
            records, {}, {
                "input_dir": str(self.source), "output_dir": str(self.output),
                "source_m4b_reflow_actual_duration": True,
                "source_m4b_reflow_limit_seconds": 180,
                "source_m4b_template": "{book}",
            }, auto_split_long=False,
            chapter_duration_overrides={str(path): 120 for path in self.files},
        )
        self.assertTrue(reflowed["changed"])
        self.assertEqual(
            [record["path"].name for record in reflowed["records"]],
            [f"Выход Серия Том {number}.m4b" for number in range(1, 5)],
        )
        self.assertTrue(all(not record["source_template_owned"]
                            for record in reflowed["records"]))

    def test_merge_file_index_is_first_file_and_group_index_varies(self):
        for fmt in studio.EXPORT_OUTPUT_FORMATS:
            with self.subTest(format=fmt):
                records = self.plan({
                    "format": fmt, "assembly_mode": "merge",
                    "filename_template": "{group_index}-{file_index} {name}",
                })
                self.assertEqual([record["path"].name for record in records],
                                 [f"1-1 Том первый.{fmt}", f"2-1 Том второй.{fmt}"])

    def test_start_snapshot_keeps_explicit_m4b_template_and_planned_paths(self):
        for template in ("Выход {part:7:02d}", studio.DEFAULT_SOURCE_M4B_TEMPLATE):
            with self.subTest(template=template):
                app = object.__new__(studio.TTSApp)
                app.config = dict(studio.DEFAULT_CONFIG)
                app.config.update(
                    input_dir=str(self.source), output_dir=str(self.output),
                    source_book_name="Серия", source_book_name_input_dir=str(self.source),
                    synthesis_m4b_template="{book}",
                    synthesis_targets=[{"format": "m4b", "filename_template": template}],
                )
                app.batch_processor = None
                app.direct_processor = None
                app._source_path_by_id = {str(path): path for path in self.files}
                app._source_plan_groups = {group["id"]: group for group in self.groups}
                app._source_tree_file_ids = lambda: tuple(str(path) for path in self.files)
                app.settings_vars = {"skip_existing": mock.Mock()}
                app.settings_vars["skip_existing"].get.return_value = True
                for name in ("save_settings", "_check_source_session_changes", "_show_error"):
                    setattr(app, name, mock.Mock())
                app._warn_if_cache_busy_for_synthesis = mock.Mock(return_value=False)
                app._validate_api_steps_ui = mock.Mock(return_value=True)
                app._validate_book_output_profile = mock.Mock(return_value=True)
                for name in ("_source_regular_rebuild_paths", "_source_m4b_rebuild_paths",
                             "_source_unrelated_target_paths"):
                    setattr(app, name, mock.Mock(return_value=()))
                # Останавливаем запуск после подготовки реального снимка:
                # тест не запускает синтез и не обращается к API или кэшу.
                app._confirm_source_cache_policy = mock.Mock(return_value=False)
                app.start_processing()
                app._show_error.assert_not_called()
                app._confirm_source_cache_policy.assert_called_once()
                snapshot = app._confirm_source_cache_policy.call_args.args[0]
                self.assertEqual(snapshot["source_targets"][0]["filename_template"], template)
                expected = self.plan({"format": "m4b", "filename_template": template})
                self.assertEqual(
                    [record["path"] for record in snapshot["source_target_records"]],
                    [record["path"] for record in expected],
                )
                self.assertTrue(all(record["target"]["filename_template"] == template
                                    for record in snapshot["source_target_records"]))

    def test_older_completed_records_ignore_preview_context_but_rebuild_changed_tags(self):
        for fmt, assembly in (("mp3", "files"), ("opus", "merge"), ("m4b", "merge")):
            with self.subTest(format=fmt, assembly=assembly):
                records = list(self.plan({"format": fmt, "assembly_mode": assembly}))
                previous = copy.deepcopy(records)
                for record in previous:
                    record.pop("template_context")
                app = object.__new__(studio.TTSApp)
                app._source_plan_dirty = True
                app._source_runtime_m4b_records = None
                app._source_completed_regular_targets = {
                    str(record["path"]): record for record in previous if record["kind"] != "m4b"
                }
                app._source_completed_m4b_targets = {
                    str(record["path"]): record for record in previous if record["kind"] == "m4b"
                }
                for record in records:
                    record["template_context"].update(index=99, parts=100, range="Новый пример")
                rebuild = (app._source_m4b_rebuild_paths if fmt == "m4b"
                           else app._source_regular_rebuild_paths)
                self.assertEqual(rebuild(records), ())
                self.assertEqual(app._source_unrelated_target_paths(records), ())
                records[0]["metadata_overrides"] = {"artist": "Другой исполнитель"}
                self.assertEqual(rebuild(records), (str(records[0]["path"]),))


class SourceTemplateDialogTests(unittest.TestCase):
    def test_m4b_template_is_editable_and_import_apply_and_format_switch_preserve_it(self):
        script = textwrap.dedent('''
            from contextlib import ExitStack
            import importlib.util
            import logging
            import os
            import shutil
            import sys
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                cleanup.callback(os.chdir, Path.cwd())
                cleanup.callback(logging.shutdown)
                module_path = Path(directory) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], module_path)
                spec = importlib.util.spec_from_file_location("template_dialog_test", module_path)
                studio = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = studio
                spec.loader.exec_module(studio)
                try:
                    root = studio.tk.Tk()
                except studio.tk.TclError as exc:
                    print("TK_UNAVAILABLE", exc)
                    sys.exit(77)
                root.withdraw()
                try:
                    app = studio.TTSApp(root)
                    root.deiconify()
                    root.update()
                    failures = []
                    app._show_error = lambda *args, **kwargs: failures.append(args)
                    app.synthesis_targets = list(studio.normalize_source_synthesis_targets([
                        {"format": "mp3", "assembly_mode": "merge", "filename_template": "MP3 {part}"},
                        {"format": "m4b", "bitrate": "96k", "filename_template": "M4B {part}"},
                    ]))

                    def descendants(widget):
                        result = []
                        for child in widget.winfo_children():
                            result.append(child)
                            result.extend(descendants(child))
                        return result

                    def button(dialog, text):
                        return next(w for w in descendants(dialog)
                                    if w.winfo_class() == "TButton" and w.cget("text") == text)

                    def open_dialog():
                        app.open_source_output_targets_dialog()
                        root.update()
                        return next(w for w in root.winfo_children()
                                    if w.winfo_class() == "Toplevel" and "Набор выходов —" in w.title())

                    def entry(dialog, value):
                        return next(w for w in descendants(dialog)
                                    if w.winfo_class() == "TEntry" and w.get() == value)

                    def assert_editable(widget):
                        assert not widget.instate(["disabled"]), widget
                        helper = next(w for w in widget.master.winfo_children()
                                      if w.winfo_class() == "TButton" and w.cget("text") == "Помощник…")
                        assert not helper.instate(["disabled"]), helper

                    dialog = open_dialog()
                    mp3 = entry(dialog, "MP3 {part}")
                    m4b = entry(dialog, "M4B {part}")
                    assert_editable(mp3)
                    assert_editable(m4b)
                    m4b.delete(0, studio.tk.END)
                    m4b.insert(0, "Книга {part:7:02d}")
                    formats = [w for w in descendants(dialog) if w.winfo_class() == "TCombobox"
                               and tuple(w.cget("values")) == studio.EXPORT_OUTPUT_FORMATS]
                    for fmt in studio.EXPORT_OUTPUT_FORMATS:
                        formats[1].set(fmt)
                        root.update()
                        assert_editable(m4b)
                    formats[1].set("m4b")
                    root.update()
                    assert m4b.get() == "Книга {part:7:02d}", m4b.get()
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[1]["filename_template"] == "Книга {part:7:02d}"
                    assert app.synthesis_targets[1]["bitrate"] == "96k"

                    imported = list(studio.normalize_source_synthesis_targets([
                        {"format": "m4b", "bitrate": "96k", "filename_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE},
                    ]))
                    app._select_output_targets_profile = lambda **kwargs: imported
                    dialog = open_dialog()
                    button(dialog, "Импорт JSON…").invoke()
                    root.update()
                    assert_editable(entry(dialog, studio.DEFAULT_SOURCE_M4B_TEMPLATE))
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets == imported, app.synthesis_targets
                    dialog = open_dialog()
                    inherited = entry(dialog, studio.DEFAULT_SOURCE_M4B_TEMPLATE)
                    inherited.delete(0, studio.tk.END)
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[0]["filename_template"] == ""
                    assert not failures, failures
                finally:
                    root.destroy()
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script,
             str(Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 77:
            self.skipTest(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
