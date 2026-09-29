"""Планирование отдельных глав и групповых аудиофайлов из одного текста."""

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


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


class SourceGroupOutputPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.output = self.root / "audio"
        self.files = [self.source / f"{index:03d}.txt" for index in range(1, 5)]
        self.groups = [
            {
                "id": "part-1", "name": "Том первый",
                "file_ids": [str(path) for path in self.files[:2]],
                "metadata_overrides": {"album": "Первая книга", "artist": "Автор"},
            },
            {
                "id": "part-2", "name": "Том второй",
                "file_ids": [str(path) for path in self.files[2:]],
                "metadata_overrides": {"album": "Вторая книга"},
            },
        ]

    def plan(self, targets, **kwargs):
        return studio.plan_source_synthesis_target_paths(
            self.source, self.output, self.files, targets,
            source_m4b_groups=kwargs.pop("groups", self.groups),
            book_name="Серия", **kwargs,
        )

    def test_three_targets_share_groups_without_overwriting_chapters(self):
        targets = [
            {"format": "mp3", "bitrate": "128k", "assembly_mode": "files"},
            {"format": "mp3", "bitrate": "128k", "assembly_mode": "merge"},
            {"format": "m4b", "bitrate": "96k"},
        ]
        records = self.plan(targets)
        chapters = [record for record in records if record["kind"] == "file"]
        groups = [record for record in records if record["kind"] == "group"]
        books = [record for record in records if record["kind"] == "m4b"]
        self.assertEqual((len(chapters), len(groups), len(books)), (4, 2, 2))
        self.assertEqual(len({record["path"] for record in records}), 8)
        self.assertEqual({record["path"].parent for record in chapters}, {self.output / "mp3"})
        self.assertEqual({record["path"].parent for record in groups}, {self.output / "mp3_2"})
        self.assertEqual(groups[0]["path"].name, "Том первый.mp3")
        self.assertEqual(groups[0]["file_ids"], books[0]["file_ids"])
        self.assertEqual(groups[0]["source_paths"], tuple(self.files[:2]))
        self.assertEqual(groups[0]["metadata_overrides"]["album"], "Первая книга")
        self.assertEqual(groups[1]["metadata_overrides"]["album"], "Вторая книга")
        self.assertEqual(chapters[0]["metadata_overrides"], groups[0]["metadata_overrides"])

    def test_legacy_inherit_stays_per_file_and_explicit_merge_survives_settings(self):
        config = studio.normalize_config({
            "synthesis_targets": [
                {"format": "mp3", "assembly_mode": "inherit"},
                {"format": "opus", "assembly_mode": "merge", "bitrate": "48k"},
                {"format": "m4b", "assembly_mode": "files"},
            ],
        })
        targets = studio.source_synthesis_targets_from_config(config)
        self.assertEqual([target["assembly_mode"] for target in targets], ["files", "merge", "merge"])
        records = self.plan(targets)
        self.assertEqual([record["kind"] for record in records], ["file"] * 4 + ["group"] * 2 + ["m4b"] * 2)

    def test_no_groups_merges_all_selected_texts_into_one_book(self):
        for fmt in ("mp3", "opus", "ogg", "m4a", "wav"):
            with self.subTest(format=fmt):
                records = self.plan([{"format": fmt, "assembly_mode": "merge"}], groups=())
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["path"], self.output / f"Серия.{fmt}")
                self.assertEqual(records[0]["source_paths"], tuple(self.files))
                self.assertEqual(records[0]["kind"], "group")

    def test_group_template_has_number_starts_and_physical_filename(self):
        records = self.plan([{
            "format": "mp3", "assembly_mode": "merge",
            "filename_template": "{book} {part:10:03d} — {filename} — {name}",
        }])
        self.assertEqual(
            [record["path"].name for record in records],
            ["Серия 010 — 001 — Том первый.mp3", "Серия 011 — 003 — Том второй.mp3"],
        )

    def test_group_template_preview_matches_planned_names(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"tag_album": "Серия", "input_dir": str(self.source)}
        app._source_path_by_id = {str(path): path for path in self.files}
        app._source_plan_groups = {group["id"]: group for group in self.groups}
        contexts = app._template_helper_source_preview_data()["contexts"]
        preview_data = {
            "file_contexts": [dict(item, format="mp3", ext="mp3") for item in contexts],
            "grouped_source": True,
        }
        for template in (
            "{index:10:03d} — {name}",
            "{part:10:03d} — {name}",
            "{volume:7:02d} — {name}",
            "{filename}",
            "{chapter:03d} — {name}",
            "{chapter:10} — {name}",
        ):
            with self.subTest(template=template):
                records = self.plan([{
                    "format": "mp3", "assembly_mode": "merge",
                    "filename_template": template,
                }])
                preview = studio.render_template_helper_preview(
                    "output_file", template, preview_data=preview_data
                )
                self.assertEqual(preview, tuple(record["path"].name for record in records))

    def test_file_template_preview_keeps_chapter_start(self):
        template = "{chapter:10:03d} — {filename}"
        records = self.plan([{
            "format": "mp3", "assembly_mode": "files",
            "filename_template": template,
        }])
        preview = studio.render_template_helper_preview(
            "output_file", template, preview_data={
                "file_contexts": [
                    {"index": index, "chapter": index, "filename": path.stem,
                     "format": "mp3", "ext": "mp3"}
                    for index, path in enumerate(self.files, 1)
                ],
                "grouped_source": False,
            },
        )
        self.assertEqual(preview, tuple(record["path"].name for record in records[:3]))

    def test_default_duplicate_group_names_are_disambiguated(self):
        groups = copy.deepcopy(self.groups)
        for group in groups:
            group["name"] = "Одинаковое имя"
        records = self.plan([{"format": "mp3", "assembly_mode": "merge"}], groups=groups)
        self.assertEqual(len({record["path"] for record in records}), 2)
        self.assertTrue(all(record["path"].name.startswith("Одинаковое имя") for record in records))
        with self.assertRaisesRegex(ValueError, "совпадающие выходные имена"):
            self.plan([{
                "format": "mp3", "assembly_mode": "merge", "filename_template": "{book}",
            }])

    def test_group_order_is_preserved_and_unassigned_file_is_not_dropped(self):
        groups = [copy.deepcopy(self.groups[0])]
        groups[0]["file_ids"] = [str(self.files[1]), str(self.files[0]), str(self.files[2])]
        records = self.plan([{"format": "mp3", "assembly_mode": "merge"}], groups=groups)
        self.assertEqual(records[0]["source_paths"], (self.files[1], self.files[0], self.files[2]))
        self.assertEqual(records[1]["source_paths"], (self.files[3],))
        self.assertEqual(records[1]["path"].name, "004.mp3")


if __name__ == "__main__":
    unittest.main()
