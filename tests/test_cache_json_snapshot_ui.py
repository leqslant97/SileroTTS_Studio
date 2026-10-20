"""Explicit JSON snapshots run outside Tk and preserve cache selection."""

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


class CacheJSONSnapshotUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.app = studio.TTSApp.__new__(studio.TTSApp)
        self.app.config = {"cache_dir": str(self.directory)}
        self.app.is_synthesis_running = mock.Mock(return_value=False)
        self.app._begin_cache_operation = mock.Mock(return_value=mock.Mock())
        self.app._end_cache_operation = mock.Mock()
        self.app._close_popup_safely = mock.Mock()
        self.app._show_warning = mock.Mock()
        self.app._show_task_notification = mock.Mock()
        self.app._flush_cache_before_operation = mock.Mock()
        self.completed = threading.Event()
        self.posts = []

        def post(callback, *args):
            self.posts.append((callback, args))
            self.completed.set()

        self.app._post_to_ui = post

    def finish(self):
        self.assertTrue(self.completed.wait(5), "snapshot worker did not finish")
        callback, args = self.posts.pop(0)
        callback(*args)

    def test_synthesis_and_other_cache_operation_block_export(self):
        with mock.patch.object(studio, "export_sqlite_cache_to_json_snapshot") as export:
            self.app.is_synthesis_running.return_value = True
            self.app.update_cache_json_snapshot()
            self.app._show_warning.assert_called_once()
            self.app._begin_cache_operation.assert_not_called()
            self.app.is_synthesis_running.return_value = False
            self.app._begin_cache_operation.return_value = None
            self.app.update_cache_json_snapshot()
            export.assert_not_called()
            self.app._flush_cache_before_operation.assert_not_called()

    def test_export_runs_in_worker_and_finishes_before_notification(self):
        ui_thread = threading.get_ident()
        result = {"path": str(self.directory / "sentence_cache.json"), "record_count": 7}
        order = []
        self.app._end_cache_operation.side_effect = lambda operation: order.append(operation)
        self.app._close_popup_safely.side_effect = lambda popup: order.append("close")
        self.app._show_task_notification.side_effect = lambda *args: order.append("notify")

        def export(directory):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            self.assertEqual(directory, self.directory)
            self.app._flush_cache_before_operation.assert_called_once()
            return result

        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "export_sqlite_cache_to_json_snapshot", side_effect=export):
            self.app.update_cache_json_snapshot()
            self.finish()
        self.assertEqual(order, ["json_export", "close", "notify"])
        self.assertIn("7", self.app._show_task_notification.call_args.args[-1])
        self.assertEqual(self.app.config["cache_dir"], str(self.directory))

    def test_json_backend_is_not_migrated_or_rewritten(self):
        with mock.patch.object(studio, "get_active_cache_backend", return_value="json"), \
                mock.patch.object(studio, "export_sqlite_cache_to_json_snapshot") as export, \
                mock.patch.object(studio, "prepare_sqlite_cache_migration") as migrate:
            self.app.update_cache_json_snapshot()
            self.finish()
        export.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(self.app._show_task_notification.call_args.args[:2],
                         ("info", "Кэш использует JSON"))

    def test_export_failure_releases_controls_and_reports_error(self):
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "export_sqlite_cache_to_json_snapshot",
                                  side_effect=OSError("disk full")), self.assertLogs(level="ERROR"):
            self.app.update_cache_json_snapshot()
            self.finish()
        self.app._end_cache_operation.assert_called_once_with("json_export")
        self.app._close_popup_safely.assert_called_once()
        self.assertEqual(self.app._show_task_notification.call_args.args[0], "error")
        self.assertIn("disk full", self.app._show_task_notification.call_args.args[-1])

    def test_thread_start_failure_releases_reserved_operation(self):
        with mock.patch.object(studio.threading.Thread, "start", side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                self.app.update_cache_json_snapshot()
        self.app._end_cache_operation.assert_called_once_with("json_export")
        self.app._close_popup_safely.assert_called_once()


if __name__ == "__main__":
    unittest.main()
