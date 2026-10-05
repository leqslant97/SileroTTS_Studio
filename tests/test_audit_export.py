import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class ExportCancellationTests(unittest.TestCase):
    def test_cancelled_remux_does_not_start_ffmpeg(self):
        process = mock.Mock(returncode=0, stderr=io.BytesIO())
        process.poll.return_value = 0
        with mock.patch.object(studio.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(InterruptedError):
                studio._run_ffmpeg_checked(["ffmpeg"], cancelled=lambda: True)
        popen.assert_not_called()

    def test_remux_detects_stop_at_process_completion(self):
        stopped = threading.Event()
        process = mock.Mock(returncode=0, stderr=io.BytesIO())

        def completed():
            stopped.set()
            return 0

        process.poll.side_effect = completed
        with mock.patch.object(studio.subprocess, "Popen", return_value=process):
            with self.assertRaises(InterruptedError):
                studio._run_ffmpeg_checked(
                    ["ffmpeg"], cancelled=stopped.is_set
                )

    def test_stopped_encoders_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sources = [folder / "chapter1.mp3", folder / "chapter2.mp3"]
            for source in sources:
                source.write_bytes(b"source")
            output = folder / "book.mp3"
            profile = {
                "sample_rate": 48000,
                "channels": 1,
                "channel_layout": "mono",
                "bitrate": "128k",
            }

            for copy_stream in (False, True):
                with self.subTest(copy_stream=copy_stream):
                    stopped = threading.Event()
                    output.write_bytes(b"previous complete output")
                    process = mock.Mock(returncode=0)

                    def completed():
                        stopped.set()
                        return 0

                    def start(command, **_kwargs):
                        Path(command[-1]).write_bytes(b"new output")
                        return process

                    process.poll.side_effect = completed
                    with (
                        mock.patch.object(studio.subprocess, "Popen", side_effect=start),
                        mock.patch.object(studio, "SESSION_TEMP_DIR", folder),
                        mock.patch.object(studio, "_select_merge_audio_profile", return_value=profile),
                    ):
                        with self.assertRaises(InterruptedError):
                            if copy_stream:
                                studio._run_ffmpeg_stream_copy_concat(
                                    sources, output, cancelled=stopped.is_set
                                )
                            else:
                                studio._export_merged_audio_ffmpeg(
                                    sources,
                                    output,
                                    output_format="mp3",
                                    cancelled=stopped.is_set,
                                    _probed_profiles=[None, None],
                                )
                    self.assertEqual(output.read_bytes(), b"previous complete output")
                    self.assertEqual(set(folder.iterdir()), {*sources, output})

    def test_m4b_stop_before_publication_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.mp3"
            source.write_bytes(b"source")
            encoded = folder / "prepared.m4a"
            encoded.write_bytes(b"prepared AAC")
            output = folder / ("a" * 180 + ".m4b")
            output.write_bytes(b"previous complete M4B")
            stopped = threading.Event()

            def remux(_command, *, out_path, **_kwargs):
                Path(out_path).write_bytes(b"new M4B")
                stopped.set()

            with mock.patch.object(studio, "_run_ffmpeg_checked", side_effect=remux):
                with self.assertRaises(InterruptedError):
                    studio._export_m4b_ffmpeg(
                        [source],
                        output,
                        chapters=[{"title": "Глава", "duration": 1}],
                        preencoded_audio=encoded,
                        cancelled=stopped.is_set,
                    )
            self.assertEqual(output.read_bytes(), b"previous complete M4B")
            self.assertEqual(set(folder.iterdir()), {source, encoded, output})


class GroupTagInheritanceTests(unittest.TestCase):
    @staticmethod
    def make_app(source, *, file_language="", group_language="rus"):
        app = object.__new__(studio.TTSApp)
        app.lbl_export_status = object()
        app._post_status_label = mock.Mock()
        app.export_tree = mock.Mock()
        app.export_tree.get_children.side_effect = (
            lambda item="": ("file",) if item == "group" else ("group",)
        )
        app.export_files = {
            "file": {"path": str(source), "title": "Глава", "language": file_language}
        }
        app.export_groups = {"group": {"name": "Книга", "language": group_language}}
        app.export_targets = []
        for name, value in {
            "export_tags_only_var": True,
            "export_outdir_var": "",
            "export_apply_fx_var": False,
            "export_fmt_var": "mp3",
            "export_bitrate_var": "auto",
            "export_sample_rate_var": "auto",
            "export_channels_var": "auto",
        }.items():
            setattr(app, name, mock.Mock(get=lambda current=value: current))
        app.export_progress = mock.Mock()
        app.config = {}
        for name in (
            "save_settings", "_set_export_running_state",
            "_set_export_progress_indeterminate", "_show_error", "_show_warning",
            "_show_info", "_set_export_progress_value", "_set_export_status",
            "_finish_export_process_ui",
        ):
            setattr(app, name, mock.Mock())
        app._post_to_ui = lambda callback, *args, **kwargs: callback(*args, **kwargs)
        return app

    @staticmethod
    def run_export(app):
        with mock.patch.object(threading.Thread, "start"):
            app.start_export_process()
        app._export_thread._target()

    def test_tags_only_inherits_group_language_when_file_language_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"source")
            app = self.make_app(source)
            app._update_file_tags_inplace = mock.Mock(return_value=True)
            self.run_export(app)
            self.assertEqual(app._update_file_tags_inplace.call_args.args[1]["language"], "rus")
            app._show_error.assert_not_called()

    def test_tags_only_keeps_explicit_file_language(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"source")
            app = self.make_app(source, file_language="eng")
            app._update_file_tags_inplace = mock.Mock(return_value=True)
            self.run_export(app)
            self.assertEqual(app._update_file_tags_inplace.call_args.args[1]["language"], "eng")
            app._show_error.assert_not_called()

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "Для проверки необходимы FFmpeg и FFprobe",
    )
    def test_real_tags_only_writes_inherited_language_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=duration=0.1",
                "-metadata", "language=eng", str(source),
            ], check=True)
            app = self.make_app(source)
            self.run_export(app)
            result = json.loads(subprocess.check_output([
                studio.get_ffprobe_path(), "-v", "error",
                "-show_format", "-of", "json", str(source),
            ]))
            self.assertEqual(result["format"]["tags"]["language"], "rus")
            self.assertEqual(list(Path(directory).iterdir()), [source])
            app._show_error.assert_not_called()


class InplaceMetadataAuditTests(unittest.TestCase):
    @staticmethod
    def make_app():
        app = object.__new__(studio.TTSApp)
        app.lbl_export_status = object()
        app._post_status_label = mock.Mock()
        return app

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "Для проверки необходимы FFmpeg и FFprobe",
    )
    def test_m4b_probe_reads_unicode_independently_of_system_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.wav"
            output = folder / "Книга.m4b"
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=duration=0.1", str(source),
            ], check=True)
            studio._export_m4b_ffmpeg(
                [source], output,
                chapters=[{"title": "Глава Ёж — 海", "duration": .1}],
                tags={"title": "Книга", "album": "Серия приключений"},
            )
            for encoding in ("cp1251", "cp1252"):
                with self.subTest(encoding=encoding), mock.patch.object(
                    subprocess, "_text_encoding", return_value=encoding,
                ):
                    result = studio.probe_m4b_chapters(output)
                    self.assertEqual(result["chapters"][0]["title"], "Глава Ёж — 海")
                    self.assertEqual(result["format"]["tags"]["title"], "Книга")
                    self.assertEqual(result["format"]["tags"]["album"], "Серия приключений")

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "Для проверки необходимы FFmpeg и FFprobe",
    )
    def test_m4b_cover_and_tags_keep_chapters_and_volume_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.wav"
            cover = folder / "cover.png"
            output = folder / ("a" * 180 + ".m4b")
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=duration=0.05", str(source),
            ], check=True)
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:s=32x32",
                "-frames:v", "1", str(cover),
            ], check=True)
            studio._export_m4b_ffmpeg(
                [source, source], output,
                chapters=[{"title": "Начало", "duration": .05},
                          {"title": "Конец", "duration": .05}],
                tags={"title": "Книга", "artist": "Старое имя"},
                disk_number=1, disk_total=3,
            )
            app = self.make_app()
            self.assertTrue(app._update_file_tags_inplace(
                output, {"title": "Новое название", "album": "Серия"},
                cover, "Книга",
            ))
            data = json.loads(subprocess.check_output([
                studio.get_ffprobe_path(), "-v", "error", "-show_format",
                "-show_streams", "-show_chapters", "-of", "json", str(output),
            ]))
            tags = data["format"]["tags"]
            self.assertEqual(tags["title"], "Новое название")
            self.assertEqual(tags["album"], "Серия")
            self.assertEqual(tags["disc"], "1/3")
            self.assertEqual(tags["track"], "1/3")
            self.assertNotIn("artist", tags)
            self.assertEqual([chapter["tags"]["title"] for chapter in data["chapters"]], ["Начало", "Конец"])
            covers = [stream for stream in data["streams"] if stream.get("disposition", {}).get("attached_pic")]
            self.assertEqual(len(covers), 1)
            self.assertEqual(covers[0]["width"], 32)

    def test_failed_xiph_cover_update_removes_temporary_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.opus"
            cover = folder / "cover.png"
            source.write_bytes(b"original audio")
            cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 524288)

            def fail(command, **_kwargs):
                self.assertLess(sum(len(str(arg)) for arg in command), 32768)
                metadata_path = Path(command[command.index("ffmetadata") + 2])
                self.assertGreater(metadata_path.stat().st_size, 524288)
                Path(command[-1]).write_bytes(b"partial output")
                return subprocess.CompletedProcess(command, 1, stderr=b"encoder error")

            app = self.make_app()
            with (
                mock.patch.object(studio, "SESSION_TEMP_DIR", folder),
                mock.patch.object(studio.subprocess, "run", side_effect=fail),
                self.assertLogs(level="ERROR"),
            ):
                self.assertFalse(app._update_file_tags_inplace(
                    source, {"title": "Глава"}, cover, "Глава"
                ))
            self.assertEqual(source.read_bytes(), b"original audio")
            self.assertEqual(set(folder.iterdir()), {source, cover})


class M4BTemplateConsistencyTests(unittest.TestCase):
    def test_source_metadata_filename_uses_physical_text_name(self):
        context = studio.source_m4b_metadata_context(
            {"input_dir": "/books"},
            {"group_name": "Том", "path": "/exports/Сборник.m4b",
             "source_paths": ("/books/001 Начало.txt", "/books/002 Продолжение.txt")},
        )
        self.assertEqual(context["filename"], "001 Начало")

    def test_export_fallback_uses_first_file_of_each_part(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            groups = {"book": {"name": "Книга", "merge": True}}
            children = {"book": ("a", "b")}
            files = {
                "a": {"path": str(folder / "Первый.mp3"), "title": "Метка 1", "duration": 1},
                "b": {"path": str(folder / "Второй.mp3"), "title": "Метка 2", "duration": 1},
            }
            target = studio.OutputTarget(
                format="m4b", filename_template="{filename}", max_chapters=1,
            ).to_dict()
            planned = studio.plan_export_target_paths(
                ("book",), children, groups, files, (target,), folder / "out"
            )
            paths = []
            app = object.__new__(studio.TTSApp)
            app.config = {}
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg",
                side_effect=lambda _sources, output, **_kwargs: paths.append(output),
            ):
                app._run_output_target_set(
                    ("book",), children, groups, files, (target,), folder / "out",
                    _reuse_planned_records=True,
                )
            self.assertEqual(paths, [record["path"] for record in planned])
            self.assertTrue(paths[1].name.startswith("Второй"))


class ExportDialogFormatSwitchTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from ExportDialogFormatSwitchTests.descendants(child)

    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.root = self.root
        app.config = {}
        app.settings_vars = {}
        app.export_fmt_var = studio.tk.StringVar(master=self.root, value="mp3")
        app.save_settings = mock.Mock(return_value=True)
        app._refresh_output_targets_summary = mock.Mock()
        app._refresh_audio_profile_summaries = mock.Mock()
        app._sync_export_mode_controls = mock.Mock()
        app._show_error = mock.Mock()
        app.get_status_color = mock.Mock(return_value="#555555")
        app._register_palette_widget = lambda widget, _kind: widget
        app._close_popup_safely = lambda dialog: dialog.destroy()
        app._center_popup = lambda dialog, *_args, **_kwargs: dialog.deiconify()

        def make_hint(parent, _text, **_kwargs):
            shell = studio.ttk.Frame(parent)
            return shell, studio.ttk.Label(shell), studio.ttk.Button(shell)

        app._make_collapsible_hint = make_hint
        return app

    def dialog(self, title):
        return next(
            child for child in self.root.winfo_children()
            if isinstance(child, studio.tk.Toplevel) and child.title() == title
        )

    def button(self, dialog, text):
        return next(
            widget for widget in self.descendants(dialog)
            if isinstance(widget, studio.ttk.Button) and widget.cget("text") == text
        )

    def test_output_target_restores_non_m4b_assembly(self):
        labels = studio.OUTPUT_TARGET_ASSEMBLY_LABELS
        for assembly_mode in ("inherit", "files", "merge"):
            with self.subTest(assembly_mode=assembly_mode):
                app = self.make_app()
                app.export_targets = [studio.OutputTarget(
                    format="mp3", assembly_mode=assembly_mode,
                    bitrate="192k", filename_template="{name}",
                    apply_effects=True,
                ).to_dict()]
                app.open_output_targets_dialog()
                dialog = self.dialog("Набор выходных форматов")
                try:
                    widgets = list(self.descendants(dialog))
                    format_combo = next(
                        widget for widget in widgets
                        if isinstance(widget, studio.ttk.Combobox)
                        and "m4b" in widget.cget("values")
                    )
                    assembly_combo = next(
                        widget for widget in widgets
                        if isinstance(widget, studio.ttk.Combobox)
                        and labels["inherit"] in widget.cget("values")
                    )
                    filename_hint = next(
                        widget for widget in widgets
                        if isinstance(widget, studio.ttk.Label)
                        and widget.cget("text").startswith("Пусто —")
                        and "{source_name}" in widget.cget("text")
                    )
                    track_check = next(
                        widget for widget in widgets
                        if isinstance(widget, studio.ttk.Checkbutton)
                        and widget.cget("text") == "N/T"
                    )

                    def volume_header_visible():
                        return any(
                            isinstance(widget, studio.ttk.Label)
                            and widget.cget("text") == "Том N/T"
                            and widget.winfo_manager() == "grid"
                            for widget in widgets
                        )

                    self.assertEqual(assembly_combo.get(), labels[assembly_mode])
                    self.assertFalse(volume_header_visible())
                    self.assertEqual(track_check.winfo_manager(), "")
                    if assembly_mode == "inherit":
                        for selected_mode, expected_hint in (
                            ("merge", "Пусто — имя группы."),
                            ("files", "Пусто — имя Title исходного файла."),
                            ("inherit", "Пусто — имя группы при склейке"),
                        ):
                            assembly_combo.set(labels[selected_mode])
                            assembly_combo.event_generate("<<ComboboxSelected>>")
                            self.assertIn(expected_hint, filename_hint.cget("text"))
                    format_combo.set("m4b")
                    self.assertEqual(assembly_combo.get(), labels["merge"])
                    self.assertIn("disabled", assembly_combo.state())
                    self.assertTrue(volume_header_visible())
                    self.assertEqual(track_check.winfo_manager(), "grid")
                    for output_format in ("wav", "ogg", "opus", "m4a", "mp3"):
                        format_combo.set(output_format)
                        self.assertEqual(assembly_combo.get(), labels[assembly_mode])
                        self.assertNotIn("disabled", assembly_combo.state())
                        self.assertFalse(volume_header_visible())
                        self.assertEqual(track_check.winfo_manager(), "")
                        if output_format != "mp3":
                            format_combo.set("m4b")
                            self.assertEqual(track_check.winfo_manager(), "grid")
                    self.button(dialog, "Применить").invoke()
                    app._show_error.assert_not_called()
                    self.assertEqual(app.export_targets[0]["assembly_mode"], assembly_mode)
                    self.assertEqual(app.export_targets[0]["format"], "mp3")
                    self.assertEqual(app.export_targets[0]["bitrate"], "192k")
                    self.assertEqual(app.export_targets[0]["filename_template"], "{name}")
                    self.assertTrue(app.export_targets[0]["apply_effects"])
                    app.save_settings.assert_called_once()
                finally:
                    if dialog.winfo_exists():
                        dialog.destroy()

    def test_hidden_m4b_values_do_not_block_regular_formats(self):
        app = self.make_app()
        values = {
            "export_bitrate_var": "auto",
            "export_sample_rate_var": "auto",
            "export_channels_var": "auto",
            "export_apply_fx_var": False,
            "exp_speed_var": 1.0,
            "exp_pitch_var": 1.0,
            "exp_echo_var": False,
            "exp_delay_var": 300,
            "exp_decay_var": 0.3,
            "export_m4b_template_var": "{invalid_field}",
            "export_m4b_hours_var": "unfinished",
            "export_m4b_chapters_var": "-1",
            "export_m4b_bitrate_var": "bad",
        }
        for name, value in values.items():
            setattr(app, name, studio.tk.StringVar(master=self.root, value=value))

        for output_format in ("mp3", "wav", "ogg", "opus", "m4a"):
            with self.subTest(output_format=output_format):
                app.export_fmt_var.set(output_format)
                app.open_export_settings_dialog()
                dialog = self.dialog("Продвинутые параметры сборки")
                self.button(dialog, "Применить").invoke()
                self.assertFalse(dialog.winfo_exists())
                self.assertEqual(app.export_fmt_var.get(), output_format)
                for name in (
                    "export_m4b_template_var", "export_m4b_hours_var",
                    "export_m4b_chapters_var", "export_m4b_bitrate_var",
                ):
                    self.assertEqual(getattr(app, name).get(), values[name])
        self.assertEqual(app.save_settings.call_count, 5)

        app.export_fmt_var.set("m4b")
        app.open_export_settings_dialog()
        dialog = self.dialog("Продвинутые параметры сборки")
        self.button(dialog, "Применить").invoke()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(app.save_settings.call_count, 5)
        dialog.destroy()

    def test_effects_return_after_temporary_m4b_selection(self):
        app = self.make_app()
        values = {
            "export_bitrate_var": "auto",
            "export_sample_rate_var": "auto",
            "export_channels_var": "auto",
            "export_apply_fx_var": True,
            "exp_speed_var": 1.2,
            "exp_pitch_var": 1.0,
            "exp_echo_var": False,
            "exp_delay_var": 300,
            "exp_decay_var": 0.3,
            "export_m4b_template_var": "",
            "export_m4b_hours_var": 0,
            "export_m4b_chapters_var": 0,
            "export_m4b_bitrate_var": "64k",
        }
        for name, value in values.items():
            setattr(app, name, studio.tk.StringVar(master=self.root, value=value))

        app.open_export_settings_dialog()
        dialog = self.dialog("Продвинутые параметры сборки")
        format_combo = next(
            widget for widget in self.descendants(dialog)
            if isinstance(widget, studio.ttk.Combobox)
            and "m4b" in widget.cget("values")
        )
        format_combo.set("m4b")
        format_combo.set("mp3")
        self.button(dialog, "Применить").invoke()

        self.assertFalse(dialog.winfo_exists())
        self.assertTrue(studio._config_bool(app.export_apply_fx_var.get()))
        self.assertEqual(app.export_fmt_var.get(), "mp3")
        app.save_settings.assert_called_once()


if __name__ == "__main__":
    unittest.main()
