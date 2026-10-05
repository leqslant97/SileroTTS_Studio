"""Проверки сохранности исходных текстов и отмены правок библиотеки профилей."""

import importlib.util
import math
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class BatchFolderSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "input"
        self.source.mkdir()
        self.original = self.source / "a.txt"
        self.original.write_text("Исходный текст.", encoding="utf-8")
        self.context = mock.Mock()
        self.context.preview_normalization.return_value = {
            "normalized_text_for_file": "Результат."
        }

    def test_overlapping_trees_are_rejected_before_normalization(self):
        nested = self.source / "input"
        nested.mkdir()
        (nested / "a.txt").write_text("Вложенный текст.", encoding="utf-8")
        for output in (self.root, self.source / "normalized"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                studio.normalize_text_folder(
                    self.source, output, self.context, overwrite=True
                )
        self.context.preview_normalization.assert_not_called()
        self.assertEqual(self.original.read_text(encoding="utf-8"), "Исходный текст.")
        self.assertFalse((self.source / "normalized").exists())

    def test_symlink_to_source_tree_is_rejected(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            studio.normalize_text_folder(self.source, alias / "results", self.context)
        self.context.preview_normalization.assert_not_called()

    def test_output_subdirectory_cannot_redirect_to_originals(self):
        nested = self.source / "volume"
        nested.mkdir()
        chapter = nested / "b.txt"
        chapter.write_text("Исходная глава.", encoding="utf-8")
        output = self.root / "output"
        output.mkdir()
        try:
            (output / "volume").symlink_to(nested, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertLogs(level="ERROR"):
            summary = studio.normalize_text_folder(
                self.source, output, self.context, overwrite=True
            )
        self.assertEqual(summary["written"], 1)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(chapter.read_text(encoding="utf-8"), "Исходная глава.")

    def test_separate_folder_supports_long_unicode_names_and_repeat_skip(self):
        name = "Глава " + "я" * 94 + ".txt"
        self.original.rename(self.source / name)
        output = self.root / "input_normalized"
        summary = studio.normalize_text_folder(self.source, output, self.context)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["written"], 1)
        self.assertEqual((output / name).read_text(encoding="utf-8"), "Результат.")
        second = studio.normalize_text_folder(self.source, output, self.context)
        self.assertEqual(second["skipped_existing"], 1)
        self.context.preview_normalization.assert_called_once()


class NormalizerProfileTransactionTests(unittest.TestCase):
    def test_delete_cancel_failed_save_and_commit_preserve_preview_identity(self):
        script = textwrap.dedent('''
            from contextlib import ExitStack
            import importlib.util
            import logging
            import os
            import shutil
            import sys
            import tempfile
            import tkinter as tk
            from pathlib import Path
            from unittest import mock

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            def button(dialog, text):
                return next(w for w in descendants(dialog)
                            if w.winfo_class() == "TButton" and w.cget("text") == text)

            with tempfile.TemporaryDirectory(prefix="stts_profile_audit_") as temp, ExitStack() as cleanup:
                # Импорт меняет рабочую папку и открывает лог: освобождаем оба
                # ресурса до удаления временной папки, в том числе при ошибке.
                cleanup.callback(os.chdir, Path.cwd())
                cleanup.callback(logging.shutdown)
                isolated = Path(temp) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], isolated)
                spec = importlib.util.spec_from_file_location("studio_profile_audit", isolated)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                try:
                    root = tk.Tk()
                except tk.TclError as exc:
                    print("SKIP_TK_UI=" + str(exc))
                    raise SystemExit(0)
                root.withdraw()
                try:
                    app = module.TTSApp(root)
                    # На Windows скрытый родитель скрывает и transient-диалог;
                    # пользовательский выбор проверяем в отображённом окне.
                    root.deiconify()
                    root.update()
                    entry = module.make_normalizer_profile_entry(
                        module.normalizer_profile_from_config(app.config, name="Личный профиль")
                    )
                    app.config["normalizer_profiles"] = [entry]
                    app._normalizer_preview_profile_id = entry["id"]
                    app._normalizer_preview_profile_name = entry["profile"]["name"]
                    app._ask_yes_no = lambda *a, **kw: True
                    app._show_error = mock.Mock()
                    app._persist_settings_snapshot = mock.Mock()

                    def delete_draft():
                        app.open_normalizer_profiles_dialog()
                        root.update()
                        dialog = next(w for w in root.winfo_children()
                                      if isinstance(w, tk.Toplevel)
                                      and w.title() == "Профили нормализации")
                        assert dialog.winfo_viewable()
                        listing = next(w for w in descendants(dialog)
                                       if w.winfo_class() == "Listbox")
                        listing.selection_clear(0, tk.END)
                        listing.selection_set(listing.size() - 1)
                        listing.event_generate("<<ListboxSelect>>")
                        root.update()
                        delete = button(dialog, "🗑 Удалить")
                        assert not delete.instate(["disabled"])
                        delete.invoke()
                        root.update()
                        assert listing.size() == len(module.builtin_normalizer_profile_entries())
                        return dialog

                    dialog = delete_draft()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    button(dialog, "Отмена").invoke()
                    root.update()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    assert app.config["normalizer_profiles"] == [entry]
                    app._persist_settings_snapshot.assert_not_called()

                    dialog = delete_draft()
                    app._persist_settings_snapshot.side_effect = OSError("Недоступная папка")
                    button(dialog, "Сохранить и закрыть").invoke()
                    root.update()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    assert app.config["normalizer_profiles"] == [entry]
                    app._show_error.assert_called_once()
                    app._persist_settings_snapshot.assert_called_once()
                    assert app._persist_settings_snapshot.call_args.args[0]["normalizer_profiles"] == []
                    app._persist_settings_snapshot.side_effect = None
                    button(dialog, "Сохранить и закрыть").invoke()
                    root.update()
                    assert app._persist_settings_snapshot.call_count == 2
                    assert app.config["normalizer_profiles"] == []
                    assert app._normalizer_preview_profile_id is None
                    assert app._normalizer_preview_profile_name == "Пользовательский"
                    print("PROFILE_TRANSACTION_OK")
                finally:
                    root.destroy()
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script, str(MODULE_PATH)],
            capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "SKIP_TK_UI=" in result.stdout:
            self.skipTest(result.stdout)
        self.assertIn("PROFILE_TRANSACTION_OK", result.stdout)


class SettingsResetRuntimeTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"api_max_requests": 3, "api_time_window": 45.0}
        app._source_plan_running = False
        app._ask_yes_no = mock.Mock(return_value=True)
        app._persist_settings_snapshot = mock.Mock()
        app.shared_rate_limiter = mock.Mock()
        app.full_ui_refresh = mock.Mock()
        app._sync_glossary_editor_cache = mock.Mock()
        app.load_files = mock.Mock()
        app._show_info = mock.Mock()
        app._show_error = mock.Mock()
        return app

    def test_reset_updates_runtime_api_limiter_after_persisting(self):
        app = self.make_app()
        with mock.patch.object(studio, "ensure_config_directories"):
            app.reset_config()

        app._persist_settings_snapshot.assert_called_once()
        app.shared_rate_limiter.update_limits.assert_called_once_with(
            app.config["api_max_requests"], app.config["api_time_window"]
        )
        app.full_ui_refresh.assert_called_once()

    def test_failed_reset_does_not_change_runtime_api_limiter(self):
        app = self.make_app()
        old_config = app.config
        app._persist_settings_snapshot.side_effect = OSError("disk full")
        with mock.patch.object(studio, "ensure_config_directories"):
            app.reset_config()

        self.assertIs(app.config, old_config)
        app.shared_rate_limiter.update_limits.assert_not_called()
        app.full_ui_refresh.assert_not_called()
        app._show_error.assert_called_once()


class NumericSettingsValidationTests(unittest.TestCase):
    def test_nonfinite_silence_threshold_uses_default(self):
        for raw_value in ("nan", "inf", "-inf"):
            with self.subTest(raw_value=raw_value):
                normalized = studio.normalize_config(
                    {"silence_threshold": raw_value}
                )
                self.assertEqual(
                    normalized["silence_threshold"],
                    studio.DEFAULT_CONFIG["silence_threshold"],
                )

    def test_nonfinite_settings_value_does_not_replace_valid_json(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "settings.json"
            path.write_text('{"safe": true}', encoding="utf-8")

            with self.assertRaises(ValueError):
                studio.TTSApp._write_json_atomic(
                    path, {"broken": math.nan}, backup=True
                )

            self.assertEqual(path.read_text(encoding="utf-8"), '{"safe": true}')
            self.assertFalse(path.with_suffix(".json.bak").exists())

    def test_nonfinite_cache_metadata_does_not_replace_index(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache = Path(tempdir)
            index = cache / "sentence_cache.json"
            index.write_text('{"safe": {}}', encoding="utf-8")

            with self.assertRaises(ValueError):
                studio.write_cache_index_atomic(
                    cache, {"broken": {"unexpected": math.inf}}
                )

            self.assertEqual(index.read_text(encoding="utf-8"), '{"safe": {}}')
            self.assertEqual(list(cache.glob(".*.tmp")), [])


class CacheLinkSafetyTests(unittest.TestCase):
    def test_clear_rejects_audio_file_before_removing_index(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache = Path(tempdir)
            (cache / "audio").write_bytes(b"personal file")
            index = cache / "sentence_cache.json"
            index.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "не является каталогом"):
                studio.clear_cache_storage(cache)

            self.assertEqual((cache / "audio").read_bytes(), b"personal file")
            self.assertTrue(index.exists())

    def test_clear_rejects_linked_audio_before_removing_index(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cache = root / "cache"
            external = root / "external"
            cache.mkdir()
            external.mkdir()
            audio = external / ("a" * 32 + ".ogg")
            audio.write_bytes(b"personal audio")
            index = cache / "sentence_cache.json"
            index.write_text("{}", encoding="utf-8")
            try:
                (cache / "audio").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))

            with self.assertRaisesRegex(ValueError, "символической ссылкой"):
                studio.clear_cache_storage(cache)

            self.assertEqual(audio.read_bytes(), b"personal audio")
            self.assertTrue(index.exists())

    def test_eviction_does_not_follow_linked_audio_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cache = root / "cache"
            external = root / "external"
            cache.mkdir()
            external.mkdir()
            audio = external / "entry.ogg"
            audio.write_bytes(b"personal audio")
            try:
                (cache / "audio").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))

            with self.assertLogs(level="WARNING"):
                paths = studio.unreferenced_cache_audio_paths(
                    cache, [{"file_name": audio.name}], {}
                )

            self.assertEqual(paths, [])
            self.assertTrue(audio.exists())


class CacheArchiveReportingTests(unittest.TestCase):
    def test_created_archive_is_reported_when_followup_clear_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cache = root / "cache"
            cache.mkdir()
            archive = root / "backup.zip"
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(cache)}
            app.del_after_zip = mock.Mock()
            app.del_after_zip.get.return_value = True
            app.is_cache_operation_running = mock.Mock(return_value=False)
            app.is_synthesis_running = mock.Mock(return_value=False)
            popup = mock.Mock()
            app._begin_cache_operation = mock.Mock(return_value=popup)
            app._post_to_ui = mock.Mock()

            with mock.patch.object(
                studio.filedialog, "asksaveasfilename", return_value=str(archive)
            ), mock.patch.object(
                studio, "create_zip_archive_atomic", return_value=archive
            ), mock.patch.object(
                studio, "clear_cache_storage", side_effect=ValueError("linked audio")
            ), mock.patch.object(studio.threading, "Thread") as thread_class:
                thread_class.return_value.start.side_effect = (
                    lambda: thread_class.call_args.kwargs["target"]()
                )
                with self.assertLogs(level="ERROR"):
                    app.archive_cache()

            callback, posted_popup, posted_archive, cleared, error = (
                app._post_to_ui.call_args.args
            )
            self.assertEqual(callback, app._finish_cache_archive)
            self.assertIs(posted_popup, popup)
            self.assertEqual(posted_archive, archive)
            self.assertFalse(cleared)
            self.assertIn(str(archive), error)
            self.assertIn("Не удалось очистить кэш", error)


if __name__ == "__main__":
    unittest.main()
