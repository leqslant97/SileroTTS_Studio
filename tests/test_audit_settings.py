"""Проверки сохранности исходных текстов и отмены правок библиотеки профилей."""

import importlib.util
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
            import importlib.util
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

            with tempfile.TemporaryDirectory(prefix="stts_profile_audit_") as temp:
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
                        listing = next(w for w in descendants(dialog)
                                       if w.winfo_class() == "Listbox")
                        listing.selection_clear(0, tk.END)
                        listing.selection_set(listing.size() - 1)
                        listing.event_generate("<<ListboxSelect>>")
                        root.update()
                        button(dialog, "🗑 Удалить").invoke()
                        root.update()
                        return dialog

                    dialog = delete_draft()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    button(dialog, "Отмена").invoke()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    assert app.config["normalizer_profiles"] == [entry]
                    app._persist_settings_snapshot.assert_not_called()

                    dialog = delete_draft()
                    app._persist_settings_snapshot.side_effect = OSError("Недоступная папка")
                    button(dialog, "Сохранить и закрыть").invoke()
                    assert app._normalizer_preview_profile_id == entry["id"]
                    assert app.config["normalizer_profiles"] == [entry]
                    app._show_error.assert_called_once()
                    app._persist_settings_snapshot.side_effect = None
                    button(dialog, "Сохранить и закрыть").invoke()
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


if __name__ == "__main__":
    unittest.main()
