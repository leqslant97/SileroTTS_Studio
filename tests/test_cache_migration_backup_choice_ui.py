"""SQLite activation asks once whether to retain a separate JSON backup."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
    )
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class CacheMigrationBackupChoiceUITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name).resolve()
        self.app = object.__new__(studio.TTSApp)
        self.app.config = {"cache_dir": str(self.cache)}
        self.app.root = mock.Mock()
        self.app._cache_operation = "migrate"
        self.app._cache_ui_loaded = True
        self.app._close_popup_safely = mock.Mock()
        self.app._update_cache_controls = mock.Mock()
        self.app._create_wait_popup = mock.Mock(return_value=mock.Mock())
        self.app._show_task_notification = mock.Mock()
        self.app._cache_generation_changed = mock.Mock()
        self.app._refresh_cache_backend_status = mock.Mock()
        self.app.load_cache_ui = mock.Mock()
        self.app._confirm_cache_migration_activation = mock.Mock(return_value=False)
        self.app._post_to_ui = lambda callback, *args, **kwargs: callback(*args, **kwargs)
        self.receipt = {"record_count": 7, "source_kind": "json", "source": str(self.cache / "sentence_cache.json"),
                        "audio_inventory": {}, "receipt_path": str(self.cache / "sentence_cache.migration.json")}

    def activate(self, choice):
        self.app._confirm_cache_migration_activation.return_value = choice
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "activate_sqlite_cache_migration") as activation,
        ):
            self.app._finish_cache_migration_prepare(mock.Mock(), self.cache, self.receipt)
            return activation

    def test_default_and_opt_in_choice_reach_worker_backend_api(self):
        for choice in (False, True):
            with self.subTest(choice=choice):
                self.app._cache_operation = "migrate"
                activation = self.activate(choice)
                activation.assert_called_once()
                self.assertIs(activation.call_args.kwargs["keep_json_backup"], choice)
                self.assertTrue(callable(activation.call_args.kwargs["warning"]))
                self.assertEqual(activation.call_args.args, (self.cache, self.receipt))
                self.assertIsNone(self.app._cache_operation)
                self.assertEqual(self.app._show_task_notification.call_args.args[0], "info")
                self.assertIn("sentence_cache.json.bak удалён", self.app._show_task_notification.call_args.args[-1])

    def test_final_confirmation_cancellation_never_activates(self):
        activation = self.activate(None)
        activation.assert_not_called()
        self.app._create_wait_popup.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_old_folder_skips_dialog_and_activation(self):
        self.app.config["cache_dir"] = str(self.cache / "other")
        activation = self.activate(False)
        activation.assert_not_called()
        self.app._confirm_cache_migration_activation.assert_not_called()
        self.app._cache_generation_changed.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_folder_change_during_final_dialog_cancels_activation(self):
        def confirm(*_args):
            self.app.config["cache_dir"] = str(self.cache / "other")
            return True

        self.app._confirm_cache_migration_activation.side_effect = confirm
        activation = self.activate(False)
        activation.assert_not_called()
        self.app._cache_generation_changed.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_cleanup_warning_still_updates_sqlite_status_and_reports_success(self):
        def activate(_cache, _receipt, **kwargs):
            kwargs["warning"]("Не удалось удалить sentence_cache.json.bak: permission denied")

        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "activate_sqlite_cache_migration", side_effect=activate),
        ):
            self.app._finish_cache_migration_prepare(mock.Mock(), self.cache, self.receipt)
        self.app._refresh_cache_backend_status.assert_called_once_with("sqlite", cache_dir=self.cache)
        self.app.load_cache_ui.assert_called_once()
        self.assertEqual(self.app._show_task_notification.call_args.args[:2], ("warning", "SQLite включён"))
        self.assertIn("permission denied", self.app._show_task_notification.call_args.args[-1])
        self.assertNotIn("sentence_cache.json.bak удалён", self.app._show_task_notification.call_args.args[-1])
        self.assertIsNone(self.app._cache_operation)

    def test_activation_failure_does_not_publish_success_or_change_table(self):
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "activate_sqlite_cache_migration", side_effect=OSError("backup failed")),
            self.assertLogs(level="ERROR"),
        ):
            self.app._finish_cache_migration_prepare(mock.Mock(), self.cache, self.receipt)
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")
        self.app._cache_generation_changed.assert_not_called()
        self.app._refresh_cache_backend_status.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_old_activation_completion_does_not_replace_new_folder_table(self):
        self.app.config["cache_dir"] = str(self.cache / "other")
        self.app._finish_cache_migration_activation(mock.Mock(), self.cache)
        self.app._cache_generation_changed.assert_not_called()
        self.app._refresh_cache_backend_status.assert_not_called()
        self.app.load_cache_ui.assert_not_called()
        self.assertTrue(self.app._cache_ui_loaded)

    def test_saved_receipt_with_old_source_snapshot_still_asks_new_choice(self):
        self.receipt["source_snapshot"] = "safety_preexisting.json"
        activation = self.activate(False)
        self.assertFalse(activation.call_args.kwargs["keep_json_backup"])
        report = self.app._confirm_cache_migration_activation.call_args.args[0]
        self.assertNotIn("Страховочная копия исходного индекса:", report)
        self.assertIn(".bak будет удалён", report)

    def test_empty_cache_does_not_claim_a_json_backup_was_created(self):
        self.receipt["source"] = None
        self.activate(True)
        self.assertNotIn("Копия исходного JSON сохранена в index_backups", self.app._show_task_notification.call_args.args[-1])
        self.assertIn("исходный JSON отсутствовал", self.app._show_task_notification.call_args.args[-1])
        self.assertNotIn("JSON сохранён как неактивный снимок", self.app._show_task_notification.call_args.args[-1])

    def dialog_choice(self, button, checked=False):
        controls = {}
        dialog = mock.Mock()
        dialog.winfo_reqheight.return_value = 440
        variable = mock.Mock(get=lambda: checked)

        def create_button(_parent, **kwargs):
            controls[kwargs["text"]] = kwargs["command"]
            return mock.Mock()

        self.app.get_status_color = lambda _status: "gray"
        self.app._center_popup = mock.Mock()
        self.app.root.wait_window.side_effect = lambda _dialog: controls[button]()
        with (
            mock.patch.object(studio.tk, "Toplevel", return_value=dialog),
            mock.patch.object(studio.tk, "BooleanVar", return_value=variable) as checkbox_value,
            mock.patch.object(studio.ttk, "Label"),
            mock.patch.object(studio.ttk, "Checkbutton") as checkbox,
            mock.patch.object(studio.ttk, "Frame"),
            mock.patch.object(studio.ttk, "Button", side_effect=create_button),
        ):
            value = studio.TTSApp._confirm_cache_migration_activation(self.app, "Отчёт", self.cache)
        self.assertFalse(checkbox_value.call_args.kwargs["value"])
        self.assertEqual(checkbox.call_args.kwargs["text"], "Сохранить копию исходного JSON в index_backups")
        return value

    def test_themed_dialog_default_off_opt_in_and_cancel(self):
        self.assertIs(self.dialog_choice("Включить SQLite"), False)
        self.assertIs(self.dialog_choice("Включить SQLite", checked=True), True)
        self.assertIsNone(self.dialog_choice("Отмена", checked=True))

    def test_dialog_exception_releases_reservation(self):
        self.app._confirm_cache_migration_activation.side_effect = RuntimeError("dialog failed")
        with self.assertRaisesRegex(RuntimeError, "dialog failed"):
            self.app._finish_cache_migration_prepare(mock.Mock(), self.cache, self.receipt)
        self.assertIsNone(self.app._cache_operation)

    def test_activation_thread_start_failure_releases_reservation(self):
        with mock.patch.object(studio.threading.Thread, "start", side_effect=RuntimeError("thread failed")):
            with self.assertRaisesRegex(RuntimeError, "thread failed"):
                self.app._finish_cache_migration_prepare(mock.Mock(), self.cache, self.receipt)
        self.assertIsNone(self.app._cache_operation)

    def test_initial_confirmation_reserves_before_question_and_cancel_releases_it(self):
        self.app._cache_operation = None
        self.app.is_synthesis_running = lambda: False

        def ask(*_args, **_kwargs):
            self.assertEqual(self.app._cache_operation, "migrate")
            return False

        self.app._ask_yes_no = mock.Mock(side_effect=ask)
        self.app.migrate_cache_to_sqlite()
        self.assertIsNone(self.app._cache_operation)
        self.app._create_wait_popup.assert_not_called()

    def test_initial_folder_change_cancels_before_preparation(self):
        self.app._cache_operation = None
        self.app.is_synthesis_running = lambda: False

        def ask(*_args, **_kwargs):
            self.app.config["cache_dir"] = str(self.cache / "new")
            return True

        self.app._ask_yes_no = mock.Mock(side_effect=ask)
        with mock.patch.object(studio, "prepare_sqlite_cache_migration") as prepare:
            self.app.migrate_cache_to_sqlite()
        prepare.assert_not_called()
        self.app._create_wait_popup.assert_not_called()
        self.assertIsNone(self.app._cache_operation)


if __name__ == "__main__":
    unittest.main()
