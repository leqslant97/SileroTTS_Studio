"""Вложенное имя варианта возвращает модальность открытого помощника."""

import copy
import unittest
from unittest import mock

from test_regressions import studio


class TemplateHelperModalTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app.config = {"template_helper_presets": {}}
        self.app._status_label_kinds = {}
        self.app._palette_widget_kinds = {}
        self.app.get_status_color = lambda _kind: "black"
        self.app._register_palette_widget = lambda widget, *_args, **_kwargs: widget
        self.app._center_popup = lambda window, *_args, **_kwargs: window.deiconify()
        self.app._persist_settings_snapshot = mock.Mock()
        self.target = studio.tk.StringVar(self.root, "{num:03d}-{title}")
        self.callback_errors = []
        self.root.report_callback_exception = lambda typ, value, _tb: self.callback_errors.append((typ, value))
        self.app._open_template_helper(self.target, "book_import")
        self.root.update()
        self.dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, studio.tk.Toplevel))
        self.save_button = next(widget for widget in self.descendants(self.dialog) if isinstance(widget, studio.ttk.Button) and str(widget.cget("text")) == "Сохранить вариант…")
        self.initial = copy.deepcopy(self.app.config)
        self.assertIs(self.root.grab_current(), self.dialog)

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def invoke_name_dialog(self, outcome=None, *, error=None, destroy_owner=False):
        def nested_name(*_args, **kwargs):
            self.assertIs(kwargs["parent"], self.dialog)
            # Имитируем также сбой вложенного запроса после снятия захвата;
            # родительский помощник восстанавливает свою модальность.
            self.dialog.grab_release()
            if destroy_owner:
                self.dialog.destroy()
            if error is not None:
                raise error
            return outcome
        with mock.patch.object(self.app, "_ask_string", side_effect=nested_name):
            self.save_button.invoke()
        self.root.update()

    def test_cancel_restores_modal_helper_without_changes(self):
        self.invoke_name_dialog()
        self.assertIs(self.root.grab_current(), self.dialog)
        self.assertEqual(self.app.config, self.initial)
        self.assertEqual(self.target.get(), "{num:03d}-{title}")
        self.app._persist_settings_snapshot.assert_not_called()
        self.assertEqual(self.callback_errors, [])

    def test_success_restores_modal_helper_and_only_stages_preset(self):
        self.invoke_name_dialog("Сохранённый вариант")
        self.assertIs(self.root.grab_current(), self.dialog)
        labels = [str(widget.cget("text")) for widget in self.descendants(self.dialog) if isinstance(widget, studio.ttk.Label)]
        self.assertTrue(any("Вариант подготовлен." in text for text in labels))
        self.assertEqual(self.app.config, self.initial)
        self.app._persist_settings_snapshot.assert_not_called()
        self.assertEqual(self.callback_errors, [])

    def test_exception_restores_modal_helper_without_hiding_error(self):
        failure = RuntimeError("Name dialog failed")
        self.invoke_name_dialog(error=failure)
        self.assertIs(self.root.grab_current(), self.dialog)
        self.assertEqual(self.app.config, self.initial)
        self.assertEqual(self.callback_errors, [(RuntimeError, failure)])

    def test_destroyed_owner_is_not_regrabbed(self):
        self.invoke_name_dialog(destroy_owner=True)
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(self.app.config, self.initial)
        self.assertEqual(self.callback_errors, [])
