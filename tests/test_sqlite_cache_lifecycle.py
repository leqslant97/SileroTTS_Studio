"""Actual GUI entry callbacks leave SQLite startup and close work outside Tk."""

import copy
import queue
import threading
import unittest
from unittest import mock

try:
    import test_close_confirmation as closing
    import test_sqlite_cache_synthesis as synthesis
except ModuleNotFoundError:
    from tests import test_close_confirmation as closing
    from tests import test_sqlite_cache_synthesis as synthesis

studio = closing.studio


class SQLiteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case = synthesis.SQLiteSynthesisTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = closing.CloseConfirmationTests("runTest").make_app()
        self.app.config = copy.deepcopy(self.case.cfg)

    def drain_ui(self):
        while True:
            try:
                callback, args, kwargs = self.app._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args, **kwargs)

    def test_app_creation_does_not_open_sqlite_until_worker_entry(self):
        caller = threading.get_ident()
        threads = []
        acquire = studio.acquire_sqlite_cache_index

        def observed_acquire(*args, **kwargs):
            threads.append(threading.get_ident())
            return acquire(*args, **kwargs)

        self.app._show_task_notification = mock.Mock()
        with mock.patch.object(studio, "acquire_sqlite_cache_index", side_effect=observed_acquire):
            processor = self.app._create_synthesis_processor(self.app.config)
            self.addCleanup(processor.close_cache)
            self.assertEqual(threads, [])
            self.assertFalse(processor._sqlite_cache_reference)
            callback = mock.Mock()
            worker = threading.Thread(target=self.app._run_synthesis_with_cache,
                                      args=(processor, callback, (), "batch"))
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            callback.assert_called_once_with()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], caller)

    def test_worker_startup_failure_never_calls_synthesis_or_api(self):
        processor = self.app._create_synthesis_processor(self.app.config)
        self.app.finish_processing = mock.Mock()
        self.app._show_task_notification = mock.Mock()
        callback = mock.Mock()
        with mock.patch.object(studio, "acquire_sqlite_cache_index", side_effect=OSError("DB unavailable")):
            worker = threading.Thread(target=self.app._run_synthesis_with_cache,
                                      args=(processor, callback, (), "batch"))
            worker.start()
            worker.join(3)
        callback.assert_not_called()
        self.drain_ui()
        self.app.finish_processing.assert_called_once_with(processor, True)
        self.app._show_task_notification.assert_called_once()
        self.assertIn("DB unavailable", processor._cache_open_failed)

    def test_close_returns_to_tk_before_slow_sqlite_flush_and_then_destroys_window(self):
        processor = self.case.processor()
        processor.synthesize_sentence("При закрытии.", "При закрытии.")
        self.app._closing_processors = (processor,)
        self.app._cache_processors = {id(processor): processor}
        started, release = threading.Event(), threading.Event()
        caller = threading.get_ident()
        calls = []
        real_flush = processor.flush_cache

        def slow_flush():
            calls.append(threading.get_ident())
            if len(calls) == 1:
                started.set()
                if not release.wait(3):
                    raise AssertionError("flush was not released")
            return real_flush()

        with mock.patch.object(processor, "flush_cache", side_effect=slow_flush):
            self.app._complete_application_close()
            self.assertTrue(started.wait(3))
            self.assertTrue(self.app._is_closing)
            self.app.root.destroy.assert_not_called()
            self.app.save_settings.assert_not_called()
            worker = self.app._sqlite_close_worker
            release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.drain_ui()
        self.assertNotEqual(calls[0], caller)
        self.assertFalse(processor._sqlite_cache_reference)
        self.app.root.destroy.assert_called_once()

    def test_close_failure_retains_pending_and_window_for_retry(self):
        processor = self.case.processor()
        processor.synthesize_sentence("Несохранённый фрагмент.", "Несохранённый фрагмент.")
        self.app._closing_processors = (processor,)
        with mock.patch.object(processor.cache, "flush", side_effect=OSError("disk full")):
            self.app._complete_application_close()
            worker = self.app._sqlite_close_worker
            worker.join(3)
            self.drain_ui()
        self.app.root.destroy.assert_not_called()
        self.app._show_error.assert_called_once()
        self.assertFalse(self.app._is_closing)
        self.assertTrue(processor._sqlite_cache_reference)
        self.assertIn(processor, self.app._closing_processors)
        self.app._complete_application_close()
        self.app._sqlite_close_worker.join(3)
        self.drain_ui()
        self.app.root.destroy.assert_called_once()

    def test_idle_cache_operation_helper_rejects_live_reader_and_preserves_failed_processor(self):
        processor = self.case.processor()
        self.app._cache_processors = {id(processor): processor}
        worker = mock.Mock()
        worker.is_alive.return_value = True
        self.app.processing_thread = worker
        with self.assertRaises(RuntimeError):
            self.app._flush_idle_cache_processors()
        self.assertTrue(processor._sqlite_cache_reference)
        self.app.processing_thread = None
        with mock.patch.object(processor.cache, "flush", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.app._flush_idle_cache_processors()
        self.assertIn(processor, self.app._closing_processors)
        self.assertTrue(processor._sqlite_cache_reference)
        self.app._flush_idle_cache_processors()
        self.assertFalse(processor._sqlite_cache_reference)
        self.assertEqual(self.app._closing_processors, ())
        self.assertEqual(self.app._cache_processors, {})

    def test_one_processor_release_does_not_close_the_shared_consumer(self):
        first, second = self.case.processor(), self.case.processor()
        index = first.cache
        self.assertIs(second.cache, index)
        first.close_cache()
        self.assertFalse(index._closed)
        second.synthesize_sentence("Ещё используемый индекс.", "Ещё используемый индекс.")
        second.close_cache()
        self.assertTrue(index._closed)

    def test_actual_direct_start_uses_worker_for_open_and_final_flush(self):
        app = self.app
        app.direct_text = mock.Mock()
        app.direct_text.get.return_value = "Прямой запуск."
        app.direct_prepared_text_var = mock.Mock(get=mock.Mock(return_value=False))
        app.direct_output_dir_var = mock.Mock(get=mock.Mock(return_value=str(self.case.root / "direct")))
        app.settings_vars = {
            key: mock.Mock(get=mock.Mock(return_value=value))
            for key, value in {
                "direct_filename": "preview.mp3", "direct_force": False,
                "direct_save": False, "direct_autoplay": False, "direct_apply_tags": False,
            }.items()
        }
        for name in ("_discard_last_direct_preview", "_set_status_label", "_finish_direct_processing",
                     "btn_direct_start", "btn_direct_stop", "btn_direct_hard_stop",
                     "chk_direct_prepared_text", "chk_direct_force"):
            setattr(app, name, mock.Mock())
        app.lbl_direct_status = mock.Mock()
        app._warn_if_cache_busy_for_synthesis = mock.Mock(return_value=False)
        app._validate_api_steps_ui = mock.Mock(return_value=True)
        app._validate_book_output_profile = mock.Mock(return_value=True)
        app._prepare_glossary_for_synthesis = mock.Mock(return_value=True)
        app._direct_processing_config = mock.Mock(return_value=dict(app.config))
        caller = threading.get_ident()
        opened = []
        original_acquire = studio.acquire_sqlite_cache_index

        def acquire(*args, **kwargs):
            opened.append(threading.get_ident())
            return original_acquire(*args, **kwargs)

        def run_text(processor, _text, filename, **kwargs):
            processor.cache.upsert("directfixture", {"file_name": "manual.ogg"})
            processor.cache_metadata_dirty = True
            kwargs["completion_callback"](filename, "success", None)

        with mock.patch.object(studio, "acquire_sqlite_cache_index", side_effect=acquire), \
             mock.patch.object(studio.TTSProcessor, "process_raw_text", autospec=True, side_effect=run_text):
            app.start_direct_processing()
            worker = app.direct_thread
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.drain_ui()
        self.assertEqual(len(opened), 1)
        self.assertNotEqual(opened[0], caller)
        app._finish_direct_processing.assert_called_once()
        processor, result, _save, _autoplay = app._finish_direct_processing.call_args.args
        self.assertFalse(processor._sqlite_cache_reference)
        self.assertEqual(result["status"], "success")
        self.assertIn("directfixture", self.case.persisted())


if __name__ == "__main__":
    unittest.main()
