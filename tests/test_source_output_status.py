"""Статусы строк и общей сборки при независимом завершении выходов."""

import copy
import threading
import unittest
from itertools import count
from pathlib import Path
from unittest import mock

import test_source_group_runtime as runtime


studio = runtime.studio


class StatusTree:
    def __init__(self, rows):
        self.rows = {row: {"values": ("⏳ В очереди", row), "tags": ("queued",)} for row in rows}

    def exists(self, item):
        return item in self.rows

    def item(self, item, option=None, **updates):
        self.rows[item].update(updates)
        return self.rows[item][option] if option else self.rows[item]

    def get_children(self, parent=""):
        return tuple(self.rows) if not parent else ()


class SourceOutputStatusTests(unittest.TestCase):
    def make_case(self, *, grouped=True, groups=1, targets=None):
        case = runtime.SourceGroupOutputRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.groups = case.groups[:groups]
        case.keys = case.keys[:2 * groups]
        case.paths = {key: case.paths[key] for key in case.keys}
        case.configure(targets or [
            {"format": "mp3", "assembly_mode": "merge"},
            {"format": "m4b"},
        ])
        if not grouped:
            case.config["source_virtual_group_ids"] = ()
        return case

    def start_queue(self, case, releases):
        worker = threading.Thread(target=case.run_queue, daemon=True)
        worker.start()

        def cleanup():
            for event in releases:
                event.set()
            worker.join(5)

        self.addCleanup(cleanup)
        return worker

    def wait(self, event):
        self.assertTrue(event.wait(5), "Output worker did not reach the expected barrier")

    def rows(self, case, item="group-1"):
        return [call.args[1:] for call in case.app.update_file_status.call_args_list
                if call.args[0] == item]

    def attach_status_tree(self, case):
        """Подключает настоящую отрисовку статусов к тестовой очереди."""
        group_ids = tuple(group["id"] for group in case.groups)
        case.app.tree = StatusTree((*case.keys, *group_ids))
        case.app.btn_go_current = mock.Mock()
        case.app.batch_processor = case.processor
        case.app._source_group_ids = set(group_ids)
        case.app.update_file_status.side_effect = (
            lambda *args: studio.TTSApp.update_file_status(case.app, *args)
        )

    def legacy_case(self, *, groups=1):
        """Возвращает очередь старого односоставного вывода без наборов целей."""
        case = self.make_case(groups=groups)
        # ``make_case`` создаёт современный набор целей по умолчанию, поэтому
        # убираем его из снимка полностью: process_queue выбирает старую ветку
        # только при отсутствии synthesis_targets/source_output_targets.
        case.config.pop("synthesis_targets", None)
        case.config.pop("source_output_targets", None)
        case.config.pop("source_target_records", None)
        case.config.pop("source_m4b_groups", None)
        case.config["source_multi_output"] = False
        self.attach_status_tree(case)
        # process_queue передаёт request_callback только реальному процессору;
        # маркер сохраняет этот контракт на лёгком тестовом объекте.
        case.processor.__class__ = studio.TTSProcessor
        return case

    def render_progress(self, case, observer=None):
        case.app.lbl_current_text = mock.Mock()
        case.app.lbl_current_text.master.winfo_width.return_value = 1
        case.app.lbl_file_pct = mock.Mock()
        case.app.file_progress = {"value": 100}
        case.app._set_status_label = mock.Mock()

        def update(pct, text, *extra):
            studio.TTSApp.update_progress_ui(case.app, pct, text, *extra)
            if observer is not None:
                observer(pct, case.app._set_status_label.call_args.args[1])

        case.app.update_progress_ui.side_effect = update

    def rendered_progress(self, case):
        return case.app._set_status_label.call_args.args[1]

    def test_target_status_callback_supports_legacy_arities(self):
        calls = []

        def three(fmt, path, status):
            calls.append(("three", fmt, path, status))

        def two(fmt, status):
            calls.append(("two", fmt, status))

        def one(status):
            calls.append(("one", status))

        path = Path("chapter.mp3")
        studio._call_source_target_status(three, "mp3", path, "encoding")
        studio._call_source_target_status(two, "mp3", path, "success")
        studio._call_source_target_status(one, "mp3", path, "error")

        self.assertEqual(calls[0], ("three", "mp3", path, "encoding"))
        self.assertEqual(calls[1], ("two", "mp3", "success"))
        self.assertEqual(calls[2], ("one", "error"))

    def cancelled_recollection_case(self, formats=("mp3",), *, missing_format=None,
                                    stopped=True, remove_ready_output=False):
        """Прерывает новый запрос для группы с ранее готовыми файлами глав."""
        case = self.make_case(targets=[
            *({"format": fmt, "assembly_mode": "files"} for fmt in formats),
            {"format": "m4b"},
        ])
        self.attach_status_tree(case)
        case.processor.__class__ = studio.TTSProcessor
        case.processor.cfg["use_cache"] = False
        existing = []
        for record in case.records:
            path = Path(record["path"])
            if record["kind"] == "file" and record["target"]["format"] != missing_format:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"complete chapter audio")
                existing.append((path, path.read_bytes(), path.stat().st_mtime_ns))

        def recollect(source_path, **kwargs):
            kwargs["request_callback"]("Новая речь для незавершённого M4B.")
            key = next(key for key, path in case.paths.items() if path == Path(source_path))
            self.assertEqual(self.rows(case, key)[-1], ("processing",))
            if remove_ready_output:
                existing[0][0].unlink()
            case.processor.is_stopped = stopped
            return {"status": "error", "audio_files": ()}

        case.processor.process_text_file.side_effect = recollect
        case.app.process_queue(
            case.processor, case.keys, copy.deepcopy(case.config), True
        )
        case.app._reset_pending_source_rows()
        return case, existing

    def test_cancelled_group_recollection_restores_ready_chapter_outputs(self):
        for formats in (("mp3",), ("mp3", "opus")):
            with self.subTest(formats=formats):
                case, existing = self.cancelled_recollection_case(formats)
                self.assertEqual(self.rows(case, case.keys[0])[-1], ("success",))
                self.assertEqual(case.app.tree.item(case.keys[0], "tags"), ("success",))
                self.assertEqual(case.app.tree.item(case.keys[1], "tags"), ("success",))
                self.assertEqual(case.app.tree.item("group-1", "tags"), ("queued",))
                for path, content, modified in existing:
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(path.stat().st_mtime_ns, modified)
                self.assertEqual(case.encoded, [])
                case.processor.process_text_file.assert_called_once()

    def test_cancelled_group_recollection_keeps_new_format_pending(self):
        case, existing = self.cancelled_recollection_case(
            ("mp3", "opus"), missing_format="opus"
        )
        self.assertEqual(case.app.tree.item(case.keys[0], "tags"), ("queued",))
        self.assertNotEqual(self.rows(case, case.keys[0])[-1], ("success",))
        for path, content, modified in existing:
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_cancelled_group_recollection_without_chapter_outputs_stays_pending(self):
        case, existing = self.cancelled_recollection_case(formats=())
        self.assertEqual(existing, [])
        self.assertEqual(case.app.tree.item(case.keys[0], "tags"), ("queued",))
        self.assertEqual(case.app.tree.item("group-1", "tags"), ("queued",))
        self.assertNotEqual(self.rows(case, case.keys[0])[-1], ("success",))

    def test_real_group_recollection_failure_remains_visible(self):
        case, existing = self.cancelled_recollection_case(stopped=False)
        self.assertEqual(case.app.tree.item(case.keys[0], "tags"), ("error",))
        self.assertEqual(case.app.tree.item("group-1", "tags"), ("error",))
        self.assertTrue(case.app.finish_processing.call_args.args[-1])
        for path, content, modified in existing:
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_cancelled_group_recollection_does_not_restore_disappeared_output(self):
        case, _existing = self.cancelled_recollection_case(remove_ready_output=True)
        self.assertEqual(case.app.tree.item(case.keys[0], "tags"), ("queued",))
        self.assertNotEqual(self.rows(case, case.keys[0])[-1], ("success",))

    def test_target_status_callback_typeerror_body_runs_once(self):
        calls = []

        def callback(fmt, path, status):
            calls.append((fmt, path, status))
            raise TypeError("callback body")

        with self.assertRaisesRegex(TypeError, "callback body"):
            studio._call_source_target_status(callback, "mp3", "chapter.mp3", "error")
        self.assertEqual(len(calls), 1)

    def test_shared_group_waits_for_both_formats_in_either_finish_order(self):
        for first_format in ("mp3", "m4b"):
            with self.subTest(first_format=first_format):
                case = self.make_case()
                started = {fmt: threading.Event() for fmt in ("mp3", "m4b")}
                release = {fmt: threading.Event() for fmt in started}
                remaining = "m4b" if first_format == "mp3" else "mp3"
                first_finished = threading.Event()

                def encode(audio, output, **kwargs):
                    fmt = Path(output).suffix[1:]
                    started[fmt].set()
                    if not release[fmt].wait(5):
                        raise TimeoutError("Test did not release encoder")
                    case.encode(audio, output, **kwargs)

                def row_status(item, status, fmt=None):
                    if (item, status, fmt) == ("group-1", "encoding", remaining):
                        if release[first_format].is_set():
                            first_finished.set()

                case.app.update_file_status.side_effect = row_status
                with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode), \
                     mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=encode):
                    worker = self.start_queue(case, release.values())
                    for event in started.values():
                        self.wait(event)
                    self.assertEqual(self.rows(case)[-1], ("encoding", ("mp3", "m4b")))
                    release[first_format].set()
                    self.wait(first_finished)
                    self.assertEqual(self.rows(case)[-1], ("encoding", remaining))
                    self.assertNotIn(("success",), self.rows(case))
                    case.app.finish_processing.assert_not_called()
                    release[remaining].set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(self.rows(case)[-1], ("success",))
                self.assertFalse(case.app.finish_processing.call_args.args[-1])

    def test_sequential_formats_and_multiple_m4b_targets_keep_group_pending(self):
        case = self.make_case(targets=[
            {"format": "mp3", "assembly_mode": "merge"},
            {"format": "opus", "assembly_mode": "merge"},
            {"format": "m4b", "bitrate": "64k"},
            {"format": "m4b", "bitrate": "96k"},
        ])
        opus_started, second_m4b_started = threading.Event(), threading.Event()
        release_opus, release_second_m4b = threading.Event(), threading.Event()
        m4b_count = 0

        def encode(audio, output, **kwargs):
            nonlocal m4b_count
            fmt = Path(output).suffix[1:]
            if fmt == "opus":
                opus_started.set()
                if not release_opus.wait(5):
                    raise TimeoutError("Opus was not released")
            if fmt == "m4b":
                m4b_count += 1
                if m4b_count == 2:
                    second_m4b_started.set()
                    if not release_second_m4b.wait(5):
                        raise TimeoutError("Second M4B was not released")
            case.encode(audio, output, **kwargs)

        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode), \
             mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=encode):
            worker = self.start_queue(case, (release_opus, release_second_m4b))
            self.wait(opus_started)
            self.wait(second_m4b_started)
            self.assertEqual(self.rows(case)[-1], ("encoding", ("opus", "m4b")))
            self.assertNotIn(("success",), self.rows(case))
            release_opus.set()
            release_second_m4b.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.rows(case)[-1], ("success",))

    def test_ungrouped_chapters_wait_for_book_without_first_chapter_encoding(self):
        case = self.make_case(grouped=False, targets=[
            {"format": "mp3", "assembly_mode": "files"}, {"format": "m4b"},
        ])
        m4b_started, release_m4b = threading.Event(), threading.Event()
        files_finished = threading.Event()
        finished_files = set()
        file_outputs = {record["item_id"]: record["path"] for record in case.records
                        if record["kind"] == "file"}

        def m4b_encode(audio, output, **kwargs):
            m4b_started.set()
            if not release_m4b.wait(5):
                raise TimeoutError("M4B was not released")
            case.encode(audio, output, **kwargs)

        def row_status(item, status, *_args):
            if status == "waiting" and item in file_outputs and file_outputs[item].exists():
                finished_files.add(item)
                if finished_files == set(case.keys):
                    files_finished.set()

        case.app.update_file_status.side_effect = row_status
        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=m4b_encode):
            worker = self.start_queue(case, (release_m4b,))
            self.wait(m4b_started)
            self.wait(files_finished)
            for item in case.keys:
                self.assertEqual(self.rows(case, item)[-1], ("waiting",))
                self.assertNotIn(("success",), self.rows(case, item))
                self.assertNotIn(("encoding", "m4b"), self.rows(case, item))
            release_m4b.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        for item in case.keys:
            self.assertEqual(self.rows(case, item)[-1], ("success",))

    def test_group_failure_survives_later_successful_book(self):
        for failure in ("warning", "error"):
            with self.subTest(failure=failure):
                events = []
                tracker = studio.SourceOutputStatusTracker(lambda *args: events.append(args))
                tracker.register("mp3", ("group",), ("group",), "mp3")
                tracker.register("m4b", ("group",), ("group",), "m4b")
                tracker.start("mp3")
                tracker.start("m4b")
                tracker.update([("mp3", failure)])
                self.assertEqual(events[-1], ("group", "encoding", "m4b"))
                tracker.update([("m4b", "success")])
                self.assertEqual(events[-1], ("group", failure))

    def test_ungrouped_merge_stays_visible_when_ready_m4b_is_skipped(self):
        case = self.make_case(grouped=False)
        m4b = next(record["path"] for record in case.records if record["kind"] == "m4b")
        m4b.parent.mkdir(parents=True, exist_ok=True)
        m4b.write_bytes(b"ready book")
        started, release = threading.Event(), threading.Event()
        visible = threading.Event()
        mp3 = next(record["path"] for record in case.records if record["kind"] == "group")

        def encode(audio, output, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("MP3 was not released")
            case.encode(audio, output, **kwargs)

        def progress(_pct, text):
            if text.startswith("Сборка:") and mp3.name in text:
                visible.set()

        self.render_progress(case, progress)
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            worker = self.start_queue(case, (release,))
            self.wait(started)
            self.wait(visible)
            self.assertEqual(self.rendered_progress(case), f"Сборка: {mp3.name}")
            for item in case.keys:
                self.assertEqual(self.rows(case, item)[-1], ("waiting",))
            case.app.finish_processing.assert_not_called()
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual([path for path, *_ in case.encoded], [mp3])

    def test_ungrouped_activity_tracks_both_completion_orders(self):
        for first_format in ("mp3", "m4b"):
            with self.subTest(first_format=first_format):
                case = self.make_case(grouped=False)
                remaining = "m4b" if first_format == "mp3" else "mp3"
                started = {fmt: threading.Event() for fmt in ("mp3", "m4b")}
                release = {fmt: threading.Event() for fmt in started}
                only_remaining = threading.Event()

                def encode(audio, output, **kwargs):
                    fmt = Path(output).suffix[1:]
                    started[fmt].set()
                    if not release[fmt].wait(5):
                        raise TimeoutError("Encoder was not released")
                    case.encode(audio, output, **kwargs)

                def progress(_pct, text):
                    if (release[first_format].is_set() and text.startswith("Сборка:")
                            and text.endswith(f".{remaining}")):
                        only_remaining.set()

                self.render_progress(case, progress)
                with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode), \
                     mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=encode):
                    worker = self.start_queue(case, release.values())
                    for event in started.values():
                        self.wait(event)
                    release[first_format].set()
                    self.wait(only_remaining)
                    self.assertTrue(self.rendered_progress(case).endswith(f".{remaining}"))
                    case.app.finish_processing.assert_not_called()
                    release[remaining].set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                for item in case.keys:
                    self.assertEqual(self.rows(case, item)[-1], ("success",))

    def test_last_source_progress_does_not_hide_earlier_ungrouped_merge(self):
        case = self.make_case(grouped=False, groups=2, targets=[
            {"format": "mp3", "assembly_mode": "merge"},
        ])
        first_path = case.records[0]["path"]
        first_started, release_first = threading.Event(), threading.Event()
        second_finished, restored = threading.Event(), threading.Event()

        def collect(path, **kwargs):
            if Path(path) == case.paths[case.keys[2]]:
                self.wait(first_started)
            kwargs["progress_callback"](1, 1, "Последний синтезированный фрагмент")
            return case.collect(path)

        def encode(audio, output, **kwargs):
            if Path(output) == first_path:
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("First group was not released")
            case.encode(audio, output, **kwargs)
            if Path(output) != first_path:
                second_finished.set()

        def progress(_pct, text):
            if (second_finished.is_set() and text == f"Сборка: {first_path.name}"):
                restored.set()

        case.processor.process_text_file.side_effect = collect
        self.render_progress(case, progress)
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            worker = self.start_queue(case, (release_first,))
            self.wait(second_finished)
            self.wait(restored)
            self.assertEqual(self.rendered_progress(case), f"Сборка: {first_path.name}")
            case.app.finish_processing.assert_not_called()
            release_first.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_ungrouped_queued_merge_switches_from_waiting_to_active(self):
        case = self.make_case(grouped=False, targets=[
            {"format": "mp3", "assembly_mode": "files"},
            {"format": "opus", "assembly_mode": "merge"},
        ])
        case.config["max_parallel_encodes"] = 1
        first_started, release_first = threading.Event(), threading.Event()
        group_started, release_group = threading.Event(), threading.Event()
        waiting, active = threading.Event(), threading.Event()

        def encode(audio, output, **kwargs):
            if not first_started.is_set():
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("First file was not released")
            if Path(output).suffix == ".opus":
                group_started.set()
                if not release_group.wait(5):
                    raise TimeoutError("Group was not released")
            case.encode(audio, output, **kwargs)

        def progress(_pct, text):
            if text.startswith("Сборка:") and text.endswith(".opus · ожидание"):
                waiting.set()
            elif text.startswith("Сборка:") and text.endswith(".opus"):
                active.set()

        self.render_progress(case, progress)
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=encode):
            worker = self.start_queue(case, (release_first, release_group))
            self.wait(first_started)
            self.wait(waiting)
            self.assertFalse(group_started.is_set())
            release_first.set()
            self.wait(group_started)
            self.wait(active)
            release_group.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_skipped_ungrouped_outputs_do_not_report_active_encoding(self):
        case = self.make_case(grouped=False)
        for record in case.records:
            record["path"].parent.mkdir(parents=True, exist_ok=True)
            record["path"].write_bytes(b"ready audio")
        case.app._source_plan_dirty = False
        self.render_progress(case)
        case.run_queue()

        case.processor.process_text_file.assert_not_called()
        self.assertEqual(case.encoded, [])
        self.assertFalse(any(
            call.args[1].startswith("Сборка:") and not call.args[1].endswith(" · ожидание")
            for call in case.app._set_status_label.call_args_list
        ))
        for item in case.keys:
            self.assertEqual(self.rows(case, item)[-1], ("success",))

    def test_ready_chapters_are_preflighted_before_missing_m4b_collection(self):
        case = self.make_case(targets=[
            {"format": "mp3", "assembly_mode": "files"},
            {"format": "m4b"},
        ])
        case.app._source_plan_dirty = False
        self.attach_status_tree(case)
        ready_files = {
            record["item_id"]: record["path"]
            for record in case.records
            if record["kind"] == "file"
        }
        for path in ready_files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ready chapter")

        observed_during_collection = []
        original_collect = case.collect

        def collect(path, **kwargs):
            observed_during_collection.append((
                {item: case.app.tree.item(item, "tags") for item in case.keys},
                case.app.tree.item("group-1", "tags"),
            ))
            return original_collect(path, **kwargs)

        case.processor.process_text_file.side_effect = collect
        case.run_queue()

        self.assertEqual(case.processor.process_text_file.call_count, len(case.keys))
        self.assertTrue(observed_during_collection)
        for chapter_tags, group_tags in observed_during_collection:
            self.assertTrue(all(tags == ("success",) for tags in chapter_tags.values()))
            self.assertNotEqual(group_tags, ("success",))
        self.assertEqual([path.suffix for path, *_ in case.encoded], [".m4b"])

    def test_missing_m4b_does_not_turn_ready_chapters_into_processing(self):
        case = self.make_case(targets=[
            {"format": "mp3", "assembly_mode": "files"},
            {"format": "m4b"},
        ])
        case.app._source_plan_dirty = False
        self.attach_status_tree(case)
        for record in case.records:
            if record["kind"] == "file":
                record["path"].parent.mkdir(parents=True, exist_ok=True)
                record["path"].write_bytes(b"ready chapter")

        case.run_queue()

        self.assertEqual(case.processor.process_text_file.call_count, len(case.keys))
        for item in case.keys:
            statuses = [call.args[1] for call in case.app.update_file_status.call_args_list
                        if call.args[0] == item]
            self.assertEqual(statuses, ["success", "success"])
            self.assertEqual(case.app.tree.item(item, "tags"), ("success",))

    def test_independent_group_can_finish_while_another_group_is_active(self):
        case = self.make_case(groups=2)
        second_started, release_second = threading.Event(), threading.Event()
        first_finished = threading.Event()

        def m4b_encode(audio, output, **kwargs):
            if Path(output) == next(record["path"] for record in case.records
                                    if record["kind"] == "m4b" and record["item_id"] == "group-2"):
                second_started.set()
                if not release_second.wait(5):
                    raise TimeoutError("Second group was not released")
            case.encode(audio, output, **kwargs)

        def row_status(item, status, *_args):
            if (item, status) == ("group-1", "success"):
                first_finished.set()

        case.app.update_file_status.side_effect = row_status
        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=m4b_encode):
            worker = self.start_queue(case, (release_second,))
            self.wait(second_started)
            self.wait(first_finished)
            self.assertEqual(self.rows(case)[-1], ("success",))
            self.assertNotIn(("success",), self.rows(case, "group-2"))
            release_second.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_draft_and_reflow_wait_for_every_final_part(self):
        for reflow in (False, True):
            with self.subTest(reflow=reflow):
                case = self.make_case(groups=2 if reflow else 1)
                case.config["source_m4b_auto_split_long"] = True
                case.config["source_m4b_reflow_actual_duration"] = reflow
                case.app._stage_source_m4b_ui_plan = mock.Mock()
                final_started, release_final = threading.Event(), threading.Event()
                draft_finished = threading.Event()
                completed_parts = []

                def rebuild(records, *_args, **_kwargs):
                    if not reflow:
                        self.assertTrue(draft_finished.is_set())
                    self.assertNotIn(("success",), self.rows(case))
                    rebuilt = []
                    members_by_part = (
                        (case.keys[::2], case.keys[1::2]) if reflow
                        else tuple((member,) for member in case.keys)
                    )
                    for number, members in enumerate(members_by_part, 1):
                        record = copy.deepcopy(records[0])
                        record.update(
                            item_id=(f"source_m4b_reflow:1:{number}" if reflow else f"group-1:part-{number}"),
                            file_ids=members, path=case.output / f"part-{number}.m4b",
                        )
                        rebuilt.append(record)
                    return {"records": rebuilt, "changed": True,
                            "durations": {member: 1 for member in case.keys}}

                def m4b_target(_processor, record, *_args, **kwargs):
                    kwargs["status_callback"]("m4b", record["path"], "encoding")
                    stage = kwargs.get("audio_stage_path")
                    if stage is not None:
                        Path(stage).write_bytes(b"draft")
                        draft_finished.set()
                        return {"status": "success", "stage_path": stage}
                    if completed_parts:
                        final_started.set()
                        if not release_final.wait(5):
                            raise TimeoutError("Final M4B part was not released")
                    completed_parts.append(record["path"])
                    return {"status": "success"}

                with mock.patch.object(studio, "reflow_source_m4b_target_records", side_effect=rebuild), \
                     mock.patch.object(studio, "run_source_synthesis_m4b_target", side_effect=m4b_target), \
                     mock.patch.object(studio, "_measure_source_m4b_chapter_durations", return_value={}):
                    worker = self.start_queue(case, (release_final,))
                    self.wait(final_started)
                    for group in case.groups:
                        self.assertNotIn(("success",), self.rows(case, group["id"]))
                        self.assertEqual(self.rows(case, group["id"])[-1], ("encoding", "m4b"))
                    release_final.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                for group in case.groups:
                    self.assertEqual(self.rows(case, group["id"])[-1], ("success",))
                self.assertEqual(len(completed_parts), 2)
                self.assertFalse(case.app.finish_processing.call_args.args[-1])

    def test_m4b_reflow_never_publishes_success_before_final_parts(self):
        case = self.make_case(groups=2)
        case.config["source_m4b_auto_split_long"] = True
        case.config["source_m4b_reflow_actual_duration"] = True
        case.app._stage_source_m4b_ui_plan = mock.Mock()
        case.app._source_plan_dirty = False
        for record in case.records:
            if record["kind"] == "group" or (
                record["kind"] == "m4b" and record["item_id"] == "group-1"
            ):
                record["path"].parent.mkdir(parents=True, exist_ok=True)
                record["path"].write_bytes(b"ready audio")
        reflow_checked, release_reflow = threading.Event(), threading.Event()
        final_started, release_final = threading.Event(), threading.Event()
        completed_parts = []

        def rebuild(records, *_args, **_kwargs):
            for group in case.groups:
                rows = self.rows(case, group["id"])
                self.assertTrue(not rows or rows[-1] != ("success",))
            reflow_checked.set()
            if not release_reflow.wait(5):
                raise TimeoutError("M4B reflow check was not released")
            return {"records": tuple(records), "changed": False, "durations": {}}

        def m4b_target(_processor, record, *_args, **kwargs):
            kwargs["status_callback"]("m4b", record["path"], "encoding")
            if completed_parts:
                final_started.set()
                if not release_final.wait(5):
                    raise TimeoutError("Final M4B output was not released")
            completed_parts.append(record["path"])
            return {"status": "success"}

        with mock.patch.object(
            studio, "reflow_source_m4b_target_records", side_effect=rebuild
        ), mock.patch.object(
            studio, "run_source_synthesis_m4b_target", side_effect=m4b_target
        ), mock.patch.object(
            studio, "_measure_source_m4b_chapter_durations", return_value={}
        ):
            worker = self.start_queue(case, (release_reflow, release_final))
            self.wait(reflow_checked)
            for group in case.groups:
                rows = self.rows(case, group["id"])
                self.assertTrue(not rows or rows[-1] != ("success",))
            release_reflow.set()
            self.wait(final_started)
            self.assertNotEqual(self.rows(case, "group-2")[-1], ("success",))
            release_final.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(completed_parts), 2)
        self.assertEqual(self.rows(case, "group-1")[-1], ("success",))
        self.assertEqual(self.rows(case, "group-2")[-1], ("success",))

    def test_m4b_only_cache_pass_is_preparing_until_an_api_request(self):
        case = self.make_case(targets=[{"format": "m4b"}])
        self.attach_status_tree(case)
        # Настоящий TTSProcessor получает request_callback. Лёгкий тестовый
        # объект временно получает маркер реального класса, чтобы проверить
        # именно эту ветку обработчика.
        case.processor.__class__ = studio.TTSProcessor
        preparing_seen, release_request = threading.Event(), threading.Event()
        original_collect = case.collect

        def collect(path, **kwargs):
            preparing_seen.set()
            if not release_request.wait(5):
                raise TimeoutError("Cache preparation was not released")
            request_callback = kwargs.get("request_callback")
            self.assertIsNotNone(request_callback)
            request_callback("new API request")
            return original_collect(path, **kwargs)

        case.processor.process_text_file.side_effect = collect
        worker = self.start_queue(case, (release_request,))
        self.wait(preparing_seen)
        self.assertEqual(self.rows(case, case.keys[0])[-1], ("preparing",))
        self.assertNotIn(("processing",), self.rows(case, case.keys[0]))
        self.assertFalse(self.rows(case, case.keys[1]))
        release_request.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        for item in case.keys:
            statuses = [call.args[1] for call in case.app.update_file_status.call_args_list
                        if call.args[0] == item]
            self.assertIn("processing", statuses)
            self.assertEqual(statuses[-1], "success")

    def test_legacy_cache_pass_is_preparing_without_api_request(self):
        """Старая одноформатная ветка обозначает проверку кэша отдельно."""
        case = self.legacy_case()
        self.render_progress(case)
        captions = []

        def collect(path, **kwargs):
            kwargs["progress_callback"](1, 1, "cached fragment")
            captions.append(self.rendered_progress(case))
            result = case.collect(path, **kwargs)
            kwargs["encoding_callback"](Path(path).name)
            kwargs["completion_callback"](Path(path).name, "success", None)
            return result

        case.processor.process_text_file.side_effect = collect
        case.run_queue()

        self.assertEqual(case.processor.process_text_file.call_count, len(case.keys))
        for item in case.keys:
            statuses = [
                call.args[1]
                for call in case.app.update_file_status.call_args_list
                if call.args[0] == item
            ]
            self.assertIn("preparing", statuses)
            self.assertNotIn("processing", statuses)
            self.assertEqual(statuses[-1], "success")
        for call in case.processor.process_text_file.call_args_list:
            self.assertIn("request_callback", call.kwargs)
        self.assertEqual(
            captions,
            [f"Подготовка: {case.paths[item].name}" for item in case.keys],
        )

    def test_legacy_api_request_switches_from_preparing_to_processing(self):
        """Промах кэша в старой ветке показывает сетевой запрос явно."""
        case = self.legacy_case()
        self.render_progress(case)
        captions = []
        original_collect = case.collect

        def collect_with_api(path, **kwargs):
            kwargs["progress_callback"](1, 2, "cached fragment")
            request_callback = kwargs.get("request_callback")
            self.assertIsNotNone(request_callback)
            request_callback("legacy API request")
            captions.append(self.rendered_progress(case))
            kwargs["progress_callback"](2, 2, "next cached fragment")
            captions.append(self.rendered_progress(case))
            result = original_collect(path, **kwargs)
            kwargs["encoding_callback"](Path(path).name)
            kwargs["completion_callback"](Path(path).name, "success", None)
            return result

        case.processor.process_text_file.side_effect = collect_with_api
        case.run_queue()

        for item in case.keys:
            statuses = [
                call.args[1]
                for call in case.app.update_file_status.call_args_list
                if call.args[0] == item
            ]
            self.assertLess(statuses.index("preparing"), statuses.index("processing"))
            self.assertEqual(statuses[-1], "success")
        self.assertEqual(
            captions,
            [caption for item in case.keys for caption in (
                "Синтез: legacy API request",
                "Синтез: legacy API request",
            )],
        )

    def test_chapter_activity_keeps_synthesis_between_fragments_and_resets(self):
        """Кэш до/после запросов не откатывает стадию синтеза текущей главы."""
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                case = (
                    self.legacy_case(groups=2)
                    if legacy
                    else self.make_case(groups=2)
                )
                case.processor.__class__ = studio.TTSProcessor
                self.render_progress(case)
                original_collect = case.collect
                captions, expected, percentages = [], [], []

                def collect(path, **kwargs):
                    item = next(
                        key for key, source in case.paths.items()
                        if source == Path(path)
                    )
                    filename = Path(path).name

                    def progress(current, total):
                        kwargs["progress_callback"](current, total, "Фрагмент")
                        captions.append(self.rendered_progress(case))
                        percentages.append(case.app.file_progress["value"])

                    def request(text):
                        kwargs["request_callback"](text)
                        captions.append(self.rendered_progress(case))
                        expected.append(f"Синтез: {text}")

                    # Первая и третья главы целиком берутся из кэша. Вторая
                    # смешивает кэш и запрос; четвёртая делает два запроса.
                    cache_only = item in (case.keys[0], case.keys[2])
                    progress(1, 4)
                    expected.append(f"Подготовка: {filename}")
                    if not cache_only:
                        # Упоминание формата в самом тексте не является сборкой.
                        request("M4B: Первый запрос")
                    progress(2, 4)
                    caption = (
                        f"Подготовка: {filename}"
                        if cache_only
                        else "Синтез: M4B: Первый запрос"
                    )
                    expected.append(caption)
                    if item == case.keys[3]:
                        request("Второй запрос")
                        caption = "Синтез: Второй запрос"
                    progress(4, 4)
                    expected.append(caption)
                    result = original_collect(path, **kwargs)
                    if legacy:
                        kwargs["encoding_callback"](filename)
                        kwargs["completion_callback"](filename, "success", None)
                    return result

                case.processor.process_text_file.side_effect = collect
                # Каждый промежуточный процент проходит ограничитель частоты;
                # ожидания и реальная сеть для проверки подписи не нужны.
                with mock.patch.object(
                    studio.time, "monotonic", side_effect=count(10.0, 0.2)
                ):
                    case.run_queue()

                self.assertEqual(captions, expected)
                self.assertEqual(percentages, [25, 50, 100] * 4)
                for item in (case.keys[0], case.keys[2]):
                    self.assertNotIn(("processing",), self.rows(case, item))
                for item in (case.keys[1], case.keys[3]):
                    self.assertIn(("processing",), self.rows(case, item))

    def test_legacy_stop_keeps_interrupted_file_in_queue(self):
        for result in ("callback", "exception"):
            with self.subTest(result=result):
                case = self.legacy_case()
                case.app.tree.rows["previous_error"] = {
                    "values": ("❌ Ошибка", "previous_error"), "tags": ("error",),
                }
                case.app.tree.rows["ready"] = {
                    "values": ("✅ Готово", "ready"), "tags": ("success",),
                }

                def collect(path, **kwargs):
                    # Остановка приходит после подготовки, до уведомления о результате.
                    case.processor.is_stopped = True
                    if result == "exception":
                        raise InterruptedError("Остановлено пользователем")
                    kwargs["completion_callback"](Path(path).name, "error", None)

                case.processor.process_text_file.side_effect = collect
                case.run_queue()

                case.processor.process_text_file.assert_called_once()
                self.assertEqual(self.rows(case, case.keys[0])[-1], ("queued",))
                self.assertNotIn(("error",), self.rows(case, case.keys[0]))
                for item in case.keys:
                    self.assertEqual(case.app.tree.item(item, "tags"), ("queued",))
                self.assertEqual(case.app.tree.item("previous_error", "tags"), ("error",))
                self.assertEqual(case.app.tree.item("ready", "tags"), ("success",))
                self.assertFalse(case.app.finish_processing.call_args.args[-1])

    def test_legacy_completion_preserves_real_failure_and_saved_success(self):
        for status, stopped in (("error", False), ("success", True)):
            with self.subTest(status=status, stopped=stopped):
                case = self.legacy_case()

                def collect(path, **kwargs):
                    case.processor.is_stopped = stopped
                    kwargs["completion_callback"](Path(path).name, status, None)

                case.processor.process_text_file.side_effect = collect
                case.run_queue()

                self.assertEqual(self.rows(case, case.keys[0])[-1], (status,))
                self.assertEqual(case.app.tree.item(case.keys[0], "tags"), (status,))
                self.assertEqual(case.app.finish_processing.call_args.args[-1], not stopped)

    def test_late_initial_success_does_not_override_newer_processing_status(self):
        processor = mock.Mock()
        app = object.__new__(studio.TTSApp)
        app.tree = StatusTree(("chapter", "pending"))
        app.batch_processor = processor
        app.btn_go_current = mock.Mock()
        studio.TTSApp.update_file_status(app, "chapter", "processing")

        statuses = (("chapter", "success"), ("pending", "success"))
        app._apply_initial_source_statuses(processor, statuses)

        self.assertEqual(app.tree.item("chapter", "tags"), ("processing",))
        self.assertEqual(
            app.tree.item("chapter", "values")[0], "🔄 Синтез..."
        )
        self.assertEqual(app.tree.item("pending", "tags"), ("success",))

        studio.TTSApp.update_file_status(app, "pending", "queued")
        app.batch_processor = mock.Mock()
        app._apply_initial_source_statuses(processor, statuses)
        self.assertEqual(app.tree.item("pending", "tags"), ("queued",))

    def test_waiting_background_output_does_not_move_current_source_or_scroll(self):
        app = object.__new__(studio.TTSApp)
        app.tree = StatusTree(("previous", "current"))
        app.btn_go_current = mock.Mock()
        app.auto_scroll_var = mock.Mock()
        app.auto_scroll_var.get.return_value = True
        app.scroll_to_current = mock.Mock()
        app.update_file_status("current", "processing")
        app.scroll_to_current.reset_mock()

        tracker = studio.SourceOutputStatusTracker(app.update_file_status)
        tracker.register("mp3", ("previous",), ("previous",), "mp3")
        tracker.register("m4b", ("previous",), (), "m4b")
        tracker.start("mp3")
        tracker.update([("mp3", "success")])

        self.assertEqual(app.tree.item("previous", "values")[0], "⏳ Ожидание сборки...")
        self.assertEqual(app.tree.item("previous", "tags"), ("processing",))
        self.assertEqual(app.current_processing_file, "current")
        app.scroll_to_current.assert_not_called()

    def test_cache_preparation_does_not_move_current_source_or_scroll(self):
        app = object.__new__(studio.TTSApp)
        app.tree = StatusTree(("current", "cached"))
        app.btn_go_current = mock.Mock()
        app.auto_scroll_var = mock.Mock()
        app.auto_scroll_var.get.return_value = True
        app.scroll_to_current = mock.Mock()
        app.update_file_status("current", "processing")
        app.scroll_to_current.reset_mock()

        app.update_file_status("cached", "preparing")

        self.assertEqual(app.tree.item("cached", "values")[0], "🔄 Подготовка...")
        self.assertEqual(app.current_processing_file, "current")
        app.scroll_to_current.assert_not_called()

    def test_stop_queues_waiting_rows_without_hiding_active_or_finished_work(self):
        for action in ("stop_processing", "hard_stop_processing"):
            with self.subTest(action=action):
                app = object.__new__(studio.TTSApp)
                app.tree = StatusTree(("waiting", "active", "finished", "failed"))
                app.btn_go_current = mock.Mock()
                app.btn_stop = mock.Mock()
                app.btn_hard_stop = mock.Mock()
                app.lbl_current_text = mock.Mock()
                app.root = mock.Mock()
                app._set_status_label = mock.Mock()
                app._show_warning = mock.Mock()
                render_events = []
                app.root.update_idletasks.side_effect = (
                    lambda: render_events.append("render")
                )
                app._show_warning.side_effect = (
                    lambda *_args: render_events.append("alert")
                )
                processor = mock.Mock(is_stopped=False)
                processor.stop.side_effect = lambda: setattr(processor, "is_stopped", True)
                app.batch_processor = processor
                app.update_file_status("waiting", "waiting")
                app.update_file_status("active", "processing")
                app.update_file_status("finished", "success")
                app.update_file_status("failed", "error")

                with mock.patch.object(studio.threading, "Thread"):
                    getattr(app, action)()

                self.assertTrue(processor.is_stopped)
                self.assertEqual(app.tree.item("waiting", "tags"), ("queued",))
                self.assertEqual(app.tree.item("active", "tags"), ("processing",))
                self.assertEqual(app.tree.item("finished", "tags"), ("success",))
                self.assertEqual(app.tree.item("failed", "tags"), ("error",))
                if action == "hard_stop_processing":
                    app.root.update_idletasks.assert_called_once()
                    app._show_warning.assert_called_once()
                    self.assertEqual(render_events, ["render", "alert"])

                app.update_file_status("waiting", "waiting")
                self.assertEqual(app.tree.item("waiting", "tags"), ("queued",))
                app.update_file_status("active", "success")
                self.assertEqual(app.tree.item("active", "tags"), ("success",))

    def test_cancelled_m4b_returns_to_queue_without_erasing_existing_errors(self):
        case = self.make_case()
        case.app.batch_processor = case.processor
        case.app.tree = StatusTree((*case.keys, "group-1", "previous_error"))
        case.app.btn_go_current = mock.Mock()
        case.app._source_group_ids = {"group-1"}
        case.app.update_file_status.side_effect = (
            lambda *args: studio.TTSApp.update_file_status(case.app, *args)
        )
        case.app.update_file_status("previous_error", "error")
        started, release = threading.Event(), threading.Event()

        def cancelled_m4b(*_args, **_kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("Cancelled encoder was not released")
            raise InterruptedError("Stopped by user")

        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=cancelled_m4b):
            worker = self.start_queue(case, (release,))
            self.wait(started)
            case.processor.is_stopped = True
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(self.rows(case)[-1], ("waiting",))
        self.assertNotIn(("error",), self.rows(case))
        self.assertFalse(case.app.finish_processing.call_args.args[-1])
        self.assertEqual(case.app.tree.item("group-1", "tags"), ("queued",))
        case.app._reset_pending_source_rows()
        self.assertEqual(case.app.tree.item("group-1", "tags"), ("queued",))
        self.assertEqual(case.app.tree.item("previous_error", "tags"), ("error",))

    def test_ui_posts_remain_in_mutation_order_when_first_post_is_blocked(self):
        first_post, release_post, second_attempt = (threading.Event() for _ in range(3))
        events = []

        def post(*args):
            if not events:
                first_post.set()
                if not release_post.wait(5):
                    raise TimeoutError("UI post was not released")
            events.append(args)

        tracker = studio.SourceOutputStatusTracker(post)
        tracker.register("m4b", ("group",), ("group",), "m4b")
        start = threading.Thread(target=lambda: tracker.start("m4b"), daemon=True)

        def finish():
            second_attempt.set()
            tracker.update([("m4b", "success")])

        complete = threading.Thread(target=finish, daemon=True)
        start.start()
        self.wait(first_post)
        complete.start()
        self.wait(second_attempt)
        release_post.set()
        start.join(5)
        complete.join(5)
        self.assertEqual(events, [("group", "encoding", "m4b"), ("group", "success")])


if __name__ == "__main__":
    unittest.main()
