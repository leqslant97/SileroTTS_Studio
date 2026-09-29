"""Проверки возобновления после частичных ошибок синтеза и кодирования."""

import copy
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


class SourceFailureResumeAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "chapter.txt"
        self.source.write_text("Первая фраза. Вторая фраза.", encoding="utf-8")
        self.fragment = self.root / "fragment.ogg"
        self.fragment.write_bytes(b"canonical audio")
        self.processor = mock.Mock()
        self.processor.cfg = {"use_cache": True}
        self.processor.is_stopped = False
        self.processor.active_threads = []
        self.processor.encode_semaphore = None
        self.processor.processing_statuses_ram = {}
        self.processor.process_text_file.return_value = {
            "status": "success", "audio_files": (self.fragment,),
        }

        def mark_status(path, status):
            key = str(Path(path).resolve())
            if status in {"error", "warning"}:
                self.processor.processing_statuses_ram[key] = status
            else:
                self.processor.processing_statuses_ram.pop(key, None)

        self.processor._mark_output_status.side_effect = mark_status
        self.app = object.__new__(studio.TTSApp)
        self.app._source_path_by_id = {"chapter": self.source}
        for name in (
            "finish_processing", "update_total_ui", "update_progress_ui",
            "update_file_status", "_remember_source_runtime_m4b_records",
        ):
            setattr(self.app, name, mock.Mock())
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        self.records = tuple(
            {
                "kind": "m4b" if fmt == "m4b" else "file",
                "target_index": index,
                "target": studio.normalize_output_target({"format": fmt}),
                "item_id": "group" if fmt == "m4b" else "chapter",
                "file_ids": ("chapter",), "source_paths": (self.source,),
                "source_path": self.source, "path": self.root / f"chapter.{fmt}",
            }
            for index, fmt in enumerate(("mp3", "opus", "m4b"))
        )
        self.config = {
            "input_dir": str(self.root), "output_dir": str(self.root),
            "output_format": "mp3", "source_multi_output": True,
            "source_path_by_id": self.app._source_path_by_id,
            "source_target_records": self.records,
            "source_m4b_auto_split_long": False,
        }
        self.encoded = []

        def encode(_audio, output, **_kwargs):
            self.encoded.append(Path(output).suffix)
            Path(output).write_bytes(f"output {len(self.encoded)}".encode())

        for name in ("_export_merged_audio_ffmpeg", "_export_m4b_ffmpeg"):
            patcher = mock.patch.object(studio, name, side_effect=encode)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_queue(self, config=None):
        self.app.process_queue(
            self.processor, ("chapter",), copy.deepcopy(config or self.config), True
        )

    def test_partial_speech_failure_keeps_all_outputs_resumable(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                self.processor.processing_statuses_ram.clear()
                for record in self.records:
                    record["path"].unlink(missing_ok=True)
                config = dict(self.config, source_m4b_reflow_actual_duration=deferred)
                self.processor.process_text_file.return_value = {
                    "status": "warning", "audio_files": (self.fragment,),
                }
                with mock.patch.object(
                    studio, "_measure_source_m4b_chapter_durations",
                    return_value={"chapter": 1.0},
                ), mock.patch.object(
                    studio, "reflow_source_m4b_target_records",
                    return_value={"records": (self.records[-1],), "durations": {"chapter": 1.0}},
                ):
                    self.run_queue(config)
                for record in self.records:
                    self.assertTrue(record["path"].is_file())
                    self.assertEqual(
                        self.processor.processing_statuses_ram.get(str(record["path"])),
                        "warning",
                    )
                self.assertTrue(self.app.finish_processing.call_args.args[-1])
                before = len(self.encoded)
                self.processor.process_text_file.return_value = {
                    "status": "success", "audio_files": (self.fragment,),
                }
                self.run_queue()
                self.assertEqual(len(self.encoded), before + 3)
                self.assertEqual(self.processor.processing_statuses_ram, {})
                self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_one_failed_format_is_reported_without_repeating_successful_format(self):
        def encode(_audio, output, **_kwargs):
            if Path(output).suffix == ".opus":
                raise OSError("Нет места для Opus")
            self.encoded.append(Path(output).suffix)
            Path(output).write_bytes(b"complete audio")

        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            self.run_queue()
        self.assertTrue(self.app.finish_processing.call_args.args[-1])
        self.assertEqual(self.encoded.count(".mp3"), 1)
        self.run_queue()
        self.assertEqual(self.encoded.count(".mp3"), 1)
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_legacy_errors_reach_queue_completion(self):
        config = {
            "input_dir": str(self.root), "output_dir": str(self.root),
            "output_format": "mp3", "source_path_by_id": self.app._source_path_by_id,
        }
        for failure in ("missing", "read", "encoding"):
            with self.subTest(failure=failure):
                self.processor.process_text_file.side_effect = None
                self.source.write_text("Текст главы.", encoding="utf-8")
                if failure == "missing":
                    self.source.unlink()
                elif failure == "read":
                    self.processor.process_text_file.side_effect = OSError("Ошибка чтения")
                else:
                    def start_failed_encoder(_path, **kwargs):
                        worker = threading.Thread(
                            target=kwargs["completion_callback"],
                            args=("chapter.mp3", "error", None),
                        )
                        self.processor.active_threads.append(worker)
                        worker.start()
                    self.processor.process_text_file.side_effect = start_failed_encoder
                self.run_queue(config)
                self.assertTrue(self.app.finish_processing.call_args.args[-1])


class ClosingAuditTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app._is_closing = False
        app.is_cache_operation_running = mock.Mock(return_value=False)
        app._normalizer_batch_running = False
        app.batch_processor = None
        app.direct_processor = None
        app.root = mock.Mock()
        for name in (
            "_show_warning", "_show_error", "_save_export_project_session",
            "_discard_last_direct_preview", "save_settings",
        ):
            setattr(app, name, mock.Mock())
        for name in (
            "_export_session_save_after_id", "_appearance_check_after_id",
            "_mac_restore_refresh_after_id", "_settings_save_after_id",
        ):
            setattr(app, name, f"pending:{name}")
        return app

    def test_closing_waits_for_batch_normalization(self):
        app = self.make_app()
        app._normalizer_batch_running = True

        app.on_closing()

        app.root.destroy.assert_not_called()
        app.root.after_cancel.assert_not_called()
        app._show_warning.assert_called_once()
        self.assertFalse(app._is_closing)
        app._normalizer_batch_running = False
        app.on_closing()
        app.root.destroy.assert_called_once()

    def test_failed_final_save_keeps_window_working_and_can_be_retried(self):
        for failure in (
            "cache_exception", "cache_false", "statuses", "statuses_false",
            "settings", "export_tree",
        ):
            with self.subTest(failure=failure):
                app = self.make_app()
                processor = app.batch_processor = mock.Mock(is_stopped=False)
                if failure == "cache_exception":
                    processor.flush_cache.side_effect = OSError("Недоступен диск")
                elif failure == "cache_false":
                    processor.flush_cache.return_value = False
                elif failure == "statuses":
                    processor._save_processing_statuses.side_effect = OSError("Нет места")
                elif failure == "statuses_false":
                    processor._save_processing_statuses.return_value = False
                elif failure == "export_tree":
                    app._save_export_project_session.return_value = False
                else:
                    app.save_settings.return_value = False

                app.on_closing()

                self.assertFalse(app._is_closing)
                app.root.destroy.assert_not_called()
                app.root.after_cancel.assert_not_called()
                app._show_error.assert_called_once()
                processor._save_processing_statuses.assert_called_once()
                app.save_settings.assert_called_once()
                processor.is_stopped = True
                processor.flush_cache.side_effect = None
                processor.flush_cache.return_value = True
                processor._save_processing_statuses.side_effect = None
                processor._save_processing_statuses.return_value = True
                app.save_settings.return_value = True
                app._save_export_project_session.return_value = True

                app.on_closing()

                app.root.destroy.assert_called_once()
                self.assertTrue(app._is_closing)

    def test_persistence_failures_are_observable_and_preserve_previous_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "processing_statuses.json"
            status_path.write_text('{"old": "warning"}', encoding="utf-8")
            processor = object.__new__(studio.TTSProcessor)
            processor.cache_lock = threading.RLock()
            processor.processing_statuses_ram = {"new": "error"}
            app = self.make_app()
            app.export_tree = mock.Mock()
            app._build_export_project_snapshot = mock.Mock(return_value={"items": []})
            app._write_json_atomic = mock.Mock(side_effect=OSError("Нет места"))
            with mock.patch.object(studio, "APP_DATA_DIR", root), mock.patch.object(
                studio.os, "replace", side_effect=OSError("Нет места")
            ):
                self.assertFalse(processor._save_processing_statuses())
                self.assertEqual(status_path.read_text(encoding="utf-8"), '{"old": "warning"}')
                self.assertEqual(processor.processing_statuses_ram, {"new": "error"})
                self.assertFalse(studio.TTSApp._save_export_project_session(app))
            del app.export_tree
            self.assertTrue(studio.TTSApp._save_export_project_session(app))


if __name__ == "__main__":
    unittest.main()
