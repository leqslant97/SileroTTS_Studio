"""Закрытие ждёт задачи и сохраняет кэш, а отмена не прерывает работу."""

import importlib.util
import json
import queue
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


class CloseConfirmationTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app._ui_queue = queue.Queue()
        app._is_closing = False
        app._glossary_dirty = False
        app._normalizer_batch_running = False
        app._import_running = False
        app._export_lock = False
        app._export_running = False
        app.is_export_stopped = False
        app.processing_thread = None
        app.direct_thread = None
        app._export_thread = None
        app.batch_processor = None
        app.direct_processor = None
        app._shared_cache_dir = None
        app._shared_cache = None
        app._shared_processing_statuses = None
        app._shared_cache_lock = threading.RLock()
        app._shared_cache_eviction_state = {}
        app._shared_synthesis_inflight = {}
        app.shared_rate_limiter = mock.Mock()
        app.show_critical_error = mock.Mock()
        app.is_cache_operation_running = mock.Mock(return_value=False)
        app._ask_yes_no = mock.Mock(return_value=True)
        app._ask_yes_no_cancel = mock.Mock(return_value=None)
        app.save_glossary_ui = mock.Mock(return_value=True)
        for name in (
            "_show_warning", "_show_error", "_create_wait_popup",
            "_close_popup_safely", "_discard_last_direct_preview",
            "save_settings", "_save_export_project_session", "_save_source_session",
        ):
            setattr(app, name, mock.Mock(return_value=True))
        app._create_wait_popup.return_value = mock.Mock()
        return app

    def add_worker(self, app, kind):
        worker = mock.Mock()
        worker.is_alive.return_value = True
        processor = mock.Mock(is_stopped=False)
        if kind == "batch":
            app.processing_thread = worker
            app.batch_processor = processor
        elif kind == "direct":
            app.direct_thread = worker
            app.direct_processor = processor
        else:
            app._export_thread = worker
            app._export_running = True
            processor = None
        return worker, processor

    def test_macos_quit_uses_the_same_confirmation_and_save_handler(self):
        """Системное завершение не обходит обработчик закрытия окна."""
        app = self.make_app()
        app._setup_mac_hotkeys()
        app.root.createcommand.assert_called_once_with(
            "::tk::mac::Quit", app.on_closing
        )
        worker = mock.Mock()
        worker.is_alive.return_value = True
        app.processing_thread = worker
        app._ask_yes_no.return_value = False
        app.root.createcommand.call_args.args[1]()
        app._ask_yes_no.assert_called_once()
        app.root.destroy.assert_not_called()

    def test_idle_and_stale_export_flags_close_without_confirmation(self):
        for stale_thread in (None, mock.Mock()):
            with self.subTest(stale_thread=stale_thread is not None):
                app = self.make_app()
                app._export_running = True
                if stale_thread is not None:
                    stale_thread.is_alive.return_value = False
                    app._export_thread = stale_thread

                app.on_closing()

                app._ask_yes_no.assert_not_called()
                app._create_wait_popup.assert_not_called()
                app.root.destroy.assert_called_once()
                self.assertFalse(app.is_export_stopped)

    def test_declining_each_active_operation_preserves_running_work(self):
        for kind in ("batch", "direct", "export"):
            with self.subTest(kind=kind):
                app = self.make_app()
                _worker, processor = self.add_worker(app, kind)
                app._ask_yes_no.return_value = False

                app.on_closing()

                app._ask_yes_no.assert_called_once()
                self.assertEqual(app._ask_yes_no.call_args.kwargs["default"], "no")
                app.root.destroy.assert_not_called()
                app.root.after.assert_not_called()
                app.save_settings.assert_not_called()
                app._create_wait_popup.assert_not_called()
                self.assertFalse(app.is_export_stopped)
                self.assertFalse(app._is_closing)
                if processor is not None:
                    self.assertFalse(processor.is_stopped)
                    processor.stop.assert_not_called()
                    processor.flush_cache.assert_not_called()

    def test_confirmation_mentions_both_concurrent_operations(self):
        app = self.make_app()
        self.add_worker(app, "batch")
        self.add_worker(app, "export")
        app._ask_yes_no.return_value = False

        app.on_closing()

        self.assertIn("синтез и обработка аудиофайлов", app._ask_yes_no.call_args.args[1])
        self.assertFalse(app.is_export_stopped)
        self.assertFalse(app.batch_processor.is_stopped)

    def test_glossary_cancel_after_confirmation_does_not_stop_worker(self):
        app = self.make_app()
        _worker, processor = self.add_worker(app, "batch")
        app._glossary_dirty = True

        app.on_closing()

        app._ask_yes_no.assert_called_once()
        app._ask_yes_no_cancel.assert_called_once()
        self.assertFalse(processor.is_stopped)
        processor.flush_cache.assert_not_called()
        app._create_wait_popup.assert_not_called()
        app.root.destroy.assert_not_called()

    def test_confirmation_is_not_reentered_by_another_close_event(self):
        app = self.make_app()
        self.add_worker(app, "batch")

        def decline_after_another_close(*_args, **_kwargs):
            app.on_closing()
            return False

        app._ask_yes_no.side_effect = decline_after_another_close
        app.on_closing()

        app._ask_yes_no.assert_called_once()
        self.assertFalse(app._close_request_in_progress)
        app.root.destroy.assert_not_called()

    def test_worker_finishing_during_confirmation_skips_wait_popup(self):
        app = self.make_app()
        worker, processor = self.add_worker(app, "batch")

        def confirm_after_completion(*_args, **_kwargs):
            worker.is_alive.return_value = False
            app.batch_processor = None
            app.processing_thread = None
            return True

        app._ask_yes_no.side_effect = confirm_after_completion
        app.on_closing()

        app._create_wait_popup.assert_not_called()
        processor.flush_cache.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_confirmed_close_waits_for_all_workers_without_joining_tk_thread(self):
        app = self.make_app()
        workers = [self.add_worker(app, kind) for kind in ("batch", "direct", "export")]

        app.on_closing()

        self.assertTrue(app._is_closing)
        self.assertTrue(app.is_export_stopped)
        app.root.after.assert_called_once_with(100, app._poll_application_close_wait)
        app.root.destroy.assert_not_called()
        app.save_settings.assert_not_called()
        for worker, processor in workers:
            worker.join.assert_not_called()
            if processor is not None:
                self.assertTrue(processor.is_stopped)
                processor.stop.assert_not_called()
                processor.flush_cache.assert_not_called()

        workers[0][0].is_alive.return_value = False
        app._poll_application_close_wait()
        app.root.destroy.assert_not_called()
        for worker, _processor in workers:
            worker.is_alive.return_value = False
        app._poll_application_close_wait()
        app._drain_ui_queue()
        app.root.destroy.assert_called_once()
        app._close_popup_safely.assert_called_once()
        for _worker, processor in workers:
            if processor is not None:
                processor.flush_cache.assert_called_once()
                processor._save_processing_statuses.assert_called_once()

    def test_confirmed_wait_ignores_repeated_close_event(self):
        app = self.make_app()
        self.add_worker(app, "batch")

        app.on_closing()
        app.on_closing()

        app._ask_yes_no.assert_called_once()
        app._create_wait_popup.assert_called_once()
        app.root.destroy.assert_not_called()

    def test_late_received_fragment_is_saved_before_window_is_destroyed(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            index = cache_dir / "sentence_cache.json"
            release = threading.Event()
            received = threading.Event()
            app = self.make_app()
            processor = app.batch_processor = mock.Mock(is_stopped=False)
            cache = {"before_close": {
                "file_name": "before.ogg", "created_at": 1,
                "last_accessed": 1, "usage_count": 0,
            }}

            def finish_response():
                release.wait(3)
                audio = cache_dir / "late.ogg"
                audio.write_bytes(b"complete fragment")
                cache["during_close"] = {
                    "file_name": audio.name, "created_at": 1,
                    "last_accessed": 1, "usage_count": 0,
                }
                received.set()

            def save_cache():
                studio.write_cache_index_atomic(cache_dir, cache)
                return True

            processor.flush_cache.side_effect = save_cache
            worker = app.processing_thread = threading.Thread(target=finish_response)
            worker.start()
            try:
                app.on_closing()
                self.assertFalse(index.exists())
                app.root.destroy.assert_not_called()
                self.assertTrue(processor.is_stopped)
                processor.stop.assert_not_called()
                release.set()
                self.assertTrue(received.wait(3))
                worker.join(3)
                self.assertFalse(worker.is_alive())
                app._poll_application_close_wait()
                app._drain_ui_queue()

                self.assertEqual(json.loads(index.read_text()), cache)
                self.assertEqual((cache_dir / "late.ogg").read_bytes(), b"complete fragment")
                app.root.destroy.assert_called_once()
            finally:
                release.set()
                worker.join(3)

    def test_ui_completion_posts_are_processed_while_waiting(self):
        app = self.make_app()
        self.add_worker(app, "direct")
        app.on_closing()
        completion = mock.Mock()

        app._post_to_ui(completion, "finished")
        app._drain_ui_queue()

        completion.assert_called_once_with("finished")
        self.assertTrue(app._is_closing)
        app.root.destroy.assert_not_called()

    def test_pending_wait_suppresses_completion_dialog_and_autoplay(self):
        app = self.make_app()
        self.add_worker(app, "direct")
        app.on_closing()
        dialog = mock.Mock()
        app.stop_audio_playback = mock.Mock()
        audio = mock.Mock()

        self.assertIsNone(app._run_messagebox(dialog, "Остановлено", "Готово"))
        app.play_audio_segment(audio)

        dialog.assert_not_called()
        app.stop_audio_playback.assert_not_called()
        audio.export.assert_not_called()

    def test_final_save_retries_retained_processor_after_ui_releases_it(self):
        app = self.make_app()
        worker, processor = self.add_worker(app, "batch")
        processor.flush_cache.return_value = False
        app._appearance_check_after_id = "appearance"
        app._source_session_save_after_id = "source"
        app.on_closing()

        def finish_ui():
            app.batch_processor = None
            app.processing_thread = None
            app.start_enabled = True

        app._post_to_ui(finish_ui)
        app._drain_ui_queue()
        worker.is_alive.return_value = False
        app._poll_application_close_wait()
        app._drain_ui_queue()

        self.assertTrue(app.start_enabled)
        self.assertFalse(app._is_closing)
        self.assertIsNone(app._closing_wait_popup)
        app.root.destroy.assert_not_called()
        app.root.after_cancel.assert_not_called()
        app._show_error.assert_called_once()
        processor.flush_cache.return_value = True
        app.on_closing()

        self.assertEqual(processor.flush_cache.call_count, 2)
        app.root.destroy.assert_called_once()
        self.assertEqual(app._closing_processors, ())

    def test_last_completed_group_is_in_session_before_final_save(self):
        app = self.make_app()
        worker, _processor = self.add_worker(app, "batch")
        app.completed_groups = []
        saved_groups = []
        app._save_source_session.side_effect = lambda: saved_groups.extend(app.completed_groups)
        app.on_closing()
        app._post_to_ui(app.completed_groups.append, "last.m4b")
        worker.is_alive.return_value = False

        app._poll_application_close_wait()

        app.root.destroy.assert_not_called()
        app._save_source_session.assert_not_called()
        app._drain_ui_queue()
        self.assertEqual(saved_groups, ["last.m4b"])
        app.root.destroy.assert_called_once()

    def test_dead_worker_still_saves_last_queued_group_and_released_cache(self):
        app = self.make_app()
        worker, processor = self.add_worker(app, "batch")
        worker.is_alive.return_value = False
        app.completed_groups = []
        saved_groups = []
        app._save_source_session.side_effect = lambda: saved_groups.extend(app.completed_groups)

        def last_ui_update():
            app.completed_groups.append("last.m4b")
            app.batch_processor = None
            app.processing_thread = None

        app._post_to_ui(last_ui_update)
        app.on_closing()

        app._ask_yes_no.assert_not_called()
        app._create_wait_popup.assert_not_called()
        app.root.destroy.assert_not_called()
        app._save_source_session.assert_not_called()
        app._drain_ui_queue()
        self.assertEqual(saved_groups, ["last.m4b"])
        processor.flush_cache.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_failed_idle_close_keeps_cache_after_later_ui_releases_processor(self):
        app = self.make_app()
        worker, processor = self.add_worker(app, "batch")
        worker.is_alive.return_value = False
        processor.flush_cache.return_value = False

        app.on_closing()

        app._ask_yes_no.assert_not_called()
        self.assertEqual(app._closing_processors, (processor,))
        app.root.destroy.assert_not_called()

        def finish_ui():
            app.batch_processor = None
            app.processing_thread = None
            app._release_shared_cache_if_idle()

        app._post_to_ui(finish_ui)
        app._drain_ui_queue()
        with mock.patch.object(studio, "TTSProcessor") as create_processor:
            with self.assertRaisesRegex(RuntimeError, "данные предыдущего запуска"):
                app._create_synthesis_processor({"cache_dir": "/unneeded"})
            create_processor.assert_not_called()
        self.assertEqual(app._closing_processors, (processor,))
        processor.flush_cache.return_value = True
        app.on_closing()
        app.root.destroy.assert_called_once()
        self.assertEqual(app._closing_processors, ())

    def test_new_synthesis_is_blocked_until_failed_close_cache_is_saved(self):
        app = self.make_app()
        processor = mock.Mock()
        processor.flush_cache.return_value = False
        app._closing_processors = (processor,)
        with mock.patch.object(studio, "TTSProcessor") as create_processor:
            with self.assertRaisesRegex(RuntimeError, "данные предыдущего запуска"):
                app._create_synthesis_processor({"cache_dir": "/unneeded"})

        create_processor.assert_not_called()
        self.assertEqual(app._closing_processors, (processor,))

    def test_new_run_after_failed_close_keeps_old_and_new_cache_and_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            index = cache_dir / "sentence_cache.json"
            statuses_path = cache_dir / "statuses.json"
            app = self.make_app()
            worker, old_processor = self.add_worker(app, "batch")
            old_cache = {"old": {
                "file_name": "old.ogg", "created_at": 1,
                "last_accessed": 1, "usage_count": 0,
            }}
            old_statuses = {"old_output": "warning"}
            cache_writable = False

            def flush_old():
                if not cache_writable:
                    return False
                studio.write_cache_index_atomic(cache_dir, old_cache)
                return True

            old_processor.flush_cache.side_effect = flush_old
            old_processor._save_processing_statuses.side_effect = lambda: app._write_json_atomic(
                statuses_path, old_statuses
            )
            app.on_closing()

            def finish_ui():
                app.batch_processor = None
                app.processing_thread = None
                app._release_shared_cache_if_idle()

            app._post_to_ui(finish_ui)
            worker.is_alive.return_value = False
            app._poll_application_close_wait()
            app._drain_ui_queue()
            app.root.destroy.assert_not_called()
            self.assertEqual(app._closing_processors, (old_processor,))
            cache_writable = True

            def load_new(_config, **_kwargs):
                new_processor = mock.Mock(is_stopped=True)
                new_processor.cache_dir = cache_dir
                new_processor.cache = json.loads(index.read_text())
                new_processor.processing_statuses_ram = json.loads(statuses_path.read_text())
                new_processor.flush_cache.side_effect = lambda: studio.write_cache_index_atomic(
                    cache_dir, new_processor.cache
                )
                new_processor._save_processing_statuses.side_effect = lambda: app._write_json_atomic(
                    statuses_path, new_processor.processing_statuses_ram
                )
                return new_processor

            with mock.patch.object(studio, "TTSProcessor", side_effect=load_new):
                new_processor = app._create_synthesis_processor({"cache_dir": str(cache_dir)})

            self.assertEqual(app._closing_processors, ())
            self.assertIn("old", new_processor.cache)
            new_processor.cache["new"] = {
                "file_name": "new.ogg", "created_at": 2,
                "last_accessed": 2, "usage_count": 0,
            }
            new_processor.processing_statuses_ram["new_output"] = "error"
            app.batch_processor = new_processor
            app.on_closing()

            self.assertEqual(set(json.loads(index.read_text())), {"old", "new"})
            self.assertEqual(json.loads(statuses_path.read_text()), {
                "old_output": "warning", "new_output": "error",
            })
            app.root.destroy.assert_called_once()

    def test_appearance_timer_keeps_running_during_wait_and_failed_save(self):
        app = self.make_app()
        worker, processor = self.add_worker(app, "batch")
        processor.flush_cache.return_value = False
        app._appearance_check_after_id = "old_timer"
        app._is_dark_appearance = False
        app._detect_dark_appearance = mock.Mock(return_value=False)
        app.on_closing()
        app.root.after.reset_mock()

        app._check_system_appearance()

        app.root.after.assert_called_once_with(1500, app._check_system_appearance)
        self.assertIsNotNone(app._appearance_check_after_id)
        worker.is_alive.return_value = False
        app._poll_application_close_wait()
        app._drain_ui_queue()
        self.assertFalse(app._is_closing)
        self.assertIsNotNone(app._appearance_check_after_id)
        app.root.after_cancel.assert_not_called()

    def test_unsafe_cache_operation_keeps_existing_blocking_warning(self):
        app = self.make_app()
        self.add_worker(app, "batch")
        app.is_cache_operation_running.return_value = True
        app._cache_operation = "archive"

        app.on_closing()

        app._show_warning.assert_called_once()
        app._ask_yes_no.assert_not_called()
        self.assertFalse(app.batch_processor.is_stopped)
        app.root.destroy.assert_not_called()
