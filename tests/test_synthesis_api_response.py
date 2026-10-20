"""Полнота ответов API, восстановление старого кэша и эквивалентная обрезка."""

import base64
import copy
import importlib.util
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydub import AudioSegment
from pydub.generators import Sine
from pydub.silence import detect_nonsilent


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


def pages(data):
    result = []
    offset = 0
    while offset < len(data):
        table_end = offset + 27 + data[offset + 26]
        page_end = table_end + sum(data[offset + 27:table_end])
        result.append(data[offset:page_end])
        offset = page_end
    return result


def reference_ogg_crc(data):
    checksum = 0
    for value in data:
        checksum ^= value << 24
        for _ in range(8):
            checksum = ((checksum << 1) ^ (0x04C11DB7 if checksum & 0x80000000 else 0)) & 0xFFFFFFFF
    return checksum


def update_page_checksum(data, offset):
    data = bytearray(data)
    table_end = offset + 27 + data[offset + 26]
    page_end = table_end + sum(data[offset + 27:table_end])
    data[offset + 22:offset + 26] = b"\x00" * 4
    checksum = reference_ogg_crc(data[offset:page_end])
    data[offset + 22:offset + 26] = checksum.to_bytes(4, "little")
    return bytes(data)


class ApiAudioCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.samples = {}
        for name, codec, channels in (("mono", "libopus", 1),
                                      ("stereo", "libopus", 2),
                                      ("vorbis", "libvorbis", 1)):
            path = cls.root / (name + ".ogg")
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=420:duration=0.25:sample_rate=48000",
                "-ar", "48000", "-ac", str(channels), "-c:a", codec, str(path),
            ], check=True, capture_output=True)
            cls.samples[name] = path.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def test_canonical_api_answer_is_copied_without_decoding_or_reencoding(self):
        source = self.work / "answer.ogg"
        target = self.work / "ready.ogg"
        source.write_bytes(self.samples["mono"])
        with mock.patch.object(studio, "_load_audio_segment") as decode:
            studio._prepare_api_audio_file(source, target, trim_silence=False)
        decode.assert_not_called()
        self.assertEqual(target.read_bytes(), source.read_bytes())
        studio._validate_complete_ogg_audio_file(target)

    def test_stereo_opus_and_vorbis_answers_become_complete_canonical_mono(self):
        for name in ("stereo", "vorbis"):
            with self.subTest(name=name):
                source = self.work / (name + ".ogg")
                target = self.work / (name + "-ready.ogg")
                source.write_bytes(self.samples[name])
                studio._prepare_api_audio_file(source, target, trim_silence=False)
                self.assertEqual(studio._inspect_ogg_audio_header(target), ("opus", 1))
                studio._validate_complete_ogg_audio_file(target)
                subprocess.run([
                    studio.get_ffmpeg_path(), "-v", "error", "-i", str(target),
                    "-f", "null", "-",
                ], check=True, capture_output=True)

    def test_valid_subset_channel_mapping_keeps_all_decodable_audio(self):
        stereo_pages = pages(self.samples["stereo"])
        first = stereo_pages[0]
        table_end = 27 + first[26]
        identity = bytearray(first[table_end:])
        self.assertEqual(len(identity), 19)
        identity[9] = 1
        identity[18] = 255
        # Выход использует первый канал корректного coupled stereo stream.
        # Закодированных каналов два, выходной канал один; это допустимый Opus.
        identity.extend(b"\x01\x01\x00")
        replacement = first[:27] + bytes([len(identity)]) + identity
        answer = update_page_checksum(replacement, 0) + b"".join(stereo_pages[1:])
        source = self.work / "subset-mapping.ogg"
        target = self.work / "subset-ready.ogg"
        source.write_bytes(answer)
        studio._prepare_api_audio_file(source, target, trim_silence=False)
        decoded = subprocess.run([
            studio.get_ffmpeg_path(), "-v", "error", "-i", str(target),
            "-ar", "48000", "-ac", "1", "-f", "s16le", "-",
        ], check=True, capture_output=True)
        self.assertEqual(len(decoded.stdout) // 2, 12000)
        self.assertEqual(decoded.stderr, b"")
        self.assertEqual(target.read_bytes(), answer)

    def test_chained_codec_or_channel_changes_are_rejected_before_publication(self):
        for next_stream in ("mono", "stereo", "vorbis"):
            for trim in (False, True):
                with self.subTest(next_stream=next_stream, trim=trim):
                    source = self.work / "chained.ogg"
                    source.write_bytes(self.samples["mono"] + self.samples[next_stream])
                    target = self.work / "old.ogg"
                    target.write_bytes(b"previous complete output")
                    with self.assertRaises(ValueError):
                        studio._prepare_api_audio_file(source, target, trim_silence=trim)
                    self.assertEqual(target.read_bytes(), b"previous complete output")

    def test_incomplete_answers_never_replace_existing_destination(self):
        complete_pages = pages(self.samples["mono"])
        headers_with_eos = bytearray(b"".join(complete_pages[:2]))
        headers_with_eos[len(complete_pages[0]) + 5] |= 4
        headers_with_eos = update_page_checksum(headers_with_eos, len(complete_pages[0]))
        missing_eos = bytearray(self.samples["mono"])
        missing_eos[len(b"".join(complete_pages[:-1])) + 5] &= ~4
        missing_eos = update_page_checksum(missing_eos, len(b"".join(complete_pages[:-1])))
        no_decoded_audio = bytearray(self.samples["mono"])
        last_page_start = len(b"".join(complete_pages[:-1]))
        no_decoded_audio[last_page_start + 6:last_page_start + 14] = b"\x00" * 8
        no_decoded_audio = update_page_checksum(no_decoded_audio, last_page_start)
        corrupt_audio = bytearray(self.samples["mono"])
        corrupt_audio[-1] ^= 1
        variants = {
            "header_only": complete_pages[0],
            "headers_with_eos_but_no_audio": bytes(headers_with_eos),
            "truncated_last_payload": self.samples["mono"][:-1],
            "missing_eos": bytes(missing_eos),
            "eos_without_decoded_audio": bytes(no_decoded_audio),
            "payload_bitflip": bytes(corrupt_audio),
        }
        for name, data in variants.items():
            for trim in (False, True):
                with self.subTest(name=name, trim=trim):
                    source = self.work / "broken.ogg"
                    target = self.work / "existing.ogg"
                    source.write_bytes(data)
                    target.write_bytes(b"previous complete destination")
                    with self.assertRaises(ValueError):
                        studio._prepare_api_audio_file(source, target, trim_silence=trim)
                    self.assertEqual(target.read_bytes(), b"previous complete destination")
                    self.assertFalse(list(self.work.glob(".*.tmp.ogg")))

    def test_wrong_page_version_sequence_and_incomplete_lacing_are_rejected(self):
        complete_pages = pages(self.samples["mono"])
        variants = []
        version = bytearray(self.samples["mono"])
        version[4] = 1
        variants.append(version)
        sequence = bytearray(self.samples["mono"])
        sequence[len(complete_pages[0]) + 18] = 2
        variants.append(update_page_checksum(sequence, len(complete_pages[0])))
        variants.append(complete_pages[0][:27])
        for data in variants:
            source = self.work / "invalid.ogg"
            source.write_bytes(data)
            with self.assertRaises(ValueError):
                studio._validate_complete_ogg_audio_file(source)

    def test_crc_matches_independent_polynomial_on_real_and_random_pages(self):
        rng = random.Random(3107)
        for length in (0, 1, 7, 27, 255, 1024, 4096):
            data = rng.randbytes(length)
            self.assertEqual(studio._ogg_page_checksum(data), reference_ogg_crc(data))
        for sample in self.samples.values():
            for page in pages(sample):
                expected = int.from_bytes(page[22:26], "little")
                zeroed = page[:22] + b"\x00" * 4 + page[26:]
                self.assertEqual(reference_ogg_crc(zeroed), expected)
                self.assertEqual(studio._ogg_page_checksum(zeroed), expected)

    def processor(self, use_cache=True):
        cache = self.work / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "sentence_cache.json").write_text("{}")
        config = copy.deepcopy(studio.DEFAULT_CONFIG)
        config.update({
            "input_dir": str(self.work / "input"),
            "output_dir": str(self.work / "output"),
            "cache_dir": str(self.work / "cache"),
            "auto_trim_silence": False, "use_cache": use_cache,
            "api_max_requests": 100, "api_time_window": 0, "max_retries": 1,
        })
        with mock.patch.object(studio, "APP_DATA_DIR", self.work / "data"):
            processor = studio.TTSProcessor(config, shared_cache={}, shared_processing_statuses={})
        self.addCleanup(processor.cleanup_transient_audio_files)
        fallback = self.work / "fallback.ogg"
        fallback.write_bytes(self.samples["mono"])
        processor._get_silence_file = mock.Mock(return_value=fallback)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [{
            "audio": base64.b64encode(self.samples["mono"]).decode("ascii"),
        }]}
        processor.session.post = mock.Mock(return_value=response)
        return processor, response

    def test_preexisting_header_only_cache_fragment_is_requested_again(self):
        processor, _response = self.processor()
        text = "Старый повреждённый фрагмент."
        key = processor.get_hash(text)
        cache_file = processor.cache_audio_dir / (key + ".ogg")
        cache_file.write_bytes(pages(self.samples["mono"])[0])
        processor.cache[key] = {"file_name": cache_file.name, "audio_codec": "opus"}
        result, success = processor.synthesize_sentence(text, text)
        self.assertTrue(success)
        self.assertEqual(result, cache_file)
        self.assertEqual(cache_file.read_bytes(), self.samples["mono"])
        processor.session.post.assert_called_once()

    def test_malformed_new_response_does_not_replace_valid_old_cache(self):
        processor, response = self.processor()
        text = "Сохраняем старый исправный звук."
        key = processor.get_hash(text)
        cache_file = processor.cache_audio_dir / (key + ".ogg")
        cache_file.write_bytes(self.samples["mono"])
        old_info = {"file_name": cache_file.name, "audio_codec": "opus", "usage_count": 3}
        processor.cache[key] = old_info.copy()
        response.json.return_value = {"results": [{
            "audio": base64.b64encode(pages(self.samples["mono"])[0]).decode("ascii"),
        }]}
        _result, success = processor.synthesize_sentence(text, text, force_new=True)
        self.assertFalse(success)
        self.assertEqual(cache_file.read_bytes(), self.samples["mono"])
        self.assertEqual(processor.cache[key], old_info)
        self.assertEqual(list(processor.cache_audio_dir.glob("temp_*.ogg")), [])
        self.assertEqual(list(processor.cache_audio_dir.glob("prepared_*.ogg")), [])

    def test_malformed_answer_is_not_published_with_disabled_cache(self):
        processor, response = self.processor(use_cache=False)
        response.json.return_value = {"results": [{
            "audio": base64.b64encode(pages(self.samples["mono"])[0]).decode("ascii"),
        }]}
        _result, success = processor.synthesize_sentence("Без кэша.", "Без кэша.")
        self.assertFalse(success)
        self.assertEqual(processor.cache, {})
        self.assertEqual(processor._transient_audio_paths, set())

    def test_repeat_cache_hit_skips_complete_read_but_replacement_is_revalidated(self):
        path = self.work / "cached.ogg"
        path.write_bytes(self.samples["mono"])
        original_validation = studio._validate_complete_ogg_audio_file
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio, "_validate_complete_ogg_audio_file", wraps=original_validation) as validate:
            studio._canonicalize_cached_audio_if_needed(path)
            studio._canonicalize_cached_audio_if_needed(path)
            self.assertEqual(validate.call_count, 1)
            original_stat = path.stat()
            replacement = self.work / "replacement.ogg"
            replacement.write_bytes(self.samples["mono"])
            os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            os.replace(replacement, path)
            studio._canonicalize_cached_audio_if_needed(path)
            self.assertEqual(validate.call_count, 2)
            path.write_bytes(pages(self.samples["mono"])[0])
            with self.assertRaises(ValueError):
                studio._canonicalize_cached_audio_if_needed(path)
            self.assertEqual(validate.call_count, 3)

    def test_successful_new_publication_is_already_validated_on_next_hit(self):
        processor, _response = self.processor()
        text = "Новый исправный фрагмент."
        processor.synthesize_sentence(text, text)
        with mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                               wraps=studio._validate_complete_ogg_audio_file) as validate:
            result, success = processor.synthesize_sentence(text, text)
        self.assertTrue(success)
        self.assertTrue(result.is_file())
        self.assertEqual(validate.call_count, 1 if studio.platform.system() == "Windows" else 0)
        processor.session.post.assert_called_once()

    def test_retargeted_audio_symlink_revalidates_same_size_and_mtime(self):
        good = self.work / "good.ogg"
        bad = self.work / "bad.ogg"
        alias = self.work / "alias.ogg"
        good.write_bytes(self.samples["mono"])
        damaged = bytearray(self.samples["mono"])
        damaged[-1] ^= 1
        bad.write_bytes(damaged)
        good_stat = good.stat()
        os.utime(bad, ns=(good_stat.st_atime_ns, good_stat.st_mtime_ns))
        try:
            alias.symlink_to(good)
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")
        validation = studio._validate_complete_ogg_audio_file
        with mock.patch.object(studio, "_validate_complete_ogg_audio_file", wraps=validation) as validate:
            studio._validate_cached_ogg_audio_file(alias)
            studio._validate_cached_ogg_audio_file(alias)
            self.assertEqual(validate.call_count, 2 if studio.platform.system() == "Windows" else 1)
            alias.unlink()
            alias.symlink_to(bad)
            with self.assertRaises(ValueError):
                studio._validate_cached_ogg_audio_file(alias)
            self.assertEqual(validate.call_count, 3 if studio.platform.system() == "Windows" else 2)
        self.assertEqual(good.read_bytes(), self.samples["mono"])

    def test_lexical_path_alias_cannot_reuse_validation_for_different_inode(self):
        real = self.work / "real"
        nested = real / "nested"
        nested.mkdir(parents=True)
        good = real / "fragment.ogg"
        bad = self.work / "fragment.ogg"
        good.write_bytes(self.samples["mono"])
        damaged = bytearray(self.samples["mono"])
        damaged[-1] ^= 1
        bad.write_bytes(damaged)
        alias = self.work / "link"
        try:
            alias.symlink_to(nested, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")
        candidate = alias / ".." / "fragment.ogg"
        # POSIX проходит по ссылке до «..», Windows нормализует такой путь
        # лексически. Проверяем содержимое файла, который реально открывает ОС.
        if candidate.samefile(good):
            studio._validate_cached_ogg_audio_file(candidate)
        else:
            with self.assertRaises(ValueError):
                studio._validate_cached_ogg_audio_file(candidate)
            studio._validate_cached_ogg_audio_file(good)
        with self.assertRaises(ValueError):
            studio._validate_cached_ogg_audio_file(bad)

    def test_validation_lru_is_bounded_and_concurrent_hits_are_safe(self):
        paths = []
        for number in range(6):
            path = self.work / f"fragment-{number}.ogg"
            path.write_bytes(self.samples["mono"])
            paths.append(path)
        with mock.patch.object(studio, "_OGG_VALIDATION_CACHE", {}), \
             mock.patch.object(studio, "_OGG_VALIDATION_CACHE_LIMIT", 4):
            for path in paths:
                studio._validate_cached_ogg_audio_file(path)
            self.assertEqual(len(studio._OGG_VALIDATION_CACHE), 4)
            with studio.concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(studio._validate_cached_ogg_audio_file, paths * 5))
            self.assertEqual(results, paths * 5)
            self.assertLessEqual(len(studio._OGG_VALIDATION_CACHE), 4)


class AudioTrimBoundsTests(unittest.TestCase):
    def expected(self, audio, threshold):
        ranges = detect_nonsilent(audio, min_silence_len=50, silence_thresh=threshold)
        return (ranges[0][0], ranges[-1][1]) if ranges else None

    def test_short_silent_and_internal_pause_cases_preserve_pydub_bounds(self):
        silence = AudioSegment.silent(duration=100, frame_rate=48000)
        voice = Sine(420).to_audio_segment(duration=110, volume=-12).set_frame_rate(48000)
        samples = [silence[:20], silence, voice, silence + voice + silence,
                   voice + silence * 4 + voice, silence[:49] + voice + silence[:49]]
        for audio in samples:
            with self.subTest(duration=len(audio)):
                self.assertEqual(studio._detect_audio_trim_bounds(audio), self.expected(audio, -55))

    def test_random_differential_preserves_overlap_and_threshold_rules(self):
        rng = random.Random(19027)
        for number in range(400):
            audio = AudioSegment.empty()
            for _ in range(rng.randint(1,8)):
                duration = rng.randint(1,110)
                if rng.random() < .6:
                    piece = AudioSegment.silent(duration=duration, frame_rate=48000)
                else:
                    piece = Sine(300).to_audio_segment(
                        duration=duration, volume=rng.choice([-25,-53,-54.5,-55,-55.5,-60])
                    )
                audio += piece
            threshold = rng.choice([-55, -54, -60])
            self.assertEqual(studio._detect_audio_trim_bounds(audio, silence_threshold=threshold),
                             self.expected(audio, threshold), f"Waveform case {number}")

    def test_long_voice_checks_only_the_ends(self):
        audio = Sine(420).to_audio_segment(duration=30000, volume=-12)
        rms = AudioSegment.rms.fget
        with mock.patch.object(AudioSegment, "rms", new_callable=mock.PropertyMock,
                               side_effect=lambda: rms(audio[:50])) as inspect:
            self.assertEqual(studio._detect_audio_trim_bounds(audio), (0, len(audio)))
        self.assertEqual(inspect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
