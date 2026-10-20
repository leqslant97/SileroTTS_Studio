"""A partial GUI page keeps valid matches when its next scan exceeds the remainder."""

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


class CachePageByteBoundaryTests(unittest.TestCase):
    def fixture(self, directory):
        database = directory / "sentence_cache.sqlite"
        with studio.SQLiteCacheIndex(database) as index:
            for number in range(1002):
                text = "needle " + "x" * 800000 if number in {0, 1, 1000} else "unrelated"
                index.upsert(f"key-{number:04d}", {"original_text": text, "usage_count": number})
        studio._write_cache_json_durable(directory / studio._CACHE_BACKEND_MARKER,
            {"schema": 1, "backend": "sqlite", "database": database.name,
             "storage_policy": "confirmed_local", "synchronous": "FULL"})

    def app(self, directory, sort):
        app = object.__new__(studio.TTSApp)
        app.config = {"cache_dir": str(directory)}
        app.root = mock.Mock()
        app._cache_operation = None
        app._cache_state_generation = 0
        app._cache_ui_request_id = 0
        app._update_cache_controls = mock.Mock()
        app._render_cache_table = mock.Mock()
        app._refresh_cache_backend_status = mock.Mock()
        app._show_task_notification = mock.Mock()
        app.lbl_cache_count = mock.Mock()
        app.search_var = mock.Mock(get=mock.Mock(return_value="needle"))
        app.cache_sort_var = mock.Mock(get=mock.Mock(return_value=sort))
        received = threading.Event()
        pending = []
        app._post_to_ui = lambda callback, *args: (pending.append((callback, args)), received.set())
        return app, received, pending

    def finish(self, app, received, pending):
        self.assertTrue(received.wait(10), "cache worker did not complete")
        received.clear()
        callback, args = pending.pop(0)
        callback(*args)
        self.assertIsNone(app._cache_operation)

    def test_later_large_match_preserves_page_and_reappears_on_next_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            self.fixture(directory)
            for sort in ("По хэшу", "Реже используются"):
                with self.subTest(sort=sort):
                    app, received, pending = self.app(directory, sort)
                    app.load_cache_ui()
                    self.finish(app, received, pending)
                    app._show_task_notification.assert_not_called()
                    self.assertEqual(list(app.cache_data), ["key-0000", "key-0001"])
                    self.assertTrue(app._cache_page_has_more)
                    app._request_cache_page(app._cache_page_next_cursor)
                    self.finish(app, received, pending)
                    self.assertEqual(list(app.cache_data), ["key-1000"])
                    app._show_task_notification.assert_not_called()

    def test_operation_popup_failure_releases_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, _received, _pending = self.app(Path(temporary), "По хэшу")
            app._create_wait_popup = mock.Mock(side_effect=RuntimeError("popup failed"))
            with self.assertRaisesRegex(RuntimeError, "popup failed"):
                app._begin_cache_operation("backup", title="Copy", message="Preparing")
            self.assertIsNone(app._cache_operation)


if __name__ == "__main__":
    unittest.main()
