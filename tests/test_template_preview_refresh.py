"""Предпросмотр читает актуальные данные и отбрасывает устаревшие расчёты."""

import queue
import threading
import time
import unittest
from unittest import mock

from test_regressions import studio
from tests.test_template_helper_scenarios import PreviewTree


class TemplatePreviewRefreshTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app.config = {"export_dir": "/tmp/preview-refresh", "export_m4b_template": ""}
        self.app._palette_widget_kinds = {}
        self.app._status_label_kinds = {}
        self.app._is_dark_appearance = False
        self.app._detect_dark_appearance = lambda: False
        self.app._sync_task_notification_appearance = lambda _window: None
        self.app._center_popup = lambda window, width, height, **_kwargs: (
            window.geometry(f"{width}x{height}"), window.deiconify(),
        )
        self.app.get_status_color = lambda _status: "black"
        self.pending = queue.Queue()
        self.posted = []

        def post(callback, *args):
            self.pending.put((callback, args))
            self.posted.append(args)

        self.app._post_to_ui = post
        self.ui_thread = threading.get_ident()
        self.set_groups(2)

    def set_groups(self, count):
        self.app.export_groups = {
            f"g{number}": {"name": f"Том {number}", "album": "Книга", "artist": "Автор", "merge": True}
            for number in range(1, count + 1)
        }
        self.app.export_files = {
            f"f{number}": {"path": f"/tmp/preview-refresh/{number}.wav", "title": f"Глава {number}", "duration": 60}
            for number in range(1, count + 1)
        }
        self.app.export_tree = PreviewTree(
            tuple(self.app.export_groups),
            {f"g{number}": (f"f{number}",) for number in range(1, count + 1)},
        )

    def snapshot(self):
        self.assertEqual(threading.get_ident(), self.ui_thread, "provider must read Tk in the UI thread")
        return self.app._template_helper_export_preview_data("m4b")

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def widget(self, window, kind):
        return next(widget for widget in self.descendants(window) if isinstance(widget, kind))

    def button(self, window, text):
        return next(
            widget for widget in self.descendants(window)
            if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == text
        )

    def pump(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while not self.pending.empty():
                callback, args = self.pending.get_nowait()
                callback(*args)
            self.root.update()
            if condition():
                self.assertEqual(self.callback_errors, [])
                return
            time.sleep(0.005)
        self.fail("preview did not reach the expected state")

    def close(self, window):
        self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))

    def full_window(self):
        return next(
            widget for widget in self.descendants(self.root)
            if isinstance(widget, studio.tk.Toplevel) and widget.title() == "Результат применения шаблона"
        )

    def table_results(self, table):
        return tuple(table.item(item, "values")[1] for item in table.get_children())

    @staticmethod
    def row(result):
        return {"source": "Источник", "result": result, "group": "Группа", "path": "", "detail": result}

    def test_quick_open_edit_refresh_and_full_open_read_current_tree(self):
        self.set_groups(1)
        stale = self.snapshot()
        self.set_groups(2)
        target = studio.tk.StringVar(master=self.root, value="{name}-{part}-{parts}")
        self.app._open_template_helper(
            target, "m4b_part", preview_data=stale, preview_data_provider=self.snapshot,
        )
        helper = next(child for child in self.root.winfo_children() if isinstance(child, studio.tk.Toplevel))
        examples = self.widget(helper, studio.tk.Listbox)
        self.assertEqual(examples.get(0, studio.tk.END), ("1. Том 1-1-2.m4b", "2. Том 2-2-2.m4b"))
        self.set_groups(3)
        entry = next(widget for widget in self.descendants(helper) if widget.winfo_class() == "TEntry")
        entry.delete(0, studio.tk.END)
        entry.insert(0, "G{group_index}-V{part}-{parts}")
        self.pump(lambda: examples.get(0) == "1. G1-V1-3.m4b")
        self.assertEqual(examples.get(0, studio.tk.END),
                         ("1. G1-V1-3.m4b", "2. G2-V2-3.m4b", "3. G3-V3-3.m4b"))
        self.set_groups(4)
        self.button(helper, "Обновить").invoke()
        self.assertEqual(examples.get(0), "1. G1-V1-4.m4b")
        self.set_groups(5)
        self.button(helper, "Все результаты…").invoke()
        full = self.full_window()
        table = self.widget(full, studio.ttk.Treeview)
        self.pump(lambda: len(table.get_children()) == 5)
        self.assertEqual(self.table_results(table), tuple(f"G{number}-V{number}-5.m4b" for number in range(1, 6)))
        self.assertEqual(target.get(), "{name}-{part}-{parts}")
        self.close(full)
        self.assertEqual(self.root.grab_current(), helper)
        self.close(helper)

    def test_rapid_template_edits_read_live_context_once_after_typing(self):
        reads = []

        def provider():
            reads.append(threading.get_ident())
            return self.snapshot()

        target = studio.tk.StringVar(master=self.root, value="{name}")
        self.app._open_template_helper(target, "m4b_part", preview_data_provider=provider)
        helper = next(child for child in self.root.winfo_children() if isinstance(child, studio.tk.Toplevel))
        entry = next(widget for widget in self.descendants(helper) if widget.winfo_class() == "TEntry")
        examples = self.widget(helper, studio.tk.Listbox)
        self.assertEqual(len(reads), 1)
        for value in ("С", "Серия ", "Серия {part}-{parts}"):
            entry.delete(0, studio.tk.END)
            entry.insert(0, value)
        self.assertEqual(len(reads), 1)
        self.assertEqual(str(self.button(helper, "Применить").cget("state")), str(studio.tk.DISABLED))
        self.assertEqual(str(self.button(helper, "Все результаты…").cget("state")), str(studio.tk.DISABLED))
        self.pump(lambda: examples.get(0) == "1. Серия 1-2.m4b")
        self.assertEqual(reads, [self.ui_thread, self.ui_thread])
        self.assertEqual(target.get(), "{name}")
        self.close(helper)

    def test_apply_rechecks_current_plan_and_keeps_invalid_changes_unapplied(self):
        unavailable = {"value": False}

        def provider():
            if unavailable["value"]:
                raise ValueError("Текущий план больше не корректен")
            return self.snapshot()

        target = studio.tk.StringVar(master=self.root, value="{name}-{part}-{parts}")
        self.app._open_template_helper(target, "m4b_part", preview_data_provider=provider)
        helper = next(child for child in self.root.winfo_children() if isinstance(child, studio.tk.Toplevel))
        entry = next(widget for widget in self.descendants(helper) if widget.winfo_class() == "TEntry")
        entry.delete(0, studio.tk.END)
        entry.insert(0, "Новое {part}")
        self.pump(lambda: str(self.button(helper, "Применить").cget("state")) == str(studio.tk.NORMAL))
        unavailable["value"] = True
        self.button(helper, "Применить").invoke()
        self.assertTrue(helper.winfo_exists())
        self.assertEqual(target.get(), "{name}-{part}-{parts}")
        self.assertEqual(str(self.button(helper, "Применить").cget("state")), str(studio.tk.DISABLED))
        unavailable["value"] = False
        self.button(helper, "Обновить").invoke()
        self.button(helper, "Применить").invoke()
        self.assertFalse(helper.winfo_exists())
        self.assertEqual(target.get(), "Новое {part}")

    def test_close_and_parent_destruction_cancel_pending_preview(self):
        for direct_destroy in (False, True):
            with self.subTest(direct_destroy=direct_destroy):
                reads = []

                def provider():
                    reads.append(True)
                    return self.snapshot()

                target = studio.tk.StringVar(master=self.root, value="{name}")
                self.app._open_template_helper(target, "m4b_part", preview_data_provider=provider)
                helper = next(child for child in self.root.winfo_children() if isinstance(child, studio.tk.Toplevel))
                entry = next(widget for widget in self.descendants(helper) if widget.winfo_class() == "TEntry")
                entry.insert(studio.tk.END, " {part}")
                if direct_destroy:
                    helper.destroy()
                else:
                    self.close(helper)
                elapsed = []
                self.root.after(300, lambda: elapsed.append(True))
                self.pump(lambda: bool(elapsed))
                self.assertEqual(reads, [True])
                self.assertEqual(target.get(), "{name}")

    def test_full_worker_receives_deep_snapshot_and_provider_stays_in_ui_thread(self):
        shared = {"nested": {"name": "Исходное имя"}}
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        observations = []

        def provider():
            self.assertEqual(threading.get_ident(), self.ui_thread)
            return shared

        def delayed(_kind, _template, data):
            entered.set()
            release.wait(2)
            observations.append((threading.get_ident(), data, data is shared))
            return (self.row(data["nested"]["name"]),)

        with mock.patch.object(studio, "template_helper_full_preview_rows", side_effect=delayed):
            window = self.app._open_template_full_preview(
                "m4b_part", "{name}", {}, preview_data_provider=provider,
            )
            self.assertTrue(entered.wait(1))
            shared["nested"]["name"] = "Новое имя"
            release.set()
            table = self.widget(window, studio.ttk.Treeview)
            self.pump(lambda: len(table.get_children()) == 1)
            self.assertEqual(self.table_results(table), ("Исходное имя",))
            self.assertNotEqual(observations[0][0], self.ui_thread)
            self.assertFalse(observations[0][2])
            self.assertEqual(shared["nested"]["name"], "Новое имя")
            self.close(window)

    def test_new_refresh_discards_old_success_and_old_error(self):
        for old_fails in (False, True):
            with self.subTest(old_fails=old_fails):
                entered = threading.Event()
                release = threading.Event()
                self.addCleanup(release.set)
                current = {"generation": 1}

                def delayed(_kind, _template, data):
                    if data["generation"] == 1:
                        entered.set()
                        release.wait(2)
                        if old_fails:
                            raise ValueError("Устаревшая ошибка")
                    return (self.row(f"Поколение {data['generation']}"),)

                posted_before = len(self.posted)
                with mock.patch.object(studio, "template_helper_full_preview_rows", side_effect=delayed):
                    window = self.app._open_template_full_preview(
                        "m4b_part", "{name}", {}, preview_data_provider=lambda: current,
                    )
                    table = self.widget(window, studio.ttk.Treeview)
                    self.assertTrue(entered.wait(1))
                    current["generation"] = 2
                    self.button(window, "Обновить").invoke()
                    release.set()
                    self.pump(lambda: self.table_results(table) == ("Поколение 2",))
                    self.pump(lambda: len(self.posted) >= posted_before + 2)
                    self.pump(self.pending.empty)
                    self.assertEqual(self.table_results(table), ("Поколение 2",))
                    self.assertFalse(any(
                        isinstance(widget, studio.ttk.Label) and "Устаревшая ошибка" in str(widget.cget("text"))
                        for widget in self.descendants(window)
                    ))
                    self.close(window)

    def test_failed_refresh_clears_old_rows_details_and_copy_then_recovers(self):
        unavailable = {"value": False}

        def provider():
            if unavailable["value"]:
                raise ValueError("Некорректный текущий лимит M4B")
            return self.snapshot()

        window = self.app._open_template_full_preview(
            "m4b_part", "{name}-{part}-{parts}", {}, preview_data_provider=provider,
        )
        table = self.widget(window, studio.ttk.Treeview)
        detail = self.widget(window, studio.tk.Text)
        copy_button = self.button(window, "Копировать список")
        self.pump(lambda: len(table.get_children()) == 2)
        table.selection_set("0")
        table.event_generate("<<TreeviewSelect>>")
        self.pump(lambda: bool(detail.get("1.0", studio.tk.END).strip()))
        unavailable["value"] = True
        self.button(window, "Обновить").invoke()
        self.assertEqual(table.get_children(), ())
        self.assertEqual(detail.get("1.0", studio.tk.END).strip(), "")
        self.assertEqual(str(copy_button.cget("state")), str(studio.tk.DISABLED))
        self.assertTrue(any(
            isinstance(widget, studio.ttk.Label) and "Некорректный текущий лимит M4B" in str(widget.cget("text"))
            for widget in self.descendants(window)
        ))
        unavailable["value"] = False
        self.set_groups(3)
        self.button(window, "Обновить").invoke()
        self.pump(lambda: len(table.get_children()) == 3)
        self.assertEqual(self.table_results(table), ("Том 1-1-3.m4b", "Том 2-2-3.m4b", "Том 3-3-3.m4b"))
        self.assertEqual(str(copy_button.cget("state")), str(studio.tk.NORMAL))
        self.close(window)

    def test_empty_refresh_removes_previous_results(self):
        window = self.app._open_template_full_preview(
            "m4b_part", "{name}-{part}-{parts}", {}, preview_data_provider=self.snapshot,
        )
        table = self.widget(window, studio.ttk.Treeview)
        self.pump(lambda: len(table.get_children()) == 2)
        self.app.export_tree.children = {"g1": (), "g2": ()}
        self.button(window, "Обновить").invoke()
        self.pump(lambda: any(
            isinstance(widget, studio.ttk.Label) and "всего: 0" in str(widget.cget("text"))
            for widget in self.descendants(window)
        ))
        self.assertEqual(table.get_children(), ())
        self.assertEqual(str(self.button(window, "Копировать список").cget("state")), str(studio.tk.DISABLED))
        self.close(window)

    def test_filter_clears_details_of_hidden_selection(self):
        window = self.app._open_template_full_preview(
            "m4b_part", "{name}-{part}-{parts}", {}, preview_data_provider=self.snapshot,
        )
        table = self.widget(window, studio.ttk.Treeview)
        detail = self.widget(window, studio.tk.Text)
        self.pump(lambda: len(table.get_children()) == 2)
        table.selection_set("0")
        table.event_generate("<<TreeviewSelect>>")
        self.pump(lambda: "Том 1" in detail.get("1.0", studio.tk.END))
        self.widget(window, studio.ttk.Entry).insert(0, "Том 2")
        self.pump(lambda: table.get_children() == ("1",))
        self.assertEqual(detail.get("1.0", studio.tk.END).strip(), "")
        self.assertEqual(table.selection(), ())
        self.close(window)

    def test_closing_during_refresh_discards_late_callback(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        current = {"delayed": False}

        def calculate(_kind, _template, data):
            if data["delayed"]:
                entered.set()
                release.wait(2)
            return (self.row("Результат"),)

        with mock.patch.object(studio, "template_helper_full_preview_rows", side_effect=calculate):
            window = self.app._open_template_full_preview(
                "m4b_part", "{name}", {}, preview_data_provider=lambda: current,
            )
            table = self.widget(window, studio.ttk.Treeview)
            self.pump(lambda: len(table.get_children()) == 1)
            current["delayed"] = True
            self.button(window, "Обновить").invoke()
            self.assertTrue(entered.wait(1))
            self.close(window)
            release.set()
            self.pump(lambda: len(self.posted) >= 2)
            self.pump(self.pending.empty)
            self.assertFalse(window.winfo_exists())
            self.assertNotIn(window, self.app._appearance_popup_windows)

    def test_full_open_handles_provider_error_after_valid_quick_preview(self):
        unavailable = {"value": False}

        def provider():
            if unavailable["value"]:
                raise ValueError("Параметры изменились и требуют исправления")
            return self.snapshot()

        target = studio.tk.StringVar(master=self.root, value="{name}-{part}-{parts}")
        self.app._open_template_helper(target, "m4b_part", preview_data_provider=provider)
        helper = next(child for child in self.root.winfo_children() if isinstance(child, studio.tk.Toplevel))
        unavailable["value"] = True
        self.button(helper, "Все результаты…").invoke()
        self.assertEqual(self.callback_errors, [])
        full = self.full_window()
        self.assertEqual(self.widget(full, studio.ttk.Treeview).get_children(), ())
        self.assertTrue(any(
            isinstance(widget, studio.ttk.Label) and "Параметры изменились" in str(widget.cget("text"))
            for widget in self.descendants(full)
        ))
        self.assertEqual(target.get(), "{name}-{part}-{parts}")
        self.close(full)
        self.close(helper)


if __name__ == "__main__":
    unittest.main()
