"""Bounded cache pages and stale GUI results without HTTP or user settings."""

import ast
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteCacheUITests(unittest.TestCase):
    def test_cache_card_and_table_text_preserve_current_and_legacy_strings(self):
        text = studio.TTSApp._cache_record_text
        self.assertEqual(text({"raw_text": "Исходная фраза"}, "original_text"), "Исходная фраза")
        self.assertEqual(text({"norm_text": "Подготовленная фраза"}, "normalized_text"), "Подготовленная фраза")
        self.assertEqual(text({"original_text": "", "raw_text": "Другой текст"}, "original_text"), "")
        self.assertEqual(text({"normalized_text": None, "norm_text": "Ранний текст"}, "normalized_text"), "Ранний текст")
        self.assertEqual(text({"normalized_text": {}, "norm_text": 42}, "normalized_text"), "")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache_dir = Path(temporary.name).resolve()
        app = self.app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = {"cache_dir": str(self.cache_dir)}
        app.root = mock.Mock()
        app._cache_operation = None
        app._cache_ui_request_id = 0
        app._cache_state_generation = 0
        app._cache_ui_loaded = False
        app.search_var = mock.Mock()
        app.search_var.get.return_value = ""
        app.cache_sort_var = mock.Mock()
        app.cache_sort_var.get.return_value = "По хэшу"
        app.lbl_cache_count = mock.Mock()
        app.cache_tree = mock.Mock()
        app.cache_tree.get_children.return_value = ()
        app._show_task_notification = mock.Mock()
        app._show_warning = mock.Mock()
        self.posts = []
        app._post_to_ui = lambda callback, *args: self.posts.append((callback, args))

    def complete(self):
        deadline = time.monotonic() + 5
        while not self.posts and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(self.posts, "worker did not post a result")
        callback, args = self.posts.pop(0)
        callback(*args)

    def result(self, **kwargs):
        result = {"backend": "sqlite", "data": {"a": {"original_text": "А"}},
                  "total": 301, "cursor": "a", "has_more": True}
        result.update(kwargs)
        return result

    def test_search_while_first_page_loads_restarts_worker_query(self):
        self.app._cache_operation = "load"
        self.app._request_cache_page = mock.Mock()
        self.app._render_cache_table = mock.Mock()
        self.app.filter_cache()
        self.app._request_cache_page.assert_called_once_with(reset_history=True)
        self.app._render_cache_table.assert_not_called()

    def test_search_and_sort_before_refresh_do_not_load_cache(self):
        self.app._request_cache_page = mock.Mock()
        self.app.search_var.get.return_value = "пока не искать"
        self.app.cache_sort_var.get.return_value = "Чаще используются"
        self.app.filter_cache()
        self.app._request_cache_page.assert_not_called()
        self.assertEqual(self.app._cache_sort_options(), ("usage_count", True))

    def test_sort_selection_is_sent_to_worker_and_old_sort_result_is_ignored(self):
        self.app.cache_sort_var.get.return_value = "Чаще используются"
        index = mock.Mock()
        index.query_page.return_value = {
            "rows": [("late", {"usage_count": 300})],
            "cursor": ("usage_count", True, 300.0, "late"),
            "has_more": False, "bytes": 30, "scanned": 1,
        }
        index.count.return_value = 1200
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
             mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
             mock.patch.object(studio, "release_sqlite_cache_index"):
            self.app.load_cache_ui()
            self.complete()
        self.assertEqual(index.query_page.call_args.kwargs["sort_by"], "usage_count")
        self.assertTrue(index.query_page.call_args.kwargs["descending"])
        self.assertEqual(self.app._cache_page_next_cursor, ("usage_count", True, 300.0, "late"))
        self.app.cache_sort_var.get.return_value = "Сначала новые"
        self.app._finish_cache_page(self.app._cache_ui_request_id, 0, self.cache_dir, "", None,
                                   self.result(sort_by="usage_count", descending=True))
        self.assertEqual(list(self.app.cache_data), ["late"])

    def test_navigation_after_sort_change_resets_cursor_instead_of_using_old_order(self):
        self.app._cache_page_has_more = True
        self.app._cache_page_sort = ("key", False)
        self.app._cache_page_next_cursor = "old-key"
        self.app._request_cache_page = mock.Mock()
        self.app.cache_sort_var.get.return_value = "Сначала новые"
        self.app._cache_page_forward()
        self.app._request_cache_page.assert_called_once_with(reset_history=True)

    def test_loaded_json_search_and_global_sort_run_in_worker_without_rereading_disk(self):
        self.app._cache_ui_loaded = True
        self.app._cache_ui_backend = "json"
        self.app.cache_sort_var.get.return_value = "Чаще используются"
        self.app.search_var.get.return_value = "needle"
        records = {f"key-{number:04}": {"raw_text": "старый исходный", "norm_text": "needle",
                                        "usage_count": number} for number in range(1400)}
        self.app.cache_data = records
        owner = threading.get_ident()
        observed_threads = set()
        original_matches = studio.cache_record_matches_search

        def matches(*args):
            observed_threads.add(threading.get_ident())
            return original_matches(*args)

        with mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("no disk read")), \
             mock.patch.object(studio, "get_active_cache_backend", side_effect=AssertionError("resident JSON")), \
             mock.patch.object(studio, "cache_record_matches_search", side_effect=matches):
            self.app.filter_cache()
            deadline = time.monotonic() + 5
            while not self.posts and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(self.posts)
            self.assertNotIn(owner, observed_threads)
            self.complete()
        self.assertIs(self.app.cache_data, records)
        self.assertEqual(len(self.app._cache_display_data), 1000)
        self.assertEqual(list(self.app._cache_display_data)[:2], ["key-1399", "key-1398"])
        self.assertEqual(self.app.cache_tree.insert.call_count, 1000)

    def test_search_after_directory_change_reads_new_backend(self):
        self.app._cache_ui_loaded = True
        self.app._cache_ui_directory = self.cache_dir / "old"
        self.app._request_cache_page = mock.Mock()
        self.app.filter_cache()
        self.app._request_cache_page.assert_called_once_with(reset_history=True)

    def test_page_toolbar_is_owned_by_cache_tab(self):
        source = (Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py").read_text()
        tree = ast.parse(source)
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSApp")
        def assigns_button(node):
            return any(isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store)
                       and item.attr == "btn_cache_page_first" for item in ast.walk(node))
        methods = [node.name for node in app.body if isinstance(node, ast.FunctionDef) and assigns_button(node)]
        self.assertEqual(methods, ["setup_cache_tab"])

    def test_page_sql_runs_off_main_thread_and_keeps_only_current_rows(self):
        owner = threading.get_ident()
        index = mock.Mock()
        calls = []
        def page(**kwargs):
            calls.append((threading.get_ident(), kwargs))
            return {"rows": [(f"key-{number:04}", {"original_text": "Глава"}) for number in range(1000)],
                    "cursor": "key-0999", "has_more": True, "bytes": 50000, "scanned": 1000}
        index.query_page.side_effect = page
        index.count.return_value = 100_000
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
             mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
             mock.patch.object(studio, "release_sqlite_cache_index") as release:
            self.app.load_cache_ui()
            self.complete()
        self.assertEqual(len(self.app.cache_data), 1000)
        self.assertEqual(self.app._cache_page_total, 100_000)
        self.assertTrue(all(thread != owner for thread, _ in calls))
        self.assertEqual(calls[0][1]["max_scan_rows"], 1000)
        self.assertEqual(self.app.cache_tree.insert.call_count, 1000)
        self.assertEqual(calls[0][1]["limit"], 1000)
        self.assertEqual(calls[0][1]["max_bytes"], 2 * 1024 * 1024)
        release.assert_called_once_with(index)

    def test_byte_limited_page_does_not_retry_with_too_small_remainder(self):
        index = mock.Mock()
        index.count.return_value = 300
        index.query_page.return_value = {
            "rows": [("a", {"original_text": "large"})], "cursor": "a",
            "has_more": True, "bytes": 1600000, "scanned": 1, "stop_reason": "byte_limit",
        }
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
             mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
             mock.patch.object(studio, "release_sqlite_cache_index"):
            self.app.load_cache_ui()
            self.complete()
        index.query_page.assert_called_once()
        self.assertTrue(self.app._cache_page_has_more)
        self.assertEqual(list(self.app.cache_data), ["a"])

    def test_sparse_search_continues_empty_scanned_pages_then_finds_row(self):
        self.app.search_var.get.return_value = "needle"
        index = mock.Mock()
        index.count.return_value = 9000
        index.query_page.side_effect = [
            {"rows": [], "cursor": "k0999", "has_more": True, "bytes": 0, "scanned": 1000},
            {"rows": [], "cursor": "k1999", "has_more": True, "bytes": 0, "scanned": 1000},
            {"rows": [("k2000", {"original_text": "needle"})], "cursor": "k2000", "has_more": False, "bytes": 40, "scanned": 1},
        ]
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
             mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
             mock.patch.object(studio, "release_sqlite_cache_index"):
            self.app.load_cache_ui()
            self.complete()
        self.assertEqual(list(self.app.cache_data), ["k2000"])
        self.assertEqual([call.kwargs["cursor"] for call in index.query_page.call_args_list], [None, "k0999", "k1999"])

    def test_stale_request_query_directory_and_generation_do_not_replace_table(self):
        self.app.cache_data = {"current": {}}
        self.app._cache_ui_request_id = 2
        self.app._cache_state_generation = 3
        self.app._cache_operation = "load"
        self.app.search_var.get.return_value = "new"
        variations = [(1, 3, self.cache_dir, "new"), (2, 2, self.cache_dir, "new"),
                      (2, 3, self.cache_dir / "old", "new"), (2, 3, self.cache_dir, "old")]
        for request, generation, directory, query in variations:
            self.app._finish_cache_page(request, generation, directory, query, None, self.result())
            self.assertEqual(self.app.cache_data, {"current": {}})
            self.assertEqual(self.app._cache_operation, "load" if request != 2 else None)
            self.app._cache_operation = "load"
        self.app._finish_cache_page(2, 3, self.cache_dir, "new", None, self.result())
        self.assertEqual(list(self.app.cache_data), ["a"])
        self.assertIsNone(self.app._cache_operation)

    def test_page_navigation_uses_saved_keyset_cursors(self):
        self.app._cache_page_history = [None]
        self.app._cache_page_number = 0
        self.app._cache_page_has_more = True
        self.app._cache_page_next_cursor = "key-099"
        self.app._request_cache_page = mock.Mock()
        self.app._cache_page_forward()
        self.app._cache_page_next_cursor = "key-199"
        self.app._cache_page_forward()
        self.app._cache_page_back()
        self.assertEqual(self.app._request_cache_page.call_args_list,
                         [mock.call("key-099"), mock.call("key-199"), mock.call("key-099")])
        self.assertEqual(self.app._cache_page_history, [None, "key-099", "key-199"])

    def test_cursor_history_is_bounded_even_for_deep_pagination(self):
        self.app._cache_page_history = [None]
        self.app._cache_page_number = 0
        self.app._cache_page_has_more = True
        self.app._request_cache_page = mock.Mock()
        for number in range(300):
            self.app._cache_page_next_cursor = f"key-{number}"
            self.app._cache_page_forward()
        self.assertEqual(len(self.app._cache_page_history), 100)
        self.assertEqual(self.app._cache_page_base + self.app._cache_page_number, 300)

    def test_release_failure_posts_error_and_unblocks_loading(self):
        index = mock.Mock()
        index.query_page.return_value = {"rows": [], "cursor": None, "has_more": False, "bytes": 0, "scanned": 0}
        index.count.return_value = 0
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
             mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
             mock.patch.object(studio, "release_sqlite_cache_index", side_effect=OSError("flush failed")):
            with self.assertLogs(level="ERROR"):
                self.app.load_cache_ui()
                self.complete()
        self.assertIsNone(self.app._cache_operation)
        self.assertFalse(self.app._cache_ui_loaded)
        self.assertIn("flush failed", self.app._show_task_notification.call_args.args[-1])

    def test_generation_cancels_old_query_and_resets_navigation(self):
        cancellation = self.app._cache_query_cancel = threading.Event()
        self.app._cache_page_history = [None, "old"]
        self.app._cache_page_number = 1
        self.app._cache_generation_changed()
        self.assertTrue(cancellation.is_set())
        self.assertEqual(self.app._cache_page_history, [None])
        self.assertEqual(self.app._cache_page_number, 0)

    def test_manual_cache_path_change_cancels_query_and_clears_without_disk_read(self):
        self.app._cache_query_cancel = threading.Event()
        self.app._cache_operation = "load"
        self.app.cache_data = {"old-key": {}}
        self.app.cache_tree.get_children.return_value = ("old-key",)
        self.app.load_cache_ui = mock.Mock()
        variable = mock.Mock()
        variable.get.return_value = str(self.cache_dir / "new")
        self.app._path_var_changed("cache_dir", variable)
        self.assertTrue(self.app._cache_query_cancel.is_set())
        self.assertIsNone(self.app._cache_operation)
        self.assertEqual(self.app.cache_data, {})
        self.assertFalse(self.app._cache_ui_loaded)
        self.app.load_cache_ui.assert_not_called()
        generation = self.app._cache_state_generation
        self.app._path_var_changed("cache_dir", variable)
        self.assertEqual(self.app._cache_state_generation, generation)

    def test_old_optimization_and_clear_result_do_not_replace_new_directory(self):
        self.app.cache_data = {"new-key": {}}
        self.app._close_popup_safely = mock.Mock()
        self.app._cache_ui_loaded = True
        self.app._finish_cache_optimization(mock.Mock(), {"old-key": {}}, cache_dir=self.cache_dir / "old")
        self.assertEqual(self.app.cache_data, {"new-key": {}})
        self.app._finish_cache_clear(mock.Mock(), cache_dir=self.cache_dir / "old")
        self.assertEqual(self.app.cache_data, {"new-key": {}})

    def test_old_directory_selection_is_cleared_before_any_delete(self):
        self.app._cache_ui_directory = self.cache_dir / "old"
        self.app.cache_data = {"old-key": {"file_name": "old.ogg"}}
        self.app.cache_tree.selection.return_value = ("old-key",)
        self.app.is_synthesis_running = mock.Mock(return_value=False)
        self.app.load_cache_ui = mock.Mock()
        self.app._ask_yes_no = mock.Mock(return_value=True)
        self.app.delete_selected_cache()
        self.app._ask_yes_no.assert_not_called()
        self.assertEqual(self.app.cache_data, {})
        self.app.load_cache_ui.assert_called_once()

    def test_card_result_from_old_directory_is_ignored(self):
        self.app._show_cache_record = mock.Mock()
        self.app._finish_cache_record(self.cache_dir / "old", 0, 0, "a", {})
        self.app._show_cache_record.assert_not_called()
        self.app._finish_cache_record(self.cache_dir, 0, 0, "a", {"speaker": "eugene"})
        self.app._show_cache_record.assert_called_once_with("a", {"speaker": "eugene"})

    def test_delete_commits_before_audio_gc_and_preserves_shared_reference(self):
        audio = self.cache_dir / "audio"
        audio.mkdir()
        unique = audio / "unique.ogg"
        unique.write_bytes(b"unique")
        shared = audio / "shared.ogg"
        shared.write_bytes(b"shared")
        index = mock.Mock()
        index.get.side_effect = lambda key: {"file_name": "unique.ogg" if key == "a" else "shared.ogg"}
        def flush():
            self.assertTrue(unique.exists())
        index.flush.side_effect = flush
        index.file_referenced.side_effect = lambda name: name == "shared.ogg"
        self.app._delete_sqlite_cache_rows(self.cache_dir, ["a", "b"], index=index)
        self.assertFalse(unique.exists())
        self.assertTrue(shared.exists())
        self.assertLess(index.mock_calls.index(mock.call.flush()), index.mock_calls.index(mock.call.file_referenced("unique.ogg")))

    def test_sql_error_does_not_delete_audio(self):
        audio = self.cache_dir / "audio"
        audio.mkdir()
        path = audio / "keep.ogg"
        path.write_bytes(b"keep")
        index = mock.Mock()
        index.get.return_value = {"file_name": path.name}
        index.flush.side_effect = OSError("full")
        with self.assertRaises(OSError):
            self.app._delete_sqlite_cache_rows(self.cache_dir, ["a"], index=index)
        self.assertEqual(path.read_bytes(), b"keep")
        index.file_referenced.assert_not_called()


if __name__ == "__main__":
    unittest.main()
