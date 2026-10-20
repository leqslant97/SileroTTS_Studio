"""Эффекты сохраняют частоту источника, точность PCM и совместимость кодеков."""

import array
import gc
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
import warnings

from test_regressions import studio


class AudioEffectsPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        try:
            subprocess.run([studio.get_ffmpeg_path(), "-version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("FFmpeg отсутствует")

    def tone(self, rate, *, duration=2.5, width=2, channels=1):
        path = self.directory / f"tone-{rate}-{width}-{channels}.wav"
        maximum = (1 << (8 * width - 1)) - 1
        samples = bytearray()
        for index in range(round(rate * duration)):
            value = round(maximum * 0.4 * math.sin(2 * math.pi * 327 * index / rate))
            samples.extend(value.to_bytes(width, "little", signed=True) * channels)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(width)
            output.setframerate(rate)
            output.writeframes(samples)
        return path

    def decode_float(self, source, filters):
        return subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(source),
            "-af", filters, "-f", "f32le", "-",
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def test_echo_buffer_preparation_preserves_float_samples(self):
        source = self.tone(48000, duration=0.717, width=3, channels=2)
        # Независимый DSP-эталон до исправления буфера; выход float не
        # ограничивает результат 16 битами и не требует MP3-кодировщика.
        reference = self.decode_float(source,
            "asetrate=52800,atempo=0.90909091,aresample=48000,"
            "atempo=1.25,aecho=0.8:0.8:120:0.2")
        filters = studio._ffmpeg_audio_effect_filters(
            speed=1.25, pitch=1.1, echo=True, echo_delay=120,
            echo_decay=0.2, sample_rate=48000)
        actual = self.decode_float(source, ",".join(filters))
        self.assertEqual(actual, reference)
        self.assertGreater(len(actual), 48000)

    def test_preview_pitch_preserves_rate_duration_and_expected_frequency(self):
        for rate in (44100, 48000, 96000):
            with self.subTest(rate=rate):
                original = studio._load_audio_segment(self.tone(rate))
                processed = studio.AudioEffects.apply_effects(original, pitch=1.1, strict=True)
                self.assertEqual(processed.frame_rate, rate)
                self.assertLess(abs(processed.duration_seconds - original.duration_seconds), 0.08)
                samples = array.array("h", processed[500:2000].get_array_of_samples())
                rising = [index for index in range(1, len(samples))
                          if samples[index-1] <= 0 < samples[index]]
                frequency = (len(rising)-1) * rate / (rising[-1]-rising[0])
                self.assertAlmostEqual(frequency, 327 * 1.1, delta=1.0)

    def test_wav_loading_does_not_leak_file_descriptor(self):
        source = self.tone(48000, duration=0.08)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            segment = studio._load_audio_segment(source)
            del segment
            gc.collect()
        leaked = [warning for warning in caught
                  if issubclass(warning.category, ResourceWarning)]
        self.assertEqual(leaked, [])

    def test_combined_effects_export_decodes_in_all_supported_regular_formats(self):
        source = self.tone(48000, duration=7.182)
        for fmt in ("mp3", "wav", "ogg", "opus", "m4a"):
            with self.subTest(format=fmt):
                target = self.directory / f"processed.{fmt}"
                studio._export_single_audio_ffmpeg(
                    source, target, output_format=fmt, speed=1.25,
                    pitch=1.1, echo=True, echo_delay=120, echo_decay=0.2)
                self.assertTrue(target.is_file())
                decoded = subprocess.run([
                    studio.get_ffmpeg_path(), "-v", "error", "-i", str(target),
                    "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
                ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
                duration = len(decoded) / 2 / 48000
                self.assertAlmostEqual(duration, 7.182 / 1.25 + 0.12, delta=0.15)


if __name__ == "__main__":
    unittest.main()
