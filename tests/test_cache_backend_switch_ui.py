"""Cache backend controls separate marker checks, snapshots, and switching."""

import importlib.util
import sys
import tempfile
import threading
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


class CacheBackendSwitchUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.app = studio.TTSApp.__new__(studio.TTSApp)
        self.app.config = {"cache_dir": str(self.directory), "use_cache": True}
        self.original_config = dict(self.app.config)
        self.app.settings_vars = {}
        self.app.cache_backend_choice = mock.Mock()
        self.app.cache_backend_choice.get.return_value = "JSON"
        self.app.lbl_cache_backend_status = mock.Mock()
        self.app.lbl_settings_cache_backend = mock.Mock()
        self.app._cache_backend_status_request_id = 0
        self.app._cache_backend_status_directory = None
        self.app._cache_backend_status = None
        self.app._cache_ui_backend = "sqlite"
        self.app._cache_ui_directory = self.directory
        self.app._cache_ui_loaded = True
        self.app._cache_operation = None
        self.app.is_synthesis_running = mock.Mock(return_value=False)
        self.app._begin_cache_operation = mock.Mock(return_value=mock.Mock())
        self.app._end_cache_operation = mock.Mock()
        self.app._close_popup_safely = mock.Mock()
        self.app._show_warning = mock.Mock()
        self.app._show_info = mock.Mock()
        self.app._show_task_notification = mock.Mock()
        self.app._ask_yes_no = mock.Mock(return_value=True)
        self.app._flush_cache_before_operation = mock.Mock()
        self.app._update_cache_controls = mock.Mock()
        self.app._cache_generation_changed = mock.Mock()
        self.app._request_cache_page = mock.Mock()
        self.app.refresh_cache_ui = mock.Mock()
        self.app.refresh_cache_list = mock.Mock()
        self.app.load_cache_ui = mock.Mock()
        self.app.cache_tree = mock.Mock()
        self.app.cache_tree.get_children.return_value = ("stale-record",)
        self.app.lbl_cache_count = mock.Mock()
        self.app.cache_data = {"stale-record": {"original_text": "previous index"}}
        self.app._cache_display_data = dict(self.app.cache_data)
        self.app._shared_cache_dir = self.directory
        self.app._shared_cache = {"stale-record": {"original_text": "previous index"}}
        self.app._shared_processing_statuses = {"previous status": True}
        self.app._shared_cache_lock = threading.RLock()
        self.completed = threading.Event()
        self.posts = []

        def post(callback, *args):
            self.posts.append((callback, args))
            self.completed.set()

        self.app._post_to_ui = post

    def finish(self):
        self.assertTrue(self.completed.wait(5), "cache backend worker did not finish")
        callback, args = self.posts.pop(0)
        self.completed.clear()
        callback(*args)

    def label_text(self, label):
        calls = label.configure.call_args_list + label.config.call_args_list
        self.assertTrue(calls, "backend status was not displayed")
        return calls[-1].kwargs["text"]

    def test_unknown_status_does_not_assume_json_or_read_an_index(self):
        with mock.patch.object(studio, "get_active_cache_backend") as read_marker, \
                mock.patch.object(studio, "acquire_sqlite_cache_index") as open_index, \
                mock.patch.object(studio, "read_cache_index_with_backup") as read_json:
            self.app._refresh_cache_backend_status()
        read_marker.assert_not_called()
        open_index.assert_not_called()
        read_json.assert_not_called()
        self.assertNotIn("JSON", self.label_text(self.app.lbl_cache_backend_status))
        self.assertNotIn("SQLite", self.label_text(self.app.lbl_settings_cache_backend))
        self.app.cache_backend_choice.set.assert_called_once_with("")
        self.assertEqual(self.app.config, self.original_config)

    def test_known_status_updates_both_views_without_changing_saved_settings(self):
        for backend, displayed in (("json", "JSON"), ("sqlite", "SQLite")):
            with self.subTest(backend=backend):
                self.app._refresh_cache_backend_status(backend=backend, cache_dir=self.directory)
                self.assertIn(displayed, self.label_text(self.app.lbl_cache_backend_status))
                self.assertIn(displayed, self.label_text(self.app.lbl_settings_cache_backend))
                self.assertEqual(self.app.config, self.original_config)
        self.assertNotIn("cache_backend", self.app.settings_vars)

    def test_repeated_active_status_does_not_overwrite_pending_format_choice(self):
        for backend, pending in (("json", "SQLite"), ("sqlite", "JSON")):
            with self.subTest(backend=backend, pending=pending):
                selected = {"value": ""}
                self.app.cache_backend_choice.get.side_effect = lambda: selected["value"]
                self.app.cache_backend_choice.set.side_effect = \
                    lambda value: selected.update(value=value)
                self.app._refresh_cache_backend_status(backend, cache_dir=self.directory)
                selected["value"] = pending
                self.app._refresh_cache_backend_status(backend, cache_dir=self.directory)
                self.assertEqual(selected["value"], pending)
                self.assertIn({"json": "JSON", "sqlite": "SQLite"}[backend],
                              self.label_text(self.app.lbl_cache_backend_status))
                self.assertEqual(self.app.config, self.original_config)

    def test_completed_cache_page_preserves_user_pending_backend_choice(self):
        self.app._cache_ui_request_id = 3
        self.app._cache_state_generation = 4
        self.app.search_var = mock.Mock()
        self.app.search_var.get.return_value = ""
        self.app._cache_sort_options = mock.Mock(return_value=("key", False))
        self.app._render_cache_table = mock.Mock()
        for backend, pending in (("json", "SQLite"), ("sqlite", "JSON")):
            with self.subTest(backend=backend, pending=pending):
                selected = {"value": ""}
                self.app.cache_backend_choice.get.side_effect = lambda: selected["value"]
                self.app.cache_backend_choice.set.side_effect = \
                    lambda value: selected.update(value=value)
                self.app._refresh_cache_backend_status(backend, cache_dir=self.directory)
                selected["value"] = pending
                page = {"backend": backend, "data": {}, "cursor": None,
                        "has_more": False, "total": 0,
                        "sort_by": "key", "descending": False}
                self.app._finish_cache_page(3, 4, self.directory, "", None, page)
                self.assertEqual(selected["value"], pending)
                self.assertIn({"json": "JSON", "sqlite": "SQLite"}[backend],
                              self.label_text(self.app.lbl_cache_backend_status))
                self.assertTrue(self.app._cache_ui_loaded)

    def test_choice_changed_during_initial_marker_read_is_preserved_by_callback(self):
        selected = {"value": ""}
        self.app.cache_backend_choice.get.side_effect = lambda: selected["value"]
        self.app.cache_backend_choice.set.side_effect = lambda value: selected.update(value=value)
        self.app._refresh_cache_backend_status()
        entered = threading.Event()
        release = threading.Event()

        def read_marker(directory):
            self.assertEqual(directory, self.directory)
            entered.set()
            self.assertTrue(release.wait(5), "marker read was not released")
            return "json"

        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker):
            self.app.refresh_cache_backend_status()
            self.assertTrue(entered.wait(5), "marker read did not start")
            selected["value"] = "SQLite"
            release.set()
            self.finish()
        self.assertEqual(selected["value"], "SQLite")
        self.assertIn("JSON", self.label_text(self.app.lbl_cache_backend_status))
        self.assertEqual(self.app.config, self.original_config)

    def test_choice_changed_during_new_folder_marker_read_is_not_replaced(self):
        selected = {"value": ""}
        self.app.cache_backend_choice.get.side_effect = lambda: selected["value"]
        self.app.cache_backend_choice.set.side_effect = lambda value: selected.update(value=value)
        self.app._refresh_cache_backend_status("sqlite", cache_dir=self.directory)
        self.app.config["cache_dir"] = str(self.directory / "new-folder")
        entered = threading.Event()
        release = threading.Event()

        def read_marker(directory):
            self.assertEqual(directory, self.directory / "new-folder")
            entered.set()
            self.assertTrue(release.wait(5), "marker read was not released")
            return "sqlite"

        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker):
            self.app.refresh_cache_backend_status()
            self.assertTrue(entered.wait(5), "marker read did not start")
            selected["value"] = "JSON"
            release.set()
            self.finish()
        self.assertEqual(selected["value"], "JSON")
        self.assertIn("SQLite", self.label_text(self.app.lbl_cache_backend_status))

    def test_old_directory_status_cannot_replace_current_folder_status(self):
        other = self.directory / "other"
        self.app._refresh_cache_backend_status(backend="sqlite", cache_dir=self.directory)
        before = self.app.lbl_cache_backend_status.configure.call_count
        before_config = self.app.lbl_cache_backend_status.config.call_count
        self.app._refresh_cache_backend_status(backend="json", cache_dir=other)
        self.assertEqual(self.app.lbl_cache_backend_status.configure.call_count, before)
        self.assertEqual(self.app.lbl_cache_backend_status.config.call_count, before_config)
        self.assertIn("SQLite", self.label_text(self.app.lbl_cache_backend_status))

    def test_status_check_reads_only_marker_outside_ui_thread(self):
        ui_thread = threading.get_ident()

        def read_marker(directory):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(directory, self.directory)
            return "sqlite"

        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker), \
                mock.patch.object(studio, "acquire_sqlite_cache_index") as open_index, \
                mock.patch.object(studio, "read_cache_index_with_backup") as read_json:
            self.app.refresh_cache_backend_status()
            self.finish()
        open_index.assert_not_called()
        read_json.assert_not_called()
        self.app._request_cache_page.assert_not_called()
        self.assertIn("SQLite", self.label_text(self.app.lbl_cache_backend_status))

    def test_older_status_request_does_not_overwrite_newer_result(self):
        self.app._cache_backend_status_request_id = 8
        self.app._finish_cache_backend_status(8, self.directory, "sqlite")
        before = self.app.lbl_cache_backend_status.configure.call_count
        before_config = self.app.lbl_cache_backend_status.config.call_count
        self.app._finish_cache_backend_status(7, self.directory, "json")
        self.assertEqual(self.app.lbl_cache_backend_status.configure.call_count, before)
        self.assertEqual(self.app.lbl_cache_backend_status.config.call_count, before_config)
        self.assertIn("SQLite", self.label_text(self.app.lbl_cache_backend_status))

    def test_completed_marker_read_cannot_describe_a_different_configured_folder(self):
        entered = threading.Event()
        release = threading.Event()

        def read_marker(directory):
            self.assertEqual(directory, self.directory)
            entered.set()
            self.assertTrue(release.wait(5), "marker read was not released")
            return "sqlite"

        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker):
            self.app.refresh_cache_backend_status()
            self.assertTrue(entered.wait(5), "marker read did not start")
            self.app.config["cache_dir"] = str(self.directory / "different")
            self.app._refresh_cache_backend_status()
            before = self.app.lbl_cache_backend_status.configure.call_count
            before_config = self.app.lbl_cache_backend_status.config.call_count
            release.set()
            self.finish()
        self.assertEqual(self.app.lbl_cache_backend_status.configure.call_count, before)
        self.assertEqual(self.app.lbl_cache_backend_status.config.call_count, before_config)
        self.assertNotIn("SQLite", self.label_text(self.app.lbl_cache_backend_status))

    def test_marker_error_cannot_be_displayed_as_successful_json_selection(self):
        self.app._cache_backend_status_request_id = 1
        self.app._finish_cache_backend_status(1, self.directory, None, "invalid backend marker")
        self.assertNotIn("JSON", self.label_text(self.app.lbl_cache_backend_status))
        self.assertNotIn("SQLite", self.label_text(self.app.lbl_settings_cache_backend))
        self.assertEqual(self.app.config, self.original_config)
        self.app._request_cache_page.assert_not_called()

    def test_settings_link_selects_cache_page_without_loading_cache_table(self):
        self.app.notebook = mock.Mock()
        self.app.tab_settings = object()
        self.app.settings_notebook = mock.Mock()
        self.app._cache_settings_page = object()
        self.app._schedule_focus_after_messagebox = mock.Mock()
        self.app.refresh_cache_backend_status = mock.Mock()
        self.app._open_cache_settings()
        self.app.notebook.select.assert_called_once_with(self.app.tab_settings)
        self.app.settings_notebook.select.assert_called_once_with(self.app._cache_settings_page)
        self.app.refresh_cache_backend_status.assert_called_once()
        self.app._request_cache_page.assert_not_called()
        self.app.refresh_cache_ui.assert_not_called()
        self.assertEqual(self.app.config, self.original_config)

    def test_switch_is_blocked_during_synthesis_or_other_cache_operation(self):
        with mock.patch.object(studio, "get_active_cache_backend") as read_marker, \
                mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch:
            self.app.is_synthesis_running.return_value = True
            self.app.apply_cache_backend()
            self.app.is_synthesis_running.return_value = False
            self.app._cache_operation = "archive"
            self.app.apply_cache_backend()
        read_marker.assert_not_called()
        switch.assert_not_called()
        self.app._flush_cache_before_operation.assert_not_called()

    def test_same_backend_is_noop_after_pending_changes_are_flushed(self):
        ui_thread = threading.get_ident()
        events = []
        self.app._flush_cache_before_operation.side_effect = lambda: events.append("flush")
        self.app.migrate_cache_to_sqlite = mock.Mock()

        def read_marker(directory):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(directory, self.directory)
            events.append("read")
            return "json"

        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch, \
                mock.patch.object(studio, "prepare_sqlite_cache_migration") as migrate:
            self.app.apply_cache_backend()
            self.finish()
        self.assertEqual(events, ["flush", "read"])
        switch.assert_not_called()
        migrate.assert_not_called()
        self.app.migrate_cache_to_sqlite.assert_not_called()
        self.assertEqual(self.app.config, self.original_config)

    def test_json_switch_requires_confirmation_on_ui_thread(self):
        ui_thread = threading.get_ident()
        self.app._ask_yes_no.return_value = False

        def decline(*args, **kwargs):
            self.assertEqual(threading.get_ident(), ui_thread)
            return False

        self.app._ask_yes_no.side_effect = decline
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch:
            self.app.apply_cache_backend()
            self.finish()
        self.app._ask_yes_no.assert_called_once()
        switch.assert_not_called()
        self.app._end_cache_operation.assert_called()
        self.assertEqual(self.app.config, self.original_config)

    def test_confirmed_json_switch_runs_outside_tk_after_marker_and_flush(self):
        ui_thread = threading.get_ident()
        order = []
        self.app._flush_cache_before_operation.side_effect = lambda: order.append("flush")

        def read_marker(directory):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(directory, self.directory)
            order.append("read")
            return "sqlite"

        def confirm(*args, **kwargs):
            self.assertEqual(threading.get_ident(), ui_thread)
            order.append("confirm")
            return True

        def switch(directory):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(directory, self.directory)
            order.append("switch")
            return {"backend": "json", "record_count": 2,
                    "path": str(self.directory / "sentence_cache.json")}

        self.app._ask_yes_no.side_effect = confirm
        with mock.patch.object(studio, "get_active_cache_backend", side_effect=read_marker), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json", side_effect=switch):
            self.app.apply_cache_backend()
            self.finish()
            self.finish()
        self.assertLess(order.index("flush"), order.index("read"))
        self.assertLess(order.index("read"), order.index("confirm"))
        self.assertLess(order.index("confirm"), order.index("switch"))
        self.assertEqual(self.app.config, self.original_config)
        self.assertIn("JSON", self.label_text(self.app.lbl_cache_backend_status))

    def test_sqlite_choice_enters_verified_migration_without_reverse_switch(self):
        self.app.cache_backend_choice.get.return_value = "SQLite"
        self.app.migrate_cache_to_sqlite = mock.Mock()

        def migrate():
            self.app._end_cache_operation.assert_called()

        self.app.migrate_cache_to_sqlite.side_effect = migrate
        with mock.patch.object(studio, "get_active_cache_backend", return_value="json"), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch:
            self.app.apply_cache_backend()
            self.finish()
        self.app.migrate_cache_to_sqlite.assert_called_once()
        switch.assert_not_called()
        self.assertEqual(self.app.config, self.original_config)

    def test_folder_changed_before_confirmation_never_switches_either_folder(self):
        self.app.migrate_cache_to_sqlite = mock.Mock()
        self.app.config["cache_dir"] = str(self.directory / "different")
        with mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch:
            self.app._finish_cache_backend_choice(object(), self.directory, "json", "sqlite")
        switch.assert_not_called()
        self.app.migrate_cache_to_sqlite.assert_not_called()
        self.app._ask_yes_no.assert_not_called()
        self.app._end_cache_operation.assert_called_once_with("backend_switch")
        self.app._close_popup_safely.assert_called_once()

    def test_marker_check_failure_does_not_attempt_switch_or_discard_cached_data(self):
        previous_cache = self.app._shared_cache
        previous_data = self.app.cache_data
        with mock.patch.object(studio, "get_active_cache_backend",
                               side_effect=OSError("marker inaccessible")), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json") as switch, \
                self.assertLogs(level="ERROR"):
            self.app.apply_cache_backend()
            self.finish()
        switch.assert_not_called()
        self.app._ask_yes_no.assert_not_called()
        self.assertIs(self.app._shared_cache, previous_cache)
        self.assertIs(self.app.cache_data, previous_data)
        self.app._end_cache_operation.assert_called_once_with("backend_switch")
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")

    def test_switch_failure_preserves_previous_view_and_releases_controls(self):
        previous_cache = self.app._shared_cache
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "switch_sqlite_cache_to_json",
                                  side_effect=OSError("backup disk full")), \
                self.assertLogs(level="ERROR"):
            self.app.apply_cache_backend()
            self.finish()
            self.finish()
        self.assertIs(self.app._shared_cache, previous_cache)
        self.app._end_cache_operation.assert_called_once_with("backend_switch")
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")
        self.assertIn("backup disk full", self.app._show_task_notification.call_args.args[-1])

    def test_preliminary_worker_start_failure_releases_reserved_operation(self):
        with mock.patch.object(studio.threading.Thread, "start",
                               side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                self.app.apply_cache_backend()
        self.app._end_cache_operation.assert_called_once_with("backend_switch")
        self.app._close_popup_safely.assert_called_once()

    def test_confirmed_worker_start_failure_releases_reserved_operation(self):
        with mock.patch.object(studio.threading.Thread, "start",
                               side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                self.app._finish_cache_backend_choice(object(), self.directory, "json", "sqlite")
        self.app._end_cache_operation.assert_called_once_with("backend_switch")
        self.app._close_popup_safely.assert_called_once()

    def test_failed_switch_releases_operation_before_reporting_without_forgetting_cache(self):
        order = []
        self.app._end_cache_operation.side_effect = lambda operation: order.append("end")
        self.app._close_popup_safely.side_effect = lambda popup: order.append("close")
        self.app._show_task_notification.side_effect = lambda *args: order.append("notify")
        previous_cache = self.app._shared_cache
        previous_data = self.app.cache_data
        self.app._finish_cache_backend_switch(object(), self.directory, "json", "disk full")
        self.assertEqual(order, ["end", "close", "notify"])
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")
        self.assertIn("disk full", self.app._show_task_notification.call_args.args[-1])
        self.assertIs(self.app._shared_cache, previous_cache)
        self.assertIs(self.app.cache_data, previous_data)
        self.app.load_cache_ui.assert_not_called()

    def test_successful_switch_forgets_resident_previous_backend_before_notification(self):
        order = []
        self.app._end_cache_operation.side_effect = lambda operation: order.append("end")
        self.app._close_popup_safely.side_effect = lambda popup: order.append("close")

        def notify(*args):
            self.assertIsNone(self.app._shared_cache)
            self.assertIsNone(self.app._shared_processing_statuses)
            order.append("notify")

        self.app._show_task_notification.side_effect = notify
        self.app._finish_cache_backend_switch(object(), self.directory, "json")
        self.assertEqual(order[:2], ["end", "close"])
        self.assertEqual(order[-1], "notify")
        self.assertIsNone(self.app._shared_cache_dir)
        self.assertEqual(self.app.config, self.original_config)
        self.assertIn("JSON", self.label_text(self.app.lbl_cache_backend_status))

    def test_completed_switch_in_previous_folder_does_not_reload_current_folder_table(self):
        other = self.directory / "other"
        self.app._refresh_cache_backend_status("sqlite", cache_dir=self.directory)
        before = self.app.lbl_cache_backend_status.configure.call_count
        before_config = self.app.lbl_cache_backend_status.config.call_count
        self.app._finish_cache_backend_switch(object(), other, "json")
        self.app.load_cache_ui.assert_not_called()
        self.app._request_cache_page.assert_not_called()
        self.assertEqual(self.app.lbl_cache_backend_status.configure.call_count, before)
        self.assertEqual(self.app.lbl_cache_backend_status.config.call_count, before_config)

    def test_new_backend_invalidates_already_running_status_for_same_folder(self):
        self.app._cache_backend_status_request_id = 9
        self.app._finish_cache_backend_switch(object(), self.directory, "json")
        self.app._finish_cache_backend_status(9, self.directory, "sqlite")
        self.assertIn("JSON", self.label_text(self.app.lbl_cache_backend_status))


if __name__ == "__main__":
    unittest.main()
