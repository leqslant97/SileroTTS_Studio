"""Готовность групп и своевременная остановка синтеза без сетевых запросов."""

import copy
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_source_group_runtime as runtime


studio = runtime.studio


def opus_header():
    packet = b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
    return b"OggS\x00\x02" + b"\x00" * 20 + b"\x01" + bytes([len(packet)]) + packet


class VerbatimRegionTests(unittest.TestCase):
    def test_many_matches_in_one_word_preserve_all_replacements_and_source(self):
        source = "ё" + "12" * 3000 + "Ж"
        result = studio.TTSProcessor._tagged_substitution(
            [("до " + source + " после", False)],
            re.compile(r"\d"), lambda match: "X" if match[0] == "1" else "Y",
            protect_adjacent_word=True,
        )
        self.assertEqual([(str(text), protected) for text, protected in result], [
            ("до ", False), ("ё" + "XY" * 3000 + "Ж", True), (" после", False),
        ])
        self.assertEqual(result[1][0].source, source)

    def test_zero_width_and_spanning_matches_merge_without_losing_word_letters(self):
        result = studio.TTSProcessor._tagged_substitution(
            [("до ab.cd после", False)], re.compile(r"(?=b)|\."),
            lambda match: "Y" if match[0] else "X",
            protect_adjacent_word=True,
        )
        self.assertEqual([(str(text), protected) for text, protected in result], [
            ("до ", False), ("aXbYcd", True), (" после", False),
        ])
        self.assertEqual(result[1][0].source, "ab.cd")


class SourceAudioDependencyTests(unittest.TestCase):
    def test_out_of_order_and_repeated_chapters_release_only_complete_groups(self):
        index = studio._SourceAudioDependencyIndex([
            ("first", ("a", "b", "a")),
            ("second", ("b", "c")),
            ("third", ("c",)),
        ])
        index.publish("a")
        index.publish("a")
        self.assertEqual(index.pop_ready(), ())
        index.publish("unrelated")
        index.publish("c")
        self.assertEqual(index.pop_ready(), ("third",))
        index.publish("b")
        self.assertEqual(index.pop_ready(), ("first", "second"))
        index.publish("b")
        self.assertEqual(index.pop_ready(), ())

    def test_overlap_waiting_preserves_plan_order_when_another_group_becomes_ready(self):
        index = studio._SourceAudioDependencyIndex([
            ("64k", ("a",)), ("96k", ("a",)), ("next", ("b",)),
        ])
        index.publish("a")
        self.assertEqual(index.pop_ready(), ("64k", "96k"))
        index.defer("96k")
        index.publish("b")
        self.assertEqual(index.pop_ready(), ("96k", "next"))
        self.assertEqual(index.pop_ready(), ())

    def test_large_overlapping_plan_releases_every_group_once(self):
        chapter_count = 3000
        dependencies = [
            (group, tuple(range(group, group + 6)))
            for group in range(chapter_count - 5)
        ]
        index = studio._SourceAudioDependencyIndex(dependencies)
        released = []
        for chapter in reversed(range(chapter_count)):
            index.publish(chapter)
            ready = index.pop_ready()
            self.assertEqual(ready, (chapter,) if chapter < chapter_count - 5 else ())
            released.extend(ready)
        self.assertEqual(len(released), len(dependencies))
        self.assertEqual(len(set(released)), len(dependencies))


class SourceOutputWorkerRecoveryTests(unittest.TestCase):
    def test_many_m4b_groups_finish_every_overlapping_target_once(self):
        case = runtime.SourceGroupOutputRuntimeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        for number in range(5, 121):
            key = f"chapter-{number}"
            path = case.source / f"{number:03d}.txt"
            path.write_text(f"Глава {number}.", encoding="utf-8")
            fragment = case.root / f"fragment-{number}.ogg"
            fragment.write_bytes(b"canonical fixture")
            case.paths[key], case.fragments[key] = path, fragment
        case.keys = tuple(case.paths)
        case.groups = [{"id": f"group-{start}", "name": f"Том {start}",
                        "file_ids": case.keys[start:start + 6]}
                       for start in range(0, len(case.keys), 6)]
        case.configure([{"format": "m4b", "bitrate": "64k"},
                        {"format": "m4b", "bitrate": "96k"}])
        case.config["max_parallel_encodes"] = 4
        active = set()
        overlaps = []
        original_encode = case.encode

        def encode(audio, output, **options):
            members = set(audio)
            with case.encoding_lock:
                if active.intersection(members):
                    overlaps.append(str(output))
                active.update(members)
            try:
                time.sleep(0.002)
                original_encode(audio, output, **options)
            finally:
                with case.encoding_lock:
                    active.difference_update(members)

        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=encode):
            worker = threading.Thread(target=case.run_queue, daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "Blocked M4B targets did not wake when chapters were released")
        self.assertEqual(overlaps, [])
        self.assertCountEqual([row[0] for row in case.encoded],
                              [row["path"] for row in case.records])
        self.assertEqual(len(case.encoded), 40)
        self.assertFalse(case.app.finish_processing.call_args.args[-1])

    def test_unexpected_terminal_callback_failure_does_not_deadlock_either_pool(self):
        for fmt, worker_prefix in (("mp3", "source-target-worker-"),
                                   ("m4b", "source-m4b-worker-")):
            with self.subTest(fmt=fmt):
                case = runtime.SourceGroupOutputRuntimeTests("runTest")
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.configure([{"format": fmt, "assembly_mode": "merge"}])
                case.config["max_parallel_encodes"] = 1
                original_update = studio.SourceOutputStatusTracker.update
                failed = threading.Event()

                def fail_terminal_once(tracker, statuses):
                    statuses = tuple(statuses)
                    if (threading.current_thread().name.startswith(worker_prefix)
                            and not failed.is_set()
                            and any(status == "success" for _, status in statuses)):
                        failed.set()
                        raise RuntimeError("isolated terminal callback failure")
                    return original_update(tracker, statuses)

                with mock.patch.object(studio.SourceOutputStatusTracker, "update", fail_terminal_once):
                    worker = threading.Thread(target=case.run_queue, daemon=True)
                    worker.start()
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), "queue.join waited for a dead output worker")
                self.assertTrue(failed.is_set())
                self.assertEqual(len(case.encoded), 2, "Other accepted jobs must finish")
                self.assertTrue(case.app.finish_processing.call_args.args[-1])


class SynthesisCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.config.update({
            "input_dir": str(self.root / "input"),
            "output_dir": str(self.root / "output"),
            "cache_dir": str(self.root / "cache"),
            "normalizer_enabled": False, "glossary_enabled": False,
            "api_max_requests": 100, "api_time_window": 0,
            "max_retries": 3, "tag_cover": "", "apply_output_tags": False,
            "auto_trim_silence": False,
        })
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.processor = studio.TTSProcessor(
            self.config, shared_cache={}, shared_processing_statuses={}
        )
        self.addCleanup(self.processor.close_cache)
        self.addCleanup(self.processor.cleanup_transient_audio_files)

    def test_stop_interrupts_both_network_and_http_retry_delays(self):
        for http in (False, True):
            with self.subTest(http=http):
                self.processor.is_stopped = False
                attempted = threading.Event()
                retry_waiting = threading.Event()
                session = mock.Mock()

                def fail(*_args, **_kwargs):
                    attempted.set()
                    if not http:
                        raise studio.requests.exceptions.ConnectionError("offline fixture")
                    response = mock.Mock(status_code=503)
                    response.raise_for_status.side_effect = (
                        studio.requests.exceptions.HTTPError("fixture 503")
                    )
                    return response

                session.post.side_effect = fail
                self.processor.session = session
                original_wait = studio.TTSProcessor._wait_for_retry.__get__(self.processor)

                def wait_for_retry():
                    retry_waiting.set()
                    return original_wait()

                self.processor._wait_for_retry = wait_for_retry
                result = []
                worker = threading.Thread(target=lambda: result.append(
                    self.processor.synthesize_sentence("Тест.", "Тест.")
                ))
                worker.start()
                self.addCleanup(worker.join, 3)
                self.assertTrue(attempted.wait(2))
                self.assertTrue(retry_waiting.wait(2), "Retry did not enter its delay")
                time.sleep(0.02)
                started = time.monotonic()
                self.processor.is_stopped = True
                worker.join(0.5)
                self.assertFalse(worker.is_alive(), "Stop waited for the two-second retry delay")
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(result, [(None, False)])
                self.assertEqual(session.post.call_count, 1)

    def test_stop_during_preparation_skips_remaining_normalization_and_api(self):
        processed = []

        def normalize(text):
            processed.append(text)
            self.processor.is_stopped = True
            return text

        self.processor.process_sentence_text = normalize
        self.processor.synthesize_sentence = mock.Mock()
        callback = mock.Mock()
        result = self.processor.process_raw_text(
            "Первая фраза. Вторая фраза.\n" * 1000,
            "cancelled.mp3", save_to_disk=False,
            return_audio_files=True, completion_callback=callback,
        )
        self.assertEqual(len(processed), 1)
        self.processor.synthesize_sentence.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["audio_files"], ())
        callback.assert_called_once_with("cancelled.mp3", "error", None)
        self.assertFalse((self.root / "output" / "cancelled.mp3").exists())

    def test_stop_terminates_running_legacy_encoder_and_preserves_previous_output(self):
        audio = self.root / "fragment.ogg"
        audio.write_bytes(opus_header())
        output = self.root / "book.mp3"
        output.write_bytes(b"previous complete output")
        ready = self.root / "encoder-started"
        callbacks = []
        self.processor.encode_semaphore = None
        original_runner = studio._run_ffmpeg_checked

        def run_stalled_encoder(command, **options):
            script = (
                "from pathlib import Path; import time; "
                f"Path({command[-1]!r}).write_bytes(b'incomplete audio'); "
                f"Path({str(ready)!r}).touch(); time.sleep(10)"
            )
            return original_runner([sys.executable, "-c", script], **options)

        with mock.patch.object(studio, "_run_ffmpeg_checked", side_effect=run_stalled_encoder):
            worker = threading.Thread(target=self.processor._merge_save_and_notify,
                args=((audio,), output, output.name, False,
                      lambda *args: callbacks.append(args)))
            worker.start()
            self.addCleanup(worker.join, 3)
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "Isolated encoder did not start")
            started = time.monotonic()
            self.processor.stop()
            worker.join(1)
            self.assertFalse(worker.is_alive(), "Encoder ignored cancellation")
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(output.read_bytes(), b"previous complete output")
        self.assertEqual(callbacks, [(output.name, "error", None)])
        self.assertFalse(list(self.root.glob(".*.tmp.mp3")))

    def test_finished_processor_releases_keep_alive_session(self):
        session = mock.Mock()
        self.processor.session = session
        self.processor.cleanup_transient_audio_files()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
