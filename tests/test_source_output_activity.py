"""Общая строка сборки обходит только нужные незавершённые выходы."""

import importlib.util
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    _module_directory = tempfile.TemporaryDirectory()
    _module_file = Path(_module_directory.name) / "SileroTTS_Studio.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py", _module_file)
    _previous_directory = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, _module_file)
        studio = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = studio
        spec.loader.exec_module(studio)
    finally:
        os.chdir(_previous_directory)
else:
    studio = sys.modules[MODULE_NAME]

import test_source_group_runtime as runtime


class SourceOutputActivityTests(unittest.TestCase):
    def test_default_activity_contract_keeps_terminal_tasks_and_registration_order(self):
        snapshots = []
        tracker = studio.SourceOutputStatusTracker(
            lambda *args: None, lambda tasks: snapshots.append(tuple(tasks))
        )
        tracker.register("first", ("row",), status="success")
        tracker.register("second", ("row",), status="pending")
        tracker.refresh_activity()
        tracker.update([("second", "skipped")])
        tracker.start("first")
        tracker.finish("warning")
        self.assertEqual(snapshots, [
            (("first", "success"), ("second", "pending")),
            (("first", "success"), ("second", "success")),
            (("first", "encoding"), ("second", "success")),
            (("first", "warning"), ("second", "success")),
        ])

    def test_filtered_activity_matches_full_snapshot_across_state_changes(self):
        allowed = set(range(0, 48, 3))
        full_events, filtered_events = [], []
        full_snapshots, filtered_snapshots = [], []
        generic = studio.SourceOutputStatusTracker(
            lambda *args: full_events.append(args),
            lambda tasks: full_snapshots.append(tuple(tasks)),
        )
        indexed = studio.SourceOutputStatusTracker(
            lambda *args: filtered_events.append(args),
            lambda tasks: filtered_snapshots.append(tuple(tasks)),
            activity_task_filter=allowed.__contains__,
        )

        def both(method, *args):
            getattr(generic, method)(*args)
            getattr(indexed, method)(*args)
            self.assertEqual(filtered_events, full_events)
            if full_snapshots:
                expected = tuple(
                    (task_id, status) for task_id, status in full_snapshots[-1]
                    if task_id in allowed and status not in generic._terminal_rank
                )
                self.assertEqual(filtered_snapshots[-1], expected)

        for task_id in range(48):
            both("register", task_id, (f"row-{task_id % 5}",),
                 (f"row-{task_id % 5}",), "m4b",
                 "success" if task_id % 7 == 0 else "pending")
        both("refresh_activity")
        rng = random.Random(162)
        for _ in range(150):
            task_id = rng.randrange(48)
            if rng.randrange(3) == 0:
                both("start", task_id, tuple(range(0, 48, 11)))
            else:
                both("update", [(task_id, rng.choice((
                    "pending", "encoding", "preparing", "processing", "skipped",
                    "success", "empty", "warning", "error",
                )))])
        # Replacing a split plan also removes completed IDs and appends new IDs
        # after retained tasks, even when the old IDs are used again.
        registrations = [
            (3, ("row-3",), ("row-3",), "m4b", "pending"),
            (48, ("row-3",), ("row-3",), "m4b", "encoding"),
        ]
        allowed.add(48)
        both("replace", (3, 6), registrations)
        both("update", [(0, "pending"), (9, "pending"), (48, "success")])
        both("finish", "warning")
        self.assertEqual(filtered_snapshots[-1], ())

    def test_large_ready_plan_does_not_visit_completed_or_unrelated_tasks(self):
        visits = []
        tracker = studio.SourceOutputStatusTracker(
            lambda *args: None, lambda tasks: visits.append(tuple(tasks)),
            activity_task_filter=lambda task_id: task_id >= 4996,
        )
        for task_id in range(5000):
            tracker.register(task_id, (str(task_id),),
                             status="success" if task_id < 4996 else "pending")
        for task_id in range(4996, 5000):
            tracker.start(task_id)
        self.assertEqual(sum(map(len, visits)), 16)
        self.assertEqual([task_id for task_id, _ in visits[-1]], list(range(4996, 5000)))
        tracker.finish("error")
        self.assertEqual(visits[-1], ())

    def test_ignored_activity_does_not_traverse_pending_index(self):
        class CountedIndex(dict):
            traversals = 0

            def __iter__(self):
                self.traversals += 1
                return super().__iter__()

        snapshots = []
        tracker = studio.SourceOutputStatusTracker(
            lambda *args: None, lambda tasks: None,
            activity_task_filter=lambda task_id: True,
        )
        tracker._pending_activity_tasks = index = CountedIndex()
        for task_id in range(1000):
            tracker.register(task_id, (str(task_id),))
        for task_id in range(20):
            tracker.update([(task_id, "processing")])
        self.assertEqual(index.traversals, 0)
        tracker._post_activity = lambda tasks: snapshots.append(tuple(tasks))
        tracker.refresh_activity()
        self.assertEqual(index.traversals, 1)
        self.assertEqual(len(snapshots[-1]), 1000)

    def test_actual_ungrouped_queue_publishes_only_pending_outputs_and_clears_them(self):
        case = runtime.SourceGroupOutputRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.configure([
            {"format": "mp3", "assembly_mode": "files"},
            {"format": "m4b", "bitrate": "64k"},
        ])
        case.config["source_virtual_group_ids"] = ()
        original = studio.SourceOutputStatusTracker
        trackers, snapshots = [], []

        class ObservedTracker(original):
            def __init__(self, post_status, post_activity=None, **kwargs):
                def observe(tasks):
                    snapshot = tuple(tasks)
                    snapshots.append(snapshot)
                    if post_activity is not None:
                        post_activity(iter(snapshot))

                super().__init__(post_status, observe, **kwargs)
                trackers.append(self)

        with mock.patch.object(studio, "SourceOutputStatusTracker", ObservedTracker):
            case.run_queue(skip=False)

        self.assertEqual(len(trackers), 1)
        tracker = trackers[0]
        self.assertIsNotNone(tracker._activity_task_filter)
        self.assertTrue(any(snapshots))
        for snapshot in snapshots:
            for task_id, status in snapshot:
                self.assertEqual(task_id[0], "output")
                self.assertNotIn(status, tracker._terminal_rank)
                self.assertEqual(tracker._tasks[task_id]["format"], "m4b")
        self.assertEqual(snapshots[-1], ())
        self.assertFalse(case.app.finish_processing.call_args.args[-1])
        self.assertEqual(len(case.encoded), len(case.records))
        self.assertEqual(case.processor.process_text_file.call_count, len(case.keys))
        final_rows = {call.args[0]: call.args[1]
                      for call in case.app.update_file_status.call_args_list}
        self.assertEqual({key: final_rows[key] for key in case.keys},
                         {key: "success" for key in case.keys})


if __name__ == "__main__":
    unittest.main()
