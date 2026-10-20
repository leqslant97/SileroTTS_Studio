"""Большие планы сохраняют имена и проверку путей без повторных обходов."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class ExportCollisionPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def plan(self, names, targets=None):
        groups = {f"g{i}": {"name": name, "merge": True} for i, name in enumerate(names)}
        children = {group: (group + "_file",) for group in groups}
        files = {group + "_file": {"path": str(self.root / f"{group}.mp3"),
                                  "title": "Глава", "duration": 1.25} for group in groups}
        arguments = (tuple(groups), children, groups, files,
                     targets or [{"format": "m4b", "filename_template": "{book}"}],
                     self.root / "output")
        original = copy.deepcopy(arguments)
        result = studio.plan_export_target_paths(*arguments)
        self.assertEqual(arguments, original)
        self.assertFalse((self.root / "output").exists())
        return result

    def test_many_equal_names_need_linear_number_of_suffix_candidates(self):
        count = 700
        calls = []
        original = Path.with_name

        def with_name(path, name):
            calls.append(name)
            return original(path, name)

        with mock.patch.object(Path, "with_name", with_name):
            records = self.plan(["Том"] * count)
        self.assertEqual([record["path"].name for record in records],
                         ["Том.m4b"] + [f"Том Часть {i}.m4b" for i in range(2, count + 1)])
        self.assertLessEqual(len(calls), count * 2)
        self.assertEqual([record["item_id"] for record in records],
                         [f"g{i}" for i in range(count)])

    def test_first_free_suffix_preserves_case_and_skips_explicit_names(self):
        records = self.plan(["Том", "Том Часть 2", "том", "Том Часть 5", "Том", "ТОМ"])
        self.assertEqual([record["path"].name for record in records], [
            "Том.m4b", "Том Часть 2.m4b", "том Часть 3.m4b",
            "Том Часть 5.m4b", "Том Часть 4.m4b", "ТОМ Часть 6.m4b",
        ])

    def test_suffixes_are_independent_between_directories_and_shared_between_targets(self):
        for same_directory in (False, True):
            with self.subTest(same_directory=same_directory):
                targets = [{"format": "m4b", "bitrate": bitrate, "filename_template": "{book}",
                            "output_dir": str(self.root / "output" / directory)}
                           for bitrate, directory in (("64k", "a"),
                                                       ("96k", "a" if same_directory else "b"))]
                records = self.plan(["Том"] * 3, targets)
                expected = ["Том.m4b", "Том Часть 2.m4b", "Том Часть 3.m4b"]
                expected += ([f"Том Часть {i}.m4b" for i in range(4, 7)]
                             if same_directory else expected[:])
                self.assertEqual([record["path"].name for record in records], expected)

    def test_regular_format_collisions_still_raise(self):
        with self.assertRaisesRegex(ValueError, "совпадающие выходные имена"):
            self.plan(["Том", "Том"], [{"format": "mp3", "assembly_mode": "merge",
                                        "filename_template": "{book}"}])


class SourcePathPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.source.mkdir()
        self.files = [{"id": str(i), "path": self.source / f"{i:03d}.txt"} for i in range(80)]

    def plan(self, files=None, groups=()):
        return studio.plan_source_synthesis_target_paths(
            self.source, self.root / "output", self.files if files is None else files,
            [{"format": "mp3", "assembly_mode": "merge"}],
            source_m4b_groups=groups, book_name="Книга",
        )

    def symlink(self, path, target):
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Символические ссылки недоступны: {exc}")

    def test_sources_without_groups_are_resolved_once_each(self):
        calls = []
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path.suffix == ".txt":
                calls.append(str(path))
            return original(path, *args, **kwargs)

        for groups in ((), [], {}, iter(())):
            with self.subTest(groups=type(groups).__name__):
                calls.clear()
                with mock.patch.object(Path, "resolve", resolve):
                    records = self.plan(groups=groups)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["file_ids"], tuple(str(i) for i in range(80)))
                self.assertEqual(calls, [str(record["path"]) for record in self.files])

    def test_internal_symlink_keeps_display_name_and_resolved_source(self):
        target = self.source / "actual.txt"
        target.write_text("Глава", encoding="utf-8")
        alias = self.source / "alias.txt"
        self.symlink(alias, target)
        records = studio.plan_source_synthesis_target_paths(
            self.source, self.root / "output", [alias], [{"format": "mp3"}],
        )
        self.assertEqual(records[0]["path"].name, "alias.mp3")
        self.assertEqual(records[0]["source_path"], target)

    def test_explicit_groups_reuse_checked_source_paths(self):
        calls = []
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path.suffix == ".txt":
                calls.append(str(path))
            return original(path, *args, **kwargs)

        groups = [{"id": "part", "name": "Первая часть",
                   "file_ids": [str(i) for i in range(60)],
                   "metadata_overrides": {"artist": "Автор"}}]
        with mock.patch.object(Path, "resolve", resolve):
            records = self.plan(groups=groups)
        self.assertEqual([row["file_ids"] for row in records],
                         [tuple(str(i) for i in range(60))]
                         + [(str(i),) for i in range(60, 80)])
        self.assertEqual(records[0]["metadata_overrides"], {"artist": "Автор"})
        self.assertEqual(records[0]["source_paths"],
                         tuple(row["path"] for row in self.files[:60]))
        self.assertEqual(calls, [str(row["path"]) for row in self.files])

    def test_external_symlink_and_duplicate_ids_still_raise(self):
        target = self.root / "outside.txt"
        target.write_text("Глава", encoding="utf-8")
        alias = self.source / "alias.txt"
        self.symlink(alias, target)
        with self.assertRaisesRegex(ValueError, "вне корневой папки"):
            self.plan(files=[alias])
        with self.assertRaisesRegex(ValueError, "дублирующийся идентификатор"):
            self.plan(files=[self.files[0], self.files[0]])

    def test_explicit_group_generator_resolves_path_aliases_and_preserves_uncovered_files(self):
        first, second, third = self.files[:3]
        alias = self.source / "alias.txt"
        self.symlink(alias, first["path"])
        groups = iter([{"id": "selected", "name": "Часть", "paths": [alias, second["path"]],
                        "metadata_overrides": {"artist": "Автор"}}])
        records = self.plan(files=[first, second, third], groups=groups)
        self.assertEqual([record["file_ids"] for record in records], [("0", "1"), ("2",)])
        self.assertEqual(records[0]["metadata_overrides"], {"artist": "Автор"})
        self.assertEqual(records[0]["source_paths"], (first["path"], second["path"]))


if __name__ == "__main__":
    unittest.main()
