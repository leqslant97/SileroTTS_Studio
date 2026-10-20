"""Границы M4B соответствуют PCM текущего кодирования, включая паузы."""

import importlib.util
import json
import shutil
import subprocess
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


class SampleCountLogTests(unittest.TestCase):
    def test_named_filters_allow_interleaved_and_out_of_order_logs(self):
        log = (
            "[astats@silero_chapter_1 @ 0xab] Overall\n"
            "[astats@unrelated @ 0xcd] Number of samples: 900000\n"
            "[astats@silero_chapter_0 @ 0xef] Number of samples: 48001\n"
            "[astats@silero_chapter_1 @ 0xab] Number of samples: 9.6e+04\n"
        )
        self.assertEqual(studio._ffmpeg_source_sample_counts(log.encode(), 2), [48001, 96000])

    def test_incomplete_or_invalid_counts_do_not_partially_change_timing(self):
        for value in ("nan", "inf", "-1", "0", "4.5"):
            with self.subTest(value=value):
                log = f"[astats@silero_chapter_0 @ 0xa] Number of samples: {value}\n"
                self.assertIsNone(studio._ffmpeg_source_sample_counts(log, 1))
        self.assertIsNone(studio._ffmpeg_source_sample_counts(
            "[astats@silero_chapter_0 @ 0xa] Number of samples: 48000\n", 2
        ))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Нужны FFmpeg и FFprobe")
class M4BSampleTimingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "without_xing.mp3"
        cls.ffmpeg([
            "-y", "-f", "lavfi", "-i", "sine=frequency=731:sample_rate=44100:duration=0.413",
            "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0", str(cls.source),
        ])
        cls.sample_count = len(cls.ffmpeg([
            "-i", str(cls.source), "-ar", "48000", "-ac", "1",
            "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
        ])) // 2

    @staticmethod
    def ffmpeg(arguments):
        return subprocess.run(
            [studio.get_ffmpeg_path(), "-v", "error", *arguments],
            check=True, capture_output=True,
        ).stdout

    def probe(self, path):
        result = subprocess.run([
            studio.get_ffprobe_path(), "-v", "error", "-show_chapters",
            "-show_streams", "-of", "json", str(path),
        ], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def assert_timeline(self, path, counts, pause_ms):
        data = self.probe(path)
        chapters = data["chapters"]
        self.assertEqual(len(chapters), len(counts))
        cursor = 0
        for index, (chapter, count) in enumerate(zip(chapters, counts)):
            self.assertAlmostEqual(float(chapter["start_time"]), cursor / 48000, delta=0.00051)
            self.assertEqual(chapter["tags"]["title"], f"Глава {index + 1}")
            cursor += count
            if index < len(counts) - 1:
                cursor += pause_ms * 48
            self.assertAlmostEqual(float(chapter["end_time"]), cursor / 48000, delta=0.00051)
        audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(audio["duration"]), cursor / 48000, delta=0.001)

    def test_twenty_two_mp3_chapters_correct_accumulating_duration_estimates(self):
        count = 22
        output = self.directory / "estimated_twenty_two.m4b"
        sources = [self.source] * count
        durations = self.sample_count / 48000 + 0.008
        commands = []
        real_popen = subprocess.Popen

        def tracked_popen(command, *args, **kwargs):
            commands.append(list(command))
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(studio.subprocess, "Popen", side_effect=tracked_popen):
            studio._export_m4b_ffmpeg(
                sources, output,
                chapters=[{"title": f"Глава {index + 1}", "duration": durations} for index in range(count)],
                pause_ms=2000,
            )
        self.assert_timeline(output, [self.sample_count] * count, 2000)
        ffmpeg_commands = [command for command in commands if Path(command[0]).name.startswith("ffmpeg")]
        self.assertEqual(len(ffmpeg_commands), 2, "Появилось дополнительное декодирование книги")

    def test_batched_decode_keeps_exact_chapters_and_separators(self):
        output = self.directory / "batched.m4b"
        with mock.patch.object(studio, "FFMPEG_DECODE_BATCH_INPUTS", 2):
            studio._export_m4b_ffmpeg(
                [self.source] * 7, output, pause_ms=2000,
                chapters=[{"title": f"Глава {index + 1}", "duration": 9} for index in range(7)],
            )
        self.assert_timeline(output, [self.sample_count] * 7, 2000)

    def test_actual_trimmed_input_duration_overrides_stale_clip_metadata(self):
        output = self.directory / "clips.m4b"
        ranges = [(0.03, 0.197), (0.11, 0.321)]
        expected = []
        for start, end in ranges:
            expected.append(len(self.ffmpeg([
                "-ss", f"{start:.9f}", "-t", f"{end - start:.9f}", "-i", str(self.source),
                "-af", f"atrim=duration={end - start:.9f},aresample=48000",
                "-ac", "1", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
            ])) // 2)
        studio._export_m4b_ffmpeg(
            [self.source] * 2, output, source_ranges=ranges, pause_ms=125,
            chapters=[{"title": f"Глава {index + 1}", "duration": 4} for index in range(2)],
        )
        self.assert_timeline(output, expected, 125)

    def test_collecting_samples_keeps_encoded_audio_identical(self):
        normal = self.directory / "normal.wav"
        measured = self.directory / "measured.wav"
        counts = []
        arguments = dict(output_format="wav", sample_rate="48000", channels="mono", pause_ms=71)
        studio._export_merged_audio_ffmpeg([self.source] * 3, normal, **arguments)
        studio._export_merged_audio_ffmpeg(
            [self.source] * 3, measured, _source_sample_counts=counts, **arguments
        )
        self.assertEqual(counts, [self.sample_count] * 3)
        self.assertEqual(self.ffmpeg(["-i", str(normal), "-f", "s16le", "pipe:1"]),
                         self.ffmpeg(["-i", str(measured), "-f", "s16le", "pipe:1"]))

    def test_source_preencoded_stage_keeps_opus_chapters_with_stored_pauses(self):
        speech = self.directory / "speech.ogg"
        silence = self.directory / "two_seconds.ogg"
        self.ffmpeg([
            "-y", "-f", "lavfi", "-i", "sine=frequency=420:sample_rate=48000:duration=0.137",
            "-c:a", "libopus", str(speech),
        ])
        self.ffmpeg([
            "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2",
            "-c:a", "libopus", str(silence),
        ])
        processor = object.__new__(studio.TTSProcessor)
        processor.is_stopped = False
        processor._mark_output_status = mock.Mock()
        fragments = [speech, silence, speech]
        output = self.directory / "source_staged.m4b"
        stage = self.directory / "source_staged.m4a"
        record = {
            "target": studio.OutputTarget(format="m4b").to_dict(),
            "path": output, "file_ids": ("one", "two"),
            "source_paths": ("Глава 1.txt", "Глава 2.txt"),
            "group_name": "Книга", "group_index": 1, "parts": 1,
        }
        with mock.patch.object(studio, "SESSION_TEMP_DIR", self.directory / "session"):
            merged = processor._run_ffmpeg_concat(fragments)
            self.assertIsNotNone(merged)
            try:
                exact_count = len(self.ffmpeg([
                    "-i", str(merged), "-ar", "48000", "-ac", "1",
                    "-f", "s16le", "pipe:1",
                ])) // 2
            finally:
                Path(merged).unlink()
            prepared = studio.run_source_synthesis_m4b_target(
                processor, record, {"one": fragments, "two": fragments},
                {"apply_output_tags": False}, audio_stage_path=stage,
            )
            self.assertEqual(prepared["status"], "success")
            self.assertEqual(prepared["chapter_durations"], {
                "one": exact_count / 48000, "two": exact_count / 48000,
            })
            with mock.patch.object(processor, "_run_ffmpeg_concat", side_effect=AssertionError("Повторная склейка")):
                published = studio.run_source_synthesis_m4b_target(
                    processor, record, {"one": fragments, "two": fragments},
                    {"apply_output_tags": False}, preencoded_audio=stage,
                    chapter_duration_overrides=prepared["chapter_durations"],
                )
            self.assertEqual(published["status"], "success")
        self.assert_timeline(output, [exact_count] * 2, 0)


if __name__ == "__main__":
    unittest.main()
