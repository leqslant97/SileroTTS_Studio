"""Explicit backups protect destructive cache actions without automatic copies."""

import copy
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


class CacheBackupPolicyUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.cache = self.directory / "cache"
        self.cache.mkdir()
        self.app = object.__new__(studio.TTSApp)
        self.app.root = mock.Mock()
        self.app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.app.config["cache_dir"] = str(self.cache)
        self.app.is_synthesis_running = mock.Mock(return_value=False)
        self.app._cache_operation = None
        self.app._cache_state_generation = 0
        self.app._cache_ui_loaded = False
        self.app._update_cache_controls = mock.Mock()
        self.app._create_wait_popup = mock.Mock(return_value=mock.Mock())
        self.app._close_popup_safely = mock.Mock()
        self.app._show_info = mock.Mock()
        self.app._show_warning = mock.Mock()
        self.app._show_error = mock.Mock()
        self.app._show_task_notification = mock.Mock()
        self.app._flush_cache_before_operation = mock.Mock()
        self.app._confirm_cache_mutation = mock.Mock(return_value=False)
        self.app._post_to_ui = lambda callback, *args, **kwargs: callback(*args, **kwargs)
        self.app._cache_selection_matches_directory = mock.Mock(return_value=True)
        self.app.cache_tree = mock.Mock()
        self.app.cache_tree.selection.return_value = ("selected",)

    def mutation_dialog(self, choice, *, requested=False, selected=""):
        controls = {}
        dialog = mock.Mock()
        dialog.winfo_reqheight.return_value = 260
        value = mock.Mock()
        value.get.return_value = requested

        def button(_parent, **kwargs):
            controls[kwargs["text"]] = kwargs["command"]
            return mock.Mock()

        self.app.root.wait_window.side_effect = lambda _dialog: controls[choice]()
        self.app.get_status_color = lambda _status: "gray"
        self.app._center_popup = mock.Mock()
        with (
            mock.patch.object(studio.tk, "Toplevel", return_value=dialog),
            mock.patch.object(studio.tk, "BooleanVar", return_value=value) as variable,
            mock.patch.object(studio.ttk, "Label"),
            mock.patch.object(studio.ttk, "Checkbutton"),
            mock.patch.object(studio.ttk, "Frame"),
            mock.patch.object(studio.ttk, "Button", side_effect=button),
            mock.patch.object(studio.filedialog, "asksaveasfilename", return_value=selected) as picker,
        ):
            result = studio.TTSApp._confirm_cache_mutation(self.app, "Изменение", "Описание", self.cache)
            self.assertFalse(variable.call_args.kwargs["value"])
            return result, picker.call_count

    def test_confirmation_default_off_and_cancel_never_open_picker(self):
        self.assertEqual(self.mutation_dialog("Продолжить"), (False, 0))
        self.assertEqual(self.mutation_dialog("Отмена", requested=True), (None, 0))

    def test_confirmation_zip_path_and_picker_cancel_and_internal_path(self):
        path = self.directory / "backup.zip"
        self.assertEqual(self.mutation_dialog("Продолжить", requested=True, selected=str(path)), (path, 1))
        self.assertEqual(self.mutation_dialog("Продолжить", requested=True), (None, 1))
        self.assertEqual(self.mutation_dialog("Продолжить", requested=True,
                                             selected=str(self.cache / "unsafe.zip")), (None, 1))
        self.app._show_error.assert_called_once()

    def test_operation_is_reserved_during_confirmation_and_cancel_releases_it(self):
        def confirm(*_args):
            self.assertEqual(self.app._cache_operation, "delete")
            self.assertTrue(self.app.is_cache_operation_running())
            return None

        self.app._confirm_cache_mutation.side_effect = confirm
        self.assertIsNone(self.app._begin_confirmed_cache_mutation(
            "delete", "Удалить", "Описание", self.cache, "Ожидание",
        ))
        self.assertIsNone(self.app._cache_operation)
        self.app._create_wait_popup.assert_not_called()

    def test_confirmation_exception_releases_operation(self):
        self.app._confirm_cache_mutation.side_effect = RuntimeError("dialog failed")
        with self.assertRaisesRegex(RuntimeError, "dialog failed"):
            self.app._begin_confirmed_cache_mutation("delete", "Удалить", "Описание", self.cache, "Ожидание")
        self.assertIsNone(self.app._cache_operation)

    def test_manual_backup_asks_external_path_and_captures_source_folder(self):
        path = self.directory / "manual.sqlite"

        def picker(**kwargs):
            self.assertEqual(self.app._cache_operation, "backup")
            self.assertEqual(kwargs["defaultextension"], ".sqlite")
            self.app.config["cache_dir"] = str(self.directory / "other")
            return str(path)

        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"),
            mock.patch.object(studio.filedialog, "asksaveasfilename", side_effect=picker),
            mock.patch.object(studio, "create_cache_index_backup", return_value=path) as backup,
        ):
            self.app.backup_cache_index()
        backup.assert_called_once_with(self.cache, "manual", destination=path)
        self.app._flush_cache_before_operation.assert_called_once()
        self.assertIsNone(self.app._cache_operation)

    def test_manual_picker_cancel_or_internal_path_does_not_copy(self):
        for selected in ("", str(self.cache / "copy.json")):
            with (
                self.subTest(selected=selected),
                mock.patch.object(studio.threading, "Thread", ImmediateThread),
                mock.patch.object(studio, "get_active_cache_backend", return_value="json"),
                mock.patch.object(studio.filedialog, "asksaveasfilename", return_value=selected),
                mock.patch.object(studio, "create_cache_index_backup") as backup,
            ):
                self.app.backup_cache_index()
                backup.assert_not_called()
                self.assertIsNone(self.app._cache_operation)

    def test_manual_backup_failure_releases_operation(self):
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "get_active_cache_backend", return_value="json"),
            mock.patch.object(studio.filedialog, "asksaveasfilename", return_value=str(self.directory / "copy.json")),
            mock.patch.object(studio, "create_cache_index_backup", side_effect=OSError("disk full")),
            self.assertLogs(level="ERROR"),
        ):
            self.app.backup_cache_index()
        self.assertIsNone(self.app._cache_operation)
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")

    def test_manual_backend_change_during_picker_requires_retry(self):
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "get_active_cache_backend", side_effect=["sqlite", "json"]),
            mock.patch.object(studio.filedialog, "asksaveasfilename", return_value=str(self.directory / "copy.sqlite")),
            mock.patch.object(studio, "create_cache_index_backup") as backup,
            self.assertLogs(level="ERROR"),
        ):
            self.app.backup_cache_index()
        backup.assert_not_called()
        self.assertIsNone(self.app._cache_operation)
        self.assertIn("Формат индекса изменился", self.app._show_task_notification.call_args.args[-1])

    def test_delete_without_zip_has_no_automatic_backup(self):
        self.app._finish_sqlite_cache_delete = mock.Mock()
        self.app._delete_sqlite_cache_rows = mock.Mock()
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"),
            mock.patch.object(studio, "create_cache_archive") as archive,
            mock.patch.object(studio, "create_cache_index_backup") as backup,
        ):
            self.app.delete_selected_cache()
        archive.assert_not_called()
        backup.assert_not_called()
        self.app._delete_sqlite_cache_rows.assert_called_once_with(self.cache, ("selected",))

    def test_delete_zip_is_created_after_flush_and_before_removal(self):
        self.app._confirm_cache_mutation.return_value = self.directory / "backup.zip"
        self.app._finish_sqlite_cache_delete = mock.Mock()
        events = []
        self.app._flush_cache_before_operation.side_effect = lambda: events.append("flush")
        self.app._delete_sqlite_cache_rows = mock.Mock(side_effect=lambda *_args: events.append("delete"))
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"),
            mock.patch.object(studio, "create_cache_archive", side_effect=lambda *_args: events.append("zip")),
        ):
            self.app.delete_selected_cache()
        self.assertEqual(events, ["flush", "zip", "delete"])

    def test_delete_zip_failure_does_not_remove_records(self):
        self.app._confirm_cache_mutation.return_value = self.directory / "backup.zip"
        self.app._delete_sqlite_cache_rows = mock.Mock()
        self.app._finish_cache_delete = mock.Mock()
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "create_cache_archive", side_effect=OSError("zip failed")),
            self.assertLogs(level="ERROR"),
        ):
            self.app.delete_selected_cache()
        self.app._delete_sqlite_cache_rows.assert_not_called()
        self.assertIn("zip failed", self.app._finish_cache_delete.call_args.args[-1])

    def test_clear_zip_failure_preserves_view_and_shared_snapshot(self):
        self.app._confirm_cache_mutation.return_value = self.directory / "backup.zip"
        self.app._invalidate_cleared_cache_memory = mock.Mock()
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "create_cache_archive", side_effect=OSError("zip failed")),
            mock.patch.object(studio, "clear_cache_storage") as clear,
            self.assertLogs(level="ERROR"),
        ):
            self.app.clear_entire_cache()
        clear.assert_not_called()
        self.app._invalidate_cleared_cache_memory.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_partial_clear_discards_shared_memory_and_view(self):
        self.app._shared_cache_dir = self.cache
        self.app._shared_cache = {"stale": {}}
        self.app._shared_processing_statuses = {"stale": True}
        self.app._cache_generation_changed = mock.Mock()
        self.app._clear_cache_view = mock.Mock()
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio, "clear_cache_storage", side_effect=OSError("partial deletion")),
            mock.patch.object(studio, "create_cache_index_backup") as backup,
            self.assertLogs(level="ERROR"),
        ):
            self.app.clear_entire_cache()
        backup.assert_not_called()
        self.assertIsNone(self.app._shared_cache)
        self.app._clear_cache_view.assert_called_once()
        self.app._cache_generation_changed.assert_called_once()
        self.assertFalse(self.app._cache_ui_loaded)

    def test_old_clear_completion_does_not_touch_new_directory_view(self):
        self.app._cache_generation_changed = mock.Mock()
        self.app._clear_cache_view = mock.Mock()
        self.app._shared_cache_dir = self.directory / "other"
        self.app._shared_cache = {"new": {}}
        self.app._finish_cache_clear(mock.Mock(), "partial deletion", cache_dir=self.directory / "old")
        self.app._cache_generation_changed.assert_not_called()
        self.app._clear_cache_view.assert_not_called()
        self.assertEqual(self.app._shared_cache, {"new": {}})

    def test_archive_then_partial_clear_invalidates_view_without_extra_index_copy(self):
        self.app.del_after_zip = mock.Mock(get=lambda: True)
        self.app._invalidate_cleared_cache_memory = mock.Mock()
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio.filedialog, "asksaveasfilename", return_value=str(self.directory / "cache.zip")),
            mock.patch.object(studio, "create_cache_archive", return_value=self.directory / "cache.zip"),
            mock.patch.object(studio, "clear_cache_storage", side_effect=OSError("partial deletion")),
            mock.patch.object(studio, "create_cache_index_backup") as backup,
            self.assertLogs(level="ERROR"),
        ):
            self.app.archive_cache()
        backup.assert_not_called()
        self.app._invalidate_cleared_cache_memory.assert_called_once_with(self.cache)
        self.assertIsNone(self.app._cache_operation)

    def test_archive_picker_reserves_operation_and_cancel_releases_it(self):
        self.app.del_after_zip = mock.Mock(get=lambda: False)

        def picker(**_kwargs):
            self.assertEqual(self.app._cache_operation, "archive")
            return ""

        with (
            mock.patch.object(studio.filedialog, "asksaveasfilename", side_effect=picker),
            mock.patch.object(studio, "create_cache_archive") as archive,
        ):
            self.app.archive_cache()
        archive.assert_not_called()
        self.assertIsNone(self.app._cache_operation)

    def test_transcode_zip_failure_cannot_start_encoder(self):
        (self.cache / "sentence_cache.json").write_text("{}", encoding="utf-8")
        self.app._confirm_cache_mutation.return_value = self.directory / "backup.zip"
        self.app._center_popup = mock.Mock()
        self.app._finish_cache_transcode = mock.Mock()
        self.app._create_wait_popup.return_value.winfo_children.return_value = []
        with (
            mock.patch.object(studio.threading, "Thread", ImmediateThread),
            mock.patch.object(studio.ttk, "Button"),
            mock.patch.object(studio, "create_cache_archive", side_effect=OSError("zip failed")),
            mock.patch.object(studio, "transcode_cache_entries_to_opus") as encoder,
            mock.patch.object(studio, "transcode_sqlite_cache_entries_to_opus") as sqlite_encoder,
            self.assertLogs(level="ERROR"),
        ):
            self.app.transcode_cache_to_opus()
        encoder.assert_not_called()
        sqlite_encoder.assert_not_called()
        self.assertIn("zip failed", self.app._finish_cache_transcode.call_args.args[-1])

    def test_old_transcode_result_does_not_replace_current_table(self):
        self.app.cache_data = {"new": {}}
        self.app._cache_generation_changed = mock.Mock()
        self.app.filter_cache = mock.Mock()
        self.app._apply_transcoded_json_snapshot(self.directory / "other", {"old": {}})
        self.assertEqual(self.app.cache_data, {"new": {}})
        self.app._cache_generation_changed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
