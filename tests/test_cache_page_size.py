"""Выбор размера страницы сохраняет поиск, порядок и переходы обоих индексов."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class CachePageSizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        app = self.app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = {"cache_dir": str(self.directory), "cache_page_size": 20}
        app.root = mock.Mock()
        app._cache_operation = None
        app._cache_ui_request_id = 0
        app._cache_state_generation = 0
        app._cache_ui_loaded = False
        app.search_var = mock.Mock(get=mock.Mock(return_value=""))
        app.cache_sort_var = mock.Mock(get=mock.Mock(return_value="По хэшу"))
        app.cache_page_size_var = mock.Mock(get=mock.Mock(return_value=20))
        app.lbl_cache_count = mock.Mock()
        app.cache_tree = mock.Mock(get_children=mock.Mock(return_value=()))
        app._show_task_notification = mock.Mock()
        app._show_warning = mock.Mock()
        self.posts = []
        app._post_to_ui = lambda callback, *args: self.posts.append((callback, args))
        self.data = {f"hash-{n:04}": {"original_text": f"Фраза {n}", "usage_count": n % 3}
                     for n in range(73)}

    def finish(self):
        end = time.monotonic() + 5
        while not self.posts and time.monotonic() < end:
            time.sleep(.005)
        self.assertTrue(self.posts, "worker did not finish")
        callback, args = self.posts.pop(0)
        callback(*args)
        self.assertIsNone(self.app._cache_operation)

    def write_fixture(self, backend, data=None):
        data = self.data if data is None else data
        if backend == "json":
            (self.directory / "sentence_cache.json").write_text(json.dumps(data))
        else:
            index = studio.acquire_sqlite_cache_index(self.directory)
            try:
                for key, metadata in data.items():
                    index.upsert(key, metadata)
            finally:
                studio.release_sqlite_cache_index(index)

    def test_config_accepts_presets_and_rejects_invalid_or_unsupported_values(self):
        for value in studio.CACHE_PAGE_SIZES:
            for saved in (value, str(value)):
                with self.subTest(value=saved):
                    self.assertEqual(studio.normalize_config({"cache_page_size": saved})["cache_page_size"], value)
        for value in (None, False, True, 0, -20, 21, 250, 2000, 20.5, "20.5", "", [], {}):
            with self.subTest(invalid=value):
                self.assertEqual(studio.normalize_config({"cache_page_size": value})["cache_page_size"], 1000)

    def test_each_preset_limits_json_and_sqlite_and_last_page_keeps_remainder(self):
        for backend in ("json", "sqlite"):
            for size in studio.CACHE_PAGE_SIZES:
                with self.subTest(backend=backend, size=size), tempfile.TemporaryDirectory() as directory:
                    self.directory = Path(directory).resolve()
                    self.app.config["cache_dir"] = str(self.directory)
                    self.app.cache_page_size_var.get.return_value = size
                    data = {f"hash-{n:04}": {"original_text": f"Фраза {n}"} for n in range(size + 3)}
                    self.write_fixture(backend, data)
                    self.app.load_cache_ui()
                    self.finish()
                    self.assertEqual(list(self.app._cache_display_data), list(data)[:size])
                    self.assertTrue(self.app._cache_page_has_more)
                    self.app._cache_page_forward()
                    self.finish()
                    self.assertEqual(list(self.app._cache_display_data), list(data)[size:])
                    self.assertFalse(self.app._cache_page_has_more)
                    self.app._cache_page_back()
                    self.finish()
                    self.assertEqual(list(self.app._cache_display_data), list(data)[:size])

    def test_json_numeric_sort_pages_do_not_repeat_or_skip_ties(self):
        self.write_fixture("json")
        self.app.cache_sort_var.get.return_value = "Чаще используются"
        self.app.load_cache_ui()
        self.finish()
        observed = list(self.app._cache_display_data)
        with mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("reread")):
            while self.app._cache_page_has_more:
                self.app._cache_page_forward()
                self.finish()
                observed.extend(self.app._cache_display_data)
        expected = sorted(self.data, key=lambda key: (-self.data[key]["usage_count"], key))
        self.assertEqual(observed, expected)

    def test_json_search_after_navigation_scans_the_whole_loaded_index(self):
        self.write_fixture("json")
        self.app.load_cache_ui()
        self.finish()
        self.app._cache_page_forward()
        self.finish()
        self.app.search_var.get.return_value = "Фраза 70"
        with mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("reread")):
            self.app.filter_cache()
            self.finish()
        self.assertEqual(list(self.app._cache_display_data), ["hash-0070"])
        self.assertEqual(self.app._cache_page_number, 0)

    def test_sqlite_query_limit_is_captured_before_starting_worker(self):
        caller = threading.get_ident()
        worker_threads = []
        index = mock.Mock()

        def query_page(**options):
            worker_threads.append(threading.get_ident())
            self.assertEqual(options["limit"], 20)
            return {"rows": [], "cursor": None, "has_more": False, "bytes": 0}

        index.query_page.side_effect = query_page
        index.count.return_value = 0
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
                mock.patch.object(studio, "release_sqlite_cache_index"):
            self.app.load_cache_ui()
            self.finish()
        self.assertTrue(worker_threads)
        self.assertNotIn(caller, worker_threads)

    def test_old_page_size_result_is_ignored(self):
        self.app._cache_ui_request_id = 1
        self.app.cache_data = {"current": {}}
        self.app._cache_operation = "load"
        result = {"backend": "sqlite", "data": {"stale": {}}, "total": 1,
                  "cursor": None, "has_more": False, "page_size": 1000}
        self.app._finish_cache_page(1, 0, self.directory, "", None, result)
        self.assertEqual(self.app.cache_data, {"current": {}})
        self.assertFalse(self.app._cache_ui_loaded)

    def test_size_change_while_first_page_is_pending_restarts_loading(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 20
                self.app._cache_ui_loaded = False
                self.write_fixture(backend)
                self.app.load_cache_ui()
                end = time.monotonic() + 5
                while not self.posts and time.monotonic() < end:
                    time.sleep(.005)
                self.assertTrue(self.posts)
                self.app.cache_page_size_var.get.return_value = 50
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                callback, args = self.posts.pop(0)
                callback(*args)
                self.assertEqual(self.app._cache_operation, "load")
                self.app.filter_cache()
                self.finish()
                self.assertEqual(len(self.app._cache_display_data), 50)
                self.assertTrue(self.app._cache_ui_loaded)

    def test_size_change_preserves_visible_position_and_saves_preference(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 20
                data = {f"hash-{n:04}": {"original_text": f"Фраза {n}"} for n in range(183)}
                self.write_fixture(backend, data)
                self.app.load_cache_ui()
                self.finish()
                for _ in range(6):
                    self.app._cache_page_forward()
                    self.finish()
                self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
                self.app.cache_tree.yview.return_value = (7 / 20, 1)
                self.app.cache_page_size_var.get.return_value = 50
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                self.app.filter_cache()
                self.finish()
                self.assertEqual(self.app.config["cache_page_size"], 50)
                self.app._schedule_settings_save.assert_called_once()
                self.assertEqual(self.app._cache_page_number + self.app._cache_page_base, 2)
                self.assertEqual(list(self.app._cache_display_data), list(data)[100:150])
                self.app.cache_tree.see.assert_called_with("hash-0127")
                self.app._cache_page_back()
                self.finish()
                self.assertEqual(list(self.app._cache_display_data), list(data)[50:100])
                self.app._cache_page_forward()
                self.finish()
                self.assertEqual(list(self.app._cache_display_data), list(data)[100:150])

    def test_smaller_size_anchors_to_scrolled_row_instead_of_old_page_start(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 100
                data = {f"hash-{n:04}": {"original_text": f"Фраза {n}"} for n in range(203)}
                self.write_fixture(backend, data)
                self.app.load_cache_ui()
                self.finish()
                self.app._cache_page_forward()
                self.finish()
                self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
                self.app.cache_tree.yview.return_value = (.7, 1)
                self.app.cache_page_size_var.get.return_value = 20
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                self.app.filter_cache()
                self.finish()
                self.assertEqual(list(self.app._cache_display_data), list(data)[160:180])
                self.assertEqual(self.app._cache_page_number + self.app._cache_page_base, 8)
                self.app.cache_tree.see.assert_called_with("hash-0170")

    def test_anchor_respects_search_and_numeric_sort_with_equal_values(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 20
                self.app.cache_sort_var.get.return_value = "Чаще используются"
                self.app.search_var.get.return_value = "совпадение"
                data = {f"hash-{n:04}": {"original_text": "Совпадение" if n % 2 else "Другой текст",
                                         "usage_count": n % 3} for n in range(213)}
                self.write_fixture(backend, data)
                self.app.load_cache_ui()
                self.finish()
                for _ in range(3):
                    self.app._cache_page_forward()
                    self.finish()
                self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
                self.app.cache_tree.yview.return_value = (.25, 1)
                anchor = list(self.app._cache_display_data)[5]
                self.app.cache_page_size_var.get.return_value = 50
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                self.app.filter_cache()
                self.finish()
                expected = sorted((key for key in data if int(key[-4:]) % 2),
                                  key=lambda key: (-data[key]["usage_count"], key))
                self.assertEqual(list(self.app._cache_display_data), expected[50:100])
                self.app.cache_tree.see.assert_called_with(anchor)

    def test_deleted_anchor_falls_back_to_first_page(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 20
                self.write_fixture(backend)
                self.app.load_cache_ui()
                self.finish()
                self.app._cache_page_forward()
                self.finish()
                self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
                self.app.cache_tree.yview.return_value = (0, 1)
                self.app.cache_page_size_var.get.return_value = 50
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                if backend == "json":
                    self.app.cache_data.pop("hash-0020")
                else:
                    index = studio.acquire_sqlite_cache_index(self.directory)
                    try:
                        index.delete(["hash-0020"])
                    finally:
                        studio.release_sqlite_cache_index(index)
                self.app.filter_cache()
                self.finish()
                self.assertEqual(self.app._cache_page_number, 0)
                self.assertEqual(list(self.app._cache_display_data), [key for key in self.data if key != "hash-0020"][:50])

    def test_search_change_discards_pending_resize_anchor(self):
        self.write_fixture("json")
        self.app.load_cache_ui()
        self.finish()
        self.app._cache_page_forward()
        self.finish()
        self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
        self.app.cache_tree.yview.return_value = (0, 1)
        self.app.cache_page_size_var.get.return_value = 50
        self.app._schedule_settings_save = mock.Mock()
        self.app._cache_page_size_changed()
        self.app.search_var.get.return_value = "Фраза 70"
        self.app._schedule_cache_filter()
        self.app.filter_cache()
        self.finish()
        self.assertEqual(list(self.app._cache_display_data), ["hash-0070"])
        self.assertEqual(self.app._cache_page_number, 0)

    def test_deep_anchor_keeps_bounded_history_and_correct_previous_page(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory).resolve()
                self.app.config["cache_dir"] = str(self.directory)
                self.app.cache_page_size_var.get.return_value = 100
                data = {f"hash-{n:04}": {"original_text": f"Фраза {n}"} for n in range(2103)}
                self.write_fixture(backend, data)
                self.app.load_cache_ui()
                self.finish()
                for _ in range(20):
                    self.app._cache_page_forward()
                    self.finish()
                self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
                self.app.cache_tree.yview.return_value = (.27, 1)
                self.app.cache_page_size_var.get.return_value = 20
                self.app._schedule_settings_save = mock.Mock()
                self.app._cache_page_size_changed()
                self.app.filter_cache()
                self.finish()
                self.assertEqual(len(self.app._cache_page_history), 100)
                self.assertEqual(self.app._cache_page_number + self.app._cache_page_base, 101)
                self.assertEqual(list(self.app._cache_display_data), list(data)[2020:2040])
                self.app._cache_page_back()
                self.finish()
                self.assertEqual(list(self.app._cache_display_data), list(data)[2000:2020])

    def test_sqlite_byte_limited_pages_preserve_anchor_and_navigation(self):
        self.write_fixture("sqlite", {f"hash-{n:04}": {"original_text": "x" * 800000}
                                      for n in range(7)})
        self.app.load_cache_ui()
        self.finish()
        for _ in range(2):
            self.app._cache_page_forward()
            self.finish()
        self.app.cache_tree.get_children.side_effect = lambda: tuple(self.app._cache_display_data)
        self.app.cache_tree.yview.return_value = (0, 1)
        self.app.cache_page_size_var.get.return_value = 50
        self.app._schedule_settings_save = mock.Mock()
        self.app._cache_page_size_changed()
        self.app.filter_cache()
        self.finish()
        self.assertEqual(list(self.app.cache_data), ["hash-0004", "hash-0005"])
        self.assertEqual(self.app._cache_page_number, 2)
        self.app._cache_page_forward()
        self.finish()
        self.assertEqual(list(self.app.cache_data), ["hash-0006"])
        self.app._cache_page_back()
        self.finish()
        self.assertEqual(list(self.app.cache_data), ["hash-0004", "hash-0005"])

    def test_sqlite_anchor_search_cancels_without_posting_a_partial_result(self):
        index = mock.Mock()
        released = threading.Event()
        calls = []

        def query_page(**options):
            calls.append(threading.get_ident())
            if len(calls) == 2:
                self.app._cache_query_cancel.set()
            return {"rows": [(f"hash-{len(calls):04}", {})], "cursor": str(len(calls)),
                    "has_more": True, "bytes": 20, "stop_reason": "byte_limit"}

        index.query_page.side_effect = query_page
        with mock.patch.object(studio, "get_active_cache_backend", return_value="sqlite"), \
                mock.patch.object(studio, "acquire_sqlite_cache_index", return_value=index), \
                mock.patch.object(studio, "release_sqlite_cache_index", side_effect=lambda _: released.set()):
            self.app._request_cache_page(reset_history=True, anchor_key="hash-9999")
            self.assertTrue(released.wait(5))
        self.assertEqual(len(calls), 2)
        self.assertNotIn(threading.get_ident(), calls)
        self.assertEqual(self.posts, [])
        self.app._cache_generation_changed()


if __name__ == "__main__":
    unittest.main()
