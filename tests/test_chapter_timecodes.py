"""Внешние таймкоды: переносимость, безопасная публикация и шкала FFmpeg."""
import copy
import json
import math
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from test_regressions import studio


class ChapterTimecodeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def options(self, **updates):
        return studio.normalize_timecode_options(dict(enabled=True, **updates))

    def timeline(self):
        return studio.build_timecode_timeline([
            {"title": "Первая\nглава", "duration": 222.125},
            {"title": "Встреча", "duration": 865.25},
            {"title": "Возвращение", "duration": 90000},
        ])

    def test_defaults_do_not_enable_new_outputs(self):
        target = studio.normalize_output_target({"format": "mp3"})
        self.assertFalse(target["timecodes"]["enabled"])
        self.assertEqual(target["timecodes"]["filename_template"], "{audio_filename}.chapters.txt")
        self.assertEqual(studio.write_timecodes_file(self.root / "a.mp3", self.timeline(), {}), (None, ()))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_presets_and_time_formats_support_hours_and_days(self):
        row = self.timeline()[1]
        self.assertEqual(studio.render_timecode_template("{start:hms} {title}", row), "00:03:42 Встреча")
        self.assertEqual(studio.render_timecode_template("{start:youtube} {title}", row), "03:42 Встреча")
        self.assertEqual(studio.render_timecode_template("{start:hms_ms} {title}", row), "00:03:42.125 Встреча")
        self.assertEqual(studio._format_timecode(90000.9999, "hms"), "25:00:00")
        self.assertEqual(studio._format_timecode(90000.9999, "hms_ms"), "25:00:01.000")
        self.assertEqual(studio._format_timecode(222.125, "seconds"), "222.125")
        self.assertEqual(studio.render_timecode_template("{{{index:03d}}} {title}", self.timeline()[0]), "{001} Первая глава")

    def test_unknown_fields_attributes_conversions_and_large_formats_are_rejected(self):
        for template in ("{unknown}", "{title.upper}", "{title[0]}", "{title!r}", "{start:bad}", "{index:100000000d}", "{title}\n{start}", "{index:{title}}"):
            with self.subTest(template=template), self.assertRaises(studio.TemplateError):
                self.options(line_template=template)

    def test_pause_timeline_has_no_gaps_and_keeps_source_order(self):
        rows = studio.build_timecode_timeline([{"title": "A", "duration": 1.25}, {"title": "B", "duration": 2.5}], pause_ms=350, pause_start_ms=200, pause_end_ms=400)
        self.assertAlmostEqual(rows[0]["start"], 0)
        self.assertAlmostEqual(rows[0]["end"], 1.8)
        self.assertAlmostEqual(rows[1]["start"], 1.8)
        self.assertAlmostEqual(rows[1]["end"], 4.7)

    def test_youtube_warnings_do_not_rewrite_actual_timeline(self):
        rows = ({"title": "A", "start": 2, "end": 4, "duration": 2},)
        snapshot = copy.deepcopy(rows)
        messages = studio.validate_timecode_timeline(rows, preset="youtube")
        self.assertEqual(len(messages), 3)
        self.assertEqual(rows, snapshot)
        self.assertEqual(studio.validate_timecode_timeline(rows, preset="contents"), ())
        self.assertEqual(studio.validate_timecode_timeline(self.timeline(), preset="youtube"), ())

    def test_youtube_checks_formatted_increasing_marks(self):
        rows = tuple({"start": value, "duration": 20} for value in (0, .3, .7))
        self.assertIn("метки глав должны возрастать", studio.validate_timecode_timeline(rows, preset="youtube"))

    def test_utf8_sidecar_is_created_next_to_audio_and_normalizes_title_newlines(self):
        audio = self.root / "Книга.opus"
        audio.touch()
        path, warnings = studio.write_timecodes_file(audio, self.timeline(), self.options())
        self.assertEqual(path, self.root / "Книга.opus.chapters.txt")
        self.assertEqual(path.read_text(), "00:00:00 Первая глава\n00:03:42 Встреча\n00:18:07 Возвращение\n")
        self.assertEqual(warnings, ())
        self.assertFalse(list(self.root.glob(".chapters_*")))

    def test_custom_directory_and_filename_keep_format(self):
        options = self.options(output_dir=str(self.root / "метки"), filename_template="{index:03d} {book}.{format}")
        path = studio.timecode_output_path(self.root / "book.mp3", options, context={"index": 7, "book": "Том"})
        self.assertEqual(path, self.root / "метки/007 Том.mp3.txt")

    def test_long_unicode_filename_and_short_temporary_name(self):
        options = self.options(filename_template="Я" * 300)
        path, _ = studio.write_timecodes_file(self.root / "audio.mp3", self.timeline(), options)
        self.assertLessEqual(len(path.name.encode()), 240)
        self.assertTrue(path.is_file())

    def test_failed_publish_preserves_old_txt_and_audio(self):
        audio = self.root / "audio.mp3"; audio.write_bytes(b"complete audio")
        path = studio.timecode_output_path(audio, self.options()); path.write_text("old")
        with mock.patch.object(studio.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(studio.TimecodeExportError):
                studio.write_timecodes_file(audio, self.timeline(), self.options())
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(audio.read_bytes(), b"complete audio")
        self.assertFalse(list(self.root.glob(".chapters_*")))

    def test_cancel_after_write_keeps_previous_txt_and_cleans_temporary(self):
        audio = self.root / "audio.mp3"; path = studio.timecode_output_path(audio, self.options()); path.write_text("old")
        with self.assertRaises(InterruptedError):
            studio.write_timecodes_file(audio, self.timeline(), self.options(), cancelled=mock.Mock(side_effect=[False, True]))
        self.assertEqual(path.read_text(), "old")
        self.assertFalse(list(self.root.glob(".chapters_*")))

    def test_profile_round_trip_preserves_per_target_options(self):
        raw = {"schema": studio.OUTPUT_TARGET_LIBRARY_SCHEMA, "version": studio.OUTPUT_TARGET_LIBRARY_VERSION, "targets": [
            {"format": "mp3", "timecodes": self.options(preset="youtube")},
            {"format": "m4b", "timecodes": self.options(preset="ranges", output_dir=str(self.root))},
        ]}
        result = studio.normalize_output_target_library(json.loads(json.dumps(raw)))
        self.assertEqual(result["targets"][0]["timecodes"], raw["targets"][0]["timecodes"])
        self.assertEqual(result["targets"][1]["timecodes"], raw["targets"][1]["timecodes"])

    def test_duplicate_txt_names_and_source_overwrites_are_rejected(self):
        records = [{"path": self.root / name, "target": {"timecodes": self.options(filename_template="same.txt")}} for name in ("a.mp3", "b.opus")]
        with self.assertRaises(ValueError): studio.plan_timecode_paths(records)
        with self.assertRaises(ValueError): studio.plan_timecode_paths(records[:1], [self.root / "same.txt"])
        records = [{"path": self.root / "audio.mp3", "target": {"timecodes": self.options(filename_template="other.txt")}}, {"path": self.root / "other.txt", "target": {}}]
        with self.assertRaises(ValueError): studio.plan_timecode_paths(records)

    def test_disabled_options_do_not_resolve_source_paths_again(self):
        class Source:
            def __str__(self): raise AssertionError("disabled timecodes read a source")
        records = ({"path": self.root / "a.mp3", "target": {}},)
        self.assertEqual(studio.plan_timecode_paths(records, [Source()]), records)

    def test_source_planner_creates_a_unique_sidecar_per_format(self):
        source = self.root / "texts"; files = [source / "Глава 1.txt", source / "Глава 2.txt"]
        records = studio.plan_source_synthesis_target_paths(source, self.root / "out", files, [
            {"format": fmt, "assembly_mode": "merge", "timecodes": self.options()} for fmt in ("mp3", "opus", "m4b")
        ], book_name="Книга")
        self.assertEqual(len(records), 3)
        self.assertEqual(len({row["timecode_path"] for row in records}), 3)
        self.assertTrue(all(row["timecode_path"].name == row["path"].name + ".chapters.txt" for row in records))

    def test_helper_has_safe_fields_presets_and_time_format_tokens(self):
        self.assertEqual(studio.build_template_helper_token("timecode_line", "start", time_format="hms_ms"), "{start:hms_ms}")
        self.assertEqual(studio.render_template_helper_preview("timecode_line", "{start:youtube} {title}")[1], "03:42 Глава 2. Встреча")
        with self.assertRaises(studio.TemplateError): studio.template_helper_full_preview_rows("timecode_line", "{start} {title}")
        with self.assertRaises(studio.TemplateError): studio.build_template_helper_token("timecode_line", "index", adaptive=True)

    def test_missing_txt_is_not_ready_even_when_audio_is_ready(self):
        audio = self.root / "book.mp3"; audio.touch()
        record = {"path": audio, "target": {"timecodes": self.options()}}
        self.assertFalse(studio.source_output_file_is_ready(record, {}))
        studio.timecode_output_path(audio, self.options()).touch()
        self.assertTrue(studio.source_output_file_is_ready(record, {}))

    def test_timecode_notice_does_not_hide_missing_speech_or_txt_errors(self):
        result = {"status": "warning", "timecodes": {"warnings": ("YouTube",)}}
        self.assertTrue(studio.source_timecode_warning_only(result))
        self.assertFalse(studio.source_timecode_warning_only(result, source_warning=True))
        result["timecodes"]["error"] = "TXT unavailable"
        self.assertFalse(studio.source_timecode_warning_only(result))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Нужны FFmpeg и FFprobe")
class ChapterTimecodeAudioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); self.sources=[]
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "app-data")
        patcher.start(); self.addCleanup(patcher.stop)
        for i, duration in enumerate((.4, .7, .9)):
            p=self.root / f"Глава {i+1}.wav"
            with wave.open(str(p), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
                data=bytearray()
                for n in range(round(48000*duration)):
                    data.extend(round(10000*math.sin(2*math.pi*(330+110*i)*n/48000)).to_bytes(2,"little",signed=True))
                stream.writeframes(data)
            self.sources.append(p)
        self.options={"enabled":True,"line_template":"{index} {start:seconds} {end:seconds} {title}"}

    def rows(self, path):
        return [line.split(" ", 3) for line in studio.timecode_output_path(path, self.options).read_text().splitlines()]

    def test_mp3_opus_and_wav_use_measured_input_boundaries_with_pauses(self):
        for fmt in ("mp3", "opus", "wav"):
            with self.subTest(fmt=fmt):
                out=self.root / ("Книга."+fmt); result={}
                studio._export_merged_audio_ffmpeg(self.sources,out,output_format=fmt,sample_rate="48000",channels="mono",pause_ms=150,pause_start_ms=100,pause_end_ms=200,timecodes=self.options,_timecode_result=result)
                self.assertFalse(result.get("error"),result)
                rows=self.rows(out); self.assertEqual(len(rows),3)
                self.assertEqual([(float(row[1]),float(row[2])) for row in rows],[(0,.65),(.65,1.5),(1.5,2.6)])

    def test_batched_decode_and_grouped_source_fragments_keep_logical_chapters(self):
        out=self.root / "Книга.opus";result={}
        with mock.patch.object(studio,"FFMPEG_DECODE_BATCH_INPUTS",1):
            studio._export_merged_audio_ffmpeg(self.sources,out,output_format="opus",sample_rate="48000",channels="mono",pause_ms=100,timecodes=self.options,timecode_chapters=({"title":"Первая глава","source_count":2},{"title":"Вторая глава","source_count":1}),_timecode_result=result)
        self.assertFalse(result.get("error"),result)
        rows=self.rows(out); self.assertEqual(len(rows),2)
        self.assertEqual([(float(row[1]),float(row[2])) for row in rows],[(0,1.3),(1.3,2.2)])

    def test_speed_changes_timeline_and_last_end_matches_actual_pcm(self):
        out=self.root / "tempo.wav"; result={}
        studio._export_merged_audio_ffmpeg(self.sources,out,output_format="wav",sample_rate="48000",channels="mono",speed=1.5,timecodes=self.options,_timecode_result=result)
        self.assertFalse(result.get("error"),result)
        rows=self.rows(out)
        self.assertAlmostEqual(float(rows[1][1]),.4/1.5,delta=.001)
        with wave.open(str(out),"rb") as stream: duration=stream.getnframes()/stream.getframerate()
        self.assertAlmostEqual(float(rows[-1][2]),duration,delta=.001)

    def test_m4b_txt_matches_embedded_final_chapters(self):
        out=self.root / "Том.m4b";result={}
        studio._export_m4b_ffmpeg(self.sources,out,chapters=[{"title":p.stem,"duration":0} for p in self.sources],pause_ms=125,pause_start_ms=100,pause_end_ms=200,timecodes=self.options,_timecode_result=result)
        self.assertFalse(result.get("error"),result)
        probe=json.loads(subprocess.run([studio.get_ffprobe_path(),"-v","error","-show_chapters","-of","json",str(out)],check=True,capture_output=True,text=True).stdout)
        for row,chapter in zip(self.rows(out),probe["chapters"]):
            self.assertAlmostEqual(float(row[1]),float(chapter["start_time"]),delta=.001)
            self.assertAlmostEqual(float(row[2]),float(chapter["end_time"]),delta=.001)
            self.assertEqual(row[3],chapter["tags"]["title"])

    def test_old_m4b_can_restore_txt_from_embedded_chapters_without_reencoding(self):
        out = self.root / "Старый том.m4b"
        studio._export_m4b_ffmpeg(self.sources, out, chapters=[{"title": p.stem, "duration": 0} for p in self.sources], pause_ms=125)
        before = out.read_bytes(), out.stat()
        self.assertIsNone(studio.load_timecode_timeline(out))
        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=AssertionError("reencoding forbidden")):
            result = studio.refresh_timecodes_file(out, self.options)
        self.assertNotIn("error", result)
        probe = studio.probe_m4b_chapters(out)
        rows = self.rows(out)
        self.assertEqual(len(rows), len(probe["chapters"]))
        for row, chapter in zip(rows, probe["chapters"]):
            self.assertAlmostEqual(float(row[1]), chapter["start"], delta=.001)
            self.assertAlmostEqual(float(row[2]), chapter["end"], delta=.001)
            self.assertEqual(row[3], chapter["title"])
        self.assertEqual(before, (out.read_bytes(), out.stat()))

    def test_restored_opus_txt_keeps_measured_speed_and_effect_boundaries(self):
        out = self.root / "Обработанная книга.opus"
        studio._export_merged_audio_ffmpeg(self.sources, out, output_format="opus", sample_rate="48000",
                                         channels="mono", speed=1.5, echo=True, echo_delay=100, echo_decay=.3,
                                         timecodes=self.options)
        txt = studio.timecode_output_path(out, self.options)
        expected = txt.read_bytes()
        txt.unlink()
        before = out.read_bytes(), out.stat()
        result = studio.refresh_timecodes_file(out, self.options)
        self.assertNotIn("error", result)
        self.assertEqual(txt.read_bytes(), expected)
        self.assertEqual(before, (out.read_bytes(), out.stat()))

    def test_audio_is_complete_when_txt_publish_fails(self):
        out=self.root / "audio.mp3";result={}
        with mock.patch.object(studio,"write_timecodes_file",side_effect=OSError("TXT unavailable")):
            studio._export_merged_audio_ffmpeg(self.sources,out,output_format="mp3",timecodes=self.options,_timecode_result=result)
        self.assertTrue(out.is_file()); self.assertIn("TXT unavailable",result["error"])
        self.assertGreater(studio._probe_audio_duration(out),1.9)

    def test_export_set_builds_split_m4b_and_group_opus_with_sidecars(self):
        files={str(i):{"path":str(p),"title":p.stem,"duration":duration} for i,(p,duration) in enumerate(zip(self.sources,(.4,.7,.9)))}
        groups={"g":{"name":"Книга","merge":True,"pause":100}}
        children={"g":tuple(files)}
        targets=[{"format":"opus","timecodes":self.options},{"format":"m4b","max_chapters":2,"timecodes":self.options}]
        records=studio.plan_export_target_paths(("g",),children,groups,files,targets,self.root / "out")
        app=object.__new__(studio.TTSApp); app.config={}
        notices=app._run_output_target_set(("g",),children,groups,files,targets,self.root / "out",planned_records=records,max_workers=2)
        self.assertFalse(notices,notices)
        self.assertEqual(len(records),3)
        for row in records:
            self.assertTrue(row["path"].is_file()); self.assertTrue(row["timecode_path"].is_file())
            self.assertEqual(float(row["timecode_path"].read_text().splitlines()[0].split(" ")[1]),0)

    def test_direct_export_rejects_txt_collisions_before_creating_audio(self):
        files = {str(i): {"path": str(path), "title": path.stem, "duration": duration}
                 for i, (path, duration) in enumerate(zip(self.sources, (.4, .7, .9)))}
        options = dict(self.options, filename_template="same.txt")
        app = object.__new__(studio.TTSApp)
        app.config = {}
        output = self.root / "out"
        with self.assertRaisesRegex(ValueError, "совпадающие выходные имена таймкодов"):
            app._run_output_target_set(tuple(files), {}, {}, files,
                                       [{"format": "opus", "timecodes": options}], output)
        self.assertFalse(output.exists())

    def test_source_group_keeps_one_chapter_per_txt_and_reuses_complete_output(self):
        target = studio.normalize_output_target({"format": "opus", "timecodes": self.options})
        path = self.root / "source-group.opus"
        record = {"kind": "group", "path": path, "target": target,
                  "source_paths": (self.root / "Первая.txt", self.root / "Вторая.txt")}
        statuses = {}
        processor = SimpleNamespace(is_stopped=False, encode_semaphore=None,
                                    processing_statuses_ram=statuses)
        processor._mark_output_status = lambda output, status: statuses.update({str(output.resolve()): status})
        result = studio.run_source_synthesis_target_chain(
            processor, self.sources, [record], {"apply_output_tags": False},
            chapter_source_counts=(2, 1),
        )
        self.assertEqual(result["status"], "success", result)
        rows = self.rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[3] for row in rows], ["Первая", "Вторая"])
        self.assertAlmostEqual(float(rows[1][1]), 1.1, places=3)
        before = path.stat().st_mtime_ns
        skipped = studio.run_source_synthesis_target_chain(
            processor, self.sources, [record], {}, skip_existing=True,
            chapter_source_counts=(2, 1),
        )
        self.assertEqual(skipped["results"][0]["status"], "skipped")
        self.assertEqual(path.stat().st_mtime_ns, before)

if __name__ == "__main__": unittest.main()
