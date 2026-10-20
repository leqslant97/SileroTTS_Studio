"""Сценарные проверки помощника на реальном порядке и плане вывода."""
import copy
import unittest
from pathlib import Path

from tests.test_regressions import studio


class PreviewTree:
    def __init__(self, roots, children, selected=()):
        self.roots = tuple(roots)
        self.children = dict(children)
        self.selected = tuple(selected)

    def get_children(self, item=""):
        return self.roots if not item else tuple(self.children.get(item, ()))

    def selection(self):
        return self.selected


class TemplateHelperScenarioTests(unittest.TestCase):
    def test_invalid_global_template_is_rejected_when_all_targets_override_names(self):
        targets = [
            {"format": "m4b", "bitrate": "64k", "filename_template": "А {group_index}-{part}"},
            {"format": "m4b", "bitrate": "96k", "filename_template": "Б {group_index}-{part}"},
        ]
        for source in (False, True):
            app = self.source_app() if source else self.export_app(counts=(1, 1, 1))
            if source:
                app.synthesis_targets = copy.deepcopy(targets)
                snapshot = app._template_helper_source_preview_data()
            else:
                app.export_targets = copy.deepcopy(targets)
                snapshot = app._template_helper_export_preview_data()
            for template in ("{unknown}", "{part", "{part!r}", "{part:banana}"):
                for kind in ("m4b_part", "output_file"):
                    with self.subTest(source=source, template=template, kind=kind):
                        with self.assertRaises(studio.TemplateError):
                            studio.render_template_helper_preview(kind, template, preview_data=snapshot)
                        with self.assertRaises(studio.TemplateError):
                            studio.template_helper_full_preview_rows(kind, template, snapshot)
            for template in ("", "{part:1:00d}", "{{part}}", "{first}-{last}"):
                with self.subTest(source=source, valid_template=template):
                    rows = studio.template_helper_full_preview_rows("m4b_part", template, snapshot)
                    self.assertTrue(rows)
                    self.assertTrue(all(row["result"].startswith(("А ", "Б ")) for row in rows))

    def export_app(self, *, selected=(), counts=(12, 2, 1), inherited_album=False):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = {"export_dir": "/tmp/helper-output", "export_m4b_template": ""}
        app.export_groups = {}
        app.export_files = {}
        children = {}
        for position, count in enumerate(counts, 1):
            group_id = f"g{position}"
            album = "Серия" if position < 3 else "Другая книга"
            app.export_groups[group_id] = {
                "name": f"Том {position}", "merge": True,
                "album": "" if inherited_album else album,
                "artist": "Автор",
            }
            members = []
            for number in range(1, count + 1):
                file_id = f"{group_id}-f{number}"
                members.append(file_id)
                app.export_files[file_id] = {
                    "path": f"/tmp/audio/{group_id}-audio-{number}.wav",
                    "title": f"Глава {number}", "duration": 60,
                    "album": album, "artist": "Автор",
                }
            children[group_id] = tuple(members)
        app.export_tree = PreviewTree(app.export_groups, children, selected)
        return app

    def source_app(self, *, groups=True):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = {
            "input_dir": "/tmp/helper-book", "output_dir": "/tmp/helper-output",
            "synthesis_book_name": "Название книги", "synthesis_m4b_template": "",
        }
        # У книги собственное логическое имя, привязанное к выбранной папке.
        app.config["source_book_name"] = "Название книги"
        app.config["source_book_name_input_dir"] = "/tmp/helper-book"
        app._source_path_by_id = {
            f"f{number}": f"/tmp/helper-book/{number:02d}.txt"
            for number in range(1, 13)
        }
        app._source_tree_file_ids = lambda: tuple(app._source_path_by_id)
        app._source_runtime_m4b_records = None
        app._source_plan_groups = ({
            "g1": {"name": "Первый том", "file_ids": tuple(f"f{n}" for n in range(1, 7))},
            "g2": {"name": "Второй том", "file_ids": tuple(f"f{n}" for n in range(7, 13))},
        } if groups else {})
        return app

    def test_selected_second_group_previews_whole_tree_and_series_volumes(self):
        app = self.export_app(selected=("g2",))
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "G{group_index}-V{part}-T{parts}-{range}", preview_data=data
            ),
            ("G1-V1-T2-01-12.m4b", "G2-V2-T2-1-2.m4b", "G3-V1-T1-1-1.m4b"),
        )

    def test_selection_click_order_does_not_change_output_tree_order(self):
        app = self.export_app(selected=("g2", "g1"), counts=(1, 1, 1))
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", "{group_index}-{part}", preview_data=data),
            ("1-1.m4b", "2-2.m4b", "3-1.m4b"),
        )

    def test_split_group_keeps_group_number_but_advances_volume(self):
        app = self.export_app(selected=("g2",))
        data = app._template_helper_export_preview_data(
            "m4b", target={"format": "m4b", "max_chapters": 5, "profile": "Речевой"}
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{group_index}-{part}-{parts}-{profile}", preview_data=data, limit=None
            ),
            ("1-1-4-Речевой.m4b", "1-2-4-Речевой.m4b", "1-3-4-Речевой.m4b",
             "2-4-4-Речевой.m4b", "3-1-1-Речевой.m4b"),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_chapter", "{global_index}/{chapter_count} G{group_index} V{part}", preview_data=data, limit=None
            )[12:14],
            ("13/14 G2 V4", "14/14 G2 V4"),
        )

    def test_separate_album_restarts_volume_and_global_chapter_counter(self):
        app = self.export_app(selected=("g3",))
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview("m4b_chapter", "G{group_index} V{part} C{global_index}/{chapter_count}", preview_data=data, limit=None)[-1:],
            ("G3 V1 C1/1",),
        )

    def test_album_inherited_from_first_file_has_shared_series(self):
        app = self.export_app(selected=("g2",), inherited_album=True)
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", "{group_index}-{part}-{parts}", preview_data=data),
            ("1-1-2.m4b", "2-2-2.m4b", "3-1-1.m4b"),
        )
        self.assertEqual(data["chapter_groups"][1]["global_offset"], 12)

    def test_chapter_preview_does_not_truncate_count_or_adaptive_width(self):
        app = self.export_app(selected=("g1",))
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_chapter", "{volume_index:00d}/{volume_chapter_count}/{chapter_count}", preview_data=data
            ),
            ("01/12/14", "02/12/14", "03/12/14"),
        )

    def test_merge_and_files_previews_follow_target_assembly(self):
        app = self.export_app(selected=("g2",), counts=(2, 2, 1))
        merge = app._template_helper_export_preview_data(
            "mp3", target={"format": "mp3", "assembly_mode": "merge", "profile": "MP3"}
        )
        files = app._template_helper_export_preview_data(
            "mp3", target={"format": "mp3", "assembly_mode": "files", "profile": "MP3"}
        )
        template = "{group_index}-{index}-{filename}-{name}-{profile}"
        self.assertEqual(
            studio.render_template_helper_preview("output_file", template, preview_data=merge, limit=None),
            ("1-1-g1-audio-1-Том 1-MP3.mp3", "2-2-g2-audio-1-Том 2-MP3.mp3",
             "3-3-g3-audio-1-Том 3-MP3.mp3"),
        )
        self.assertEqual(
            studio.render_template_helper_preview("output_file", template, preview_data=files, limit=None),
            ("1-1-g1-audio-1-Глава 1-MP3.mp3", "1-2-g1-audio-2-Глава 2-MP3.mp3",
             "2-1-g2-audio-1-Глава 1-MP3.mp3", "2-2-g2-audio-2-Глава 2-MP3.mp3",
             "3-1-g3-audio-1-Глава 1-MP3.mp3"),
        )

    def test_group_child_root_file_and_reversed_selection_have_same_full_preview(self):
        expected = (
            "Том 1-1-2.m4b", "Том 2-2-2.m4b", "Том 3-1-1.m4b", "Послесловие-1-1.m4b",
        )
        for selected in ((), ("g2",), ("g2-f1",), ("standalone",), ("g2", "g1")):
            with self.subTest(selected=selected):
                app = self.export_app(selected=selected, counts=(2, 2, 1))
                app.export_files["standalone"] = {
                    "path": "/tmp/audio/epilogue.wav", "title": "Послесловие", "duration": 30,
                }
                app.export_tree.roots += ("standalone",)
                data = app._template_helper_export_preview_data("m4b")
                full = studio.render_template_helper_preview(
                    "m4b_part", "{name}-{part}-{parts}", preview_data=data, limit=None
                )
                self.assertEqual(full, expected)
                self.assertEqual(
                    studio.render_template_helper_preview(
                        "m4b_part", "{name}-{part}-{parts}", preview_data=data
                    ), full[:3],
                )
                self.assertEqual(
                    studio.render_template_helper_preview(
                        "m4b_chapter", "{name}-{part}-{global_index}/{chapter_count}",
                        preview_data=data, limit=None,
                    ),
                    ("Глава 1-1-1/4", "Глава 2-1-2/4", "Глава 1-2-3/4", "Глава 2-2-4/4",
                     "Глава 1-1-1/1", "Послесловие-1-1/1"),
                )

    def test_two_m4b_targets_keep_independent_volume_and_chapter_totals(self):
        app = self.export_app(selected=("g2", "g1"), counts=(5, 1, 1))
        targets = [
            {"format": "m4b", "profile": "A", "max_chapters": 2,
             "output_dir": "/tmp/helper-output/a", "filename_template": "A-G{group_index}-V{part}"},
            {"format": "m4b", "profile": "B", "max_chapters": 4,
             "output_dir": "/tmp/helper-output/b", "filename_template": "B-G{group_index}-V{part}"},
        ]
        before = copy.deepcopy(targets)
        data = app._template_helper_export_preview_data(
            "m4b", target=targets[1], targets=targets, edited_target_index=1,
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{profile}-G{group_index}-V{part}-{parts}-{range}",
                preview_data=data, limit=None,
            ),
            ("B-G1-V1-3-1-4.m4b", "B-G1-V2-3-5-5.m4b",
             "B-G2-V3-3-1-1.m4b", "B-G3-V1-1-1-1.m4b"),
        )
        self.assertEqual(
            [(group["part"], group["parts"], group["global_offset"], group["chapter_count"])
             for group in data["chapter_groups"]],
            [(1, 3, 0, 6), (2, 3, 4, 6), (3, 3, 5, 6), (1, 1, 0, 1)],
        )
        baseline = studio._template_helper_plan_records(data)
        edited = studio._template_helper_plan_records(data, "{profile}-Новый-{group_index}-{part}")
        self.assertEqual(
            [record["path"] for record in baseline if record["target_index"] == 0],
            [record["path"] for record in edited if record["target_index"] == 0],
        )
        self.assertEqual(targets, before)

    def test_preview_volume_numbers_match_written_disc_metadata_across_split_targets(self):
        app = self.export_app(selected=("g3", "g2", "g1"), counts=(5, 1, 1))
        targets = [
            {"format": "m4b", "max_chapters": limit, "output_dir": f"/tmp/helper-output/{limit}"}
            for limit in (2, 4)
        ]
        data = app._template_helper_export_preview_data("m4b", targets=targets)
        records = studio._template_helper_plan_records(data)
        volumes, offsets, chapter_totals = studio._export_m4b_sequence_metadata(
            records, app.export_tree.children, app.export_groups, app.export_files,
        )
        for record in records:
            key = (record["target_index"], record["item_id"], record["part"].number)
            context = record["template_context"]
            self.assertEqual((context["part"], context["parts"]), volumes[key])
        self.assertEqual(volumes[(0, "g2", 1)], (4, 4))
        self.assertEqual(volumes[(1, "g2", 1)], (3, 3))
        self.assertEqual(offsets[(0, "g2", 1)], 5)
        self.assertEqual(chapter_totals[(1, "g2", 1)], 6)

    def test_empty_groups_do_not_create_volumes_or_reset_existing_group_numbers(self):
        app = self.export_app(selected=("g1",), counts=(0, 2, 1))
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{group_index}-{part}-{parts}", preview_data=data, limit=None,
            ),
            ("2-1-1.m4b", "3-1-1.m4b"),
        )
        self.assertEqual([group["chapter_count"] for group in data["chapter_groups"]], [2, 1])

    def test_all_empty_groups_show_no_synthetic_volume_chapter_or_album(self):
        app = self.export_app(selected=("g2",), counts=(0, 0, 0))
        data = app._template_helper_export_preview_data("m4b")
        for kind in ("m4b_part", "m4b_chapter", "m4b_album"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    studio.render_template_helper_preview(kind, "{name}", preview_data=data, limit=None), (),
                )

    def test_same_album_with_different_album_artist_has_separate_volume_sequence(self):
        app = self.export_app(counts=(2, 2, 1), selected=("g2",))
        app.export_groups["g1"]["album_artist"] = "Автор А"
        app.export_groups["g2"]["album_artist"] = "Автор Б"
        data = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{group_index}-{part}-{parts}", preview_data=data, limit=None,
            ),
            ("1-1-1.m4b", "2-1-1.m4b", "3-1-1.m4b"),
        )
        self.assertEqual([group["global_offset"] for group in data["chapter_groups"]], [0, 0, 0])
        self.assertEqual([group["chapter_count"] for group in data["chapter_groups"]], [2, 2, 1])

    def test_global_m4b_helper_preserves_explicit_target_template_and_resolved_directories(self):
        app = self.export_app(selected=("g2",), counts=(5, 1))
        app.config["export_m4b_template"] = "Старое-{group_index}-{part}"
        app.export_targets = [
            {"format": "mp3", "profile": "MP3", "assembly_mode": "files", "filename_template": "MP3-{filename}"},
            {"format": "m4b", "profile": "AAC-64", "max_chapters": 2, "bitrate": "64k",
             "filename_template": "Свой-G{group_index}-V{part}"},
            {"format": "m4b", "profile": "AAC-96", "max_chapters": 4, "bitrate": "96k",
             "output_dir": "мои-книги", "filename_template": ""},
            {"format": "m4b", "profile": "Отключено", "enabled": False, "max_chapters": 1},
        ]
        before = copy.deepcopy((app.export_targets, app.config))
        data = app._template_helper_export_preview_data("m4b")
        self.assertTrue(data["edits_global_template"])
        records = studio._template_helper_plan_records(data, "Общий-G{group_index}-V{part}")
        self.assertEqual([record["target"]["profile"] for record in records], ["AAC-64"] * 4 + ["AAC-96"] * 3)
        self.assertEqual(
            [Path(record["path"]).name for record in records],
            ["Свой-G1-V1.m4b", "Свой-G1-V2.m4b", "Свой-G1-V3.m4b", "Свой-G2-V4.m4b",
             "Общий-G1-V1.m4b", "Общий-G1-V2.m4b", "Общий-G2-V3.m4b"],
        )
        self.assertEqual(
            {Path(record["path"]).parent for record in records if record["target"]["profile"] == "AAC-96"},
            {Path(app.config["export_dir"]).resolve() / "мои-книги"},
        )
        auto_dirs = {Path(record["path"]).parent for record in records if record["target"]["profile"] == "AAC-64"}
        self.assertEqual(len(auto_dirs), 1)
        self.assertEqual(next(iter(auto_dirs)).parent, Path(app.config["export_dir"]).resolve())
        self.assertNotIn(Path(app.config["export_dir"]).resolve(), auto_dirs)
        full = studio.template_helper_full_preview_rows("m4b_part", "Общий-G{group_index}-V{part}", data)
        self.assertEqual([row["path"] for row in full], [str(record["path"]) for record in records])
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", "Общий-G{group_index}-V{part}", preview_data=data),
            ("Свой-G1-V1.m4b", "Свой-G1-V2.m4b", "Свой-G1-V3.m4b"),
        )
        actual_arguments = copy.deepcopy(data["export_plan"])
        actual_arguments["m4b_template"] = "Общий-G{group_index}-V{part}"
        actual = studio.plan_export_target_paths(**actual_arguments)
        self.assertEqual([record["path"] for record in records], [record["path"] for record in actual])
        self.assertEqual((app.export_targets, app.config), before)

    def test_global_m4b_preview_updates_when_actual_audio_duration_becomes_known(self):
        app = self.export_app(selected=("g2",), counts=(3, 1))
        app.export_targets = [{"format": "m4b", "max_duration_seconds": 100, "filename_template": ""}]
        for file in app.export_files.values():
            file["duration"] = 0
        initial = app._template_helper_export_preview_data("m4b")
        template = "G{group_index}-V{part}-{parts}"
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", template, preview_data=initial, limit=None),
            ("G1-V1-2.m4b", "G2-V2-2.m4b"),
        )
        for file in app.export_files.values():
            file["duration"] = 60
        current = app._template_helper_export_preview_data("m4b")
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", template, preview_data=current, limit=None),
            ("G1-V1-4.m4b", "G1-V2-4.m4b", "G1-V3-4.m4b", "G2-V4-4.m4b"),
        )
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", template, preview_data=initial, limit=None),
            ("G1-V1-2.m4b", "G2-V2-2.m4b"),
        )
        self.assertEqual([group["chapter_count"] for group in current["chapter_groups"]], [4] * 4)

    def test_virtual_chapter_physical_filename_is_distinct_from_saved_source_name(self):
        rendered = studio.render_m4b_chapter_titles([
            {"title": "Редактируемая глава", "source_name": "Старое имя главы", "path": "/tmp/book.m4b"}
        ], "{filename}|{source_name}|{name}", book="Книга")
        self.assertEqual(rendered[0]["title"], "book|Старое имя главы|Редактируемая глава")

    def test_source_merge_numbers_and_ranges_follow_groups(self):
        app = self.source_app()
        data = app._template_helper_source_preview_data(target={"format": "mp3", "assembly_mode": "merge"})
        self.assertEqual(
            studio.render_template_helper_preview("output_file", "G{group_index}-F{file_index}-I{index}-{range}-{filename}", preview_data=data),
            ("G1-F1-I1-01-06-01.mp3", "G2-F1-I2-07-12-07.mp3"),
        )

    def test_source_separate_files_have_one_group_and_sequential_file_index(self):
        app = self.source_app()
        data = app._template_helper_source_preview_data(target={"format": "mp3", "assembly_mode": "files"})
        self.assertEqual(
            studio.render_template_helper_preview("output_file", "{group_index}-{file_index}-{name}", preview_data=data),
            ("1-1-01.mp3", "1-2-02.mp3", "1-3-03.mp3"),
        )

    def test_source_m4b_override_and_empty_inheritance_match_planner(self):
        app = self.source_app()
        data = app._template_helper_source_preview_data(target={"format": "m4b"})
        self.assertEqual(studio.render_template_helper_preview("m4b_part", "", preview_data=data),
                         ("Первый том.m4b", "Второй том.m4b"))
        self.assertEqual(studio.render_template_helper_preview("m4b_part", "Книга {group_index}-{range}", preview_data=data),
                         ("Книга 1-01-06.m4b", "Книга 2-07-12.m4b"))

    def test_source_group_name_alias_uses_book_before_names_are_generated(self):
        data = {"contexts": [{"book": "Книга", "name": "Старая группа", "part": 1, "parts": 1}]}
        self.assertEqual(studio.render_template_helper_preview("source_group", "{name}|{group}|{source_name}", preview_data=data),
                         ("Книга_Книга_Книга",))

    def test_accepted_source_m4b_boundaries_replace_initial_tree_groups(self):
        app = self.source_app(groups=False)
        target = {"format": "m4b", "assembly_mode": "merge"}
        app._source_runtime_m4b_records = studio.plan_source_synthesis_target_paths(
            "/tmp/helper-book", "/tmp/helper-output",
            [{"id": key, "path": path} for key, path in app._source_path_by_id.items()],
            targets=[target], m4b_template="", source_m4b_groups={
                "a": {"name": "Принятая часть 1", "file_ids": tuple(f"f{n}" for n in range(1, 5))},
                "b": {"name": "Принятая часть 2", "file_ids": tuple(f"f{n}" for n in range(5, 13))},
            },
        )
        data = app._template_helper_source_preview_data(target=target)
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", "{part}-{first_index}-{last_index}", preview_data=data),
            ("1-1-4.m4b", "2-5-12.m4b"),
        )

    def test_preview_does_not_mutate_groups_files_or_target(self):
        app = self.export_app(selected=("g2",))
        target = {"format": "m4b", "max_chapters": 5, "profile": "Речевой"}
        before = copy.deepcopy((app.export_groups, app.export_files, target))
        data = app._template_helper_export_preview_data("m4b", target=target)
        studio.render_template_helper_preview("m4b_part", "{group_index}-{part}", preview_data=data)
        self.assertEqual((app.export_groups, app.export_files, target), before)

    def test_source_chapter_and_album_author_follow_global_local_and_empty_values(self):
        for overrides, expected in (
            ({}, "Общий исполнитель"),
            ({"artist": "Локальный исполнитель"}, "Локальный исполнитель"),
            ({"artist": ""}, ""),
        ):
            for apply_tags in (True, False):
                with self.subTest(overrides=overrides, apply_tags=apply_tags):
                    app = self.source_app()
                    app.config.update(tag_artist="Общий исполнитель", apply_output_tags=apply_tags)
                    for group in app._source_plan_groups.values():
                        group["metadata_overrides"] = copy.deepcopy(overrides)
                    data = app._template_helper_source_preview_data(target={"format": "m4b"})
                    self.assertEqual(
                        studio.render_template_helper_preview(
                            "m4b_chapter", "[{author}]-{volume_index}", preview_data=data
                        ),
                        tuple(f"[{expected}]-{number}" for number in range(1, 4)),
                    )
                    self.assertEqual(
                        studio.render_template_helper_preview("m4b_album", "[{author}]", preview_data=data),
                        ((f"[{expected}]",) * 2 if apply_tags else ("(тег не записывается)",) * 2),
                    )
                    # Автор имени файла имеет отдельный контракт: только
                    # локальное значение группы, без общих метаданных.
                    self.assertEqual(data["contexts"][0]["author"], overrides.get("artist", ""))

    def test_empty_source_tree_does_not_preview_excluded_paths(self):
        app = self.source_app()
        app._source_tree_file_ids = lambda: ()
        snapshot = app._template_helper_source_preview_data()
        for kind, template in (("m4b_part", "{book}-{part}"), ("m4b_chapter", "{name}"), ("m4b_album", "{book}")):
            with self.subTest(kind=kind):
                self.assertEqual(studio.render_template_helper_preview(kind, template, preview_data=snapshot), ())
                self.assertEqual(studio.template_helper_full_preview_rows(kind, template, snapshot), ())
        self.assertNotIn("excluded", str(snapshot))

    def test_fully_cleared_export_tree_never_returns_teaching_samples(self):
        app = self.export_app()
        app.export_tree.roots = ()
        snapshot = app._template_helper_export_preview_data()
        for kind, template in (("m4b_part", "{book}-{part}"), ("m4b_chapter", "{name}"), ("m4b_album", "{book}"), ("output_file", "{name}")):
            with self.subTest(kind=kind):
                self.assertEqual(studio.render_template_helper_preview(kind, template, preview_data=snapshot), ())
                self.assertEqual(studio.template_helper_full_preview_rows(kind, template, snapshot), ())
        with self.assertRaises(studio.TemplateError):
            studio.render_template_helper_preview("m4b_part", "{broken", preview_data=snapshot)


if __name__ == "__main__":
    unittest.main()
