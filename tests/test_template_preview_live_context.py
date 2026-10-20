"""Быстрые примеры альбомов показывают фактические теги текущего плана."""

import copy
import unittest

from test_regressions import studio


class TemplateAlbumLiveContextTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "album_contexts": [{
                "book": "Книга", "group": f"Часть {part}",
                "part": part, "parts": 5, "metadata_overrides": {},
            } for part in range(1, 6)],
            "metadata_config": {
                "apply_output_tags": True,
                "tag_album": "Общий альбом",
                "synthesis_m4b_album_template": "{book}",
            },
        }
        self.snapshot["album_contexts"][1]["metadata_overrides"] = {
            "album": "Локальный альбом",
        }
        self.snapshot["album_contexts"][2]["metadata_overrides"] = {"album": ""}

    def quick(self, template, limit=3):
        return studio.render_template_helper_preview(
            "m4b_album", template, preview_data=self.snapshot, limit=limit
        )

    def assert_full_agrees(self, template):
        full = studio.template_helper_full_preview_rows(
            "m4b_album", template, self.snapshot
        )
        self.assertEqual(self.quick(template), tuple(row["result"] for row in full[:3]))
        self.assertEqual(self.quick(template, limit=None), tuple(row["result"] for row in full))

    def test_standard_template_preserves_common_album_and_local_overrides(self):
        self.assertEqual(self.quick("{book}"), (
            "Общий альбом", "Локальный альбом", "(тег не записывается)",
        ))
        self.assert_full_agrees("{book}")

    def test_custom_template_keeps_local_album_and_explicit_clear(self):
        self.assertEqual(self.quick("{book}-{part}"), (
            "Книга-1", "Локальный альбом", "(тег не записывается)",
        ))
        self.assert_full_agrees("{book}-{part}")

    def test_empty_template_shows_inherited_effective_album(self):
        self.assertEqual(self.quick(""), (
            "Общий альбом", "Локальный альбом", "(тег не записывается)",
        ))
        self.assert_full_agrees("")

    def test_disabled_tags_do_not_promise_album_metadata(self):
        self.snapshot["metadata_config"]["apply_output_tags"] = False
        for template in ("{book}", "{book}-{part}", ""):
            with self.subTest(template=template):
                self.assertEqual(self.quick(template), ("(тег не записывается)",) * 3)
                self.assert_full_agrees(template)

    def test_repeated_preview_uses_current_snapshot_values_without_mutation(self):
        self.assertEqual(self.quick("{book}")[0], "Общий альбом")
        self.snapshot["metadata_config"]["tag_album"] = "Новый общий альбом"
        self.snapshot["album_contexts"][1]["metadata_overrides"]["album"] = "Новый локальный альбом"
        before = copy.deepcopy(self.snapshot)
        self.assertEqual(self.quick("{book}")[:2], (
            "Новый общий альбом", "Новый локальный альбом",
        ))
        self.assert_full_agrees("{book}")
        self.assertEqual(self.snapshot, before)

    def test_examples_without_metadata_snapshot_keep_compatibility(self):
        self.assertEqual(studio.render_template_helper_preview("m4b_album", "{book}"), (
            "Моя книга", "Моя книга", "Моя книга",
        ))
        self.assertEqual(studio.render_template_helper_preview("m4b_album", ""), (
            "(тег альбома из настроек)",
        ) * 3)


if __name__ == "__main__":
    unittest.main()
