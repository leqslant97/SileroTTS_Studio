"""Явный шаблон цели M4B переживает нормализацию и сохраняется при сборке."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class M4BTargetTemplatePriorityTests(unittest.TestCase):
    def test_explicit_default_template_survives_normalization_planning_and_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups = {"group": {"name": "Книга", "merge": True}}
            children = {"group": ("first", "last")}
            files = {}
            for file_id in children["group"]:
                path = root / f"{file_id}.mp3"
                path.write_bytes(b"audio")
                files[file_id] = {"path": str(path), "title": file_id, "duration": 10}
            for global_template in ("", "{book}", "Глобальный {book}"):
                for workers in (1, 2):
                    with self.subTest(global_template=global_template, workers=workers):
                        config = studio.normalize_config({
                            "export_m4b_template": global_template,
                            "export_targets": [{
                                "format": "m4b", "output_dir": str(root / "output"),
                                "filename_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE,
                                "max_chapters": 1,
                            }],
                        })
                        target = config["export_targets"][0]
                        self.assertEqual(target["filename_template"], studio.DEFAULT_SOURCE_M4B_TEMPLATE)
                        restored = studio.normalize_config(copy.deepcopy(config))
                        self.assertEqual(restored["export_targets"][0]["filename_template"],
                                         studio.DEFAULT_SOURCE_M4B_TEMPLATE)
                        planned = studio.plan_export_target_paths(
                            ("group",), children, groups, files, (target,), root,
                            m4b_template=config["export_m4b_template"],
                        )
                        expected = ["Книга Главы 1-1.m4b", "Книга Главы 2-2.m4b"]
                        self.assertEqual([record["path"].name for record in planned], expected)
                        app = object.__new__(studio.TTSApp)
                        app.config = config
                        with mock.patch.object(studio, "_export_m4b_ffmpeg") as encode:
                            app._run_output_target_set(
                                ("group",), children, groups, files, (target,), root,
                                max_workers=workers,
                            )
                        self.assertCountEqual([Path(call.args[1]).name for call in encode.call_args_list],
                                              expected)


if __name__ == "__main__":
    unittest.main()
