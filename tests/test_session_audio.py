"""Временная речь сохраняется для продолжения и безопасно освобождается."""

import base64
import copy
import importlib.util
import shutil
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


def opus_header(contents=b""):
    """Создаёт первую страницу с полноценным идентификационным пакетом Opus."""
    packet = b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
    return b"OggS\x00\x02" + b"\x00" * 20 + b"\x01" + bytes([len(packet)]) + packet + contents


class SessionAudioStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = studio.SessionAudioStore(self.root / "speech")
        self.addCleanup(self.store.close)
        self.number = 0

    def publish(self, key="phrase", scope="book", contents=b"first"):
        self.number += 1
        prepared = self.root / f"prepared-{self.number}.ogg"
        prepared.write_bytes(opus_header(contents))
        return self.store.publish(key, prepared, scope)

    def test_config_key_uses_effective_defaults(self):
        self.assertEqual(
            self.store.config_key({}),
            self.store.config_key(studio.DEFAULT_CONFIG),
        )

    def test_scopes_share_audio_until_last_owner_is_discarded(self):
        path = self.publish()
        self.assertEqual(self.store.get("phrase", "direct"), path)
        self.assertTrue(self.store.discard_scope("book"))
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.get("phrase", "direct"), path)
        self.assertTrue(self.store.discard_scope("direct"))
        self.assertFalse(path.exists())

    def test_scope_discard_waits_for_all_active_consumers(self):
        self.store.acquire("book")
        self.store.acquire("direct")
        path = self.publish()
        self.assertFalse(self.store.discard_scope("book"))
        self.assertTrue(path.is_file())
        self.store.release("book")
        self.assertTrue(path.is_file())
        self.store.release("direct")
        self.assertFalse(path.exists())

    def test_late_publication_does_not_revive_discarded_active_scope(self):
        self.store.acquire("book")
        self.store.discard_scope("book")
        path = self.publish()
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.get("phrase", "book"), path)
        self.store.release("book")
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.get("phrase", "book"))

    def test_new_run_can_revive_previously_discarded_scope(self):
        self.store.acquire("book")
        self.store.acquire("direct")
        self.store.discard_scope("book")
        self.store.release("book")
        self.store.acquire("book")
        path = self.publish()
        self.store.release("book")
        self.store.release("direct")
        self.assertTrue(path.is_file())
        self.store.discard_scope("book")
        self.assertFalse(path.exists())

    def test_replacement_keeps_old_path_until_consumers_finish(self):
        self.store.acquire("book")
        first = self.publish(contents=b"first")
        self.store.get("phrase", "other-book")
        second = self.publish(contents=b"second")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), opus_header(b"first"))
        self.assertEqual(self.store.get("phrase", "book"), second)
        self.store.release("book")
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.store.discard_scope("book")
        self.assertEqual(self.store.get("phrase", "other-book"), second)

    def test_bad_replacement_preserves_previous_valid_audio(self):
        first = self.publish()
        prepared = self.root / "bad.ogg"
        prepared.write_bytes(b"broken audio")
        with self.assertRaises(ValueError):
            self.store.publish("phrase", prepared, "book")
        self.assertEqual(self.store.get("phrase", "book"), first)
        self.assertEqual(list(self.store.directory.iterdir()), [first])
        self.assertTrue(prepared.exists())

    def test_discarded_waiting_scope_is_not_restored_by_late_publication(self):
        self.store.acquire("book")
        self.store.acquire("direct")
        token = self.store.register_request("phrase", "direct")
        self.store.discard_scope("direct")
        path = self.publish()
        self.store.release_request("phrase", token)
        self.store.discard_scope("book")
        self.store.release("book")
        self.store.release("direct")
        self.assertFalse(path.exists())

    def test_cancelled_wait_does_not_keep_future_publication_alive(self):
        self.store.acquire("book")
        self.store.acquire("direct")
        token = self.store.register_request("phrase", "direct")
        self.store.release_request("phrase", token)
        path = self.publish()
        self.store.discard_scope("book")
        self.store.release("book")
        self.store.release("direct")
        self.assertFalse(path.exists())

    def test_publication_only_renames_files_within_session_directory(self):
        replacements = []
        original_replace = studio.os.replace

        def local_replace(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(source.parent, self.store.directory)
            self.assertEqual(destination.parent, self.store.directory)
            replacements.append((source, destination))
            return original_replace(source, destination)

        with mock.patch.object(studio.os, "replace", side_effect=local_replace):
            path = self.publish()
        self.assertEqual(len(replacements), 1)
        self.assertTrue(path.is_file())
        self.assertEqual(list(self.store.directory.iterdir()), [path])

    def test_close_deletes_only_owned_audio(self):
        path = self.publish()
        foreign = self.store.directory / "foreign.ogg"
        foreign.write_bytes(b"unrelated")
        self.assertTrue(self.store.close())
        self.assertFalse(path.exists())
        self.assertEqual(foreign.read_bytes(), b"unrelated")
        with self.assertRaises(RuntimeError):
            self.store.acquire("next-book")

    def test_close_waits_for_active_request_and_consumers(self):
        self.store.acquire("book")
        first = self.publish()
        self.assertFalse(self.store.close())
        self.assertTrue(first.is_file())
        late = self.publish(key="late", contents=b"late")
        self.assertTrue(late.is_file())
        self.assertIsNone(self.store.get("late", "book"))
        self.store.release("book")
        self.assertFalse(first.exists())
        self.assertFalse(late.exists())

    def test_cleanup_failure_can_be_retried(self):
        path = self.publish()
        original_unlink = Path.unlink

        def denied_unlink(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("Файл занят")
            return original_unlink(candidate, *args, **kwargs)

        with mock.patch.object(Path, "unlink", denied_unlink):
            self.assertFalse(self.store.close())
        self.assertTrue(path.is_file())
        self.assertTrue(self.store.close())
        self.assertFalse(path.exists())


class SessionAudioProcessorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = studio.SessionAudioStore(self.root / "speech")
        self.addCleanup(self.store.close)
        self.processors = []
        self.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.config.update({
            "input_dir": str(self.root / "input"),
            "output_dir": str(self.root / "output"),
            "cache_dir": str(self.root / "cache"),
            "api_url": "https://offline.invalid/api", "api_token": "",
            "use_cache": False, "normalizer_enabled": False,
            "glossary_enabled": False, "auto_trim_silence": False,
            "api_steps_enabled": False, "synthesis_mode": "sentence",
            "max_retries": 1, "api_max_requests": 100,
            "api_time_window": 0, "pause_file_start": 0,
            "pause_file_end": 0, "pause_sentence": 0,
            "pause_paragraph": 0, "pause_speech": 0,
            "pause_colon": 0, "pause_separator": 0,
        })
        for name, replacement in (
            ("APP_DATA_DIR", self.root / "data"),
            ("SESSION_TEMP_DIR", self.root / "temporary"),
            ("_prepare_api_audio_file", mock.Mock(side_effect=lambda source, destination, **kwargs: shutil.copyfile(source, destination))),
        ):
            patcher = mock.patch.object(studio, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_processor(self, *, scope="book", store=True, **overrides):
        config = self.config.copy()
        config.update(overrides)
        processor = studio.TTSProcessor(
            config,
            shared_session_audio=self.store if store else None,
            session_audio_scope=scope,
        )
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [{
            "audio": base64.b64encode(opus_header(b"response")).decode(),
        }]}
        processor.session.post = mock.Mock(return_value=response)
        self.processors.append(processor)
        self.addCleanup(processor.session.close)
        self.addCleanup(processor.cleanup_transient_audio_files)
        return processor

    def synthesize(self, processor, **kwargs):
        return processor.synthesize_sentence("Готовая фраза.", "Готовая фраза.", **kwargs)

    def test_stop_and_new_processor_reuse_speech_without_post(self):
        first = self.make_processor()
        path, success = self.synthesize(first)
        self.assertTrue(success)
        first.is_stopped = True
        first.cleanup_transient_audio_files()
        self.assertTrue(path.is_file())
        second = self.make_processor()
        seen = []
        resumed, success = self.synthesize(second, request_callback=seen.append)
        self.assertTrue(success)
        self.assertEqual(resumed, path)
        second.session.post.assert_not_called()
        self.assertEqual(seen, [])

    def test_late_successful_answer_survives_stop(self):
        first = self.make_processor()
        response = first.session.post.return_value

        def stop_before_answer(*args, **kwargs):
            first.is_stopped = True
            return response

        first.session.post.side_effect = stop_before_answer
        result = first.process_raw_text(
            "Готовая фраза.", "chapter.mp3", save_to_disk=False,
            return_audio_files=True,
        )
        self.assertEqual(result["status"], "error")
        first.cleanup_transient_audio_files()
        second = self.make_processor()
        path, success = self.synthesize(second)
        self.assertTrue(success)
        self.assertTrue(path.is_file())
        second.session.post.assert_not_called()

    def test_session_speech_does_not_enter_persistent_cache(self):
        processor = self.make_processor()
        path, success = self.synthesize(processor)
        self.assertTrue(success)
        self.assertEqual(path.parent, self.store.directory)
        self.assertEqual(processor.cache, {})
        self.assertEqual(processor._transient_audio_paths, set())
        self.assertTrue(processor.flush_cache())
        self.assertFalse(processor.cache_index_path.exists())
        self.assertEqual(list(processor.cache_audio_dir.iterdir()), [])

    def test_disabled_cache_does_not_use_existing_persistent_speech(self):
        processor = self.make_processor()
        key = processor.get_hash("Готовая фраза.")
        persistent = processor.cache_audio_dir / f"{key}.ogg"
        persistent.write_bytes(opus_header(b"persistent"))
        processor.cache[key] = {"file_name": persistent.name}
        path, success = self.synthesize(processor)
        self.assertTrue(success)
        self.assertNotEqual(path, persistent)
        self.assertEqual(persistent.read_bytes(), opus_header(b"persistent"))
        processor.session.post.assert_called_once()
        self.assertEqual(processor.cache, {key: {"file_name": persistent.name}})

    def test_standalone_processor_keeps_previous_cleanup_behavior(self):
        processor = self.make_processor(store=False)
        path, success = self.synthesize(processor)
        self.assertTrue(success)
        self.assertIn(path, processor._transient_audio_paths)
        processor.cleanup_transient_audio_files()
        self.assertFalse(path.exists())

    def test_missing_fragment_is_requested_again(self):
        processor = self.make_processor()
        first, _success = self.synthesize(processor)
        first.unlink()
        second, success = self.synthesize(processor)
        self.assertTrue(success)
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_file())
        self.assertEqual(processor.session.post.call_count, 2)

    def test_corrupted_fragment_is_requested_again(self):
        processor = self.make_processor()
        first, _success = self.synthesize(processor)
        first.write_bytes(b"broken audio")
        second, success = self.synthesize(processor)
        self.assertTrue(success)
        self.assertNotEqual(first, second)
        self.assertEqual(processor.session.post.call_count, 2)
        self.assertEqual(second.read_bytes(), opus_header(b"response"))

    def test_force_new_keeps_path_used_by_another_processor(self):
        first = self.make_processor(scope="book")
        original, _success = self.synthesize(first)
        second = self.make_processor(scope="direct")
        self.assertEqual(self.synthesize(second)[0], original)
        replacement, success = self.synthesize(second, force_new=True)
        self.assertTrue(success)
        self.assertNotEqual(original, replacement)
        self.assertTrue(original.is_file())
        first.cleanup_transient_audio_files()
        self.assertTrue(original.is_file())
        second.cleanup_transient_audio_files()
        self.assertFalse(original.exists())
        self.store.discard_scope("direct")
        resumed = self.make_processor(scope="book")
        self.assertEqual(self.synthesize(resumed)[0], replacement)
        resumed.session.post.assert_not_called()

    def test_actual_steps_are_separate_even_with_common_persistent_key(self):
        first = self.make_processor(api_steps_enabled=True, api_steps=16, cache_include_steps=False)
        original, _success = self.synthesize(first)
        second = self.make_processor(api_steps_enabled=True, api_steps=20, cache_include_steps=False)
        replacement, success = self.synthesize(second)
        self.assertTrue(success)
        self.assertNotEqual(original, replacement)
        second.session.post.assert_called_once()

    def test_voice_server_and_trimming_changes_require_new_speech(self):
        first = self.make_processor()
        original, _success = self.synthesize(first)
        for overrides in (
            {"speaker": "other-voice"},
            {"api_url": "https://other.invalid/api"},
            {"auto_trim_silence": True},
            {"auto_trim_silence": True, "silence_threshold": -30},
        ):
            with self.subTest(overrides=overrides):
                processor = self.make_processor(**overrides)
                replacement, success = self.synthesize(processor)
                self.assertTrue(success)
                self.assertNotEqual(original, replacement)
                processor.session.post.assert_called_once()

    def test_unused_trimming_threshold_does_not_trigger_new_request(self):
        first = self.make_processor()
        original, _success = self.synthesize(first)
        second = self.make_processor(silence_threshold=-30)
        self.assertEqual(self.synthesize(second)[0], original)
        second.session.post.assert_not_called()

    def test_concurrent_processors_make_one_request(self):
        first, second = self.make_processor(scope="book"), self.make_processor(scope="direct")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        response = first.session.post.return_value
        results, failures = [], []

        def blocked_post(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Ответ не освобождён")
            return response

        first.session.post.side_effect = blocked_post

        def run(processor):
            try:
                results.append(self.synthesize(processor))
            except BaseException as exc:
                failures.append(exc)

        one, two = threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))
        one.start()
        try:
            self.assertTrue(entered.wait(5))
            two.start()
        finally:
            release.set()
            one.join(5)
            if two.ident is not None:
                two.join(5)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        first.session.post.assert_called_once()
        second.session.post.assert_not_called()
        self.assertEqual(self.store.inflight, {})

    def test_waiting_processor_reuses_speech_after_publisher_finishes(self):
        first = self.make_processor(scope="book")
        second = self.make_processor(scope="direct")
        entered, release = threading.Event(), threading.Event()
        waiting, continue_waiter = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(continue_waiter.set)
        response = first.session.post.return_value
        results, failures = {}, []

        def blocked_post(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Ответ не освобождён")
            return response

        first.session.post.side_effect = blocked_post

        def run(processor, name):
            try:
                results[name] = self.synthesize(processor)
            except BaseException as exc:
                failures.append(exc)

        one = threading.Thread(target=run, args=(first, "book"))
        two = threading.Thread(target=run, args=(second, "direct"))
        one.start()
        try:
            self.assertTrue(entered.wait(5))
            event = next(iter(self.store.inflight.values()))
            original_wait = event.wait

            def delayed_wait(timeout):
                waiting.set()
                result = original_wait(timeout)
                if not continue_waiter.wait(5):
                    raise AssertionError("Ожидающий процессор не освобождён")
                return result

            with mock.patch.object(event, "wait", side_effect=delayed_wait):
                two.start()
                self.assertTrue(waiting.wait(5))
                release.set()
                one.join(5)
                self.assertFalse(one.is_alive())
                self.store.discard_scope("book")
                first.cleanup_transient_audio_files()
                continue_waiter.set()
                two.join(5)
        finally:
            release.set()
            continue_waiter.set()
            one.join(5)
            if two.ident is not None:
                two.join(5)
        self.assertFalse(two.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results["book"], results["direct"])
        first.session.post.assert_called_once()
        second.session.post.assert_not_called()

    def test_failed_wait_releases_interest_in_future_speech(self):
        first = self.make_processor(scope="book")
        second = self.make_processor(scope="direct")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        response = first.session.post.return_value
        results = []

        def blocked_post(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Ответ не освобождён")
            return response

        first.session.post.side_effect = blocked_post
        worker = threading.Thread(target=lambda: results.append(self.synthesize(first)))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            event = next(iter(self.store.inflight.values()))
            with mock.patch.object(event, "wait", side_effect=RuntimeError("Ошибка ожидания")):
                with self.assertRaisesRegex(RuntimeError, "Ошибка ожидания"):
                    self.synthesize(second)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        path = results[0][0]
        self.store.discard_scope("book")
        first.cleanup_transient_audio_files()
        second.cleanup_transient_audio_files()
        self.assertFalse(path.exists())
        second.session.post.assert_not_called()

    def test_cleanup_releases_processor_lease_only_once(self):
        first, second = self.make_processor(), self.make_processor()
        path, _success = self.synthesize(first)
        self.store.discard_scope("book")
        first.cleanup_transient_audio_files()
        first.cleanup_transient_audio_files()
        self.assertTrue(path.is_file())
        second.cleanup_transient_audio_files()
        self.assertFalse(path.exists())

    def test_constructor_failure_does_not_acquire_lease(self):
        with mock.patch.object(studio.TTSProcessor, "load_glossary_file", side_effect=RuntimeError("Некорректный глоссарий")), mock.patch.object(self.store, "acquire", wraps=self.store.acquire) as acquire:
            with self.assertRaises(RuntimeError):
                self.make_processor()
        acquire.assert_not_called()
        self.assertTrue(self.store.close())


if __name__ == "__main__":
    unittest.main()
