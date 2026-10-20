"""Cleanup failures cannot discard valid paid speech or leak SQLite references."""

import base64
import copy
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import threading
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


class SynthesisCleanupFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        source = Path(directory.name) / "sample.ogg"
        subprocess.run([
            studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=550:duration=0.12:sample_rate=48000", "-ac", "1",
            "-c:a", "libopus", str(source),
        ], check=True, capture_output=True)
        cls.audio = source.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("APP_DATA_DIR", "SESSION_TEMP_DIR"):
            patcher = mock.patch.object(studio, name, self.root / name.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(studio.requests.Session, "post",
                                    side_effect=AssertionError("unmocked HTTP forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def processor(self, backend="json", *, store=None, deferred=False):
        cache = self.root / (backend + "-cache")
        if backend == "json":
            cache.mkdir(exist_ok=True)
            (cache / "sentence_cache.json").write_text("{}")
        cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        cfg.update(input_dir=str(self.root / "input"), output_dir=str(self.root / "output"),
                   cache_dir=str(cache), use_cache=backend != "disabled", normalizer_enabled=False,
                   auto_trim_silence=False, api_time_window=0, api_max_requests=100, max_retries=3,
                   cache_save_frequency=100, output_format="wav", apply_output_tags=False)
        if backend == "sqlite":
            cache.mkdir()
            receipt = studio.prepare_sqlite_cache_migration(cache, confirmed_local_storage=True)
            studio.activate_sqlite_cache_migration(cache, receipt)
        processor = studio.TTSProcessor(cfg, shared_processing_statuses={},
                                        shared_session_audio=store, session_audio_scope="book",
                                        defer_sqlite_open=deferred)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [{"audio": base64.b64encode(self.audio).decode()}]}
        processor.session.post = mock.Mock(return_value=response)
        self.addCleanup(processor.close_cache)
        self.addCleanup(processor.cleanup_transient_audio_files)
        return processor

    @staticmethod
    def copied_audio(source, destination, **_kwargs):
        return shutil.copyfile(source, destination)

    def fail_temp_cleanup(self, candidate, *args, **kwargs):
        if candidate.name.startswith("temp_"):
            raise PermissionError("temporary audio busy")
        return self.original_unlink(candidate, *args, **kwargs)

    def test_paid_audio_survives_cleanup_error_and_next_call_reuses_it(self):
        for backend in ("json", "sqlite", "disabled"):
            with self.subTest(backend=backend):
                store = studio.SessionAudioStore(self.root / "speech") if backend == "disabled" else None
                if store is not None:
                    self.addCleanup(store.close)
                processor = self.processor(backend, store=store)
                self.original_unlink = Path.unlink
                with (
                    mock.patch.object(studio, "_prepare_api_audio_file", side_effect=self.copied_audio),
                    mock.patch.object(Path, "unlink", lambda candidate, *args, **kwargs: self.fail_temp_cleanup(candidate, *args, **kwargs)),
                    self.assertLogs(level="WARNING"),
                ):
                    first, success = processor.synthesize_sentence("Проверенная фраза.", "Проверенная фраза.")
                self.assertTrue(success)
                self.assertEqual(first.read_bytes(), self.audio)
                self.assertEqual(processor.session.post.call_count, 1)
                second, success = processor.synthesize_sentence("Проверенная фраза.", "Проверенная фраза.")
                self.assertTrue(success)
                self.assertEqual(second, first)
                self.assertEqual(processor.session.post.call_count, 1)
                failed = tuple(processor._transient_audio_paths)
                self.assertTrue(failed)
                self.assertTrue(all(path.exists() for path in failed))
                processor.cleanup_transient_audio_files()
                self.assertTrue(all(not path.exists() for path in failed))

    def test_invalid_response_and_cleanup_error_keep_original_failure_without_http_retry(self):
        processor = self.processor()
        self.original_unlink = Path.unlink

        def bad_prepare(source, destination, **_kwargs):
            Path(destination).write_bytes(b"bad")
            raise ValueError("invalid API audio")

        fallback = self.root / "silence.ogg"
        with (
            mock.patch.object(studio, "_prepare_api_audio_file", side_effect=bad_prepare),
            mock.patch.object(Path, "unlink", lambda candidate, *args, **kwargs: self.fail_temp_cleanup(candidate, *args, **kwargs)),
            mock.patch.object(processor, "_get_silence_file", return_value=fallback),
            self.assertLogs(level="WARNING"),
        ):
            path, success = processor.synthesize_sentence("Повреждённый ответ.", "Повреждённый ответ.")
        self.assertEqual(path, fallback)
        self.assertFalse(success)
        self.assertEqual(processor.session.post.call_count, 1)
        self.assertEqual(processor.cache, {})
        self.assertFalse(processor.synthesis_inflight)
        self.assertTrue(processor._transient_audio_paths)

    def test_session_publication_cleanup_does_not_lose_published_owner(self):
        store = studio.SessionAudioStore(self.root / "speech")
        self.addCleanup(store.close)
        prepared = self.root / "prepared.ogg"
        prepared.write_bytes(self.audio)
        original_unlink = Path.unlink

        def unlink(candidate, *args, **kwargs):
            if candidate.suffix == ".tmp":
                raise PermissionError("temporary staging busy")
            return original_unlink(candidate, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink), self.assertLogs(level="WARNING"):
            path = store.publish("phrase", prepared, "book")
        self.assertEqual(path.read_bytes(), self.audio)
        self.assertEqual(store.get("phrase", "book"), path)
        self.assertIn("book", store._owners[path])
        store.discard_scope("book")
        self.assertFalse(path.exists())

    def test_completed_ffmpeg_output_survives_manifest_cleanup_error(self):
        processor = self.processor()
        source = self.root / "source.ogg"
        source.write_bytes(self.audio)
        manifest = self.root / "concat.txt"
        manifest.write_text(f"file '{source}'\n", encoding="utf-8")
        output = self.root / "completed.wav"
        callback = mock.Mock()
        original_unlink = Path.unlink

        def unlink(candidate, *args, **kwargs):
            if candidate == manifest:
                raise PermissionError("manifest busy")
            return original_unlink(candidate, *args, **kwargs)

        with (
            mock.patch.object(studio, "_create_ffmpeg_concat_manifest", return_value=manifest),
            mock.patch.object(Path, "unlink", unlink),
            self.assertLogs(level="WARNING"),
        ):
            processor._merge_save_and_notify((source,), output, output.name, False, callback)
        self.assertTrue(output.is_file())
        callback.assert_called_once_with(output.name, "success", str(output))
        self.assertIn(manifest, processor._transient_audio_paths)
        processor.cleanup_transient_audio_files()
        self.assertFalse(manifest.exists())

    def test_failed_session_publication_retains_cleanup_retry_for_owned_staging(self):
        store = studio.SessionAudioStore(self.root / "speech")
        self.addCleanup(store.close)
        prepared = self.root / "prepared.ogg"
        prepared.write_bytes(self.audio)
        original_unlink = Path.unlink

        def unlink(candidate, *args, **kwargs):
            if candidate.suffix == ".tmp":
                raise PermissionError("staging busy")
            return original_unlink(candidate, *args, **kwargs)

        with (
            mock.patch.object(Path, "unlink", unlink),
            mock.patch.object(studio, "_require_opus_audio_file", side_effect=ValueError("invalid staging")),
            self.assertLogs(level="WARNING"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid staging"):
                store.publish("phrase", prepared, "book")
        self.assertEqual(store._entries, {})
        owned = tuple(store._owners)
        self.assertEqual(len(owned), 1)
        self.assertTrue(owned[0].exists())
        self.assertTrue(store.close())
        self.assertFalse(owned[0].exists())
        self.assertTrue(prepared.exists())

    def test_deferred_open_on_same_processor_acquires_exactly_one_reference(self):
        processor = self.processor("sqlite", deferred=True)
        calls = []
        first_inside = threading.Event()
        second_at_lock = threading.Event()
        release_first = threading.Event()
        actual_acquire = studio.acquire_sqlite_cache_index
        context_lock = threading.Lock()

        def acquire(directory):
            calls.append(directory)
            if len(calls) == 1:
                first_inside.set()
                self.assertTrue(release_first.wait(5))
            return actual_acquire(directory)

        errors = []

        def open_cache():
            try:
                processor._open_sqlite_cache()
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=open_cache)
        second = threading.Thread(target=open_cache)

        class ObservedContextLock:
            def __enter__(self):
                if threading.current_thread() is second:
                    second_at_lock.set()
                context_lock.acquire()

            def __exit__(self, *_args):
                context_lock.release()

        with (
            mock.patch.object(studio, "acquire_sqlite_cache_index", side_effect=acquire),
            mock.patch.object(studio.TTSProcessor, "_sqlite_context_lock", ObservedContextLock()),
        ):
            first.start()
            self.assertTrue(first_inside.wait(5))
            second.start()
            self.assertTrue(second_at_lock.wait(5))
            release_first.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(studio._SQLITE_CACHE_POOL[str(processor.cache.path)]["references"], 1)
        processor.close_cache()
        self.assertNotIn(str(processor.cache.path), studio._SQLITE_CACHE_POOL)


if __name__ == "__main__":
    unittest.main()
