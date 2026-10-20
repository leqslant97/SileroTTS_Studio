"""Уведомления результата не захватывают ввод и не останавливают очередь UI."""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class TaskNotificationSchedulingTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = True
        app.root.grab_current.return_value = None
        app.root.after_idle.return_value = "idle-notice"
        app.root.after.return_value = "retry-notice"
        app._create_task_notification = mock.Mock()
        return app

    def test_closing_application_suppresses_late_task_results(self):
        app = self.make_app()
        app._is_closing = True
        app._show_task_notification("error", "Ошибка", "Поздний результат")
        app.root.after_idle.assert_not_called()
        app._create_task_notification.assert_not_called()

    def test_results_wait_for_current_callback_and_keep_their_order(self):
        app = self.make_app()
        for kind in ("info", "warning", "error"):
            app._show_task_notification(kind, kind, f"result-{kind}")
        app._create_task_notification.assert_not_called()
        app.root.after_idle.assert_called_once_with(app._drain_task_notifications)
        for _ in range(3):
            app._drain_task_notifications()
        self.assertEqual(app._create_task_notification.call_args_list, [
            mock.call(kind, kind, f"result-{kind}") for kind in ("info", "warning", "error")
        ])
        self.assertFalse(app._task_notifications)
        self.assertIsNone(app._task_notification_after_id)

    def test_editor_and_confirmation_postpone_results_without_losing_them(self):
        for blocker in ("editor", "confirmation"):
            with self.subTest(blocker=blocker):
                app = self.make_app()
                if blocker == "editor":
                    app.root.grab_current.return_value = object()
                else:
                    app._messagebox_depth = 1
                app._show_task_notification("info", "Готово", "Результат")
                app._drain_task_notifications()
                app._create_task_notification.assert_not_called()
                self.assertEqual(len(app._task_notifications), 1)
                app.root.after.assert_called_once_with(100, app._drain_task_notifications)
                app.root.grab_current.return_value = None
                app._messagebox_depth = 0
                app._drain_task_notifications()
                app._create_task_notification.assert_called_once_with("info", "Готово", "Результат")

    def test_confirmation_keeps_its_answer_and_depth_is_restored_after_failure(self):
        app = self.make_app()
        app._schedule_focus_after_messagebox = mock.Mock()

        def confirm(*_args, **_kwargs):
            self.assertEqual(app._messagebox_depth, 1)
            return False

        self.assertFalse(app._run_messagebox(confirm, "Подтверждение", "Продолжить?"))
        self.assertEqual(app._messagebox_depth, 0)
        with self.assertRaises(studio.tk.TclError):
            app._run_messagebox(mock.Mock(side_effect=studio.tk.TclError("closed")), "Ошибка", "Текст")
        self.assertEqual(app._messagebox_depth, 0)


class TaskNotificationTkTests(unittest.TestCase):
    def test_open_results_allow_queue_updates_clicks_and_long_message_scrolling(self):
        script = textwrap.dedent('''
            import importlib.util
            import json
            import logging
            import os
            import queue
            import shutil
            import sys
            import tempfile
            import threading
            import time
            from pathlib import Path

            from contextlib import ExitStack

            original_directory = Path.cwd()
            with tempfile.TemporaryDirectory(prefix="silero_notification_test_") as directory, ExitStack() as cleanup:
                cleanup.callback(os.chdir, original_directory)
                cleanup.callback(logging.shutdown)
                path = Path(directory) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], path)
                spec = importlib.util.spec_from_file_location("notification_test", path)
                studio = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = studio
                spec.loader.exec_module(studio)
                try:
                    root = studio.tk.Tk()
                except studio.tk.TclError as exc:
                    print("TK_UNAVAILABLE", exc)
                    sys.exit(77)
                app = object.__new__(studio.TTSApp)
                app.root = root
                app._ui_queue = queue.SimpleQueue()
                app._palette_widget_kinds = {}
                app._status_label_kinds = {}
                app._is_dark_appearance = False
                root.geometry("900x600+40+40")
                label = studio.ttk.Label(root, text="0")
                label.pack()
                clicks = []
                button = studio.ttk.Button(root, text="Рабочая кнопка", command=lambda: clicks.append("click"))
                button.pack()
                root.after(25, app._drain_ui_queue)
                app._post_to_ui(app._show_task_notification, "info", "Экспорт завершён", "Сохранено в папку")
                app._post_to_ui(label.configure, text="Завершено")

                def pump_until(predicate, timeout=3):
                    deadline = time.monotonic() + timeout
                    while not predicate() and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                    assert predicate(), "timeout"

                try:
                    pump_until(lambda: bool(getattr(app, "_task_notification_windows", None)))
                    assert label.cget("text") == "Завершено"
                    assert root.grab_current() is None
                    notices = list(app._task_notification_windows)
                    updates = []
                    posted = []
                    def producer():
                        for index in range(1, 6):
                            time.sleep(0.04)
                            posted.append(time.monotonic())
                            app._post_to_ui(lambda value=index: (label.configure(text=str(value)), updates.append((value, time.monotonic()))))
                    worker = threading.Thread(target=producer)
                    worker.start()
                    pump_until(lambda: len(updates) == 5)
                    worker.join()
                    assert notices[0].winfo_exists(), "result unexpectedly dismissed"
                    assert max(applied - sent for (_, applied), sent in zip(updates, posted)) < 0.3
                    button.invoke()
                    assert clicks == ["click"]
                    assert label.cget("text") == "5"

                    editor = studio.tk.Toplevel(root)
                    editor.grab_set()
                    app._show_task_notification("error", "Ошибка сборки", "\\n".join("Длинная строка " + str(i) for i in range(150)))
                    root.update()
                    assert len(app._task_notification_windows) == 1
                    assert root.grab_current() is editor
                    editor.grab_release()
                    editor.destroy()
                    pump_until(lambda: len(app._task_notification_windows) == 2)
                    long_notice = next(w for w in app._task_notification_windows if w is not notices[0])
                    def descendants(widget):
                        for child in widget.winfo_children():
                            yield child
                            yield from descendants(child)
                    text = next(w for w in descendants(long_notice) if isinstance(w, studio.tk.Text))
                    assert "149" in text.get("1.0", "end")
                    assert text.cget("state") == "disabled"
                    text.yview_moveto(1)
                    root.update()
                    assert text.yview()[1] == 1
                    for window in list(app._task_notification_windows):
                        close = next(w for w in descendants(window) if isinstance(w, studio.ttk.Button) and w.cget("text") == "Закрыть")
                        close.invoke()
                    root.update()
                    assert not app._task_notification_windows
                    app._show_task_notification("warning", "Позднее", "После закрытия")
                    app._is_closing = True
                    root.update()
                    assert not app._task_notifications
                    assert not app._task_notification_windows
                    print(json.dumps({"updates": len(updates), "clicks": len(clicks), "success": True}))
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
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["success"])


if __name__ == "__main__":
    unittest.main()
