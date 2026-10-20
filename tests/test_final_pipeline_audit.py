"""Malformed successful API payloads never repeat a possibly paid synthesis."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_NAME = 'silero_tts_studio_under_test'
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / 'SileroTTS_Studio.py')
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SuccessfulPayloadRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'complete.ogg'
            subprocess.run([
                studio.get_ffmpeg_path(), '-y', '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=frequency=480:duration=0.1:sample_rate=48000',
                '-ar', '48000', '-ac', '1', '-c:a', 'libopus', str(path),
            ], check=True, capture_output=True)
            cls.audio = path.read_bytes()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.http_guard = mock.patch.object(studio.requests.Session, 'post',
                                           side_effect=AssertionError('real HTTP forbidden'))
        self.http_guard.start()
        self.addCleanup(self.http_guard.stop)

    def processor(self, backend):
        cache_dir = self.root / backend / 'cache'
        if backend == 'json':
            cache_dir.mkdir(parents=True)
            (cache_dir / 'sentence_cache.json').write_text('{}')
        if backend == 'sqlite':
            cache_dir.mkdir(parents=True)
            with studio.SQLiteCacheIndex(cache_dir / 'sentence_cache.sqlite') as index:
                index._request('set_meta', 'activation', 'active')
            (cache_dir / 'sentence_cache.backend.json').write_text(json.dumps({
                'schema': 1, 'backend': 'sqlite', 'database': 'sentence_cache.sqlite',
                'storage_policy': 'confirmed_local', 'synchronous': 'FULL',
            }), encoding='utf-8')
        config = copy.deepcopy(studio.DEFAULT_CONFIG)
        config.update(cache_dir=str(cache_dir), input_dir=str(self.root / backend / 'input'),
                      output_dir=str(self.root / backend / 'output'), max_retries=3,
                      use_cache=backend != 'disabled', auto_trim_silence=False,
                      api_max_requests=100, api_time_window=0)
        with mock.patch.object(studio, 'APP_DATA_DIR', self.root / 'data'):
            processor = studio.TTSProcessor(config, shared_processing_statuses={})
        self.addCleanup(processor.close_cache)
        self.addCleanup(processor.cleanup_transient_audio_files)
        processor._get_silence_file = mock.Mock(return_value=None)
        processor._wait_for_retry = mock.Mock(return_value=True)
        return processor

    def response(self, payload=None, *, error=None, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        response.json.side_effect = error
        return response

    def test_malformed_success_payloads_never_retry_or_publish_for_all_backends(self):
        cases = [({}, None), ({'results': []}, None),
                 ({'results': [{'audio': '%%%'}]}, None),
                 (None, json.JSONDecodeError('truncated response', '{', 1)),
                 ({'results': [{'audio': None}]}, None)]
        for backend in ('json', 'sqlite', 'disabled'):
            processor = self.processor(backend)
            for number, (payload, error) in enumerate(cases):
                with self.subTest(backend=backend, case=number):
                    processor.session.post = mock.Mock(return_value=self.response(payload, error=error))
                    self.assertEqual(processor.synthesize_sentence(f'Текст {number}.', f'Текст {number}.'), (None, False))
                    processor.session.post.assert_called_once()
                    self.assertEqual(processor.cache.count() if backend == 'sqlite' else len(processor.cache), 0)
                    self.assertFalse(list(processor.cache_audio_dir.glob('temp_*.ogg')))
                    self.assertFalse(list(processor.cache_audio_dir.glob('prepared_*.ogg')))
            processor._wait_for_retry.assert_not_called()

    def test_extra_base64_characters_cannot_publish_valid_audio_with_disabled_cache(self):
        processor = self.processor('disabled')
        encoded = base64.b64encode(self.audio).decode('ascii')
        polluted = encoded[:8] + '%' + encoded[8:]
        processor.session.post = mock.Mock(return_value=self.response({'results': [{'audio': polluted}]}))
        self.assertEqual(processor.synthesize_sentence('Ответ.', 'Ответ.'), (None, False))
        processor.session.post.assert_called_once()
        self.assertEqual(processor._transient_audio_paths, set())
        processor._wait_for_retry.assert_not_called()

    def test_failed_force_new_payload_preserves_existing_audio_and_metadata(self):
        for backend in ('json', 'sqlite'):
            with self.subTest(backend=backend):
                processor = self.processor(backend)
                text = 'Существующая запись.'
                key = processor.get_hash(text)
                path = processor.cache_audio_dir / (key + '.ogg')
                path.write_bytes(self.audio)
                metadata = {'file_name': path.name, 'speaker': processor.cfg['speaker'],
                            'audio_codec': 'opus', 'usage_count': 7, 'last_accessed': 12}
                if backend == 'sqlite':
                    processor.cache.upsert(key, metadata)
                    processor.cache.flush()
                else:
                    processor.cache[key] = copy.deepcopy(metadata)
                processor.session.post = mock.Mock(return_value=self.response({'results': [{}]}))
                self.assertEqual(processor.synthesize_sentence(text, text, force_new=True), (None, False))
                self.assertEqual(processor.cache.get(key), metadata)
                self.assertEqual(path.read_bytes(), self.audio)
                processor.session.post.assert_called_once()

    def test_network_and_non_success_http_errors_keep_retry_policy(self):
        for first_failure in ('network', 'http'):
            with self.subTest(first_failure=first_failure):
                processor = self.processor('disabled')
                success = self.response({'results': [{'audio': base64.b64encode(self.audio).decode('ascii')}]})
                if first_failure == 'network':
                    failure = studio.requests.exceptions.ConnectionError('connection interrupted')
                else:
                    failure = self.response(status=503)
                    failure.raise_for_status.side_effect = studio.requests.exceptions.HTTPError('503', response=failure)
                processor.session.post = mock.Mock(side_effect=[failure, success])
                path, completed = processor.synthesize_sentence('Повтор сети.', 'Повтор сети.')
                self.assertTrue(completed)
                self.assertTrue(path.is_file())
                self.assertEqual(processor.session.post.call_count, 2)
                processor._wait_for_retry.assert_called_once()


class AudioTempoBoundaryTests(unittest.TestCase):
    def test_nonfinite_tempo_is_rejected_before_an_unbounded_filter_loop(self):
        for factor in (float('inf'), float('-inf'), float('nan')):
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                studio.AudioEffects._atempo_filters(factor)

    def test_valid_extreme_factors_keep_each_link_in_the_supported_range(self):
        for factor in (.125, .5, 1, 2, 8):
            with self.subTest(factor=factor):
                filters = studio.AudioEffects._atempo_filters(factor)
                reconstructed = 1.0
                for expression in filters:
                    value = float(expression.split('=', 1)[1])
                    self.assertGreaterEqual(value, .5)
                    self.assertLessEqual(value, 2)
                    reconstructed *= value
                self.assertAlmostEqual(reconstructed, factor)

    def test_rejected_tempo_does_not_leave_cover_metadata_or_replace_output(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / 'source.ogg'
            subprocess.run([
                studio.get_ffmpeg_path(), '-y', '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=duration=0.05:sample_rate=48000', '-ac', '1',
                '-c:a', 'libopus', str(source),
            ], check=True, capture_output=True)
            cover = work / 'cover.png'
            Image.new('RGB', (8, 8), 'red').save(cover)
            output = work / 'existing.opus'
            output.write_bytes(b'previous complete output')
            metadata_dir = work / 'operation_temporary'
            with mock.patch.object(studio, 'SESSION_TEMP_DIR', metadata_dir):
                for factor in (0, float('inf')):
                    with self.subTest(factor=factor), self.assertRaises(ValueError):
                        studio._export_merged_audio_ffmpeg(
                            [source], output, output_format='opus', cover=cover, speed=factor)
                    self.assertEqual(output.read_bytes(), b'previous complete output')
                    self.assertFalse(list(metadata_dir.glob('*.ffmeta')))
                    self.assertFalse(list(work.glob('.audio_*.tmp*')))


if __name__ == '__main__':
    unittest.main()
