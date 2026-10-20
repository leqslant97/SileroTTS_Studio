"""Повторная запись точных таймкодов сохраняет аудио и не обращается к синтезу."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_regressions import studio
import test_source_group_runtime as runtime


class TimecodeRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "app-data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.audio = self.root / "Книга.opus"
        self.audio.write_bytes(b"published audio")
        self.options = studio.normalize_timecode_options({"enabled": True})
        self.timeline = studio.build_timecode_timeline([
            {"title": "Пролог", "duration": 22.125},
            {"title": "Первая глава", "duration": 33.25},
            {"title": "Финал", "duration": 44.5},
        ])

    def publish(self, audio=None):
        studio._publish_timecodes(audio or self.audio, self.timeline, self.options)

    def test_deleted_txt_and_new_templates_reuse_exact_boundaries_and_preserve_audio(self):
        self.publish()
        before = self.audio.stat(), hashlib.sha256(self.audio.read_bytes()).hexdigest()
        studio.timecode_output_path(self.audio, self.options).unlink()
        changed = dict(self.options, line_template="{index:03d} {start:seconds} {end:seconds} {title}",
                       filename_template="{audio_stem}-оглавление", output_dir=str(self.root / "txt"))
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=AssertionError("encoding forbidden")):
            result = studio.refresh_timecodes_file(self.audio, changed)
        self.assertNotIn("error", result)
        self.assertEqual(result["path"].read_text().splitlines()[1], "002 22.125 55.375 Первая глава")
        self.assertEqual(before, (self.audio.stat(), hashlib.sha256(self.audio.read_bytes()).hexdigest()))

    def test_identical_txt_is_not_replaced(self):
        self.publish()
        txt = studio.timecode_output_path(self.audio, self.options)
        before = txt.stat().st_mtime_ns, txt.stat().st_ino
        studio.refresh_timecodes_file(self.audio, self.options)
        self.assertEqual(before, (txt.stat().st_mtime_ns, txt.stat().st_ino))

    def test_timeline_survives_txt_failure_and_can_be_retried(self):
        with mock.patch.object(studio, "write_timecodes_file", side_effect=OSError("TXT unavailable")):
            result = {}
            studio._publish_timecodes(self.audio, self.timeline, self.options, result=result)
        self.assertIn("error", result)
        self.assertIsNotNone(studio.load_timecode_timeline(self.audio))
        self.assertNotIn("error", studio.refresh_timecodes_file(self.audio, self.options))

    def test_replaced_audio_and_corrupt_or_invalid_timeline_are_not_guessed(self):
        self.publish()
        cache = studio._timecode_timeline_path(self.audio)
        original = cache.read_bytes()
        for data in (b"not JSON", b"[]", b'{"version":99}'):
            with self.subTest(data=data):
                cache.write_bytes(data)
                self.assertIsNone(studio.refresh_timecodes_file(self.audio, self.options))
        cache.write_bytes(original)
        value = json.loads(original)
        value["timeline"][1]["start"] = 10
        cache.write_text(json.dumps(value))
        self.assertIsNone(studio.load_timecode_timeline(self.audio))
        cache.write_bytes(original)
        replacement = self.root / "replacement"
        replacement.write_bytes(self.audio.read_bytes())
        os.replace(replacement, self.audio)
        self.assertIsNone(studio.refresh_timecodes_file(self.audio, self.options))

    def test_cancel_and_failed_replace_preserve_existing_txt(self):
        self.publish()
        txt = studio.timecode_output_path(self.audio, self.options)
        previous = txt.read_bytes()
        with self.assertRaises(InterruptedError):
            studio.refresh_timecodes_file(self.audio, self.options, cancelled=lambda: True)
        with mock.patch.object(studio.os, "replace", side_effect=OSError("readonly")):
            result = studio.refresh_timecodes_file(self.audio, dict(self.options, line_template="{title}"))
        self.assertIn("error", result)
        self.assertEqual(previous, txt.read_bytes())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_partial_speech_is_not_promoted_to_ready_by_saved_timeline(self):
        self.publish()
        record = {"path": self.audio, "target": {"format": "opus", "timecodes": self.options}}
        processor = SimpleNamespace(processing_statuses_ram={str(self.audio.resolve()): "warning"})
        self.assertIsNone(studio.refresh_source_output_timecodes(processor, record))
        self.assertFalse(studio.source_output_file_is_ready(record, processor.processing_statuses_ram))

    def test_retryable_txt_failure_is_distinct_from_incomplete_speech(self):
        self.publish()
        record = {"path": self.audio, "target": {"format": "opus", "timecodes": self.options}}
        processor = SimpleNamespace(processing_statuses_ram={str(self.audio.resolve()): "timecodes_error"})
        processor._mark_output_status = lambda path, status: processor.processing_statuses_ram.update({str(Path(path).resolve()): status})
        self.assertFalse(studio.source_output_file_is_ready(record, processor.processing_statuses_ram))
        disabled = copy.deepcopy(record)
        disabled["target"]["timecodes"]["enabled"] = False
        self.assertTrue(studio.source_output_file_is_ready(disabled, processor.processing_statuses_ram))
        result = studio.refresh_source_output_timecodes(processor, record)
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(studio.source_output_file_is_ready(record, processor.processing_statuses_ram))

    def test_timecode_only_changes_do_not_invalidate_measured_audio(self):
        self.publish()
        record = {"kind": "group", "path": self.audio, "target": {"format": "opus", "timecodes": self.options}}
        app = object.__new__(studio.TTSApp)
        app._source_plan_dirty = False
        app._source_completed_regular_targets = {str(self.audio.resolve()): copy.deepcopy(record)}
        record["target"]["timecodes"] = dict(self.options, line_template="{title}")
        self.assertEqual(app._source_regular_rebuild_paths([record]), ())
        record["metadata_overrides"] = {"album": "Другой альбом"}
        self.assertEqual(app._source_regular_rebuild_paths([record]), (str(self.audio.resolve()),))
        record.pop("metadata_overrides")
        record["target"]["bitrate"] = "64k"
        self.assertEqual(app._source_regular_rebuild_paths([record]), (str(self.audio.resolve()),))

    def test_ready_queue_recreates_deleted_txt_without_collecting_or_encoding(self):
        case = runtime.SourceGroupOutputRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.configure([{"format": "opus", "assembly_mode": "merge", "timecodes": self.options}])
        case.app._source_plan_dirty = False
        for record in case.records:
            record["path"].parent.mkdir(parents=True, exist_ok=True)
            record["path"].write_bytes(b"completed group")
            studio._publish_timecodes(record["path"], self.timeline, self.options)
            record["timecode_path"].unlink()
        case.processor.process_text_file.side_effect = AssertionError("collecting speech forbidden")
        before = {r["path"]: r["path"].stat() for r in case.records}
        case.run_queue()
        case.processor.process_text_file.assert_not_called()
        self.assertEqual(case.encoded, [])
        self.assertTrue(all(r["timecode_path"].is_file() for r in case.records))
        self.assertEqual(before, {r["path"]: r["path"].stat() for r in case.records})

    def test_txt_error_during_skip_keeps_audio_and_does_not_collect_speech(self):
        case = runtime.SourceGroupOutputRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.configure([{"format": "opus", "assembly_mode": "merge", "timecodes": self.options}])
        case.app._source_plan_dirty = False
        for record in case.records:
            record["path"].parent.mkdir(parents=True, exist_ok=True)
            record["path"].write_bytes(b"complete audio")
            studio._publish_timecodes(record["path"], self.timeline, self.options)
        case.processor.process_text_file.side_effect = AssertionError("speech forbidden")
        with mock.patch.object(studio, "write_timecodes_file", side_effect=OSError("readonly TXT")):
            case.run_queue()
        case.processor.process_text_file.assert_not_called()
        self.assertEqual(case.encoded, [])
        self.assertTrue(all(value == "timecodes_error" for value in case.processor.processing_statuses_ram.values()))
        case.run_queue()
        self.assertEqual(case.processor.processing_statuses_ram, {})


class TimecodeRealPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = []
        for name, count in (("Пролог.txt", 900), ("Знакомство.txt", 450), ("Развязка.txt", 1350)):
            path = self.root / name
            path.write_text("а" * count, encoding="utf-8")
            self.paths.append(path)

    def source_data(self, targets=None):
        return {"edited_target_index": 0, "source_plan": {
            "source_root": self.root, "output_root": self.root / "out",
            "files": self.paths, "book_name": "Моя книга",
            "targets": targets or [{"format": "opus", "assembly_mode": "merge"}],
        }}

    def test_source_preview_uses_real_names_and_estimates_without_creating_output(self):
        data = self.source_data()
        before = copy.deepcopy(data)
        rows = studio.template_helper_full_preview_rows("timecode_line", "{start:seconds} {title}", data)
        self.assertEqual([row["result"] for row in rows], ["0 Пролог", "60 Знакомство", "90 Развязка"])
        self.assertTrue(all("оценка" in row["detail"] for row in rows))
        self.assertEqual(data, before)
        self.assertFalse((self.root / "out").exists())
        self.assertEqual(studio.render_template_helper_preview("timecode_line", "{start:seconds} {title}", preview_data=data),
                         tuple(row["result"] for row in rows))

    def test_m4b_title_template_and_per_part_zero_are_used_in_preview(self):
        targets = [{"format": "m4b", "chapter_title_template": "Глава {volume_index:02d}: {name}"}]
        data = self.source_data(targets)
        data["source_plan"]["source_m4b_groups"] = [
            {"name": "Первая часть", "file_ids": self.paths[:2]},
            {"name": "Вторая часть", "file_ids": self.paths[2:]},
        ]
        rows = studio.template_helper_full_preview_rows("timecode_line", "{start:seconds} {title}", data)
        self.assertEqual([row["result"] for row in rows],
                         ["0 Глава 01: Пролог", "60 Глава 02: Знакомство", "0 Глава 01: Развязка"])

    def test_export_preview_uses_edited_titles_clips_and_pauses(self):
        data = {"edited_target_index": 0, "export_plan": {
            "items": ("g",), "group_children": {"g": ("a", "b")},
            "export_groups": {"g": {"name": "Том", "merge": True, "pause": 500}},
            "export_files": {
                "a": {"path": str(self.root / "original.opus"), "title": "Реальный заголовок", "duration": 20, "clip_start": 2, "clip_end": 10},
                "b": {"path": str(self.root / "second.opus"), "title": "Встреча", "duration": 30},
            },
            "targets": [{"format": "mp3", "assembly_mode": "merge"}], "base_dir": self.root / "export",
        }}
        for fmt in ("mp3", "m4b"):
            with self.subTest(format=fmt):
                data["export_plan"]["targets"][0]["format"] = fmt
                rows = studio.template_helper_full_preview_rows("timecode_line", "{start:seconds} {title}", data)
                self.assertEqual([row["result"] for row in rows], ["0 Реальный заголовок", "8.5 Встреча"])
        self.assertFalse((self.root / "export").exists())

    def test_m4b_preview_keeps_first_matching_context_for_target_and_part(self):
        data = self.source_data([
            {"format": "m4b", "bitrate": bitrate, "chapter_title_template": "{book}: {name}"}
            for bitrate in ("64k", "96k")
        ])
        data.pop("edited_target_index")
        records = studio.plan_source_synthesis_target_paths(**data["source_plan"])
        self.assertEqual(len(records), 2)
        item_id = records[0]["item_id"]
        self.assertEqual(records[1]["item_id"], item_id)
        common = {"item_id": item_id, "book": "Общий контекст"}
        data["chapter_groups"] = [
            {"item_id": item_id, "part": 2, "book": "Другой том"},
            {"item_id": item_id, "target_index": 7, "book": "Другая цель"},
            {"item_id": item_id, "target_index": 1, "book": "Вторая цель"},
            common,
            {"item_id": item_id, "target_index": 0, "book": "Поздний дубль"},
        ]
        before = copy.deepcopy(data)
        rows = studio.template_helper_full_preview_rows("timecode_line", "{title}", data)
        names = [path.stem for path in self.paths]
        self.assertEqual([row["result"] for row in rows],
                         [f"Общий контекст: {name}" for name in names]
                         + [f"Вторая цель: {name}" for name in names])
        self.assertEqual(data, before)
        data["chapter_groups"].insert(0, common)
        rows = studio.template_helper_full_preview_rows("timecode_line", "{title}", data)
        self.assertEqual([row["result"] for row in rows],
                         [f"Общий контекст: {name}" for name in names] * 2)

    def test_large_m4b_preview_does_not_rescan_unrelated_contexts(self):
        accesses = []

        class ChapterGroup(dict):
            def get(self, key, default=None):
                if key == "item_id":
                    accesses.append(key)
                return super().get(key, default)

        files = {f"f{i}": {"path": str(self.root / f"chapter-{i}.opus"),
                            "title": f"Глава {i}", "duration": 10} for i in range(200)}
        data = {"export_plan": {
            "items": tuple(files), "export_files": files, "export_groups": {},
            "group_children": {}, "targets": [{"format": "m4b", "chapter_title_template": "{book}: {name}"}],
            "base_dir": self.root / "export",
        }, "chapter_groups": [ChapterGroup(item_id=key, book=f"Книга {i}")
                               for i, key in enumerate(files)]}
        rows = studio.template_helper_full_preview_rows("timecode_line", "{title}", data)
        self.assertEqual([row["result"] for row in rows],
                         [f"Книга {i}: Глава {i}" for i in range(len(files))])
        self.assertLessEqual(len(accesses), len(files) * 2)
        self.assertFalse((self.root / "export").exists())

    def test_empty_real_sources_are_not_replaced_with_demo_chapters(self):
        self.assertEqual(studio.render_template_helper_preview("timecode_line", "{title}",
                         preview_data={"empty_real_sources": True}), ())

    def test_manual_template_determines_mode_without_a_stale_preset(self):
        self.assertEqual(studio.timecode_preset_for_template("{start:youtube} {index:03d}. {title}"), "youtube")
        self.assertEqual(studio.timecode_preset_for_template("{index} {title}"), "custom")
        self.assertEqual(studio.timecode_preset_for_template("{start:hms} {title}"), "contents")
