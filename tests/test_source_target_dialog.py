"""Редактор целей сохраняет звук строк до явного выбора другого профиля."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class SourceTargetDialogTests(unittest.TestCase):
    def test_real_dialog_preserves_custom_targets_and_applies_explicit_profile_changes(self):
        script = textwrap.dedent('''
            from contextlib import ExitStack
            import copy
            import importlib.util
            import logging
            import os
            import shutil
            import sys
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                # Windows не позволяет удалить текущую папку или открытый лог.
                cleanup.callback(os.chdir, Path.cwd())
                cleanup.callback(logging.shutdown)
                module_path = Path(directory) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], module_path)
                spec = importlib.util.spec_from_file_location("dialog_test", module_path)
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
                    custom = studio.normalize_source_synthesis_targets([
                        {"format": "mp3", "bitrate": "192k", "sample_rate": "44100", "channels": "stereo", "assembly_mode": "files", "apply_effects": True, "effects": {"speed": 1.25}},
                        {"format": "m4b", "bitrate": "96k", "assembly_mode": "merge"},
                    ])
                    app.synthesis_targets = list(custom)
                    app.save_settings()

                    def children(widget):
                        result = []
                        for child in widget.winfo_children():
                            result.append(child)
                            result.extend(children(child))
                        return result

                    def button(dialog, label):
                        return next(w for w in children(dialog) if w.winfo_class() == "TButton" and w.cget("text") == label)

                    def open_dialog():
                        app.open_source_output_targets_dialog()
                        root.update()
                        dialog = next(w for w in root.winfo_children() if w.winfo_class() == "Toplevel" and "Набор выходов —" in w.title())
                        assert dialog.winfo_viewable()
                        return dialog

                    def profiles(dialog):
                        return [w for w in children(dialog) if w.winfo_class() == "TCombobox" and "Текущие параметры" in w.cget("values")]

                    dialog = open_dialog()
                    assert [w.get() for w in profiles(dialog)] == ["Сохранённые параметры", "Сохранённые параметры"]
                    button(dialog, "＋ Добавить цель").invoke()
                    modes = [w for w in children(dialog) if w.winfo_class() == "TCombobox" and "Отдельные файлы" in w.cget("values")]
                    modes[-1].set("Один файл")
                    modes[-1].event_generate("<<ComboboxSelected>>")
                    root.update()
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[:2] == list(custom), (custom, app.synthesis_targets)

                    dialog = open_dialog()
                    modes = [w for w in children(dialog) if w.winfo_class() == "TCombobox" and "Отдельные файлы" in w.cget("values")]
                    modes[0].set("Один файл")
                    modes[0].event_generate("<<ComboboxSelected>>")
                    root.update()
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[0] == dict(custom[0], assembly_mode="merge")

                    dialog = open_dialog()
                    profile = profiles(dialog)[0]
                    label = next(value for value in profile.cget("values") if "128" in value)
                    profile.set(label)
                    profile.event_generate("<<ComboboxSelected>>")
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[0]["bitrate"] == "128k"
                    assert not app.synthesis_targets[0]["apply_effects"]

                    app.settings_vars["output_bitrate"].set("160k")
                    dialog = open_dialog()
                    profile = profiles(dialog)[0]
                    profile.set("Текущие параметры")
                    profile.event_generate("<<ComboboxSelected>>")
                    button(dialog, "Применить").invoke()
                    root.update()
                    assert app.synthesis_targets[0]["bitrate"] == "160k", app.synthesis_targets[0]
                    assert not app.synthesis_targets[0]["profile"]
                    assert not failures, failures
                finally:
                    root.destroy()
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")],
            capture_output=True, text=True, timeout=40,
        )
        if result.returncode == 77:
            self.skipTest(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
