"""Имена физических частей совпадают с фактическими примерами помощника."""

import copy
import unittest

from test_m4b_whole_file_export import studio  # Импорт из временной копии source.


class ExportM4BTreeTemplateTests(unittest.TestCase):
    def snapshot(self, counts=(10, 2), names=("Том 5", "Другая книга"), *, pause_end=100):
        groups, children, files = {}, {}, {}
        for group_index, (count, name) in enumerate(zip(counts, names), 1):
            item_id = f"group{group_index}"
            groups[item_id] = {"name": name, "pause_start": 50, "pause_end": pause_end,
                               "artist": "Автор группы" if group_index == 1 else ""}
            children[item_id] = tuple(f"{item_id}file{index}" for index in range(1, count + 1))
            for index, file_id in enumerate(children[item_id], 1):
                files[file_id] = {"path": f"/virtual/{item_id}/source{index}.wav",
                                  "title": f"Глава {index}", "artist": f"Автор файла {index}",
                                  "duration": 0.2}
        target = {"format": "m4b", "max_chapters": 1}
        plan = studio.plan_export_m4b_tree_split(tuple(groups), children, groups, files, target)
        return groups, files, plan

    def test_contexts_keep_original_names_local_numbers_ranges_and_metadata(self):
        groups, files, plan = self.snapshot()
        before = copy.deepcopy((groups, files, plan))
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        self.assertEqual([context["part"] for context in contexts], list(range(1, 11)) + [1, 2])
        self.assertEqual([context["parts"] for context in contexts], [10] * 10 + [2, 2])
        self.assertEqual(contexts[0]["book"], "Том 5")
        self.assertEqual(contexts[0]["name"], contexts[0]["group"])
        self.assertEqual(contexts[9]["range"], "10-10")
        self.assertEqual(contexts[0]["range"], "01-01")
        self.assertEqual(contexts[9]["first_index"], 10)
        self.assertEqual(contexts[9]["last_name"], "Глава 10")
        self.assertEqual(contexts[0]["author"], "Автор группы")
        self.assertEqual(contexts[10]["author"], "Автор файла 1")
        self.assertEqual(contexts[10]["_file_ids"], ("group2file1",))
        self.assertEqual(contexts[10]["_source_paths"], ("/virtual/group2/source1.wav",))
        self.assertAlmostEqual(contexts[0]["_duration_seconds"], 0.35)
        self.assertEqual((groups, files, plan), before)

    def test_adaptive_numbers_match_runtime_quick_and_full_preview_for_all_parts(self):
        groups, files, plan = self.snapshot()
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        template = "{group}-{part:00d}"
        actual = studio.render_export_m4b_tree_group_names(template, contexts)
        self.assertEqual(actual[:10], tuple(f"Том 5-{index:02d}" for index in range(1, 11)))
        self.assertEqual(actual[10:], ("Другая книга-1", "Другая книга-2"))
        data = {"contexts": contexts}
        self.assertEqual(studio.render_template_helper_preview("m4b_tree_split", template,
                                                               preview_data=data), actual[:3])
        rows = studio.template_helper_full_preview_rows("m4b_tree_split", template, data)
        self.assertEqual(tuple(row["result"] for row in rows), actual)
        self.assertIn("/virtual/group1/source1.wav", rows[0]["detail"])
        self.assertIn("0.350 с", rows[0]["detail"])
        self.assertTrue(all(row["path"] == "" for row in rows))
        self.assertFalse(any(row["result"].endswith(".m4b") for row in rows))

    def test_custom_start_and_aliases_share_adaptive_total(self):
        groups, files, plan = self.snapshot()
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        for counter in ("part", "num", "index"):
            with self.subTest(counter=counter):
                actual = studio.render_export_m4b_tree_group_names(
                    "{group}-{" + counter + ":5:00d}", contexts)
                self.assertEqual(actual[:10], tuple(f"Том 5-{index:02d}" for index in range(5, 15)))
                self.assertEqual(actual[10:], ("Другая книга-5", "Другая книга-6"))

    def test_group_names_with_dots_are_not_stripped_as_file_extensions(self):
        groups, files, plan = self.snapshot((2,), ("Книга.m4b.mp3",))
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        self.assertEqual(studio.render_export_m4b_tree_group_names("{group}.{part}", contexts),
                         ("Книга.m4b.mp3.1", "Книга.m4b.mp3.2"))

    def test_collisions_are_rejected_after_sanitization_casefold_and_length(self):
        for names, template in ((("Книга",), "{group}"),
                                (("A:B", "A?B"), "{group}-{part}"),
                                (("Book", "book"), "{group}-{part}"),
                                (("К" * 250,), "{group}-{part}")):
            with self.subTest(names=names):
                groups, files, plan = self.snapshot((2,) * len(names), names)
                contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
                with self.assertRaisesRegex(studio.TemplateError, "совпадающие имена"):
                    studio.render_export_m4b_tree_group_names(template, contexts)
                with self.assertRaises(studio.TemplateError):
                    studio.render_template_helper_preview("m4b_tree_split", template,
                                                          preview_data={"contexts": contexts})

    def test_registry_presets_and_tokens_support_new_context(self):
        spec = studio.template_helper_context("m4b_tree_split")
        self.assertEqual(frozenset(field for field, _label, _description in spec["fields"]),
                         studio.TEMPLATE_FIELDS_M4B_TREE_SPLIT)
        self.assertFalse(spec["empty_allowed"])
        self.assertEqual(studio.build_template_helper_token(
            "m4b_tree_split", "part", start=5, adaptive=True), "{part:5:00d}")
        for _name, template in spec["presets"]:
            self.assertEqual(len(studio.render_template_helper_preview("m4b_tree_split", template)), 3)
        with self.assertRaises(studio.TemplateError):
            studio.render_export_m4b_tree_group_names("{ext}", ())
        with self.assertRaises(studio.TemplateError):
            studio.render_export_m4b_tree_group_names(" ", ())
        with self.assertRaises(studio.TemplateError):
            studio.template_helper_full_preview_rows("m4b_tree_split", "{group}-{part}", {})

    def test_unchanged_group_collision_has_same_runtime_quick_and_full_error(self):
        groups, files, plan = self.snapshot((2,), ("Книга",))
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        existing = ("Другая книга", "книга-1")
        data = {"contexts": contexts, "existing_names": existing}
        errors = []
        operations = (
            lambda: studio.render_export_m4b_tree_group_names(
                "{group}-{part}", contexts, existing_names=existing),
            lambda: studio.render_template_helper_preview(
                "m4b_tree_split", "{group}-{part}", preview_data=data),
            lambda: studio.template_helper_full_preview_rows(
                "m4b_tree_split", "{group}-{part}", data),
        )
        for operation in operations:
            with self.assertRaises(studio.TemplateError) as caught:
                operation()
            errors.append(str(caught.exception))
        self.assertEqual(errors, ["совпадающие имена новых групп: Книга-1"] * 3)
        self.assertEqual(data["existing_names"], existing)

    def test_unchanged_collision_after_quick_preview_limit_is_still_rejected(self):
        groups, files, plan = self.snapshot((10,), ("Книга",))
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        data = {"contexts": contexts, "existing_names": ("Книга-10",)}
        for render in (
            lambda: studio.render_template_helper_preview(
                "m4b_tree_split", "{group}-{part}", preview_data=data, limit=3),
            lambda: studio.template_helper_full_preview_rows(
                "m4b_tree_split", "{group}-{part}", data),
        ):
            with self.assertRaisesRegex(studio.TemplateError, "Книга-10"):
                render()

    def test_unrelated_existing_names_do_not_change_runtime_or_preview_results(self):
        groups, files, plan = self.snapshot((2,), ("Книга",))
        contexts = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        data = {"contexts": contexts, "existing_names": ("Книга", "Книга-3")}
        expected = ("Книга-1", "Книга-2")
        self.assertEqual(studio.render_export_m4b_tree_group_names(
            "{group}-{part}", contexts, existing_names=data["existing_names"]), expected)
        self.assertEqual(studio.render_template_helper_preview(
            "m4b_tree_split", "{group}-{part}", preview_data=data), expected)
        self.assertEqual(tuple(row["result"] for row in studio.template_helper_full_preview_rows(
            "m4b_tree_split", "{group}-{part}", data)), expected)

    def test_new_snapshot_uses_current_names_pauses_and_part_plan(self):
        groups, files, plan = self.snapshot((2,), ("Старое имя",), pause_end=100)
        old = studio.build_export_m4b_tree_split_contexts(plan, groups, files)
        groups["group1"]["name"] = "Новое имя"
        groups["group1"]["pause_end"] = 300
        children = {"group1": ("group1file1", "group1file2")}
        updated_plan = studio.plan_export_m4b_tree_split(
            ("group1",), children, groups, files, {"format": "m4b", "max_chapters": 1})
        updated = studio.build_export_m4b_tree_split_contexts(updated_plan, groups, files)
        self.assertEqual(studio.render_export_m4b_tree_group_names("{group}-{part}", updated),
                         ("Новое имя-1", "Новое имя-2"))
        self.assertAlmostEqual(updated[0]["_duration_seconds"], 0.55)
        self.assertEqual(old[0]["group"], "Старое имя")
        self.assertAlmostEqual(old[0]["_duration_seconds"], 0.35)


if __name__ == "__main__":
    unittest.main()
