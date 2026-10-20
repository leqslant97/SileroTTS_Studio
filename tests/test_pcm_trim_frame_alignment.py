"""Регрессии точного PCM-среза автообрезки ответа API."""

import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydub import AudioSegment


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


def mono_audio(frame_count, frame_rate=48000, value=10000):
    raw = struct.pack("<h", value) * frame_count
    return AudioSegment(
        data=raw, sample_width=2, frame_rate=frame_rate, channels=1
    )


def stereo_audio(frame_count, frame_rate=44100):
    raw = b"".join(
        struct.pack("<hh", (number % 30000) - 15000, 15000 - (number % 30000))
        for number in range(frame_count)
    )
    return AudioSegment(
        data=raw, sample_width=2, frame_rate=frame_rate, channels=2
    )


class PcmTrimFrameAlignmentTests(unittest.TestCase):
    def test_fractional_tail_is_preserved_for_mono_48khz(self):
        for frame_count in (4801, 4824, 4825):
            with self.subTest(frame_count=frame_count):
                audio = mono_audio(frame_count, 48000)
                result = studio._slice_audio_trim_bounds(audio, (0, len(audio)))
                self.assertEqual(result.raw_data, audio.raw_data)
                self.assertEqual(
                    len(result.raw_data) // result.frame_width, frame_count
                )

    def test_fractional_tail_is_preserved_for_stereo_44100(self):
        for frame_count in (4411, 4433):
            with self.subTest(frame_count=frame_count):
                audio = stereo_audio(frame_count)
                result = studio._slice_audio_trim_bounds(audio, (0, len(audio)))
                self.assertEqual(result.raw_data, audio.raw_data)
                self.assertEqual(
                    len(result.raw_data) // result.frame_width, frame_count
                )
                self.assertEqual(result.channels, 2)
                self.assertEqual(result.frame_rate, 44100)

    def test_fractional_tail_is_preserved_for_mono_44100(self):
        for frame_count in (4411, 4433):
            with self.subTest(frame_count=frame_count):
                audio = mono_audio(frame_count, 44100)
                result = studio._slice_audio_trim_bounds(audio, (0, len(audio)))
                self.assertEqual(result.raw_data, audio.raw_data)
                self.assertEqual(
                    len(result.raw_data) // result.frame_width, frame_count
                )
                self.assertEqual(result.frame_rate, 44100)

    def test_fractional_tail_is_preserved_for_stereo_48000(self):
        for frame_count in (4801, 4824, 4825):
            with self.subTest(frame_count=frame_count):
                audio = stereo_audio(frame_count, 48000)
                result = studio._slice_audio_trim_bounds(audio, (0, len(audio)))
                self.assertEqual(result.raw_data, audio.raw_data)
                self.assertEqual(
                    len(result.raw_data) // result.frame_width, frame_count
                )
                self.assertEqual(result.channels, 2)
                self.assertEqual(result.frame_rate, 48000)

    def test_short_audio_and_full_silence_are_kept(self):
        one_frame = mono_audio(1)
        self.assertIs(
            studio._slice_audio_trim_bounds(one_frame, (0, len(one_frame))),
            one_frame,
        )

        silence = AudioSegment.silent(duration=100, frame_rate=48000)
        self.assertIsNone(studio._detect_audio_trim_bounds(silence))
        self.assertIs(studio._slice_audio_trim_bounds(silence, None), silence)

    def test_edge_trim_uses_expected_frame_aligned_20ms_padding(self):
        prefix = AudioSegment.silent(duration=80, frame_rate=48000)
        voice = mono_audio(120 * 48, 48000)
        suffix = AudioSegment.silent(duration=80, frame_rate=48000)
        audio = prefix + voice + suffix

        bounds = studio._detect_audio_trim_bounds(audio)
        self.assertEqual(bounds, (80, 200))
        result = studio._slice_audio_trim_bounds(audio, bounds)
        expected = audio.get_sample_slice(60 * 48, 220 * 48)
        self.assertEqual(result.raw_data, expected.raw_data)
        self.assertEqual(
            len(result.raw_data) // result.frame_width,
            len(expected.raw_data) // expected.frame_width,
        )

    def test_invalid_empty_range_keeps_original_audio(self):
        audio = mono_audio(4825, 48000)
        result = studio._slice_audio_trim_bounds(audio, (200, 200))
        self.assertIs(result, audio)
        self.assertEqual(result.raw_data, audio.raw_data)

    def test_start_only_trim_keeps_last_fractional_frame(self):
        audio = stereo_audio(4433, 44100)
        result = studio._slice_audio_trim_bounds(audio, (40, len(audio)))
        start_frame = int(audio.frame_count(ms=20))
        expected = audio.get_sample_slice(start_frame, 4433)
        self.assertEqual(result.raw_data, expected.raw_data)
        self.assertEqual(
            len(result.raw_data) // result.frame_width, 4433 - start_frame
        )
        self.assertEqual(
            result.raw_data[-audio.frame_width :],
            audio.raw_data[-audio.frame_width :],
        )

    def test_internal_pause_is_preserved(self):
        before = mono_audio(60 * 48, 48000)
        pause = AudioSegment.silent(duration=70, frame_rate=48000)
        after = mono_audio(60 * 48, 48000, value=-10000)
        edge_silence = AudioSegment.silent(duration=80, frame_rate=48000)
        audio = edge_silence + before + pause + after + edge_silence

        result = studio._slice_audio_trim_bounds(audio, (80, 270))
        expected = audio.get_sample_slice(60 * 48, 290 * 48)
        self.assertEqual(result.raw_data, expected.raw_data)
        pause_start = (80 + 60) * 48
        pause_end = pause_start + 70 * 48
        result_pause = result.get_sample_slice(pause_start - 60 * 48, pause_end - 60 * 48)
        self.assertEqual(result_pause.raw_data, pause.raw_data)

    def test_prepare_api_audio_file_uses_exact_helper_in_production_path(self):
        audio = mono_audio(4801, 48000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "answer.ogg"
            destination = root / "prepared.ogg"
            source.write_bytes(b"placeholder")
            exported = {}

            def fake_export(segment, path, **_kwargs):
                exported["segment"] = segment
                Path(path).write_bytes(b"encoded")
                return Path(path)

            with mock.patch.object(
                studio, "_validate_complete_ogg_audio_file"
            ), mock.patch.object(
                studio, "_inspect_ogg_audio_header", return_value=("opus", 1)
            ), mock.patch.object(
                studio, "_load_audio_segment", return_value=audio
            ), mock.patch.object(
                studio,
                "_detect_audio_trim_bounds",
                return_value=(40, len(audio)),
            ), mock.patch.object(
                studio,
                "_slice_audio_trim_bounds",
                wraps=studio._slice_audio_trim_bounds,
            ) as slicer, \
                 mock.patch.object(studio, "_export_audio_atomic", side_effect=fake_export), \
                 mock.patch.object(studio, "_require_opus_audio_file"), \
                 mock.patch.object(studio, "_validate_cached_ogg_audio_file"):
                result = studio._prepare_api_audio_file(source, destination)

            slicer.assert_called_once_with(audio, (40, len(audio)))
            start_frame = int(audio.frame_count(ms=20))
            self.assertEqual(exported["segment"].raw_data, audio.raw_data[start_frame * 2 :])
            self.assertEqual(result, destination)


if __name__ == "__main__":
    unittest.main()
