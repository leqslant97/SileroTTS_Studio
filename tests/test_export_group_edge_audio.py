"""Краевые паузы группы сохраняют PCM и добавляются к каждой части M4B."""

import array
import copy
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import wave

from test_m4b_whole_file_export import studio  # Изолированный импорт копии исходника.


class GroupEdgePausePlanningTests(unittest.TestCase):
    def test_limits_reserve_edges_without_changing_source_chapters(self):
        chapters = [{"index": index, "title": str(index), "duration": 0.2}
                    for index in range(1, 5)]
        original = copy.deepcopy(chapters)
        parts = studio.plan_m4b_parts(
            chapters, max_duration_seconds=0.65, pause_seconds=0.05,
            pause_start_seconds=0.15, pause_end_seconds=0.25,
        )
        self.assertEqual([(part.chapter_start, part.chapter_end) for part in parts],
                         [(1, 1), (2, 2), (3, 3), (4, 4)])
        for actual in parts:
            self.assertAlmostEqual(actual.duration_seconds, 0.6)
            self.assertLessEqual(actual.duration_seconds, 0.65)
        self.assertEqual(chapters, original)
        self.assertTrue(all(chapter["duration"] == 0.2
                            for part in parts for chapter in part.chapters))

    def test_single_indivisible_chapter_keeps_both_edges_even_above_limit(self):
        parts = studio.plan_m4b_parts(
            [{"title": "one", "duration": 0.2}], max_duration_seconds=0.3,
            pause_start_seconds=0.15, pause_end_seconds=0.25,
        )
        self.assertEqual(len(parts), 1)
        self.assertAlmostEqual(parts[0].duration_seconds, 0.6)
        self.assertEqual(parts[0].chapters[0]["duration"], 0.2)

    def test_auto_long_guard_counts_edges_for_every_created_part(self):
        chapters = [{"title": str(index), "duration": 43199.5}
                    for index in range(2)]
        parts = studio.plan_m4b_parts(
            chapters, auto_split_long=True,
            pause_start_seconds=1, pause_end_seconds=1,
        )
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(sum(part.duration_seconds for part in parts), 86403)
        self.assertEqual([part.chapters[0]["duration"] for part in parts],
                         [43199.5, 43199.5])


class GroupEdgePauseAudioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.first, self.first_pcm = self.make_wav("first", 9600, 650, trailing=4800)
        self.second, self.second_pcm = self.make_wav("second", 14400, 870)
        self.short, self.short_pcm = self.make_wav("short", 480, 650)

    def make_wav(self, name, sample_count, frequency, *, trailing=0):
        samples = array.array("h", (
            round(12000 * math.sin(2 * math.pi * frequency * index / 48000))
            if index < sample_count - trailing else 0
            for index in range(sample_count)
        ))
        data = samples.tobytes()
        path = self.base / f"{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(data)
        return path, data

    def pcm(self, path):
        return subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-ar", "48000", "-ac", "1", "-f", "s16le", "-",
        ], check=True, capture_output=True).stdout

    def wav_pcm(self, path):
        with wave.open(str(path), "rb") as sound:
            self.assertEqual((sound.getframerate(), sound.getnchannels(),
                              sound.getsampwidth()), (48000, 1, 2))
            return sound.readframes(sound.getnframes())

    def m4b_metadata(self, path):
        result = subprocess.run([
            studio.get_ffprobe_path(), "-v", "error", "-show_streams",
            "-show_chapters", "-of", "json", str(path),
        ], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def assert_m4b_duration(self, path, duration):
        metadata = self.m4b_metadata(path)
        audio = next(item for item in metadata["streams"] if item["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertAlmostEqual(float(audio["duration"]), duration, delta=0.001)
        self.assertAlmostEqual(float(metadata["chapters"][-1]["end_time"]),
                               duration, delta=0.00051)
        return metadata

    def app_and_records(self, sources=None):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        sources = sources or (self.first, self.second)
        files = {f"file{index}": {
            "path": str(source), "title": f"Глава {index}",
            "duration": (len(self.first_pcm) / 96000
                         if source == self.first else len(self.second_pcm) / 96000),
        } for index, source in enumerate(sources, 1)}
        return app, files

    def test_merged_wav_has_exact_edges_and_preserves_existing_source_silence(self):
        target = self.base / "merged.wav"
        counts = []
        studio._export_merged_audio_ffmpeg(
            (self.first, self.second), target, output_format="wav",
            sample_rate="48000", channels="mono", pause_ms=100,
            pause_start_ms=125, pause_end_ms=175, _source_sample_counts=counts,
        )
        expected = (b"\x00" * (6000 * 2) + self.first_pcm
                    + b"\x00" * (4800 * 2) + self.second_pcm
                    + b"\x00" * (8400 * 2))
        self.assertEqual(self.wav_pcm(target), expected)
        self.assertEqual(counts, [9600, 14400])

    def test_batched_64_sources_get_edges_once_and_exact_source_sample_counts(self):
        target = self.base / "batched.wav"
        counts = []
        with mock.patch.object(studio, "FFMPEG_DECODE_BATCH_INPUTS", 16):
            studio._export_merged_audio_ffmpeg(
                [self.short] * 64, target, output_format="wav",
                sample_rate="48000", channels="mono", pause_ms=3,
                pause_start_ms=7, pause_end_ms=11, _source_sample_counts=counts,
            )
        expected = (b"\x00" * (336 * 2)
                    + (b"\x00" * (144 * 2)).join([self.short_pcm] * 64)
                    + b"\x00" * (528 * 2))
        self.assertEqual(self.wav_pcm(target), expected)
        self.assertEqual(counts, [480] * 64)

    def test_clipped_group_source_counts_exclude_new_edge_pauses(self):
        target = self.base / "clipped.wav"
        counts = []
        studio._export_merged_audio_ffmpeg(
            (self.first, self.second), target, output_format="wav",
            sample_rate="48000", channels="mono",
            source_ranges=((0.025, 0.075), None),
            pause_start_ms=10, pause_end_ms=20, _source_sample_counts=counts,
        )
        expected = (b"\x00" * (480 * 2) + self.first_pcm[1200 * 2:3600 * 2]
                    + self.second_pcm + b"\x00" * (960 * 2))
        self.assertEqual(self.wav_pcm(target), expected)
        self.assertEqual(counts, [2400, 14400])

    def test_zero_default_edges_preserve_exact_wav_audio(self):
        for options in ({}, {"pause_start_ms": 0, "pause_end_ms": 0},
                        {"pause_start_ms": -7, "pause_end_ms": "invalid"}):
            with self.subTest(options=options):
                target = self.base / f"copy{len(options)}.wav"
                with mock.patch.object(studio, "_run_ffmpeg_stream_copy_concat",
                                       wraps=studio._run_ffmpeg_stream_copy_concat) as copy_audio:
                    studio._export_merged_audio_ffmpeg(
                        (self.first, self.first), target, output_format="wav", **options,
                    )
                copy_audio.assert_not_called()
                self.assertEqual(self.wav_pcm(target), self.first_pcm * 2)

    def test_zero_edges_preserve_mp3_stream_copy_and_nonzero_edges_disable_it(self):
        source = self.base / "source.mp3"
        subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(self.first),
            "-c:a", "libmp3lame", "-b:a", "128k", str(source),
        ], check=True, capture_output=True)
        for start, end in ((0, 0), (125, 175)):
            with self.subTest(edges=(start, end)):
                target = self.base / f"copy-{start}.mp3"
                with mock.patch.object(studio, "_run_ffmpeg_stream_copy_concat",
                                       wraps=studio._run_ffmpeg_stream_copy_concat) as copy_audio:
                    studio._export_merged_audio_ffmpeg(
                        (source,), target, output_format="mp3",
                        pause_start_ms=start, pause_end_ms=end,
                    )
                self.assertEqual(copy_audio.call_count, 0 if start or end else 1)
                self.assertEqual(len(self.pcm(target)), 2 * (9600 + round((start + end) * 48)))

    def test_m4b_first_and_last_chapters_include_edges_with_measured_durations(self):
        target = self.base / "chapters.m4b"
        chapters = [{"title": "Первая", "duration": 9},
                    {"title": "Вторая", "duration": 9}]
        original = copy.deepcopy(chapters)
        studio._export_m4b_ffmpeg(
            (self.first, self.second), target, chapters=chapters, pause_ms=100,
            pause_start_ms=125, pause_end_ms=175,
        )
        data = self.assert_m4b_duration(target, 0.9)
        actual = data["chapters"]
        self.assertEqual([item["tags"]["title"] for item in actual], ["Первая", "Вторая"])
        self.assertEqual(float(actual[0]["start_time"]), 0)
        self.assertAlmostEqual(float(actual[0]["end_time"]), 0.425, delta=0.00051)
        self.assertAlmostEqual(float(actual[1]["start_time"]), 0.425, delta=0.00051)
        decoded = array.array("h", self.pcm(target))
        self.assertEqual(list(decoded[:2400]), [0] * 2400)
        self.assertEqual(list(decoded[round(0.82 * 48000):round(0.88 * 48000)]),
                         [0] * (round(0.88 * 48000) - round(0.82 * 48000)))
        self.assertEqual(chapters, original)

    def test_parallel_split_m4b_every_part_gets_group_edges(self):
        app, files = self.app_and_records((self.first,) * 3)
        group = {"name": "Том", "merge": False, "pause": 50,
                 "pause_start": 100, "pause_end": 200}
        before = copy.deepcopy((files, group))
        target_dir = self.base / "parts"
        with mock.patch.object(studio, "_export_m4b_ffmpeg",
                               wraps=studio._export_m4b_ffmpeg) as export:
            app._run_output_target_set(
                ("group",), {"group": tuple(files)}, {"group": group}, files,
                [{"format": "m4b", "max_chapters": 1}], str(target_dir), max_workers=2,
            )
        calls = sorted(export.call_args_list, key=lambda call: Path(call.args[1]).name)
        self.assertEqual(len(calls), 3)
        self.assertEqual([(call.kwargs["pause_start_ms"], call.kwargs["pause_end_ms"])
                          for call in calls], [(100, 200)] * 3)
        for call in calls:
            data = self.assert_m4b_duration(Path(call.args[1]), 0.5)
            self.assertEqual(len(data["chapters"]), 1)
        self.assertEqual((files, group), before)

    def test_filtered_middle_m4b_job_keeps_its_own_group_edges(self):
        app, files = self.app_and_records((self.first,) * 3)
        group = {"name": "Том", "pause": 50, "pause_start": 100, "pause_end": 200}
        with mock.patch.object(studio, "_export_m4b_ffmpeg",
                               wraps=studio._export_m4b_ffmpeg) as export:
            app._run_output_target_set(
                ("group",), {"group": tuple(files)}, {"group": group}, files,
                [{"format": "m4b", "max_chapters": 1}], str(self.base / "middle"),
                _m4b_job_filter=((0, "group", 2),),
            )
        export.assert_called_once()
        self.assertEqual((export.call_args.kwargs["pause_start_ms"],
                          export.call_args.kwargs["pause_end_ms"]), (100, 200))
        self.assert_m4b_duration(Path(export.call_args.args[1]), 0.5)

    def test_file_assembly_target_and_standalone_files_ignore_group_edges(self):
        app, files = self.app_and_records()
        files["standalone"] = dict(files["file1"], title="Отдельный",
                                   pause_start=5000, pause_end=5000)
        group = {"name": "Группа", "merge": True, "pause": 100,
                 "pause_start": 125, "pause_end": 175}
        target_dir = self.base / "files"
        app._run_output_target_set(
            ("group", "standalone"), {"group": ("file1", "file2")},
            {"group": group}, files,
            [{"format": "wav", "assembly_mode": "files"}], str(target_dir),
        )
        outputs = {path.stem: self.wav_pcm(path) for path in target_dir.rglob("*.wav")}
        self.assertEqual(outputs, {"Глава 1": self.first_pcm,
                                   "Глава 2": self.second_pcm,
                                   "Отдельный": self.first_pcm})

    def test_general_codecs_all_keep_group_edges_and_decode(self):
        for fmt in ("mp3", "ogg", "opus", "m4a"):
            with self.subTest(format=fmt):
                target = self.base / f"edges.{fmt}"
                studio._export_merged_audio_ffmpeg(
                    (self.first, self.second), target, output_format=fmt,
                    sample_rate="48000", channels="mono", pause_ms=100,
                    pause_start_ms=125, pause_end_ms=175,
                )
                decoded = array.array("h", self.pcm(target))
                self.assertLess(abs(len(decoded) / 48000 - 0.9), 0.025)
                self.assertTrue(all(abs(value) <= 1 for value in decoded[:2400]))
                self.assertGreater(max(decoded), 1000)

    def test_speed_effect_is_applied_after_all_group_pauses(self):
        original = self.base / "with-pauses.wav"
        accelerated = self.base / "accelerated.wav"
        options = dict(output_format="wav", sample_rate="48000", channels="mono",
                       pause_ms=100, pause_start_ms=125, pause_end_ms=175)
        studio._export_merged_audio_ffmpeg((self.first, self.second), original, **options)
        studio._export_merged_audio_ffmpeg((self.first, self.second), accelerated,
                                          speed=2, **options)
        # Независимое применение atempo к уже склеенному PCM включает начало,
        # существующую тишину источника, паузу между файлами и конец группы.
        reference = subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(original),
            "-af", "atempo=2.00000000", "-f", "s16le", "-",
        ], check=True, capture_output=True).stdout
        self.assertEqual(self.wav_pcm(accelerated), reference)
        self.assertLess(abs(len(reference) / 96000 - 0.45), 0.05)

    def test_clean_plan_and_runtime_duration_split_reserve_both_edges_per_volume(self):
        app, files = self.app_and_records()
        group = {"name": "Том", "pause": 100, "pause_start": 125, "pause_end": 175}
        target = {"format": "m4b", "max_duration_seconds": 0.65}
        target_dir = self.base / "duration-parts"
        plan = studio.plan_export_target_paths(
            ("group",), {"group": tuple(files)}, {"group": group}, files,
            [target], str(target_dir),
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual([(record["part"].chapter_start, record["part"].chapter_end)
                          for record in plan], [(1, 1), (2, 2)])
        self.assertAlmostEqual(plan[0]["part"].duration_seconds, 0.5)
        self.assertAlmostEqual(plan[1]["part"].duration_seconds, 0.6)
        app._run_output_target_set(
            ("group",), {"group": tuple(files)}, {"group": group}, files,
            [target], str(target_dir),
        )
        self.assertEqual(set(target_dir.rglob("*.m4b")),
                         {record["path"] for record in plan})
        for record in plan:
            self.assert_m4b_duration(record["path"], record["part"].duration_seconds)


if __name__ == "__main__":
    unittest.main()
