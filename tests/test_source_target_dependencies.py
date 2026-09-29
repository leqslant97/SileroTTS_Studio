"""Независимые варианты кодирования не пересобирают готовые группы."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class SourceTargetDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        source = self.root / "chapter.txt"
        source.write_text("Одна глава.", encoding="utf-8")
        fragment = self.root / "fragment.ogg"
        fragment.write_bytes(b"audio")
        self.app = object.__new__(studio.TTSApp)
        self.app._source_plan_dirty = False
        self.app._invalidate_source_runtime_m4b_plan()
        self.app._source_path_by_id = {"chapter": source}
        for name in ("finish_processing", "update_total_ui", "update_progress_ui", "update_file_status"):
            setattr(self.app, name, mock.Mock())
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        self.processor = mock.Mock()
        self.processor.cfg = {"use_cache": True}
        self.processor.is_stopped = False
        self.processor.active_threads = []
        self.processor.encode_semaphore = None
        self.processor.processing_statuses_ram = {}
        self.processor.process_text_file.return_value = {"status": "success", "audio_files": (fragment,)}

        def mark(path, status):
            key = str(Path(path).resolve())
            if status in {"error", "warning"}:
                self.processor.processing_statuses_ram[key] = status
            else:
                self.processor.processing_statuses_ram.pop(key, None)

        self.processor._mark_output_status.side_effect = mark
        self.single = {
            "kind": "file", "target_index": 0, "item_id": "chapter",
            "target": studio.normalize_output_target({"format": "mp3", "bitrate": "128k"}),
            "path": self.root / "chapter.mp3", "source_path": source,
            "source_paths": (source,), "file_ids": ("chapter",),
        }
        self.group = dict(self.single, kind="group", target_index=1, item_id="group", path=self.root / "group.mp3")
        self.group["target"] = studio.normalize_output_target({"format": "mp3", "bitrate": "128k", "assembly_mode": "merge"})
        self.m4b = dict(self.single, kind="m4b", target_index=2, item_id="group", path=self.root / "group.m4b")
        self.m4b["target"] = studio.normalize_output_target({"format": "m4b", "bitrate": "96k"})
        self.records = [self.single, self.group, self.m4b]
        self.encoded = []

        def encode(_audio, output, **_kwargs):
            path = Path(output)
            self.encoded.append(path)
            path.write_bytes(f"encoded {len(self.encoded)}".encode())

        for name in ("_export_merged_audio_ffmpeg", "_export_m4b_ffmpeg"):
            patcher = mock.patch.object(studio, name, side_effect=encode)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_queue(self):
        config = {
            "input_dir": str(self.root), "output_dir": str(self.root),
            "output_format": "mp3", "source_multi_output": True,
            "source_path_by_id": self.app._source_path_by_id,
            "synthesis_targets": [record["target"] for record in self.records],
            "source_target_records": tuple(self.records), "source_m4b_auto_split_long": False,
            "source_plan_revision": self.app._source_plan_revision,
            "source_targets_rebuild_paths": self.app._source_regular_rebuild_paths(self.records),
            "source_unrelated_target_paths": self.app._source_unrelated_target_paths(self.records),
        }
        self.app.process_queue(self.processor, ("chapter",), config, True)
        self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_new_single_format_does_not_rebuild_existing_groups(self):
        self.run_queue()
        before = {record["path"]: record["path"].read_bytes() for record in self.records}
        opus = dict(self.single, target_index=3, path=self.root / "chapter.opus")
        opus["target"] = studio.normalize_output_target({"format": "opus", "bitrate": "48k"})
        self.records.append(opus)
        self.encoded.clear()
        self.run_queue()
        self.assertEqual(self.encoded, [opus["path"]])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_changed_single_bitrate_does_not_rebuild_existing_groups(self):
        self.run_queue()
        self.single["target"] = dict(self.single["target"], bitrate="192k")
        self.encoded.clear()
        self.run_queue()
        self.assertEqual(self.encoded, [self.single["path"]])

    def test_adding_merged_mp3_keeps_original_chapter_and_m4b(self):
        self.records = [self.single, self.m4b]
        self.run_queue()
        before = {record["path"]: record["path"].read_bytes() for record in self.records}
        self.records.append(self.group)
        self.encoded.clear()
        self.run_queue()
        self.assertEqual(self.encoded, [self.group["path"]])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_missing_unchanged_chapter_still_rebuilds_its_existing_groups(self):
        self.run_queue()
        self.single["path"].unlink()
        self.encoded.clear()
        self.run_queue()
        self.assertCountEqual(self.encoded, [record["path"] for record in self.records])

    def test_warning_chapter_is_not_hidden_by_independent_target_change(self):
        self.run_queue()
        self.single["target"] = dict(self.single["target"], bitrate="192k")
        self.processor.processing_statuses_ram[str(self.single["path"])] = "warning"
        self.encoded.clear()
        self.run_queue()
        self.assertCountEqual(self.encoded, [record["path"] for record in self.records])


if __name__ == "__main__":
    unittest.main()
