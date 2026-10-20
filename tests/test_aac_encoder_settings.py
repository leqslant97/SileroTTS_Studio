"""Выбор AAC сохраняется отдельно от речи и изменяет только AAC-результаты."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_regressions import studio


class AacEncoderSettingsTests(unittest.TestCase):
    def test_old_and_invalid_settings_keep_native_encoder(self):
        self.assertEqual(studio.normalize_config({})["aac_encoder"], "aac")
        self.assertEqual(studio.normalize_config({"aac_encoder": "aac_at"})["aac_encoder"], "aac_at")
        for value in (None, "", "unknown", 123, False):
            with self.subTest(value=value):
                self.assertEqual(studio.normalize_config({"aac_encoder": value})["aac_encoder"], "aac")

    def test_config_transfer_includes_encoder_only_with_output_settings(self):
        settings = {"aac_encoder": "aac_at", "output_format": "m4a", "input_dir": "input"}
        output = studio.select_config_values(settings, {"tags"})
        self.assertEqual(output["aac_encoder"], "aac_at")
        self.assertNotIn("input_dir", output)
        self.assertNotIn("aac_encoder", studio.select_config_values(settings, {"folders"}))
        merged = studio.merge_config_values({"aac_encoder": "aac"}, settings, {"tags"})
        self.assertEqual(merged["aac_encoder"], "aac_at")

    def test_display_and_saved_value_follow_each_other_without_probe(self):
        interpreter = studio.tk.Tcl()
        app = object.__new__(studio.TTSApp)
        app.settings_vars = {"aac_encoder": studio.tk.StringVar(interpreter, "aac")}
        app.aac_encoder_choice_var = studio.tk.StringVar(interpreter)
        app.aac_encoder_hint = mock.Mock()
        canonical_trace = app.settings_vars["aac_encoder"].trace_add(
            "write", lambda *_: app._sync_aac_encoder_choice()
        )
        display_trace = app.aac_encoder_choice_var.trace_add(
            "write", lambda *_: app._on_aac_encoder_choice_changed()
        )
        # Освобождаем Tcl callbacks в потоке теста: они замыкают app и переменные.
        self.addCleanup(app.settings_vars["aac_encoder"].trace_remove, "write", canonical_trace)
        self.addCleanup(app.aac_encoder_choice_var.trace_remove, "write", display_trace)
        app._sync_aac_encoder_choice()
        self.assertEqual(app.aac_encoder_choice_var.get(), "FFmpeg AAC")
        with mock.patch.object(studio.platform, "system", return_value="Darwin"):
            app.aac_encoder_choice_var.set("Apple AudioToolbox (macOS)")
            self.assertEqual(app.settings_vars["aac_encoder"].get(), "aac_at")
            self.assertIn("несовместимости", app.aac_encoder_hint.configure.call_args.kwargs["text"])
        for operating_system in ("Windows", "Linux"):
            with self.subTest(operating_system=operating_system):
                with mock.patch.object(studio.platform, "system", return_value=operating_system):
                    app._sync_aac_encoder_choice()
                    self.assertIn("будет использован FFmpeg AAC", app.aac_encoder_hint.configure.call_args.kwargs["text"])
        app.settings_vars["aac_encoder"].set("aac")
        self.assertEqual(app.aac_encoder_choice_var.get(), "FFmpeg AAC")

    def test_encoder_change_rebuilds_only_aac_and_preserves_native_records(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            output = Path(directory) / "output"
            files = [{"id": "chapter", "path": source / "chapter.txt"}]
            targets = [{"format": fmt, "output_dir": fmt} for fmt in ("mp3", "wav", "m4a", "m4b")]
            options = dict(targets=targets, book_name="Книга")
            legacy = studio.plan_source_synthesis_target_paths(source, output, files, **options)
            native = studio.plan_source_synthesis_target_paths(source, output, files, aac_encoder="aac", **options)
            apple = studio.plan_source_synthesis_target_paths(source, output, files, aac_encoder="aac_at", **options)
            self.assertEqual(native, legacy)
            self.assertTrue(all("aac_encoder" not in record for record in native))
            app = object.__new__(studio.TTSApp)
            app._source_plan_dirty = False
            app._source_completed_regular_targets = {
                str(Path(record["path"]).resolve()): copy.deepcopy(record)
                for record in native if record["kind"] != "m4b"
            }
            app._source_completed_m4b_targets = {}
            app._source_runtime_m4b_records = tuple(record for record in native if record["kind"] == "m4b")
            regular = app._source_regular_rebuild_paths(apple)
            books = app._source_m4b_rebuild_paths(apple)
            self.assertEqual(len(regular), 1)
            self.assertTrue(regular[0].endswith(".m4a"))
            self.assertEqual(len(books), 1)
            self.assertTrue(books[0].endswith(".m4b"))
            for before, after in zip(native, apple):
                if after["target"]["format"] in {"mp3", "wav"}:
                    self.assertEqual(before, after)
                else:
                    self.assertEqual(after["aac_encoder"], "aac_at")
                    if after["kind"] == "m4b":
                        self.assertNotEqual(app._source_m4b_record_target_key(before), app._source_m4b_record_target_key(after))

    def test_encoder_choice_does_not_change_shared_source_settings_signature(self):
        app = object.__new__(studio.TTSApp)
        app.config = dict(studio.DEFAULT_CONFIG, glossary_enabled=False)
        native = app._source_session_settings_signature()
        app.config["aac_encoder"] = "aac_at"
        self.assertEqual(app._source_session_settings_signature(), native)


if __name__ == "__main__":
    unittest.main()
