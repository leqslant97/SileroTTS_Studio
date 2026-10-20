"""Русский текстовый диалог: точный результат, отмена и вложенная модальность."""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class StringPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='silero_string_prompt_')
        cls.addClassCleanup(cls.directory.cleanup)
        path = Path(cls.directory.name) / 'SileroTTS_Studio.py'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'SileroTTS_Studio.py', path)
        spec = importlib.util.spec_from_file_location('silero_string_prompt_under_test', path)
        cls.studio = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.studio
        isolated_home = Path(cls.directory.name) / 'home'
        isolated_home.mkdir()
        with mock.patch.object(Path, 'home', return_value=isolated_home):
            spec.loader.exec_module(cls.studio)

    def setUp(self):
        studio = self.studio
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.root.destroy)
        self.root.geometry('560x260+40+40')
        self.root.update()
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app._is_dark_appearance = False
        self.app._detect_dark_appearance = lambda: self.app._is_dark_appearance
        self.app._refresh_status_colors = mock.Mock()
        self.errors = []
        self.root.report_callback_exception = lambda _kind, error, _tb: self.errors.append(error)

    def descendants(self, parent):
        for widget in parent.winfo_children():
            yield widget
            yield from self.descendants(widget)

    def ask(self, action, *, parent=None, initial='  исходное имя  '):
        parent = parent or self.root
        before = set(self.descendants(parent))
        seen = []
        def operate():
            dialogs = [widget for widget in self.descendants(parent)
                       if isinstance(widget, self.studio.tk.Toplevel) and widget not in before]
            if not dialogs:
                self.root.after(10, operate)
                return
            dialog = dialogs[0]
            seen.append(dialog)
            try:
                self.assertIs(self.root.grab_current(), dialog)
                entry = next(widget for widget in self.descendants(dialog) if isinstance(widget, self.studio.ttk.Entry))
                buttons = [widget.cget('text') for widget in self.descendants(dialog) if isinstance(widget, self.studio.ttk.Button)]
                self.assertEqual(set(buttons), {'ОК', 'Отмена'})
                self.assertEqual(entry.get(), initial)
                self.assertEqual(str(entry.cget('state')), 'normal')
                self.assertEqual(dialog.transient(), str(parent))
                action(dialog, entry)
            except BaseException as exc:
                self.errors.append(exc)
                if dialog.winfo_exists():
                    dialog.destroy()
        self.root.after(50, operate)
        with mock.patch.object(self.studio.requests.Session, 'request', side_effect=AssertionError('HTTP forbidden')):
            result = self.app._ask_string('Название', 'Введите название:', parent=parent, initialvalue=initial)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].winfo_exists())
        return result

    def key(self, dialog, entry, sequence):
        entry.focus_force()
        self.root.update()
        entry.event_generate(sequence)

    def test_return_accepts_exact_string_without_normalization(self):
        expected = '  Ёж\tи пробелы  '
        def accept(dialog, entry):
            entry.delete(0, self.studio.tk.END)
            entry.insert(0, expected)
            self.key(dialog, entry, '<Return>')
        self.assertEqual(self.ask(accept), expected)
        self.assertIsNone(self.root.grab_current())

    def test_escape_and_window_close_cancel_without_a_result(self):
        for action in (lambda dialog, entry: self.key(dialog, entry, '<Escape>'),
                       lambda dialog, _entry: self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'))):
            with self.subTest(action=action):
                self.assertIsNone(self.ask(action))
                self.assertIsNone(self.root.grab_current())

    def test_buttons_accept_empty_string_and_cancel_edited_string(self):
        for label, expected in (('ОК', ''), ('Отмена', None)):
            with self.subTest(label=label):
                def press(dialog, entry):
                    entry.delete(0, self.studio.tk.END)
                    next(widget for widget in self.descendants(dialog)
                         if isinstance(widget, self.studio.ttk.Button) and widget.cget('text') == label).invoke()
                self.assertEqual(self.ask(press), expected)

    def test_foreign_modal_owner_grab_is_restored_for_accept_and_cancel(self):
        owner = self.studio.tk.Toplevel(self.root)
        parent = self.studio.tk.Toplevel(self.root)
        owner.update()
        parent.update()
        for sequence, expected in (('<Return>', '  исходное имя  '), ('<Escape>', None)):
            with self.subTest(sequence=sequence):
                owner.grab_set()
                self.assertEqual(self.ask(lambda dialog, entry: self.key(dialog, entry, sequence), parent=parent), expected)
                self.assertIs(self.root.grab_current(), owner)
                self.assertEqual(owner.grab_status(), 'local')

    def test_destroyed_modal_parent_is_not_regrabbed(self):
        parent = self.studio.tk.Toplevel(self.root)
        parent.update()
        parent.grab_set()
        self.assertIsNone(self.ask(lambda _dialog, _entry: parent.destroy(), parent=parent))
        self.assertIsNone(self.root.grab_current())

    def test_foreign_grab_is_restored_even_when_prompt_parent_is_destroyed(self):
        owner = self.studio.tk.Toplevel(self.root)
        parent = self.studio.tk.Toplevel(self.root)
        owner.update()
        parent.update()
        owner.grab_set()
        self.assertIsNone(self.ask(lambda _dialog, _entry: parent.destroy(), parent=parent))
        self.assertIs(self.root.grab_current(), owner)

    def test_global_grab_kind_is_restored_after_cancellation(self):
        owner = self.studio.tk.Toplevel(self.root)
        owner.update()
        owner.grab_set_global()
        self.assertIsNone(self.ask(lambda dialog, entry: self.key(dialog, entry, '<Escape>'), parent=owner))
        self.assertIs(self.root.grab_current(), owner)
        self.assertEqual(owner.grab_status(), 'global')

    def test_setup_exception_keeps_existing_modal_grab(self):
        owner = self.studio.tk.Toplevel(self.root)
        owner.update()
        owner.grab_set()
        with mock.patch.object(self.app, '_center_popup', side_effect=RuntimeError('setup failed')):
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                self.app._ask_string('Название', 'Введите название:', parent=owner)
        self.assertIs(self.root.grab_current(), owner)
        self.assertFalse(any(isinstance(widget, self.studio.tk.Toplevel) for widget in owner.winfo_children()))

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS appearance attribute')
    def test_prompt_uses_the_current_native_light_or_dark_appearance(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                self.app._is_dark_appearance = dark
                expected = 'darkaqua' if dark else 'aqua'
                self.root.attributes('-appearance', expected)
                def check(dialog, _entry):
                    self.assertEqual(dialog.attributes('-appearance'), expected)
                    self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
                self.assertIsNone(self.ask(check))
        self.assertEqual(getattr(self.app, '_appearance_popup_windows', set()), set())


if __name__ == '__main__':
    unittest.main()
