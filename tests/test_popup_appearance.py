"""Оформление служебного диалога, статус ошибки и освобождение окон на macOS."""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from test_regressions import studio


class PopupAppearanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS Tk")
    def test_modal_popup_tracks_root_and_cleans_registered_window_and_status(self):
        script = textwrap.dedent('''
            import importlib.util, json, logging, shutil, sys, tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory(prefix="silero_popup_theme_") as directory:
                directory = Path(directory)
                Path.home = classmethod(lambda cls: directory)
                source = directory / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], source)
                spec = importlib.util.spec_from_file_location("popup_theme", source)
                studio = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = studio
                spec.loader.exec_module(studio)
                root = studio.tk.Tk()
                root.geometry("820x540+40+40")
                studio.ttk.Style(root).theme_use("aqua")
                app = object.__new__(studio.TTSApp)
                app.root = root
                app._is_closing = False
                app._is_dark_appearance = False
                app._palette_widget_kinds = {}
                app._status_label_kinds = {}
                app.current_selected_export_item = "group"
                app.export_groups = {"group": {}}
                try:
                    try:
                        root.attributes("-appearance", "darkaqua")
                    except studio.tk.TclError:
                        print("TK_APPEARANCE_UNAVAILABLE")
                        sys.exit(77)
                    root.update()
                    # Открытие само согласует тему, не ожидая таймера.
                    app.open_mass_apply_dialog()
                    root.update()
                    popup = root.grab_current()
                    assert popup in app._appearance_popup_windows
                    status = next(widget for widget in app._palette_widget_kinds
                                  if widget.winfo_toplevel() is popup)
                    status.configure(text="Проверьте значения пауз")
                    for theme in ("darkaqua", "aqua", "darkaqua"):
                        root.attributes("-appearance", theme)
                        root.update()
                        app._check_system_appearance(reschedule=False)
                        root.update()
                        assert popup.attributes("-appearance") == theme
                        assert bool(popup.attributes("-isdark")) == (theme == "darkaqua")
                        assert str(status.cget("foreground")) == app.get_status_color("error")
                        assert root.grab_current() is popup
                    # Destroy дочернего виджета не снимает регистрацию окна.
                    child = studio.ttk.Label(popup, text="Временная подпись")
                    child.destroy()
                    assert popup in app._appearance_popup_windows
                    popup.destroy()
                    root.update()
                    assert popup not in app._appearance_popup_windows
                    assert status not in app._palette_widget_kinds
                    assert root.grab_current() is None
                    print(json.dumps({"success": True, "themes": 3}))
                finally:
                    root.destroy()
                    logging.shutdown()
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(studio.__file__))],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 77:
            self.skipTest(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {"success": True, "themes": 3})


if __name__ == "__main__":
    unittest.main()
