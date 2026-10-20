"""Предпросмотр читает черновики соседних полей без их применения."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class TemplatePreviewDraftTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app.config = {
            "input_dir": str(self.base / "texts"),
            "output_dir": str(self.base / "audio"),
            "export_dir": str(self.base / "export"),
            "source_book_name": "Старая книга",
            "source_book_name_input_dir": str(self.base / "texts"),
            "synthesis_m4b_template": "{book}",
            "synthesis_m4b_album_template": "{book}",
        }
        self.app.get_status_color = lambda _kind: "black"
        self.app._center_popup = lambda window, width, height, **_kwargs: (
            window.geometry(f"{width}x{height}"), window.deiconify()
        )
        self.app._make_collapsible_hint = lambda parent, *_args, **_kwargs: (
            studio.ttk.Frame(parent), None, None
        )
        self.app._register_palette_widget = lambda widget, *_args, **_kwargs: widget
        self.app.save_settings = mock.Mock(side_effect=AssertionError("preview saved settings"))
        self.app._persist_settings_snapshot = mock.Mock(side_effect=AssertionError("preview applied settings"))
        self.captured = []
        self.app._open_template_helper = lambda *args, **kwargs: self.captured.append((args, kwargs))
        self.app._source_path_by_id = {
            f"f{index}": str(self.base / "texts" / f"{index:03d}.txt")
            for index in range(1, 5)
        }
        self.app._source_tree_file_ids = lambda: tuple(self.app._source_path_by_id)
        self.app._source_plan_groups = {}
        self.app._source_plan_template = "{book}"
        self.app._source_plan_album_template = "{book}"
        self.app.settings_vars = {"skip_existing": studio.tk.BooleanVar(self.root, True)}
        self.app.include_subdirs_var = studio.tk.BooleanVar(self.root, False)
        self.app.batch_prepared_text_var = studio.tk.BooleanVar(self.root, False)
        self.app.synthesis_m4b_reflow_actual_var = studio.tk.BooleanVar(self.root, False)
        self.app.synthesis_m4b_reflow_minutes_var = studio.tk.StringVar(self.root, "60")
        self.app.export_fmt_var = studio.tk.StringVar(self.root, "m4b")
        self.app.export_m4b_template_var = studio.tk.StringVar(self.root, "{group}-{part:02d}")
        self.app.export_m4b_hours_var = studio.tk.StringVar(self.root, "0")
        self.app.export_m4b_chapters_var = studio.tk.StringVar(self.root, "0")
        for attr, value in {
            "export_bitrate_var": "auto", "export_sample_rate_var": "auto",
            "export_channels_var": "auto", "export_apply_fx_var": "0",
            "exp_speed_var": "1", "exp_pitch_var": "1", "exp_echo_var": "0",
            "exp_delay_var": "300", "exp_decay_var": "0.3",
            "export_m4b_bitrate_var": "64k",
        }.items():
            setattr(self.app, attr, studio.tk.StringVar(self.root, value))
        tree = studio.ttk.Treeview(self.root)
        self.app.export_tree = tree
        self.app.export_groups = {}
        self.app.export_files = {}
        for number in (1, 2):
            group = f"g{number}"
            tree.insert("", "end", iid=group)
            self.app.export_groups[group] = {"name": f"Том {number}", "album": "Серия", "merge": True}
            for chapter in range(1, 5):
                file_id = f"{group}_{chapter}"
                tree.insert(group, "end", iid=file_id)
                self.app.export_files[file_id] = {
                    "path": str(self.base / f"{file_id}.mp3"),
                    "title": f"Глава {chapter}", "duration": 10.0,
                }

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def export_controls(self):
        self.app.open_export_settings_dialog()
        dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, studio.tk.Toplevel))
        section = next(widget for widget in self.descendants(dialog) if isinstance(widget, studio.ttk.LabelFrame) and str(widget.cget("text")) == "M4B (AAC-LC, главы)")
        entries = {int(widget.grid_info()["row"]): widget for widget in section.winfo_children() if isinstance(widget, studio.ttk.Entry)}
        helper = next(widget for widget in section.winfo_children() if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…")
        return dialog, entries, helper

    def edit(self, entry, value):
        entry.delete(0, studio.tk.END)
        entry.insert(0, value)

    def rows(self):
        args, kwargs = self.captured[-1]
        return studio.template_helper_full_preview_rows(args[1], args[0].get(), kwargs["preview_data"])

    def test_export_unsaved_chapter_limit_matches_result_after_apply(self):
        before = copy.deepcopy(self.app.config)
        _dialog, entries, helper = self.export_controls()
        self.edit(entries[2], "2")
        helper.invoke()
        arguments = copy.deepcopy(self.captured[-1][1]["preview_data"]["export_plan"])
        arguments["m4b_max_chapters"] = 2
        arguments["m4b_template"] = entries[0].get()
        expected = studio.plan_export_target_paths(**arguments)
        self.assertEqual([row["path"] for row in self.rows()], [str(record["path"]) for record in expected])
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.app.export_m4b_chapters_var.get(), "0")
        self.assertEqual(self.app.config, before)
        self.app.save_settings.assert_not_called()

    def test_export_unsaved_duration_limit_and_reopen_use_latest_draft(self):
        dialog, entries, helper = self.export_controls()
        self.edit(entries[1], str(15 / 3600))
        helper.invoke()
        self.assertEqual(len(self.rows()), 8)
        self.edit(entries[1], "0")
        self.edit(entries[2], "3")
        helper.invoke()
        self.assertEqual(len(self.rows()), 4)
        self.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
        _dialog, _entries, helper = self.export_controls()
        helper.invoke()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.app.export_m4b_hours_var.get(), "0")

    def test_source_unsaved_book_name_is_used_for_name_and_album(self):
        before = copy.deepcopy(self.app.config)
        self.app.open_source_synthesis_settings_dialog()
        staged = self.app._source_settings_stage_vars
        staged["book_name"].set("  Новая   книга  ")
        staged["m4b_template"].set("{book}-{part}")
        staged["m4b_album_template"].set("{book}-{part}")
        helpers = [widget for widget in self.descendants(self.app._source_settings_dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…"]
        helpers[0].invoke()
        self.assertEqual(self.rows()[0]["result"], "Новая книга-1.m4b")
        helpers[1].invoke()
        self.assertEqual(self.rows()[0]["result"], "Новая книга-1")
        self.assertEqual(self.app.config, before)
        self.app._persist_settings_snapshot.assert_not_called()

    def test_source_empty_book_draft_uses_folder_fallback_not_old_name(self):
        self.app.open_source_synthesis_settings_dialog()
        self.app._source_settings_stage_vars["book_name"].set("")
        helper = next(widget for widget in self.descendants(self.app._source_settings_dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…")
        helper.invoke()
        self.assertEqual(self.rows()[0]["result"], "texts.m4b")
        self.assertEqual(self.app.config["source_book_name"], "Старая книга")

    def test_invalid_export_limit_does_not_show_a_successful_old_plan(self):
        _dialog, entries, helper = self.export_controls()
        for field, value in ((1, "nan"), (1, "-1"), (2, "не число"), (2, "-2")):
            with self.subTest(field=field, value=value):
                self.edit(entries[1], "0")
                self.edit(entries[2], "0")
                self.edit(entries[field], value)
                helper.invoke()
                snapshot = self.captured[-1][1]["preview_data"]
                self.assertIn("full_preview_error", snapshot)
                with self.assertRaises(studio.TemplateError):
                    self.rows()
                with self.assertRaises(studio.TemplateError):
                    studio.render_template_helper_preview("m4b_part", entries[0].get(), preview_data=snapshot)

    def test_unchanged_source_draft_retains_accepted_runtime_boundaries(self):
        accepted = studio.plan_source_synthesis_target_paths(
            source_root=self.app.config["input_dir"], output_root=self.app.config["output_dir"],
            files=[{"id": file_id, "path": path} for file_id, path in self.app._source_path_by_id.items()],
            targets=[{"format": "m4b", "assembly_mode": "merge"}],
            source_m4b_groups=[{"id": "one", "file_ids": ("f1", "f2")}, {"id": "two", "file_ids": ("f3", "f4")}],
            book_name="Старая книга", m4b_template="{book}",
        )
        self.app._reuse_source_runtime_m4b_records = mock.Mock(return_value=accepted)
        self.app.open_source_synthesis_settings_dialog()
        helper = next(widget for widget in self.descendants(self.app._source_settings_dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…")
        helper.invoke()
        self.assertEqual(len(self.rows()), 2)
        self.app._reuse_source_runtime_m4b_records.assert_called_once()
        self.app._source_settings_stage_vars["book_name"].set("Другая книга")
        helper.invoke()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["result"], "Другая книга.m4b")
        self.app._reuse_source_runtime_m4b_records.assert_called_once()

    def test_export_preview_equals_reopened_helper_after_actual_apply(self):
        dialog, entries, helper = self.export_controls()
        self.edit(entries[2], "2")
        self.edit(entries[0], "{group}-{volume:10:03d}-{parts}")
        helper.invoke()
        before_apply = self.rows()
        self.app.save_settings = mock.Mock(return_value=True)
        self.app._sync_export_mode_controls = mock.Mock()
        self.app._refresh_audio_profile_summaries = mock.Mock()
        apply_button = next(widget for widget in self.descendants(dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Применить")
        apply_button.invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.app.export_m4b_chapters_var.get(), "2")
        _dialog, _entries, helper = self.export_controls()
        helper.invoke()
        self.assertEqual(self.rows(), before_apply)

    def test_source_preview_equals_reopened_helper_after_actual_apply(self):
        self.app.open_source_synthesis_settings_dialog()
        dialog = self.app._source_settings_dialog
        staged = self.app._source_settings_stage_vars
        staged["book_name"].set("Название из черновика")
        staged["m4b_template"].set("{book}-{part:02d}")
        staged["m4b_album_template"].set("{book}-{part:02d}")
        helpers = [widget for widget in self.descendants(dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…"]
        helpers[0].invoke()
        name_before_apply = self.rows()
        helpers[1].invoke()
        album_before_apply = self.rows()
        self.app._persist_settings_snapshot = mock.Mock()
        self.app._invalidate_source_runtime_m4b_plan = mock.Mock()
        apply_button = next(widget for widget in self.descendants(dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Применить")
        apply_button.invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.app.config["source_book_name"], "Название из черновика")
        self.app.open_source_synthesis_settings_dialog()
        helpers = [widget for widget in self.descendants(self.app._source_settings_dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…"]
        helpers[0].invoke()
        self.assertEqual(self.rows(), name_before_apply)
        helpers[1].invoke()
        self.assertEqual(self.rows(), album_before_apply)

    def test_unfinished_source_name_can_be_corrected_in_helper_without_callback_error(self):
        self.app.open_source_synthesis_settings_dialog()
        staged = self.app._source_settings_stage_vars
        staged["m4b_template"].set("{boo")
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(str(args[1]))
        helpers = [widget for widget in self.descendants(self.app._source_settings_dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Помощник…"]
        helpers[0].invoke()
        self.assertFalse(errors)
        snapshot = self.captured[-1][1]["preview_data"]
        with self.assertRaises((studio.TemplateError, ValueError)):
            studio.render_template_helper_preview("m4b_part", staged["m4b_template"].get(), preview_data=snapshot)
        repaired = studio.template_helper_full_preview_rows("m4b_part", "{book}-{part}", snapshot)
        self.assertEqual(repaired[0]["result"], "Старая книга-1.m4b")
        helpers[1].invoke()
        self.assertFalse(errors)
        snapshot = self.captured[-1][1]["preview_data"]
        self.assertIn("соседний шаблон", snapshot["full_preview_error"])
        with self.assertRaises(studio.TemplateError):
            studio.render_template_helper_preview("m4b_album", "{book}", preview_data=snapshot)


if __name__ == "__main__":
    unittest.main()
