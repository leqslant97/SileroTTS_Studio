"""Настоящая сборка больших групп с ограниченным числом входов FFmpeg."""

import array
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


class BoundedDecodePlanningTests(unittest.TestCase):
    def test_long_unicode_paths_and_ranges_keep_order_without_large_batches(self):
        sources = tuple(
            Path("/исходники") / ("Название длинного каталога" * 15) / f"{index:04d}.ogg"
            for index in range(120)
        )
        ranges = tuple((index / 10, index / 10 + 0.5) for index in range(len(sources)))

        batches = studio._plan_ffmpeg_decode_batches(sources, ranges)
        short_batches = studio._plan_ffmpeg_decode_batches(
            tuple(Path(f"{index}.ogg") for index in range(len(sources))), ranges
        )

        self.assertEqual(tuple(item for batch in batches for item in batch), tuple(zip(sources, ranges)))
        self.assertGreater(len(batches), len(short_batches))
        self.assertTrue(all(len(batch) <= studio.FFMPEG_DECODE_BATCH_INPUTS for batch in batches))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Нужны FFmpeg и FFprobe")
class BoundedAudioMergeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.sources = []
        for index, rate, channels, codec, extension, duration in (
            (0, 22050, 1, "pcm_s16le", "wav", "0.11317"),
            (1, 48000, 2, "pcm_f32le", "wav", "0.09731"),
            (2, 48000, 1, "libopus", "ogg", "0.12923"),
            (3, 48000, 1, "libopus", "ogg", "0.0121875"),
        ):
            path = cls.root / f"source_{index}.{extension}"
            cls.ffmpeg([
                "-y", "-f", "lavfi", "-i", f"sine=frequency={330 + 110 * index}:sample_rate={rate}",
                "-t", duration, "-ac", str(channels), "-c:a", codec, str(path),
            ])
            cls.sources.append(path)
        cls.profiles = [studio._probe_audio_stream_profile(path) for path in cls.sources]

    @staticmethod
    def ffmpeg(arguments):
        return subprocess.run(
            [studio.get_ffmpeg_path(), "-v", "error", *arguments],
            check=True, capture_output=True,
        ).stdout

    @classmethod
    def pcm(cls, path):
        return cls.ffmpeg(["-i", str(path), "-map", "0:a:0", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"])

    def independent_graph_pcm(self, sources, ranges, pause_ms=0, effects=None):
        arguments, filters, labels = [], [], []
        for index, (path, clip) in enumerate(zip(sources, ranges)):
            if clip is not None:
                arguments.extend(["-ss", f"{clip[0]:.9f}", "-t", f"{clip[1] - clip[0]:.9f}"])
            arguments.extend(["-i", str(path)])
            trim = f"atrim=duration={clip[1] - clip[0]:.9f}," if clip is not None else ""
            filters.append(f"[{index}:a:0]{trim}aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]")
            labels.append(f"[a{index}]")
            if pause_ms and index < len(sources) - 1:
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={pause_ms / 1000:.6f},asetpts=PTS-STARTPTS[p{index}]")
                labels.append(f"[p{index}]")
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[merged]")
        effect_filters = studio._ffmpeg_audio_effect_filters(sample_rate=48000, **(effects or {}))
        if effect_filters:
            filters.append("[merged]" + ",".join(effect_filters) + "[out]")
        else:
            filters.append("[merged]anull[out]")
        return self.ffmpeg([
            *arguments, "-filter_complex", ";".join(filters), "-map", "[out]",
            "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
        ])

    def test_large_mixed_group_preserves_cuts_pauses_and_global_effects(self):
        sources = [self.sources[index % 3] for index in range(40)]
        profiles = [self.profiles[index % 3] for index in range(40)]
        ranges = [(0.013, 0.081) if index % 4 == 0 else None for index in range(40)]
        for effects in ({}, {"speed": 1.2, "pitch": 0.9, "echo": True, "echo_delay": 80, "echo_decay": 0.3}):
            with self.subTest(effects=effects):
                expected = self.independent_graph_pcm(sources, ranges, pause_ms=37, effects=effects)
                output = self.root / ("mixed_effects.wav" if effects else "mixed.wav")
                studio._export_merged_audio_ffmpeg(
                    sources, output, output_format="wav", sample_rate="48000", channels="stereo",
                    source_ranges=ranges, pause_ms=37, _probed_profiles=profiles, **effects,
                )
                actual = self.pcm(output)
                self.assertEqual(len(actual), len(expected), "Изменились длительность или паузы")
                differences = [abs(first - second) for first, second in zip(array.array("h", actual), array.array("h", expected))]
                # Смена планарного и упакованного float допускает округление
                # в младшем разряде PCM16; пропуски и сдвиги этим не скрываются.
                self.assertLessEqual(max(differences), 2)
                self.assertLess((sum(value * value for value in differences) / len(differences)) ** 0.5, 0.15)

    def test_thousand_opus_fragments_keep_all_audio_with_bounded_inputs(self):
        output = self.root / "thousand_fragments.wav"
        source = self.sources[3]
        count = 1200
        source_pcm = self.ffmpeg(["-i", str(source), "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"])
        processes = []
        real_popen = subprocess.Popen

        def tracked_popen(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            processes.append((list(command), process))
            return process

        with mock.patch.object(studio.subprocess, "Popen", side_effect=tracked_popen):
            studio._export_merged_audio_ffmpeg(
                [source] * count, output, output_format="wav", sample_rate="48000", channels="mono",
                _probed_profiles=[self.profiles[3]] * count,
            )

        self.assertEqual(self.pcm(output), source_pcm * count)
        self.assertLess(max(command.count("-i") for command, _ in processes), 100)
        self.assertTrue(all(process.poll() is not None for _, process in processes))

    def test_batched_mp3_and_opus_keep_cover_and_tags(self):
        cover = self.root / "cover.png"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1", str(cover)])
        for fmt, bitrate in (("mp3", "128k"), ("opus", "48k")):
            with self.subTest(format=fmt):
                output = self.root / f"cover.{fmt}"
                with mock.patch.object(studio, "FFMPEG_DECODE_BATCH_INPUTS", 2):
                    studio._export_merged_audio_ffmpeg(
                        [self.sources[3]] * 3, output, output_format=fmt,
                        bitrate=bitrate, bitrate_mode=bitrate,
                        sample_rate="48000", channels="mono",
                        tags={"title": "Название группы", "album": "Книга"}, cover=cover,
                        _probed_profiles=[self.profiles[3]] * 3,
                    )
                metadata = json.loads(subprocess.run(
                    [studio.get_ffprobe_path(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                    check=True, capture_output=True,
                ).stdout)
                audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
                tags = dict(metadata["format"].get("tags", {}))
                tags.update(audio.get("tags", {}))
                tags = {key.lower(): value for key, value in tags.items()}
                pictures = [stream for stream in metadata["streams"] if stream.get("disposition", {}).get("attached_pic")]
                self.assertEqual(tags.get("title"), "Название группы")
                self.assertEqual(tags.get("album"), "Книга")
                self.assertEqual(len(pictures), 1)
                self.assertEqual(pictures[0]["codec_name"], "mjpeg" if fmt == "mp3" else "png")

    def test_decoder_failure_preserves_existing_output(self):
        corrupt = self.root / "corrupt.ogg"
        corrupt.write_bytes(b"invalid audio")
        output = self.root / "existing_failure.wav"
        output.write_bytes(b"previous completed result")
        sources = [self.sources[3]] * 35 + [corrupt]
        profiles = [self.profiles[3]] * len(sources)

        with self.assertRaises((RuntimeError, OSError)):
            studio._export_merged_audio_ffmpeg(
                sources, output, output_format="wav", sample_rate="48000", channels="mono",
                _probed_profiles=profiles,
            )

        self.assertEqual(output.read_bytes(), b"previous completed result")
        self.assertEqual(list(self.root.glob(".audio_*.tmp*")), [])

    def test_stop_terminates_encoder_and_decoder_without_replacing_output(self):
        output = self.root / "existing_cancelled.wav"
        output.write_bytes(b"previous completed result")
        processes = []
        real_popen = subprocess.Popen
        cancellation_observed = []

        def tracked_popen(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            processes.append(process)
            return process

        def cancelled():
            stop = len(processes) >= 2
            if stop:
                cancellation_observed.append(True)
            return stop

        with mock.patch.object(studio.subprocess, "Popen", side_effect=tracked_popen):
            with self.assertRaises(InterruptedError):
                studio._export_merged_audio_ffmpeg(
                    [self.sources[3]] * 1200, output,
                    output_format="wav", sample_rate="48000", channels="mono",
                    _probed_profiles=[self.profiles[3]] * 1200, cancelled=cancelled,
                )

        self.assertTrue(cancellation_observed)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(output.read_bytes(), b"previous completed result")
        self.assertEqual(list(self.root.glob(".audio_*.tmp*")), [])


if __name__ == "__main__":
    unittest.main()
