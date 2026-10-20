import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class ExportGroupNumberingTests(unittest.TestCase):
    def test_volume_conflicts_keep_gaps_hints_and_local_part_offsets(self):
        entries = [
            ("book", "a", 1, 7, 12),
            ("book", "b", 1, 0, 0),
            ("book", "a", 2, 0, 0),
            ("book", "c", 1, 1, 0),
            ("book", "d", 1, 7, 0),
            ("book", "e", 1, -10, 0),
            ("book", "a", 4, 0, 0),
            ("book", "f", 1, "bad", "bad"),
            ("another-book", "z", 1, 0, 0),
        ]
        before = copy.deepcopy(entries)
        self.assertEqual(studio.assign_m4b_volume_numbers(entries), {
            ("a", 1): (7, 12), ("b", 1): (1, 12), ("a", 2): (8, 12),
            ("c", 1): (2, 12), ("d", 1): (9, 12), ("e", 1): (3, 12),
            ("a", 4): (10, 12), ("f", 1): (4, 12), ("z", 1): (1, 1),
        })
        self.assertEqual(entries, before)

    def run_outputs(self, root, groups, children, files, items, workers, max_chapters=0):
        app = object.__new__(studio.TTSApp)
        app.config = dict(studio.DEFAULT_CONFIG)
        target = studio.OutputTarget(
            format="m4b",
            output_dir=str(root / f"out-{workers}"),
            filename_template="{book} G{group_index} V{part}",
            chapter_title_template=(
                "G{group_index} F{file_index} V{part}/{parts} "
                "C{global_index}/{chapter_count} L{volume_index}/{volume_chapter_count}"
            ),
            max_chapters=max_chapters,
        ).to_dict()
        calls = {}
        lock = threading.Lock()
        barrier = threading.Barrier(2) if workers > 1 else None
        started = 0

        def encode(sources, output, **kwargs):
            nonlocal started
            with lock:
                started += 1
                ordinal = started
            if barrier is not None and ordinal <= 2:
                barrier.wait(timeout=5)
            with lock:
                calls[Path(output).name] = {
                    "sources": tuple(Path(path).name for path in sources),
                    "chapters": tuple(chapter["title"] for chapter in kwargs["chapters"]),
                    "album": kwargs["tags"]["album"],
                    "disc": (kwargs["disk_number"], kwargs["disk_total"]),
                }

        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=encode):
            app._run_output_target_set(
                items, children, groups, files, (target,), root,
                max_workers=workers,
            )
        return calls

    def test_parallel_and_sequential_group_fields_match_for_selected_order_and_splits(self):
        for separate_books, max_chapters in ((False, 0), (False, 1), (True, 1)):
            with self.subTest(separate_books=separate_books, max_chapters=max_chapters):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    groups = {
                        "unselected": {"name": "Не выбрана", "merge": True},
                        "first": {"name": "Первая", "merge": True, "album": "Серия"},
                        "second": {"name": "Вторая", "merge": True, "album": "Другая" if separate_books else "Серия"},
                    }
                    children = {"first": ("a", "b"), "second": ("c", "d")}
                    files = {}
                    for file_id in "abcd":
                        source = root / f"{file_id}.mp3"
                        source.write_bytes(b"audio")
                        files[file_id] = {"path": str(source), "title": file_id, "duration": 10}
                    original = copy.deepcopy((groups, children, files))
                    items = ("second", "first")
                    sequential = self.run_outputs(root, groups, children, files, items, 1, max_chapters)
                    parallel = self.run_outputs(root, groups, children, files, items, 2, max_chapters)
                    self.assertEqual(parallel, sequential)
                    for name, output in parallel.items():
                        group_number = 1 if name.startswith("Вторая") else 2
                        self.assertIn(f"G{group_number}", name)
                        self.assertTrue(all(title.startswith(f"G{group_number} ") for title in output["chapters"]))
                    self.assertEqual((groups, children, files), original)

    def test_imported_subset_keeps_volume_hints_when_one_group_splits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups = {
                "unselected": {"name": "Не выбрана", "merge": True, "album": "Серия"},
                "selected": {
                    "name": "Том семь", "merge": True, "album": "Серия",
                    "source_volume_number": 7, "source_volume_total": 9,
                },
            }
            children = {"selected": ("one", "two")}
            files = {}
            for file_id in ("one", "two"):
                source = root / f"{file_id}.mp3"
                source.write_bytes(b"audio")
                files[file_id] = {"path": str(source), "title": file_id, "duration": 10}
            expected = {
                "Том семь G1 V7.m4b": {
                    "sources": ("one.mp3",), "album": "Серия", "disc": (7, 9),
                    "chapters": ("G1 F1 V7/9 C1/2 L1/1",),
                },
                "Том семь G1 V8.m4b": {
                    "sources": ("two.mp3",), "album": "Серия", "disc": (8, 9),
                    "chapters": ("G1 F2 V8/9 C2/2 L1/1",),
                },
            }
            for workers in (1, 2):
                with self.subTest(workers=workers):
                    self.assertEqual(
                        self.run_outputs(root, groups, children, files, ("selected",), workers, 1),
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
