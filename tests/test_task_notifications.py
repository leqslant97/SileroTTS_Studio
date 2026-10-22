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


class TaskNotificationFocusTests(unittest.TestCase):
    def make_notice(self):
        app = object.__new__(studio.TTSApp)
        app.root = root = mock.Mock()
        root.winfo_exists.return_value = True
        root.winfo_width.return_value = 900
        root.winfo_height.return_value = 600
        root.winfo_rootx.return_value = 0
        root.winfo_rooty.return_value = 0
        root.grab_current.return_value = None
        previous = mock.Mock()
        previous.winfo_exists.return_value = True
        root.focus_get.return_value = previous
        idle = []
        root.after_idle.side_effect = idle.append
        app._is_dark_appearance = False
        app._detect_dark_appearance = mock.Mock(return_value=False)
        app._sync_task_notification_appearance = mock.Mock()
        app._register_palette_widget = mock.Mock()
        app.get_status_color = mock.Mock(return_value="black")
        popup = mock.Mock()
        popup.winfo_screenwidth.return_value = 1600
        popup.winfo_screenheight.return_value = 1000
        with mock.patch.object(studio.tk, "Toplevel", return_value=popup), \
                mock.patch.object(studio.tk, "Text"), \
                mock.patch.object(studio.ttk, "Frame"), \
                mock.patch.object(studio.ttk, "Label"), \
                mock.patch.object(studio.ttk, "Scrollbar"), \
                mock.patch.object(studio.ttk, "Button") as button:
            app._create_task_notification("warning", "Остановлено", "Сборка прервана")
        close = button.call_args.kwargs["command"]
        focused = mock.Mock()
        focused.winfo_toplevel.return_value = popup
        root.focus_get.return_value = focused
        popup.destroy.side_effect = lambda: setattr(root.focus_get, "return_value", None)
        return app, previous, popup, close, idle

    def test_closing_result_recovers_input_when_window_manager_leaves_no_focus(self):
        app, previous, _popup, close, idle = self.make_notice()
        close()
        for callback in idle:
            callback()
        previous.focus_set.assert_called_once_with()
        app.root.grab_set.assert_not_called()

    def test_closing_background_result_keeps_the_current_input(self):
        app, previous, _popup, close, idle = self.make_notice()
        current = mock.Mock()
        current.winfo_toplevel.return_value = app.root
        app.root.focus_get.return_value = current
        close()
        for callback in idle:
            callback()
        previous.focus_set.assert_not_called()

    def test_deferred_restore_preserves_new_editor_and_handles_closed_owner(self):
        for state in ("editor", "closed_owner", "closing"):
            with self.subTest(state=state):
                app, previous, _popup, close, idle = self.make_notice()
                close()
                if state == "editor":
                    editor = mock.Mock()
                    editor.winfo_toplevel.return_value = editor
                    app.root.grab_current.return_value = editor
                elif state == "closed_owner":
                    app.root.winfo_exists.return_value = False
                else:
                    app._is_closing = True
                for callback in idle:
                    callback()
                previous.focus_set.assert_not_called()


class TaskNotificationTkTests(unittest.TestCase):
    def test_closing_result_restores_input_and_preserves_a_new_editor_focus(self):
        script = textwrap.dedent('''
            import importlib.util
            import json
            import logging
            import os
            import shutil
            import sys
            import tempfile
            import time
            from pathlib import Path

            original_directory = Path.cwd()
            with tempfile.TemporaryDirectory(prefix="silero_notification_focus_") as directory:
                path = Path(directory) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], path)
                spec = importlib.util.spec_from_file_location("notification_focus_test", path)
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
                app._palette_widget_kinds = {}
                app._status_label_kinds = {}
                app._is_dark_appearance = False
                root.geometry("600x300+40+40")
                entry = studio.ttk.Entry(root)
                entry.pack()
                other = studio.ttk.Entry(root)
                other.pack()

                def pump():
                    end = time.monotonic() + .12
                    while time.monotonic() < end:
                        root.update()
                        time.sleep(.005)

                def close(window):
                    root.tk.call(window.protocol("WM_DELETE_WINDOW"))

                try:
                    root.update()
                    entry.focus_force()
                    pump()
                    notice = app._create_task_notification("warning", "Остановлено", "Сборка прервана")
                    pump()
                    notice.focus_force()
                    pump()
                    close(notice)
                    pump()
                    assert root.focus_get() is entry, str(root.focus_get())
                    assert root.grab_current() is None

                    notice = app._create_task_notification("info", "Готово", "Результат")
                    pump()
                    other.focus_force()
                    pump()
                    close(notice)
                    pump()
                    assert root.focus_get() is other

                    entry.focus_force()
                    pump()
                    notice = app._create_task_notification("warning", "Остановлено", "Результат")
                    pump()
                    notice.focus_force()
                    pump()
                    close(notice)
                    editor = studio.tk.Toplevel(root)
                    editor_input = studio.ttk.Entry(editor)
                    editor_input.pack()
                    editor.update_idletasks()
                    editor.grab_set()
                    editor_input.focus_force()
                    pump()
                    assert root.grab_current() is editor
                    assert root.focus_get() is editor_input
                    editor.grab_release()
                    editor.destroy()
                    print(json.dumps({"focus_restored": True, "new_focus_preserved": True, "editor_grab_preserved": True}))
                finally:
                    root.destroy()
                    logging.shutdown()
                    os.chdir(original_directory)
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(studio.__file__))],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 77:
            self.skipTest(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["focus_restored"])

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
