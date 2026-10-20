"""Окна итогов следуют теме macOS, включая смену темы уже открытого окна."""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class NotificationAppearanceCompatibilityTests(unittest.TestCase):
    def test_old_tk_without_appearance_attribute_keeps_working(self):
        app = object.__new__(studio.TTSApp)
        app._is_dark_appearance = True
        popup = mock.Mock()
        popup.attributes.side_effect = studio.tk.TclError("unknown option")
        with mock.patch.object(studio.sys, "platform", "darwin"):
            app._sync_task_notification_appearance(popup)
        popup.attributes.assert_called_once_with("-appearance", "darkaqua")

    def test_other_platforms_keep_their_native_window_theme(self):
        app = object.__new__(studio.TTSApp)
        popup = mock.Mock()
        with mock.patch.object(studio.sys, "platform", "linux"):
            app._sync_task_notification_appearance(popup)
        popup.attributes.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS Tk")
    def test_results_inherit_dark_theme_and_open_windows_switch_with_root(self):
        script = textwrap.dedent('''
            import importlib.util
            import json
            import logging
            import shutil
            import sys
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory(prefix="silero_notice_appearance_") as directory:
                path = Path(directory) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], path)
                spec = importlib.util.spec_from_file_location("notice_appearance", path)
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

                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)

                checks = []
                try:
                    try:
                        root.attributes("-appearance", "darkaqua")
                    except studio.tk.TclError:
                        print("TK_APPEARANCE_UNAVAILABLE")
                        sys.exit(77)
                    root.update()
                    # Уведомление может прийти до очередной проверки темы.
                    assert not app._is_dark_appearance
                    popups = [app._create_task_notification(kind, "Завершено", "Результат синтеза")
                              for kind in ("info", "warning", "error")]
                    assert app._is_dark_appearance
                    root.update()
                    for theme in ("darkaqua", "aqua", "darkaqua"):
                        root.attributes("-appearance", theme)
                        root.update()
                        app._check_system_appearance(reschedule=False)
                        root.update()
                        dark = theme == "darkaqua"
                        assert app._is_dark_appearance == dark
                        for popup, status in zip(popups, ("success", "warning", "error")):
                            assert bool(popup.attributes("-isdark")) == dark
                            assert popup.attributes("-appearance") == theme
                            widgets = list(descendants(popup))
                            text = next(w for w in widgets if isinstance(w, studio.tk.Text))
                            red, green, blue = text.winfo_rgb(text.cget("background"))
                            brightness = (red + green + blue) / (3 * 65535)
                            assert (brightness < 0.5) == dark, (theme, brightness)
                            heading = next(w for w in widgets if isinstance(w, studio.ttk.Label))
                            assert str(heading.cget("foreground")) == app.get_status_color(status)
                            assert root.grab_current() is None
                            checks.append({"theme": theme, "status": status,
                                           "text_background_brightness": brightness})
                    print(json.dumps({"success": True, "checks": checks}))
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
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(report["success"])
        self.assertEqual(len(report["checks"]), 9)


if __name__ == "__main__":
    unittest.main()
