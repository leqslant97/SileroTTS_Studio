"""Прогресс считает готовые фрагменты, а остановка не собирает неполную главу."""

import base64
import copy
import importlib.util
import queue
import shutil
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


class SynthesisFragmentProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = copy.deepcopy(studio.DEFAULT_CONFIG)
        config.update({
            "input_dir": str(self.root / "input"),
            "output_dir": str(self.root / "output"),
            "cache_dir": str(self.root / "cache"),
            "normalizer_enabled": False, "glossary_enabled": False,
            "auto_abbreviations": False, "auto_short_words": False,
            "synthesis_mode": "sentence", "separator_symbols": "---",
            "pause_file_start": 0, "pause_file_end": 0,
            "pause_sentence": 0, "pause_paragraph": 0,
            "pause_speech": 0, "pause_colon": 0, "max_retries": 1,
            "api_max_requests": 100, "cache_save_frequency": 100,
        })
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.processor = studio.TTSProcessor(config)
        self.addCleanup(self.processor.session.close)
        self.progress = []
        self.requests = []
        self.callbacks = []
        self.failures = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.response = mock.Mock(status_code=200)
        self.response.json.return_value = {"results": [{
            "audio": base64.b64encode(b"complete test audio").decode(),
        }]}
        self.processor.session.post = mock.Mock(return_value=self.response)

        def prepare_audio(source, destination, **_kwargs):
            shutil.copyfile(source, destination)

        for name, replacement in (
            ("_prepare_api_audio_file", mock.Mock(side_effect=prepare_audio)),
            ("_require_opus_audio_file", mock.Mock()),
            ("_canonicalize_cached_audio_if_needed", mock.Mock(return_value="opus")),
        ):
            patcher = mock.patch.object(studio, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        silence = self.root / "silence.ogg"
        silence.write_bytes(b"complete pause")
        self.processor._get_silence_file = mock.Mock(return_value=silence)
        self.processor._merge_save_and_notify = mock.Mock()

    def cache_text(self, text):
        normalized = self.processor.process_sentence_text(text)
        key = self.processor.get_hash(normalized)
        path = self.processor.cache_audio_dir / f"{key}.ogg"
        path.write_bytes(b"complete cached audio")
        self.processor.cache[key] = {
            "file_name": path.name, "normalized_text": normalized,
            "original_text": text, "speaker": self.processor.cfg["speaker"],
            "created_at": 1, "last_accessed": 1, "usage_count": 0,
            "audio_codec": "opus",
        }
        return key

    def blocked_response(self, *_args, **_kwargs):
        self.started.set()
        if not self.release.wait(5):
            raise AssertionError("Тест не разрешил завершить ответ API")
        return self.response

    def start_processing(self, text, *, save_to_disk=False, collect=True):
        result = []

        def run():
            try:
                result.append(self.processor.process_raw_text(
                    text, "chapter.mp3", save_to_disk=save_to_disk,
                    return_audio_files=collect,
                    progress_callback=lambda *event: self.progress.append(event),
                    request_callback=lambda text: self.requests.append(
                        (text, self.progress[-1][0])
                    ),
                    completion_callback=lambda *event: self.callbacks.append(event),
                ))
            except BaseException as exc:
                self.failures.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(lambda: worker.join(5))
        return worker, result

    def finish_processing(self, worker):
        self.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.failures, [])

    def test_single_fragment_stays_at_zero_until_http_answer_is_ready(self):
        self.processor.session.post.side_effect = self.blocked_response
        worker, result = self.start_processing("Новая фраза.")
        try:
            self.assertTrue(self.started.wait(5))
            self.assertEqual(self.progress, [(0, 1, "")])
            self.assertEqual(self.requests, [("Новая фраза.", 0)])
        finally:
            self.finish_processing(worker)
        self.assertEqual([event[0] for event in self.progress], [0, 1])
        self.assertEqual(result[0]["status"], "success")
        self.assertTrue(result[0]["audio_files"][0].is_file())

    def test_cache_pause_and_request_count_only_completed_fragments(self):
        self.cache_text("Готовая фраза.")
        self.processor.session.post.side_effect = self.blocked_response
        worker, result = self.start_processing(
            "Готовая фраза.\n---\nНовая фраза."
        )
        try:
            self.assertTrue(self.started.wait(5))
            self.assertEqual([event[:2] for event in self.progress], [
                (0, 3), (1, 3), (2, 3),
            ])
            self.assertEqual(self.requests, [("Новая фраза.", 2)])
        finally:
            self.finish_processing(worker)
        self.assertEqual([event[0] for event in self.progress], [0, 1, 2, 3])
        self.assertEqual(result[0]["status"], "success")
        self.processor.session.post.assert_called_once()

    def test_all_cache_hits_reach_full_progress_without_requests(self):
        self.cache_text("Первая фраза.")
        self.cache_text("Вторая фраза.")
        worker, result = self.start_processing("Первая фраза. Вторая фраза.")
        self.finish_processing(worker)
        self.assertEqual([event[:2] for event in self.progress], [
            (0, 2), (1, 2), (2, 2),
        ])
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(self.requests, [])
        self.processor.session.post.assert_not_called()

    def test_stop_during_last_failed_request_keeps_partial_chapter_unbuilt(self):
        self.cache_text("Готовая фраза.")

        def interrupted_response(*args, **kwargs):
            self.blocked_response(*args, **kwargs)
            raise studio.requests.ConnectionError("Соединение прервано")

        self.processor.session.post.side_effect = interrupted_response
        worker, _result = self.start_processing(
            "Готовая фраза. Новая фраза.", save_to_disk=True, collect=False
        )
        try:
            self.assertTrue(self.started.wait(5))
            self.processor.is_stopped = True
        finally:
            self.finish_processing(worker)
        self.assertEqual([event[0] for event in self.progress], [0, 1])
        self.assertEqual(self.callbacks, [("chapter.mp3", "error", None)])
        self.processor._merge_save_and_notify.assert_not_called()
        self.assertFalse((Path(self.processor.cfg["output_dir"]) / "chapter.mp3").exists())

    def test_stop_retains_successful_last_response_without_starting_build(self):
        self.cache_text("Готовая фраза.")
        self.processor.session.post.side_effect = self.blocked_response
        worker, _result = self.start_processing(
            "Готовая фраза. Новая фраза.", save_to_disk=True, collect=False
        )
        try:
            self.assertTrue(self.started.wait(5))
            self.processor.is_stopped = True
        finally:
            self.finish_processing(worker)
        self.assertEqual([event[0] for event in self.progress], [0, 1, 2])
        self.assertEqual(len(self.processor.cache), 2)
        self.assertTrue(self.processor.cache_index_path.is_file())
        self.assertEqual(self.callbacks, [("chapter.mp3", "error", None)])
        self.processor._merge_save_and_notify.assert_not_called()

    def test_failed_fragment_finishes_processing_with_warning(self):
        self.cache_text("Готовая фраза.")
        self.response.status_code = 422
        self.response.json.return_value = {"detail": "Некорректный текст"}
        self.response.raise_for_status.side_effect = studio.requests.HTTPError(
            "HTTP 422", response=self.response
        )
        worker, result = self.start_processing("Готовая фраза. Новая фраза.")
        self.finish_processing(worker)
        self.assertEqual([event[0] for event in self.progress], [0, 1, 2])
        self.assertEqual(result[0]["status"], "warning")
        self.assertTrue(result[0]["file_has_errors"])
        self.processor.session.post.assert_called_once()

    def make_direct_app(self):
        app = object.__new__(studio.TTSApp)
        app.direct_processor = None
        app.batch_processor = None
        app.config = self.processor.cfg.copy()
        app.direct_text = mock.Mock()
        app.direct_text.get.return_value = "Новая фраза."
        app.direct_prepared_text_var = mock.Mock()
        app.direct_prepared_text_var.get.return_value = False
        app.direct_output_dir_var = mock.Mock()
        app.direct_output_dir_var.get.return_value = str(self.root / "direct")
        app.settings_vars = {
            key: mock.Mock(get=mock.Mock(return_value=value))
            for key, value in {
                "direct_filename": "preview.mp3", "direct_force": False,
                "direct_save": False, "direct_autoplay": False,
                "direct_apply_tags": False,
            }.items()
        }
        for name in (
            "save_settings", "_discard_last_direct_preview", "_set_status_label",
            "_finish_direct_processing", "btn_direct_start", "btn_direct_stop",
            "btn_direct_hard_stop", "chk_direct_prepared_text", "chk_direct_force",
        ):
            setattr(app, name, mock.Mock())
        app._warn_if_cache_busy_for_synthesis = mock.Mock(return_value=False)
        app._validate_api_steps_ui = mock.Mock(return_value=True)
        app._validate_book_output_profile = mock.Mock(return_value=True)
        app._prepare_glossary_for_synthesis = mock.Mock(return_value=True)
        app._direct_processing_config = mock.Mock(return_value=self.processor.cfg)
        app._create_synthesis_processor = mock.Mock(return_value=self.processor)
        statuses = []
        app.lbl_direct_status = mock.Mock()
        app._set_status_label = lambda _label, text, _kind: statuses.append(text)
        app._post_to_ui = lambda callback, *args: callback(*args)
        preview = self.root / "preview.ogg"
        preview.write_bytes(b"complete preview")
        self.processor._run_ffmpeg_concat = mock.Mock(return_value=preview)
        self.processor.session.post.side_effect = self.blocked_response
        return app, statuses

    def test_direct_ui_counts_completed_fragments_during_active_request(self):
        app, statuses = self.make_direct_app()
        app.start_direct_processing()
        worker = app.direct_thread
        self.addCleanup(lambda: worker.join(5))
        try:
            self.assertTrue(self.started.wait(5))
            self.assertTrue(statuses)
            self.assertTrue(all(
                "0/1" in text for text in statuses if text.startswith("Синтез:")
            ))
        finally:
            self.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses[-1], "Синтез: 1/1...")
        self.assertEqual(app._finish_direct_processing.call_args.args[1]["status"], "success")

    def test_direct_stop_caption_is_preserved_after_late_successful_response(self):
        app, statuses = self.make_direct_app()
        app.start_direct_processing()
        worker = app.direct_thread
        self.addCleanup(lambda: worker.join(5))
        try:
            self.assertTrue(self.started.wait(5))
            self.processor.is_stopped = True
            statuses.append("Остановка...")
        finally:
            self.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses[-1], "Остановка...")
        self.assertEqual(len(self.processor.cache), 1)

    def test_direct_queued_progress_does_not_overwrite_stop_caption(self):
        app, statuses = self.make_direct_app()
        callbacks = queue.Queue()
        app._post_to_ui = lambda callback, *args: callbacks.put((callback, args))
        app.start_direct_processing()
        worker = app.direct_thread
        self.addCleanup(lambda: worker.join(5))
        try:
            self.assertTrue(self.started.wait(5))
            self.assertGreaterEqual(callbacks.qsize(), 2)
            self.processor.is_stopped = True
            app._set_status_label(app.lbl_direct_status, "Остановка...", "warning")
            while not callbacks.empty():
                callback, args = callbacks.get_nowait()
                callback(*args)
            self.assertEqual(statuses[-1], "Остановка...")
        finally:
            self.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_direct_queued_progress_belongs_only_to_the_original_run(self):
        app, statuses = self.make_direct_app()
        callbacks = queue.Queue()
        app._post_to_ui = lambda callback, *args: callbacks.put((callback, args))
        app.start_direct_processing()
        worker = app.direct_thread
        self.addCleanup(lambda: worker.join(5))
        try:
            self.assertTrue(self.started.wait(5))
            app.direct_processor = mock.Mock(is_stopped=False)
            app._set_status_label(app.lbl_direct_status, "Новый запуск", "info")
            while not callbacks.empty():
                callback, args = callbacks.get_nowait()
                callback(*args)
            self.assertEqual(statuses[-1], "Новый запуск")
        finally:
            self.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_source_stop_caption_is_preserved_when_stale_activity_is_drained(self):
        app = object.__new__(studio.TTSApp)
        app.batch_processor = mock.Mock(is_stopped=True)
        app.lbl_file_pct = mock.Mock()
        app.lbl_file_pct.cget.return_value = "40%"
        app._set_source_activity_status = mock.Mock()
        app.update_progress_ui(50, "Запоздалое сообщение", "Подготовка")
        app.lbl_file_pct.config.assert_called_once_with(text="50%")
        app._set_source_activity_status.assert_not_called()

    def configure_persistence_app(self, app):
        app.root = mock.Mock()
        app._closing_processors = ()
        app._shared_cache_dir = None
        app._shared_cache = None
        app._shared_processing_statuses = None
        app._shared_cache_lock = threading.RLock()
        app._shared_cache_eviction_state = {}
        app._shared_synthesis_inflight = {}
        app.shared_rate_limiter = mock.Mock()
        app.show_critical_error = mock.Mock()
        app._show_error = mock.Mock()
        app._show_warning = mock.Mock()
        app._show_info = mock.Mock()

    def assert_retained_cache_is_retried_before_next_run(self, app):
        self.assertIsNone(app.batch_processor)
        self.assertIsNone(app.direct_processor)
        self.assertEqual(app._closing_processors, (self.processor,))
        app._show_error.assert_called_once()
        app._show_warning.assert_not_called()
        processor = studio.TTSApp._create_synthesis_processor(app, self.processor.cfg)
        self.addCleanup(processor.session.close)
        self.assertEqual(app._closing_processors, ())
        self.assertEqual(len(processor.cache), 1)
        self.assertTrue(processor.cache_index_path.is_file())
        processor.session.post = mock.Mock(side_effect=AssertionError("Кэш уже готов"))
        result = processor.process_raw_text(
            "Новая фраза.", "retry.mp3", save_to_disk=False,
            return_audio_files=True,
        )
        self.assertEqual(result["status"], "success")
        processor.session.post.assert_not_called()

    def test_direct_stop_retains_cache_after_failed_write_and_retries_next_run(self):
        app, statuses = self.make_direct_app()
        self.configure_persistence_app(app)
        app.btn_direct_play = mock.Mock()
        app._finish_direct_processing = studio.TTSApp._finish_direct_processing.__get__(app)
        dialog_states = []

        def capture_error_dialog_state(*_args):
            dialog_states.append((
                statuses[-1], app.root.update_idletasks.called,
                app.btn_direct_start.config.call_args.kwargs.get("state"),
                app.btn_direct_stop.config.call_args.kwargs.get("state"),
            ))

        app._show_error.side_effect = capture_error_dialog_state
        with mock.patch.object(
            studio, "write_cache_index_atomic", side_effect=OSError("Диск недоступен")
        ):
            app.start_direct_processing()
            worker = app.direct_thread
            self.addCleanup(lambda: worker.join(5))
            try:
                self.assertTrue(self.started.wait(5))
                self.processor.is_stopped = True
            finally:
                self.release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(dialog_states, [(
            "Ошибка сохранения. Данные остаются в памяти.",
            True, studio.tk.NORMAL, studio.tk.DISABLED,
        )])
        self.assertFalse(self.processor.cache_index_path.exists())
        self.assert_retained_cache_is_retried_before_next_run(app)

    def test_batch_stop_retains_cache_after_failed_write_and_retries_next_run(self):
        app = object.__new__(studio.TTSApp)
        self.configure_persistence_app(app)
        app.config = self.processor.cfg.copy()
        app.batch_processor = self.processor
        app.direct_processor = None
        app.processor = self.processor
        app._batch_hard_stop_requested = False
        source = self.root / "chapter.txt"
        source.write_text("Новая фраза.", encoding="utf-8")
        app._source_path_by_id = {"chapter": source}
        for name in (
            "btn_start_all", "btn_start_sel", "btn_refresh", "btn_remove_sel",
            "btn_prepare_m4b_plan", "btn_source_group_selection", "btn_source_merge_groups",
            "btn_source_reset_groups", "btn_source_rename_group", "btn_source_group_tags",
            "btn_source_output_targets", "btn_source_settings", "chk_include_subdirs",
            "batch_prepared_text_var", "chk_batch_prepared_text", "btn_stop", "btn_hard_stop",
            "lbl_current_text", "_refresh_source_settings_summary", "_set_source_m4b_reflow_ui_state",
            "_set_source_file_progress_indeterminate", "_reset_pending_source_rows",
            "_reset_source_progress_ui", "_set_status_label", "_render_source_status_before_alert",
            "_save_source_session", "update_file_status", "update_progress_ui", "update_total_ui",
        ):
            setattr(app, name, mock.Mock())
        app._post_to_ui = lambda callback, *args: callback(*args)
        self.processor.session.post.side_effect = self.blocked_response
        with mock.patch.object(
            studio, "write_cache_index_atomic", side_effect=OSError("Диск недоступен")
        ):
            worker = threading.Thread(
                target=app.process_queue,
                args=(self.processor, ("chapter",), self.processor.cfg, True),
            )
            app.processing_thread = worker
            worker.start()
            self.addCleanup(lambda: worker.join(5))
            try:
                self.assertTrue(self.started.wait(5))
                self.processor.is_stopped = True
            finally:
                self.release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(self.processor._persistence_failed)
        self.assertFalse(self.processor.cache_index_path.exists())
        self.assert_retained_cache_is_retried_before_next_run(app)


if __name__ == "__main__":
    unittest.main()
