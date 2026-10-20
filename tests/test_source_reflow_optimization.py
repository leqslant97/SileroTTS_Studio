"""Перепланирование M4B сохраняет пути, локальные теги и границы ошибок."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_regressions import studio


class SourceReflowOptimizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.source.mkdir()
        self.output = self.root / "audio"

    def record(self, index, ids, metadata=None, **extra):
        result = {
            "kind": "m4b", "target_index": 0,
            "target": {"format": "m4b", "max_duration_seconds": 3},
            "path": self.output / f"old-{index}.m4b",
            "item_id": f"old-{index}", "group_name": f"Том {index}",
            "book": "Книга", "file_ids": tuple(ids),
            "source_paths": tuple(self.source / f"{key}.txt" for key in ids),
            "metadata_overrides": copy.deepcopy(metadata or {}),
        }
        result.update(extra)
        return result

    def reflow(self, records, *, paths=None, explicit=False):
        durations = {str(key): 1.0 for record in records for key in record["file_ids"]}
        return studio.reflow_source_m4b_target_records(
            records, {}, {"source_m4b_reflow_actual_duration": explicit},
            source_root=self.source, output_root=self.output,
            source_path_by_id=paths, chapter_duration_overrides=durations,
        )

    def test_many_split_groups_keep_own_tags_paths_and_input(self):
        records = [self.record(index, tuple(f"c{index}-{n}" for n in range(6)),
                               {"artist": f"Автор {index}", "album": "Книга"})
                   for index in range(24)]
        original = copy.deepcopy(records)
        for with_paths in (False, True):
            with self.subTest(with_paths=with_paths):
                paths = {key: path for record in records
                         for key, path in zip(record["file_ids"], record["source_paths"])} if with_paths else None
                result = self.reflow(records, paths=paths)
                self.assertTrue(result["changed"])
                self.assertEqual(len(result["records"]), 48)
                for index, row in enumerate(result["records"]):
                    old = records[index // 2]
                    start = (index % 2) * 3
                    self.assertEqual(row["file_ids"], old["file_ids"][start:start + 3])
                    self.assertEqual(row["source_paths"], old["source_paths"][start:start + 3])
                    self.assertEqual(row["metadata_overrides"], old["metadata_overrides"])
                self.assertEqual(records, original)
                result["records"][0]["metadata_overrides"]["artist"] = "Изменён"
                self.assertEqual(result["records"][1]["metadata_overrides"]["artist"], "Автор 0")
                self.assertEqual(records, original)

    def test_merged_parts_keep_equal_tags_and_clear_conflicting_owners(self):
        for second in ({"artist": "Автор", "album": "Книга"}, {"artist": "Другой"}, {}):
            with self.subTest(second=second):
                first = {"artist": "Автор", "album": "Книга"}
                records = [self.record(1, ("a", "b"), first),
                           self.record(2, ("c", "d"), second)]
                result = self.reflow(records, explicit=True)
                rows = result["records"]
                self.assertEqual([row["file_ids"] for row in rows], [("a", "b", "c"), ("d",)])
                self.assertEqual(rows[0]["metadata_overrides"], first if first == second else {})
                self.assertEqual(rows[1]["metadata_overrides"], second)

    def test_tags_stay_separate_between_targets_with_same_chapters(self):
        records = [self.record(1, ("a", "b", "c", "d"), {"artist": "Первый"}),
                   self.record(2, ("a", "b", "c", "d"), {"artist": "Второй"}, target_index=1,
                               target={"format": "m4b", "max_duration_seconds": 3,
                                       "output_dir": str(self.output / "second")})]
        result = self.reflow(records)
        self.assertEqual([row["target_index"] for row in result["records"]], [0, 0, 1, 1])
        self.assertEqual([row["metadata_overrides"]["artist"] for row in result["records"]],
                         ["Первый", "Первый", "Второй", "Второй"])

    def test_path_map_wins_and_missing_entries_use_record_paths(self):
        record = self.record(1, ("a", "b", "c", "d"))
        replacement = self.source / "replacement.txt"
        result = self.reflow([record], paths={"a": replacement, "b": None})
        paths = tuple(path for row in result["records"] for path in row["source_paths"])
        self.assertEqual(paths, (replacement, *record["source_paths"][1:]))

    def test_unused_malformed_fallback_paths_are_not_read(self):
        records = [self.record(1, ("a", "b", "c", "d")),
                   self.record(2, ("e", "f", "g", "h"), source_paths=42)]
        paths = {key: self.source / f"{key}.txt" for key in records[1]["file_ids"]}
        result = self.reflow(records, paths=paths)
        self.assertTrue(result["changed"])
        self.assertEqual(len(result["records"]), 4)

    def test_missing_path_and_path_outside_root_still_fail(self):
        record = self.record(1, ("a", "b", "c", "d"), source_paths=())
        with self.assertRaisesRegex(ValueError, "Для главы a не найден исходный TXT"):
            self.reflow([record])
        with self.assertRaisesRegex(ValueError, "источник находится вне корневой папки"):
            self.reflow([record], paths={key: self.root / f"{key}.txt" for key in record["file_ids"]})

    def test_first_record_owns_fallback_even_if_its_path_is_missing(self):
        records = [self.record(1, ("a", "b"), source_paths=()),
                   self.record(2, ("a", "c"))]
        result = studio.M4BReflowResult(groups=(("a",), ("b",), ("c",)))
        with mock.patch.object(studio, "reflow_m4b_groups_by_duration", return_value=result):
            with self.assertRaisesRegex(ValueError, "Для главы a не найден исходный TXT"):
                self.reflow(records, explicit=True)

    def test_fallback_does_not_rescan_every_record_for_each_chapter(self):
        class CountedRecord(dict):
            reads = 0

            def get(self, key, default=None):
                if key == "file_ids":
                    type(self).reads += 1
                return super().get(key, default)

        records = [CountedRecord(self.record(index, tuple(f"c{index}-{n}" for n in range(6))))
                   for index in range(40)]
        result = self.reflow(records)
        self.assertEqual(len(result["records"]), 80)
        self.assertLess(CountedRecord.reads, 20 * len(records),
                        "Поиск путей повторно обходит все старые группы для каждой главы")

    def test_saved_path_list_is_materialized_once_per_used_group(self):
        class CountedPaths(list):
            reads = 0

            def __iter__(self):
                type(self).reads += 1
                return super().__iter__()

        record = self.record(1, tuple(f"c{n}" for n in range(120)))
        record["source_paths"] = CountedPaths(record["source_paths"])
        result = self.reflow([record])
        self.assertEqual(len(result["records"]), 40)
        self.assertEqual(CountedPaths.reads, 1)


if __name__ == "__main__":
    unittest.main()
