"""Очередь синтеза с независимыми файлами, склейками групп и M4B."""

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


class SourceGroupOutputRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.source.mkdir()
        self.output = self.root / "audio"
        # Настоящий TTSProcessor создаёт папку вывода до запуска очереди.
        self.output.mkdir()
        self.paths = {}
        self.fragments = {}
        for number in range(1, 5):
            key = f"chapter-{number}"
            path = self.source / f"{number:03d}.txt"
            path.write_text(f"Глава {number}. Текст для синтеза.", encoding="utf-8")
            self.paths[key] = path
            fragment = self.root / f"fragment-{number}.ogg"
            fragment.write_bytes(f"canonical fragment {number}".encode())
            self.fragments[key] = fragment
        self.keys = tuple(self.paths)
        self.groups = [
            {"id": "group-1", "name": "Первая группа", "file_ids": self.keys[:2]},
            {"id": "group-2", "name": "Вторая группа", "file_ids": self.keys[2:]},
        ]
        self.processor = mock.Mock()
        self.processor.cfg = {"use_cache": True}
        self.processor.is_stopped = False
        self.processor.active_threads = []
        self.processor.encode_semaphore = None
        self.processor.processing_statuses_ram = {}
        self.processor.process_text_file.side_effect = self.collect
        self.processor.cache_lock = threading.RLock()
        self.processor._mark_output_status.side_effect = (
            studio.TTSProcessor._mark_output_status.__get__(self.processor)
        )
        self.app = object.__new__(studio.TTSApp)
        self.app._source_plan_dirty = True
        self.app._invalidate_source_runtime_m4b_plan()
        self.app._source_path_by_id = self.paths
        for name in (
            "finish_processing", "update_total_ui", "update_progress_ui",
            "update_file_status", "_remember_source_runtime_m4b_records",
        ):
            setattr(self.app, name, mock.Mock())
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        self.encoded = []
        self.encoding_lock = threading.Lock()
        for name in ("_export_merged_audio_ffmpeg", "_export_m4b_ffmpeg"):
            patcher = mock.patch.object(studio, name, side_effect=self.encode)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure()

    def configure(self, targets=None):
        self.targets = targets or [
            {"format": "mp3", "bitrate": "128k", "assembly_mode": "files"},
            {"format": "mp3", "bitrate": "128k", "assembly_mode": "merge"},
            {"format": "m4b", "bitrate": "96k"},
        ]
        self.records = studio.plan_source_synthesis_target_paths(
            self.source, self.output,
            [{"id": key, "path": path} for key, path in self.paths.items()],
            self.targets, source_m4b_groups=self.groups, book_name="Книга",
        )
        self.config = {
            "input_dir": str(self.source), "output_dir": str(self.output),
            "output_format": "mp3", "source_multi_output": True,
            "source_path_by_id": self.paths, "synthesis_targets": self.targets,
            "source_target_records": self.records,
            "source_m4b_groups": self.groups,
            "source_virtual_group_ids": [group["id"] for group in self.groups],
            "source_m4b_auto_split_long": False,
            "max_parallel_encodes": 2,
        }

    def collect(self, path, **_kwargs):
        key = next(key for key, source in self.paths.items() if source == Path(path))
        return {"status": "success", "audio_files": (self.fragments[key],)}

    def encode(self, audio, output, **kwargs):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.encoding_lock:
            self.encoded.append((path, tuple(Path(value) for value in audio), kwargs))
            path.write_bytes(f"encoded {len(self.encoded)}".encode())

    def run_queue(self, *, skip=True, planned=True):
        config = copy.deepcopy(self.config)
        config["source_plan_revision"] = self.app._source_plan_revision
        config["source_targets_rebuild_paths"] = self.app._source_regular_rebuild_paths(self.records)
        if not planned:
            config.pop("source_target_records", None)
        self.app.process_queue(self.processor, self.keys, config, skip)

    def output_records(self, kind):
        return [record for record in self.records if record["kind"] == kind]

    def test_each_chapter_is_collected_once_for_files_groups_and_m4b(self):
        self.run_queue()

        self.assertEqual(self.processor.process_text_file.call_count, 4)
        self.assertCountEqual(
            [call.args[0] for call in self.processor.process_text_file.call_args_list],
            list(self.paths.values()),
        )
        self.assertEqual(len(self.encoded), 8)
        self.assertCountEqual([path for path, _, _ in self.encoded], [record["path"] for record in self.records])
        for record in self.output_records("group"):
            encoded = next(entry for entry in self.encoded if entry[0] == record["path"])
            self.assertEqual(encoded[1], tuple(self.fragments[key] for key in record["file_ids"]))
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_group_only_run_skips_everything_when_groups_are_ready(self):
        self.configure([{"format": "opus", "bitrate": "48k", "assembly_mode": "merge"}])
        self.run_queue()
        self.assertEqual(self.processor.process_text_file.call_count, 4)
        self.assertEqual(len(self.encoded), 2)
        before = {record["path"]: record["path"].stat().st_mtime_ns for record in self.records}
        self.processor.process_text_file.reset_mock()
        self.app.update_file_status.reset_mock()

        self.run_queue()

        self.processor.process_text_file.assert_not_called()
        self.assertEqual(len(self.encoded), 2)
        self.assertTrue(all(path.stat().st_mtime_ns == stamp for path, stamp in before.items()))
        final_statuses = {
            str(call.args[0]): call.args[1]
            for call in self.app.update_file_status.call_args_list
        }
        self.assertEqual(
            {group["id"]: final_statuses.get(group["id"]) for group in self.groups},
            {group["id"]: "success" for group in self.groups},
        )
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_runtime_fallback_planning_keeps_configured_group_boundaries(self):
        self.configure([{"format": "mp3", "assembly_mode": "merge"}])

        self.run_queue(planned=False)

        self.assertEqual(len(self.encoded), 2)
        self.assertCountEqual([entry[0] for entry in self.encoded], [record["path"] for record in self.records])
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_restored_m4b_plans_insert_each_target_partition_only_once(self):
        self.configure([
            {"format": "mp3", "assembly_mode": "files"},
            {"format": "mp3", "assembly_mode": "merge"},
            {"format": "m4b", "bitrate": "64k"},
            {"format": "m4b", "bitrate": "96k"},
        ])
        runtime = copy.deepcopy(self.output_records("m4b"))
        for record in runtime:
            record["path"] = record["path"].with_stem(record["path"].stem + " принятый план")
        self.app._source_runtime_m4b_records = tuple(runtime)
        self.app._source_plan_dirty = False

        restored = self.app._reuse_source_runtime_m4b_records(self.records, self.keys, self.paths)

        ordinary = tuple(record for record in self.records if record["kind"] != "m4b")
        self.assertEqual(tuple(record for record in restored if record["kind"] != "m4b"), ordinary)
        self.assertEqual(tuple(record for record in restored if record["kind"] == "m4b"), tuple(runtime))
        self.assertEqual(len({record["path"] for record in restored}), len(restored))

    def test_m4b_duration_reflow_preserves_independent_mp3_group_boundaries(self):
        original_groups = {group["id"]: copy.deepcopy(group) for group in self.groups}
        self.app._source_plan_groups = copy.deepcopy(original_groups)
        self.app._source_tree_file_ids = mock.Mock(return_value=list(self.keys))
        self.app._finish_m4b_source_plan = mock.Mock()
        self.app._remember_source_runtime_m4b_records = (
            studio.TTSApp._remember_source_runtime_m4b_records.__get__(self.app)
        )
        m4b = self.output_records("m4b")[0]
        reflowed = tuple(
            dict(
                copy.deepcopy(m4b),
                path=m4b["path"].with_name(f"Часть {number}.m4b"),
                item_id=f"reflow-{number}", group_name=f"Часть {number}",
                file_ids=(key,), source_paths=(self.paths[key],),
                group_index=number, parts=len(self.keys),
                chapter_start=number, chapter_end=number,
            )
            for number, key in enumerate(self.keys, 1)
        )
        self.config["source_m4b_reflow_actual_duration"] = True
        with mock.patch.object(
            studio, "_measure_source_m4b_chapter_durations",
            return_value={key: 1.0 for key in self.keys},
        ), mock.patch.object(
            studio, "reflow_source_m4b_target_records",
            return_value={"records": reflowed, "changed": True, "durations": {key: 1.0 for key in self.keys}},
        ):
            self.run_queue()

        self.assertFalse(self.app.finish_processing.call_args.args[-1])
        self.assertTrue(self.app._pending_source_m4b_ui_plan["preserve_group_tree"])
        self.app._apply_pending_source_m4b_ui_plan()
        self.app._finish_m4b_source_plan.assert_not_called()
        self.assertEqual(self.app._source_plan_groups, original_groups)
        self.assertEqual(self.app._source_runtime_m4b_records, reflowed)

        self.app._source_plan_dirty = False
        planned = studio.plan_source_synthesis_target_paths(
            self.source, self.output,
            [{"id": key, "path": path} for key, path in self.paths.items()],
            self.targets, source_m4b_groups=self.app._source_plan_groups,
            book_name="Книга",
        )
        reused = self.app._reuse_source_runtime_m4b_records(planned, self.keys, self.paths)
        self.assertEqual(
            [record for record in reused if record["kind"] == "group"],
            self.output_records("group"),
        )
        self.assertEqual(tuple(record for record in reused if record["kind"] == "m4b"), reflowed)
        self.config["source_target_records"] = reused
        self.encoded.clear()
        self.processor.process_text_file.reset_mock()

        with mock.patch.object(studio, "reflow_source_m4b_target_records") as reflow, mock.patch.object(
            studio, "_measure_source_m4b_chapter_durations"
        ) as measure:
            self.run_queue()
        reflow.assert_not_called()
        measure.assert_not_called()

        self.processor.process_text_file.assert_not_called()
        self.assertEqual(self.encoded, [])
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_ready_m4b_is_not_preencoded_while_restoring_another_volume(self):
        self.run_queue()
        deleted, ready = self.output_records("m4b")
        deleted["path"].unlink()
        before = ready["path"].stat().st_mtime_ns
        self.config["source_m4b_auto_split_long"] = True
        durations = {key: 1.0 for key in self.keys}
        with mock.patch.object(
            studio, "_measure_source_m4b_chapter_durations", return_value=durations,
        ), mock.patch.object(
            studio, "reflow_source_m4b_target_records",
            return_value={"records": tuple(self.output_records("m4b")), "changed": False, "durations": durations},
        ), mock.patch.object(
            studio, "run_source_synthesis_m4b_target", wraps=studio.run_source_synthesis_m4b_target,
        ) as assemble:
            self.run_queue()
        prepared = [
            call.args[1]["path"] for call in assemble.call_args_list
            if call.kwargs.get("audio_stage_path") is not None
        ]
        self.assertEqual(prepared, [deleted["path"]])
        self.assertEqual(ready["path"].stat().st_mtime_ns, before)
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_successful_background_m4b_records_are_remembered(self):
        self.run_queue()
        self.app._remember_source_runtime_m4b_records.assert_called_once()
        accepted = self.app._remember_source_runtime_m4b_records.call_args.args[0]
        self.assertEqual(tuple(accepted), tuple(self.output_records("m4b")))

    def test_m4b_completion_survives_another_group_format_failure(self):
        self.app._remember_source_runtime_m4b_records = (
            studio.TTSApp._remember_source_runtime_m4b_records.__get__(self.app)
        )
        broken = self.output_records("group")[0]["path"]

        def encode(audio, output, **kwargs):
            if Path(output) == broken:
                raise OSError("Нет места для группового MP3")
            return self.encode(audio, output, **kwargs)

        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            self.run_queue()
        self.assertTrue(self.app.finish_processing.call_args.args[-1])
        self.assertEqual(self.app._source_m4b_rebuild_paths(self.records), ())
        self.assertCountEqual(
            self.app._source_runtime_m4b_records,
            self.output_records("m4b"),
        )
        self.encoded.clear()
        self.run_queue()
        self.assertEqual([entry[0] for entry in self.encoded], [broken])
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_deleted_group_rebuilds_only_that_result(self):
        self.run_queue()
        deleted = self.output_records("group")[0]
        stable = {
            record["path"]: (record["path"].read_bytes(), record["path"].stat().st_mtime_ns)
            for record in self.records if record is not deleted
        }
        deleted["path"].unlink()
        self.encoded.clear()
        self.processor.process_text_file.reset_mock()

        self.run_queue()

        self.assertEqual([entry[0] for entry in self.encoded], [deleted["path"]])
        self.assertEqual(self.processor.process_text_file.call_count, 2)
        for path, state in stable.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_missing_or_failed_member_never_publishes_a_successful_partial_group(self):
        self.configure([{"format": "mp3", "assembly_mode": "merge"}])
        first_group, healthy_group = self.records
        failing_path = self.paths[self.keys[1]]
        for failure in ("missing", "exception", "error", "empty"):
            with self.subTest(failure=failure):
                self.encoded.clear()
                self.processor.processing_statuses_ram.clear()
                for record in self.records:
                    record["path"].unlink(missing_ok=True)
                failing_path.write_text("Текст главы.", encoding="utf-8")
                if failure == "missing":
                    failing_path.unlink()

                def collect(path, **kwargs):
                    if Path(path) == failing_path:
                        if failure == "exception":
                            raise OSError("Не удалось прочитать исходник")
                        if failure in {"error", "empty"}:
                            return {"status": failure, "audio_files": (self.fragments[self.keys[1]],)}
                    return self.collect(path, **kwargs)

                self.processor.process_text_file.side_effect = collect
                self.run_queue()

                self.assertFalse(first_group["path"].exists())
                self.assertEqual(self.processor.processing_statuses_ram[str(first_group["path"].resolve())], "error")
                self.assertTrue(healthy_group["path"].is_file())
                self.assertTrue(self.app.finish_processing.call_args.args[-1])

    def test_warning_group_remains_retryable_and_recovers_without_repeating_healthy_group(self):
        self.configure([{"format": "mp3", "assembly_mode": "merge"}])
        first_group, healthy_group = self.records

        def collect(path, **kwargs):
            result = self.collect(path, **kwargs)
            if Path(path) == self.paths[self.keys[1]]:
                result["status"] = "warning"
            return result

        self.processor.process_text_file.side_effect = collect
        self.run_queue()
        self.assertTrue(first_group["path"].is_file())
        self.assertEqual(self.processor.processing_statuses_ram[str(first_group["path"].resolve())], "warning")
        self.assertTrue(self.app.finish_processing.call_args.args[-1])
        healthy_state = healthy_group["path"].read_bytes(), healthy_group["path"].stat().st_mtime_ns
        self.encoded.clear()
        self.processor.process_text_file.side_effect = self.collect

        self.run_queue()

        self.assertEqual([entry[0] for entry in self.encoded], [first_group["path"]])
        self.assertEqual((healthy_group["path"].read_bytes(), healthy_group["path"].stat().st_mtime_ns), healthy_state)
        self.assertEqual(self.processor.processing_statuses_ram, {})
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_ready_group_starts_encoding_while_next_group_is_being_collected(self):
        self.configure([{"format": "mp3", "assembly_mode": "merge"}])
        started = threading.Event()
        advanced = threading.Event()
        observed = []
        first_output = self.records[0]["path"]
        queue_thread = threading.current_thread()

        def encode(audio, output, **kwargs):
            if Path(output) == first_output:
                observed.append(threading.current_thread() is not queue_thread)
                started.set()
                observed.append(advanced.wait(3))
            return self.encode(audio, output, **kwargs)

        def collect(path, **kwargs):
            if Path(path) == self.paths[self.keys[2]]:
                observed.append(started.wait(3))
                advanced.set()
            return self.collect(path, **kwargs)

        self.processor.process_text_file.side_effect = collect
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            self.run_queue()

        self.assertEqual(len(observed), 3)
        self.assertTrue(all(observed), observed)
        self.assertEqual(len(self.encoded), 2)
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_failed_group_format_retries_without_repeating_successful_group_format(self):
        self.configure([
            {"format": "mp3", "bitrate": "128k", "assembly_mode": "merge"},
            {"format": "opus", "bitrate": "48k", "assembly_mode": "merge"},
        ])
        failed = next(record for record in self.records if record["target_index"] == 1)

        def encode(audio, output, **kwargs):
            if Path(output) == failed["path"]:
                raise OSError("Недостаточно места для результата")
            return self.encode(audio, output, **kwargs)

        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            self.run_queue()
        self.assertTrue(self.app.finish_processing.call_args.args[-1])
        self.assertEqual(self.processor.processing_statuses_ram[str(failed["path"].resolve())], "error")
        stable = {
            record["path"]: (record["path"].read_bytes(), record["path"].stat().st_mtime_ns)
            for record in self.records if record is not failed
        }
        self.encoded.clear()

        self.run_queue()

        self.assertEqual([entry[0] for entry in self.encoded], [failed["path"]])
        for path, state in stable.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
        self.assertEqual(self.processor.processing_statuses_ram, {})
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_disabled_cache_keeps_fragments_until_all_outputs_finish(self):
        self.processor.cfg["use_cache"] = False
        cleaned = []

        def encode(audio, output, **kwargs):
            self.assertFalse(cleaned)
            self.assertTrue(all(Path(fragment).is_file() for fragment in audio))
            return self.encode(audio, output, **kwargs)

        def cleanup():
            self.assertEqual(len(self.encoded), len(self.records))
            cleaned.append(True)
            for fragment in self.fragments.values():
                fragment.unlink()

        self.processor.cleanup_transient_audio_files.side_effect = cleanup
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode), mock.patch.object(
            studio, "_export_m4b_ffmpeg", side_effect=encode
        ):
            self.run_queue()

        self.assertEqual(cleaned, [True])
        self.processor.defer_cache_eviction.assert_not_called()
        self.processor.resume_cache_eviction.assert_not_called()
        self.assertTrue(all(record["path"].is_file() for record in self.records))
        self.assertFalse(self.app.finish_processing.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()
