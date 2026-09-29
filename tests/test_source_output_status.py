"""Статусы строк и общей сборки при независимом завершении выходов."""

import copy
import threading
import unittest
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

    def render_progress(self, case, observer=None):
        case.app.lbl_current_text = mock.Mock()
        case.app.lbl_current_text.master.winfo_width.return_value = 1
        case.app.lbl_file_pct = mock.Mock()
        case.app.file_progress = {"value": 100}
        case.app._set_status_label = mock.Mock()

        def update(pct, text):
            studio.TTSApp.update_progress_ui(case.app, pct, text)
            if observer is not None:
                observer(pct, case.app._set_status_label.call_args.args[1])

        case.app.update_progress_ui.side_effect = update

    def rendered_progress(self, case):
        return case.app._set_status_label.call_args.args[1]

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

    def test_cancelled_m4b_returns_to_queue_without_erasing_existing_errors(self):
        case = self.make_case()
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
        case.app._reset_pending_source_rows()
        self.assertEqual(self.rows(case)[-1], ("queued",))
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
