"""Предпросмотр использует принятые части каждой текущей цели M4B."""

import copy
import tempfile
import unittest
from pathlib import Path

from tests.test_regressions import studio


class TemplatePreviewRuntimeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.app = studio.TTSApp.__new__(studio.TTSApp)
        self.app.config = {
            "input_dir": str(self.base / "texts"),
            "output_dir": str(self.base / "audio"),
            "source_book_name": "Книга",
            "source_book_name_input_dir": str(self.base / "texts"),
            "synthesis_m4b_template": "",
            "synthesis_m4b_album_template": "{book}",
            "apply_output_tags": True,
        }
        self.app._source_path_by_id = {
            f"f{number}": str(self.base / "texts" / f"{number:02d}.txt")
            for number in range(1, 7)
        }
        self.app._source_tree_file_ids = lambda: tuple(self.app._source_path_by_id)
        self.app._source_plan_groups = {}
        self.app._source_runtime_m4b_records = None
        self.groups_a = [
            {"id": "a1", "name": "Часть А1", "file_ids": ("f1", "f2", "f3")},
            {"id": "a2", "name": "Часть А2", "file_ids": ("f4", "f5", "f6")},
        ]
        self.groups_b = [
            {"id": f"b{number}", "name": f"Часть Б{number}",
             "file_ids": tuple(f"f{2 * number + offset}" for offset in (-1, 0))}
            for number in range(1, 4)
        ]
        self.target_a = {
            "format": "m4b", "assembly_mode": "merge", "bitrate": "64k",
            "output_dir": str(self.base / "audio" / "a"),
        }
        self.target_b = {
            "format": "m4b", "assembly_mode": "merge", "bitrate": "96k",
            "sample_rate": "48000", "profile": "builtin:m4a-96k",
            "output_dir": str(self.base / "audio" / "b"),
        }

    def plan(self, targets, groups):
        return studio.plan_source_synthesis_target_paths(
            source_root=self.app.config["input_dir"],
            output_root=self.app.config["output_dir"],
            files=[{"id": key, "path": path} for key, path in self.app._source_path_by_id.items()],
            targets=targets, source_m4b_groups=groups,
            book_name="Книга", m4b_template="",
        )

    def accept_two_targets(self, *, targets=None):
        targets = targets or [self.target_a, self.target_b]
        self.app.synthesis_targets = copy.deepcopy(targets)
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = copy.deepcopy(targets)
        self.app._source_runtime_m4b_records = (
            *self.plan([self.target_a], self.groups_a),
            *self.plan([self.target_b], self.groups_b),
        )

    def records(self, snapshot, template="{part}-{parts}-{range}"):
        return studio._template_helper_plan_records(snapshot, template)

    def assert_preview_matches_all_visible_records(self, snapshot, template="{part}-{parts}-{range}"):
        records = studio._template_helper_visible_records(self.records(snapshot, template), snapshot)
        expected = tuple(Path(record["path"]).name for record in records)
        quick = studio.render_template_helper_preview("m4b_part", template, preview_data=snapshot)
        rows = studio.template_helper_full_preview_rows("m4b_part", template, snapshot)
        self.assertEqual(quick, expected[:3])
        self.assertEqual(tuple(row["result"] for row in rows), expected)
        return records, rows

    def test_global_helper_uses_actual_audio_profile_and_accepted_parts(self):
        target = dict(self.target_b, output_dir=self.app.config["output_dir"])
        self.app.synthesis_targets = [copy.deepcopy(target)]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(target)]
        self.app._source_runtime_m4b_records = self.plan([target], self.groups_a)
        snapshot = self.app._template_helper_source_preview_data()
        records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_a])
        self.assertEqual([row["group"] for row in rows], ["Часть А1 · том 1/2", "Часть А2 · том 2/2"])
        self.assertTrue(all(record["target"]["bitrate"] == "96k" for record in records))
        self.assertTrue(all(record["target"]["sample_rate"] == "48000" for record in records))

    def test_global_helper_uses_legacy_m4b_audio_parameters(self):
        self.app.config.update(output_format="m4b", output_bitrate="96k",
                               output_sample_rate="48000", output_channels="mono")
        targets = studio.source_synthesis_targets_from_config(self.app.config)
        self.app._source_runtime_m4b_records = self.plan(targets, self.groups_a)
        snapshot = self.app._template_helper_source_preview_data()
        records, _rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_a])
        self.assertTrue(all(record["target"]["bitrate"] == "96k" for record in records))
        self.assertTrue(all(record["target"]["sample_rate"] == "48000" for record in records))
        self.assertTrue(all(record["target"]["channels"] == "mono" for record in records))

    def test_orphaned_virtual_groups_block_preview_but_physical_folders_do_not(self):
        self.app.tree = type("Tree", (), {"exists": lambda _self, _item: True})()
        self.app._source_group_ids = {"m4b_plan:missing"}
        snapshot = self.app._template_helper_source_preview_data()
        for renderer in (
            lambda: studio.render_template_helper_preview("m4b_part", "{part}-{parts}", preview_data=snapshot),
            lambda: studio.template_helper_full_preview_rows("m4b_part", "{part}-{parts}", snapshot),
        ):
            with self.assertRaisesRegex(studio.TemplateError, "Обновить папку"):
                renderer()
        self.app._source_group_ids = {"physdir:folder"}
        snapshot = self.app._template_helper_source_preview_data()
        _records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(rows), 1)

    def test_each_m4b_target_keeps_its_own_accepted_boundaries(self):
        self.accept_two_targets()
        before = copy.deepcopy((self.app.config, self.app._source_plan_groups, self.app._source_runtime_m4b_records))
        for edited_index, target in enumerate((self.target_a, self.target_b)):
            with self.subTest(edited_index=edited_index):
                snapshot = self.app._template_helper_source_preview_data(
                    target=target, targets=[self.target_a, self.target_b], edited_target_index=edited_index,
                )
                self.assert_preview_matches_all_visible_records(snapshot)
                records = self.records(snapshot)
                for index, groups in enumerate((self.groups_a, self.groups_b)):
                    self.assertEqual([record["file_ids"] for record in records if record["target_index"] == index],
                                     [group["file_ids"] for group in groups])
        self.assertEqual((self.app.config, self.app._source_plan_groups, self.app._source_runtime_m4b_records), before)

    def test_reordered_targets_reuse_by_profile_and_directory(self):
        self.accept_two_targets(targets=[self.target_b, self.target_a])
        snapshot = self.app._template_helper_source_preview_data(
            target=self.target_b, targets=[self.target_b, self.target_a], edited_target_index=0,
        )
        self.assert_preview_matches_all_visible_records(snapshot)
        records = self.records(snapshot)
        self.assertEqual([record["file_ids"] for record in records if record["target_index"] == 0],
                         [group["file_ids"] for group in self.groups_b])
        self.assertEqual([record["file_ids"] for record in records if record["target_index"] == 1],
                         [group["file_ids"] for group in self.groups_a])

    def test_global_helper_ignores_disabled_targets_and_regular_file_outputs(self):
        disabled = dict(self.target_a, enabled=False)
        regular = {"format": "mp3", "assembly_mode": "files", "output_dir": str(self.base / "audio" / "mp3")}
        target = dict(self.target_b, output_dir=self.app.config["output_dir"])
        self.app.synthesis_targets = [regular, disabled, target]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = copy.deepcopy(self.app.synthesis_targets)
        self.app._source_runtime_m4b_records = self.plan([target], self.groups_b)
        snapshot = self.app._template_helper_source_preview_data()
        records, _rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["target"]["format"] == "m4b" for record in records))
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_b])

    def test_global_helper_keeps_implicit_directory_of_actual_multiple_target_set(self):
        regular = {"format": "mp3", "assembly_mode": "files", "output_dir": ""}
        target = dict(self.target_b, output_dir="")
        targets = [regular, target]
        self.app.synthesis_targets = copy.deepcopy(targets)
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = copy.deepcopy(targets)
        accepted = self.plan(targets, self.groups_b)
        self.app._source_runtime_m4b_records = tuple(record for record in accepted if record["kind"] == "m4b")
        snapshot = self.app._template_helper_source_preview_data()
        records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(rows), 3)
        expected_parent = Path(self.app._source_runtime_m4b_records[0]["path"]).parent
        self.assertTrue(all(Path(record["path"]).parent == expected_parent for record in records))
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_b])

    def test_accepted_m4b_boundaries_do_not_change_regular_merge_groups(self):
        regular = {"format": "mp3", "assembly_mode": "merge", "output_dir": str(self.base / "audio" / "mp3")}
        self.app._source_plan_groups = {group["id"]: copy.deepcopy(group) for group in self.groups_a}
        self.app._source_runtime_m4b_records = self.plan([self.target_b], self.groups_b)
        targets = [regular, self.target_b]
        snapshot = self.app._template_helper_source_preview_data(
            target=self.target_b, targets=targets, edited_target_index=1,
        )
        records = self.records(snapshot)
        self.assertEqual([record["file_ids"] for record in records if record["target_index"] == 0],
                         [group["file_ids"] for group in self.groups_a])
        self.assertEqual([record["file_ids"] for record in records if record["target_index"] == 1],
                         [group["file_ids"] for group in self.groups_b])

    def test_target_name_draft_changes_names_without_losing_runtime_boundaries(self):
        original = dict(self.target_b, filename_template="Старое {part}")
        accepted = self.plan([original], self.groups_b)
        self.app.synthesis_targets = [copy.deepcopy(original)]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(original)]
        self.app._source_runtime_m4b_records = accepted
        draft = dict(original, filename_template="")
        snapshot = self.app._template_helper_source_preview_data(
            target=draft, targets=[draft], edited_target_index=0,
        )
        records, rows = self.assert_preview_matches_all_visible_records(snapshot, "Новое {part}/{parts}")
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_b])
        self.assertEqual([row["result"] for row in rows], ["Новое 1_3.m4b", "Новое 2_3.m4b", "Новое 3_3.m4b"])
        self.assertEqual(self.app._source_runtime_m4b_records, accepted)

    def test_unfinished_target_name_can_be_corrected_against_real_runtime_groups(self):
        self.accept_two_targets()
        draft = dict(self.target_b, filename_template="{par")
        snapshot = self.app._template_helper_source_preview_data(
            target=draft, targets=[self.target_a, draft], edited_target_index=1,
        )
        with self.assertRaises((studio.TemplateError, ValueError)):
            studio.template_helper_full_preview_rows("m4b_part", "{par", snapshot)
        records, rows = self.assert_preview_matches_all_visible_records(snapshot, "{group}-{part}")
        self.assertEqual(len(rows), 3)
        self.assertEqual([record["file_ids"] for record in records], [group["file_ids"] for group in self.groups_b])

    def test_global_template_preserves_explicit_target_filename_priority(self):
        original = dict(self.target_b, output_dir=self.app.config["output_dir"],
                        filename_template="Явное имя {part}-{parts}")
        self.app.synthesis_targets = [copy.deepcopy(original)]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(original)]
        accepted = self.plan([original], self.groups_b)
        self.app._source_runtime_m4b_records = accepted
        snapshot = self.app._template_helper_source_preview_data()
        draft = "Новый общий шаблон {part}"
        rows = studio.template_helper_full_preview_rows("m4b_part", draft, snapshot)
        expected = self.plan([original], self.groups_b)
        self.assertEqual([row["result"] for row in rows], [Path(record["path"]).name for record in expected])
        self.assertEqual([row["result"] for row in rows],
                         ["Явное имя 1-3.m4b", "Явное имя 2-3.m4b", "Явное имя 3-3.m4b"])
        self.assertEqual(self.app._source_runtime_m4b_records, accepted)
        self.assertEqual(studio.render_template_helper_preview("m4b_part", draft, preview_data=snapshot),
                         tuple(row["result"] for row in rows))

    def test_changed_limits_do_not_restore_previous_target_boundaries(self):
        self.accept_two_targets()
        for change in ({"max_chapters": 4}, {"max_duration_seconds": 120}):
            with self.subTest(change=change):
                changed = dict(self.target_b, **change)
                snapshot = self.app._template_helper_source_preview_data(
                    target=changed, targets=[changed], edited_target_index=0,
                )
                records, _rows = self.assert_preview_matches_all_visible_records(snapshot)
                expected = self.plan([changed], {})
                self.assertEqual([record["file_ids"] for record in records],
                                 [record["file_ids"] for record in expected])

    def test_duplicate_audio_targets_keep_distinct_accepted_parts_after_reordering(self):
        self.target_a = dict(self.target_b, output_dir=self.app.config["output_dir"],
                             filename_template="Вариант А {part}")
        self.target_b = dict(self.target_a, filename_template="Вариант Б {part}")
        for targets in ([self.target_a, self.target_b], [self.target_b, self.target_a]):
            with self.subTest(targets=targets):
                self.accept_two_targets(targets=targets)
                snapshot = self.app._template_helper_source_preview_data()
                records, _rows = self.assert_preview_matches_all_visible_records(snapshot)
                expected = (self.groups_a, self.groups_b) if targets[0] == self.target_a else (self.groups_b, self.groups_a)
                for index, groups in enumerate(expected):
                    self.assertEqual([record["file_ids"] for record in records if record["target_index"] == index],
                                     [group["file_ids"] for group in groups])

    def test_duplicate_audio_targets_keep_parts_while_one_name_is_edited(self):
        self.target_a = dict(self.target_b, output_dir=self.app.config["output_dir"],
                             filename_template="Вариант А {part}")
        self.target_b = dict(self.target_a, filename_template="Вариант Б {part}")
        self.accept_two_targets(targets=[self.target_b, self.target_a])
        for index, target in enumerate((self.target_b, self.target_a)):
            with self.subTest(index=index):
                draft = dict(target, filename_template="{par")
                targets = [self.target_b, self.target_a]
                targets[index] = draft
                snapshot = self.app._template_helper_source_preview_data(
                    target=draft, targets=targets, edited_target_index=index,
                )
                _records, rows = self.assert_preview_matches_all_visible_records(snapshot, "Новое {part}-{parts}")
                count = 3 if index == 0 else 2
                self.assertEqual([row["result"] for row in rows],
                                 [f"Новое {part}-{count}.m4b" for part in range(1, count + 1)])

    def test_changed_aac_encoder_does_not_reuse_previous_runtime_parts(self):
        target = dict(self.target_b, output_dir=self.app.config["output_dir"])
        self.app.synthesis_targets = [copy.deepcopy(target)]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(target)]
        self.app._source_runtime_m4b_records = self.plan([target], self.groups_b)
        snapshot = self.app._template_helper_source_preview_data(config_overrides={"aac_encoder": "aac_at"})
        records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(records[0]["file_ids"], tuple(self.app._source_path_by_id))

    def test_reordered_source_ids_do_not_restore_stale_runtime_ranges(self):
        target = dict(self.target_b, output_dir=self.app.config["output_dir"])
        self.app.synthesis_targets = [copy.deepcopy(target)]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(target)]
        self.app._source_runtime_m4b_records = self.plan([target], self.groups_b)
        ordered = ("f3", "f4", "f5", "f6", "f1", "f2")
        self.app._source_tree_file_ids = lambda: ordered
        snapshot = self.app._template_helper_source_preview_data()
        records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(records[0]["file_ids"], ordered)

    def test_changed_audio_profile_cannot_reuse_incompatible_runtime_parts(self):
        self.accept_two_targets()
        changed = dict(self.target_b, bitrate="128k")
        snapshot = self.app._template_helper_source_preview_data(
            target=changed, targets=[changed], edited_target_index=0,
        )
        records, rows = self.assert_preview_matches_all_visible_records(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(records[0]["file_ids"], tuple(self.app._source_path_by_id))
        self.assertEqual(records[0]["template_context"]["parts"], 1)

    def test_changed_source_book_draft_does_not_restore_old_accepted_names(self):
        target = dict(self.target_b, output_dir=self.app.config["output_dir"])
        self.app.synthesis_targets = [target]
        self.app.config[studio.SYNTHESIS_TARGETS_CONFIG_KEY] = [copy.deepcopy(target)]
        self.app._source_runtime_m4b_records = self.plan([target], self.groups_b)
        snapshot = self.app._template_helper_source_preview_data(config_overrides={"source_book_name": "Другая книга"})
        records, rows = self.assert_preview_matches_all_visible_records(snapshot, "{book}-{part}")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "Другая книга-1.m4b")
        self.assertEqual(records[0]["file_ids"], tuple(self.app._source_path_by_id))


if __name__ == "__main__":
    unittest.main()
