"""Фоновая задача освобождает интерфейс до уведомления о своём результате."""

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_regressions import studio
import test_audit_export


class TaskCompletionUITests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        app.root = mock.Mock()
        for name in ("_show_info", "_show_warning", "_show_error"):
            setattr(app, name, mock.Mock(side_effect=AssertionError("Задача открыла модальный диалог")))
        return app

    def batch_app(self, processor):
        app = self.make_app()
        app.batch_processor = processor
        app.direct_processor = None
        app.processor = processor
        app.processing_thread = object()
        app._batch_hard_stop_requested = False
        app._source_plan_dirty = True
        app._source_reload_after_processing = True
        app._source_run_has_m4b = True
        app._source_run_m4b_full = True
        app._source_run_plan_full = True
        app._source_run_has_targets = True
        for name in (
            "btn_start_all", "btn_start_sel", "btn_refresh", "btn_remove_sel",
            "btn_prepare_m4b_plan", "btn_source_group_selection", "btn_source_merge_groups",
            "btn_source_reset_groups", "btn_source_rename_group", "btn_source_group_tags",
            "btn_source_output_targets", "btn_source_settings", "chk_include_subdirs",
            "batch_prepared_text_var", "chk_batch_prepared_text", "btn_stop", "btn_hard_stop",
            "lbl_current_text", "_refresh_source_settings_summary", "_set_source_m4b_reflow_ui_state",
            "_set_source_file_progress_indeterminate", "_apply_pending_source_m4b_ui_plan",
            "_reset_pending_source_rows", "_reset_source_progress_ui", "_set_status_label",
            "_retain_unsaved_synthesis_processor", "_discard_processor_session_audio",
            "_release_shared_cache_if_idle", "_save_source_session", "load_files",
        ):
            setattr(app, name, mock.Mock())
        return app

    def test_batch_finalizes_success_error_stop_and_failed_save_before_result(self):
        for outcome in ("success", "error", "stop", "hard_stop", "save_error"):
            with self.subTest(outcome=outcome):
                processor = SimpleNamespace(
                    is_stopped=outcome in ("stop", "hard_stop"),
                    _persistence_failed=outcome == "save_error", cfg={"use_cache": False},
                )
                app = self.batch_app(processor)
                app._batch_hard_stop_requested = outcome == "hard_stop"
                observations = []

                def result(kind, title, message):
                    self.assertIsNone(app.batch_processor)
                    self.assertIsNone(app.processing_thread)
                    self.assertIsNone(app.processor)
                    self.assertFalse(app._batch_hard_stop_requested)
                    self.assertFalse(app._source_run_has_targets)
                    self.assertFalse(app._source_reload_after_processing)
                    self.assertEqual(app.btn_start_all.config.call_args.kwargs["state"], studio.tk.NORMAL)
                    self.assertEqual(app.btn_stop.config.call_args.kwargs["state"], studio.tk.DISABLED)
                    app._set_source_file_progress_indeterminate.assert_called_once_with(False)
                    app._release_shared_cache_if_idle.assert_called_once()
                    app._save_source_session.assert_called_once()
                    app.load_files.assert_called_once()
                    if processor.is_stopped:
                        app._reset_source_progress_ui.assert_called_once()
                    observations.append((kind, title, message))

                app._show_task_notification = mock.Mock(side_effect=result)
                app.finish_processing(processor, outcome == "error")
                self.assertEqual(len(observations), 1)
                expected = "info" if outcome == "success" else "warning" if processor.is_stopped else "error"
                self.assertEqual(observations[0][0], expected)

    def test_direct_failed_save_releases_processor_and_updates_playback_before_result(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "result.mp3"
            audio.write_bytes(b"completed audio")
            processor = SimpleNamespace(is_stopped=False)
            app = self.make_app()
            app.direct_processor = app.processor = processor
            app.direct_thread = object()
            app.batch_processor = None
            app._direct_hard_stop_requested = False
            for name in (
                "btn_direct_start", "btn_direct_stop", "btn_direct_hard_stop", "btn_direct_play",
                "direct_prepared_text_var", "chk_direct_prepared_text", "chk_direct_force",
                "lbl_direct_status", "_set_status_label", "_retain_unsaved_synthesis_processor",
                "_release_shared_cache_if_idle", "play_last_audio",
            ):
                setattr(app, name, mock.Mock())

            def result(kind, _title, _message):
                self.assertEqual(kind, "error")
                self.assertIsNone(app.direct_processor)
                self.assertIsNone(app.direct_thread)
                self.assertIsNone(app.processor)
                self.assertEqual(app.last_direct_audio, str(audio))
                self.assertEqual(app.btn_direct_play.config.call_args.kwargs["state"], studio.tk.NORMAL)
                app.play_last_audio.assert_called_once()
                app._release_shared_cache_if_idle.assert_called_once()

            app._show_task_notification = mock.Mock(side_effect=result)
            app._finish_direct_processing(
                processor, {"status": "warning", "audio": str(audio), "persistence_failed": True},
                True, True,
            )
            app._show_task_notification.assert_called_once()

    def test_export_finisher_releases_controls_and_animation_before_result(self):
        for outcome in ("success", "warning", "error", "stopped"):
            with self.subTest(outcome=outcome):
                app = self.make_app()
                app._export_thread = object()
                app._export_running = True
                app.is_export_stopped = outcome == "stopped"
                app.export_progress = mock.Mock()
                app._set_export_running_state = lambda value: setattr(app, "_export_running", value)
                app._set_export_progress_indeterminate = mock.Mock()
                app._set_export_status = mock.Mock()

                def result(*_args):
                    self.assertFalse(app._export_running)
                    self.assertIsNone(app._export_thread)
                    self.assertFalse(app.is_export_stopped)
                    app._set_export_progress_indeterminate.assert_called_once_with(False)
                    if outcome == "stopped":
                        app.export_progress.configure.assert_called_once_with(value=0)
                        app._set_export_status.assert_called_once_with("Ожидание...", "info")
                    else:
                        app.export_progress.configure.assert_not_called()

                app._show_task_notification = mock.Mock(side_effect=result)
                app._finish_export_process_ui(outcome, ("info", "Результат", outcome))
                app._show_task_notification.assert_called_once()

    def test_tag_worker_enqueues_result_with_finisher_for_every_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"source")
            for outcome in ("success", "warning", "error", "stopped"):
                with self.subTest(outcome=outcome):
                    app = test_audit_export.GroupTagInheritanceTests.make_app(source)
                    app._set_export_running_state = lambda value: setattr(app, "_export_running", value)
                    app._finish_export_process_ui = studio.TTSApp._finish_export_process_ui.__get__(app)
                    pending = []
                    app._post_to_ui = lambda callback, *args, **kwargs: pending.append((callback, args, kwargs))
                    if outcome == "error":
                        app._update_file_tags_inplace = mock.Mock(side_effect=RuntimeError("Сбой тегов"))
                    elif outcome == "stopped":
                        app._update_file_tags_inplace = mock.Mock(side_effect=InterruptedError())
                    else:
                        app._update_file_tags_inplace = mock.Mock(return_value=outcome == "success")
                    with mock.patch.object(studio.threading.Thread, "start"):
                        app.start_export_process()
                    app._export_thread._target()
                    self.assertTrue(app._export_running)
                    terminal = [item for item in pending if item[0] == app._finish_export_process_ui]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0][1][0], outcome)

                    def result(*_args):
                        self.assertFalse(app._export_running)
                        self.assertIsNone(app._export_thread)
                    app._show_task_notification = mock.Mock(side_effect=result)
                    for callback, args, kwargs in pending:
                        callback(*args, **kwargs)
                    expected = "info" if outcome == "success" else "error" if outcome == "error" else "warning"
                    self.assertEqual(app._show_task_notification.call_args.args[0], expected)
                    for name in ("_show_info", "_show_warning", "_show_error"):
                        getattr(app, name).assert_not_called()

    def test_book_import_finishes_queue_actions_before_success_or_error_result(self):
        for outcome in ("success", "partial", "error", "exception"):
            with self.subTest(outcome=outcome):
                app = self.make_app()
                app.config["import_outdir"] = "/tmp/import-result"
                app.settings_vars = {
                    name: mock.Mock(get=lambda current=value: current)
                    for name, value in {"import_template": "{name}_{num}", "import_regex": "", "import_single_file": False}.items()
                }
                app.import_filepaths = ["first.txt", "second.txt"]
                app.import_filepath_var = mock.Mock(get=lambda: "")
                for name in ("save_settings", "_update_import_queue_actions", "_set_status_label", "load_files", "lbl_import_status"):
                    setattr(app, name, mock.Mock())
                pending = []
                app._post_to_ui = lambda callback, *args, **kwargs: pending.append((callback, args, kwargs))
                successful = {"path": "first.txt", "error": None, "saved_files": ["one.txt"]}
                failed = {"path": "second.txt", "error": "Сбой чтения", "saved_files": []}
                results = [successful, failed] if outcome == "partial" else [failed] if outcome == "error" else [successful]
                with (
                    mock.patch.object(studio.threading.Thread, "start"),
                    mock.patch.object(studio.BookExtractor, "import_files", return_value=results,
                                      side_effect=RuntimeError("Не прочитано") if outcome == "exception" else None),
                ):
                    app.start_import()
                    app._import_thread._target()

                def result(*_args):
                    self.assertFalse(app._import_running)
                    self.assertIsNone(app._import_thread)
                    self.assertEqual(app._update_import_queue_actions.call_count, 2)
                    self.assertTrue(app._set_status_label.called)

                app._show_task_notification = mock.Mock(side_effect=result)
                for callback, args, kwargs in pending:
                    callback(*args, **kwargs)
                app._show_task_notification.assert_called_once()
                expected = "info" if outcome == "success" else "warning" if outcome == "partial" else "error"
                self.assertEqual(app._show_task_notification.call_args.args[0], expected)
                self.assertEqual(app.load_files.called, outcome in ("success", "partial"))

    def test_cache_optimization_updates_loaded_table_and_closes_wait_window_before_result(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                app = self.make_app()
                app._cache_operation = "optimize"
                app._cache_ui_loaded = True
                app.cache_data = {"stale": {}}
                app._cache_generation_changed = mock.Mock()
                app.filter_cache = mock.Mock()
                app.lbl_cache_count = mock.Mock()
                popup = mock.Mock()
                app._end_cache_operation = lambda _kind: setattr(app, "_cache_operation", None)
                app._close_popup_safely = mock.Mock()

                def result(*_args):
                    self.assertIsNone(app._cache_operation)
                    app._close_popup_safely.assert_called_once_with(popup)
                    self.assertEqual(app.cache_data, {"retained": {}})
                    app.lbl_cache_count.config.assert_called_once_with(text="Всего записей: 1")
                    app.filter_cache.assert_called_once()

                app._show_task_notification = mock.Mock(side_effect=result)
                app._finish_cache_optimization(
                    popup, {"retained": {}}, "Не сохранено" if failed else None,
                    ("info", "Готово", "Лишних записей нет"),
                )
                self.assertEqual(app._show_task_notification.call_args.args[0], "error" if failed else "info")

    def test_cache_optimization_early_returns_also_finalize_before_result(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for empty_cache in (False, True):
                with self.subTest(empty_cache=empty_cache):
                    if empty_cache:
                        (source / "one.txt").write_text("Текст", encoding="utf-8")
                    app = self.make_app()
                    app.config["input_dir"] = str(source)
                    cache = source / "cache"
                    cache.mkdir(exist_ok=True)
                    (cache / "sentence_cache.json").write_text("{}")
                    app.config["cache_dir"] = str(cache)
                    app._cache_ui_loaded = False
                    app.is_synthesis_running = lambda: False
                    app._validate_api_steps_ui = lambda **_kwargs: True
                    app.save_settings = mock.Mock()
                    app._choose_cache_variant_policy = lambda _config: studio.CACHE_VARIANT_KEEP_ALL
                    app._ask_yes_no = mock.Mock(return_value=True)
                    popup = mock.Mock()
                    app._confirm_cache_mutation = mock.Mock(return_value=False)
                    app._create_wait_popup = mock.Mock(return_value=popup)

                    def begin(kind, **_kwargs):
                        app._cache_operation = kind
                        return popup
                    app._begin_cache_operation = begin
                    app._end_cache_operation = lambda _kind: setattr(app, "_cache_operation", None)
                    app._close_popup_safely = mock.Mock()
                    app._cache_generation_changed = mock.Mock()
                    pending = []
                    app._post_to_ui = lambda callback, *args, **kwargs: pending.append((callback, args, kwargs))
                    processor = SimpleNamespace(
                        cache={} if empty_cache else {"existing": {}},
                        cache_dir=source / "cache",
                    )

                    def result(*_args):
                        self.assertIsNone(app._cache_operation)
                        app._close_popup_safely.assert_called_once_with(popup)
                    app._show_task_notification = mock.Mock(side_effect=result)
                    with (
                        mock.patch.object(studio, "TTSProcessor", return_value=processor),
                        mock.patch.object(studio, "read_cache_index_with_backup", return_value=(
                            processor.cache, processor.cache_dir / "sentence_cache.json", [],
                        )),
                        mock.patch.object(studio.threading, "Thread") as thread,
                    ):
                        app.optimize_cache()
                        thread.call_args.kwargs["target"]()
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0][0], app._finish_cache_optimization)
                    pending[0][0](*pending[0][1], **pending[0][2])
                    self.assertEqual(app._show_task_notification.call_args.args[0], "info" if empty_cache else "warning")

    def test_m4b_chapter_import_releases_tree_and_saves_before_result(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.m4b"
            source.write_bytes(b"container")
            for failed in (False, True):
                with self.subTest(failed=failed):
                    app = self.make_app()
                    app.export_groups = {}
                    app.export_files = {}
                    app.export_tree = mock.Mock()
                    app.export_outdir_var = mock.Mock(get=lambda: directory)
                    for name in (
                        "save_settings", "_set_export_status", "_set_export_ui_state",
                        "_schedule_export_project_save", "update_group_duration", "update_total_export_duration",
                    ):
                        setattr(app, name, mock.Mock())
                    pending = []
                    app._post_to_ui = lambda callback, *args: pending.append((callback, args))
                    prepared = {
                        "group": {"name": "Книга", "source_kind": "m4b_chapters", "source_path": str(source)},
                        "chapters": [{"path": str(source), "title": "Глава", "duration": 1}],
                    }
                    with (
                        mock.patch.object(studio.threading.Thread, "start"),
                        mock.patch.object(studio, "probe_m4b_chapters", return_value={}),
                        mock.patch.object(studio, "build_m4b_virtual_export_group", return_value=prepared,
                                          side_effect=ValueError("Нет глав") if failed else None),
                    ):
                        app.add_export_m4b_chapters([str(source)])
                        app._export_import_thread._target()

                    def result(*_args):
                        self.assertFalse(app._export_lock)
                        self.assertIsNone(app._export_import_thread)
                        self.assertEqual(app._set_export_ui_state.call_args.args[0], studio.tk.NORMAL)
                        app._schedule_export_project_save.assert_called_once()
                        self.assertEqual(len(app.export_groups), 0 if failed else 1)
                        self.assertEqual(len(app.export_files), 0 if failed else 1)
                    app._show_task_notification = mock.Mock(side_effect=result)
                    for callback, args in pending:
                        callback(*args)
                    self.assertEqual(app._show_task_notification.call_args.args[0], "error" if failed else "info")

    def test_normalizer_finishes_or_recovers_failed_thread_launch_before_result(self):
        for launch_failure in (False, True):
            with self.subTest(launch_failure=launch_failure):
                app = self.make_app()
                app._normalizer_batch_running = not launch_failure
                app._normalizer_batch_thread = object()
                app._normalizer_batch_cancel = threading.Event()
                for name in ("btn_batch_normalize_text", "btn_cancel_batch_normalize", "lbl_normalizer_preview_status", "_set_status_label"):
                    setattr(app, name, mock.Mock())

                def result(*_args):
                    self.assertFalse(app._normalizer_batch_running)
                    self.assertIsNone(app._normalizer_batch_thread)
                    self.assertEqual(app.btn_batch_normalize_text.configure.call_args.kwargs["state"], studio.tk.NORMAL)
                    self.assertEqual(app.btn_cancel_batch_normalize.configure.call_args.kwargs["state"], studio.tk.DISABLED)

                app._show_task_notification = mock.Mock(side_effect=result)
                if launch_failure:
                    with mock.patch.object(studio.threading.Thread, "start", side_effect=RuntimeError("Нет потока")):
                        app._start_batch_normalization(Path("source"), Path("output"), object(),
                                                       include_subdirs=False, overwrite=False, files=["one.txt"])
                else:
                    app._finish_batch_normalization({"total": 1, "written": 1})
                app._show_task_notification.assert_called_once()

    def test_stale_source_plan_resets_progress_and_releases_controls_before_warning(self):
        app = self.make_app()
        app._source_plan_running = True
        app._source_plan_snapshot = None
        app._source_path_by_id = {"file": Path("file.txt")}
        app._source_tree_file_ids = lambda: ()
        app.tree = mock.Mock()
        app.btn_prepare_m4b_plan = mock.Mock()
        app.lbl_current_text = mock.Mock()
        app._set_status_label = mock.Mock()
        app._reset_source_progress_ui = mock.Mock()

        def result(*_args):
            self.assertFalse(app._source_plan_running)
            app._reset_source_progress_ui.assert_called_once()
            self.assertEqual(app.btn_prepare_m4b_plan.configure.call_args.kwargs["state"], studio.tk.NORMAL)
        app._show_task_notification = mock.Mock(side_effect=result)
        app._finish_m4b_source_plan([["file"]], {})
        self.assertEqual(app._show_task_notification.call_args.args[0], "warning")

    def test_api_error_posts_nonblocking_notification_without_native_dialog(self):
        app = self.make_app()
        app._post_to_ui = mock.Mock()
        app._show_task_notification = mock.Mock()
        app.show_critical_error("Неверный токен")
        app._post_to_ui.assert_called_once_with(
            app._show_task_notification, "error", "Критическая ошибка API", "Неверный токен"
        )


if __name__ == "__main__":
    unittest.main()
