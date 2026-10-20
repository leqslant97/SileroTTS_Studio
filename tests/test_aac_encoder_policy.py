"""Выбор AAC сохраняет профиль, отмену, старые результаты и метки глав."""

import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_m4b_whole_file_export import studio  # Изолированная копия исходника.


class AACEncoderPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.binary = self.base / "ffmpeg"
        self.binary.write_bytes(b"binary")
        with studio._AAC_AT_COMPATIBILITY_LOCK:
            studio._AAC_AT_COMPATIBILITY_CACHE.clear()
            studio._AAC_AT_COMPATIBILITY_INFLIGHT.clear()

    def fake_probe(self, command, **kwargs):
        Path(command[-1]).write_bytes(b"AAC")
        self.assertEqual(command[command.index("-profile:a") + 1], "1")
        self.assertEqual(command[command.index("-aac_at_quality") + 1], "0")
        self.assertEqual(command[command.index("-aac_at_mode") + 1], "cbr")
        self.assertEqual(kwargs["timeout"], 5)
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    def args(self, **kwargs):
        return studio._aac_lc_encoder_args(
            "aac_at", ffmpeg_path=self.binary, **kwargs,
        )

    def test_native_and_unknown_selection_never_probe(self):
        with mock.patch.object(studio.subprocess, "run") as run:
            for selection in ("aac", None, "unknown", " AAC "):
                self.assertEqual(studio._aac_lc_encoder_args(selection),
                                 ["-c:a", "aac", "-profile:a", "aac_low"])
        run.assert_not_called()

    def test_other_platform_keeps_native_and_warns_once(self):
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio.subprocess, "run") as run, \
                self.assertLogs(level="WARNING") as messages:
            self.assertEqual(self.args()[1], "aac")
            self.assertEqual(self.args()[1], "aac")
        run.assert_not_called()
        self.assertEqual(len(messages.output), 1)

    def test_exact_profile_probed_once_and_binary_replacement_invalidates_cache(self):
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio.subprocess, "run", side_effect=self.fake_probe) as run:
            self.assertEqual(self.args()[1], "aac_at")
            self.assertEqual(self.args()[1], "aac_at")
            self.assertEqual(run.call_count, 1)
            self.args(bitrate="96k")
            self.args(channels=2)
            self.args(sample_rate=44100)
            self.assertEqual(run.call_count, 4)
            stat = self.binary.stat()
            os.utime(self.binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.args()
            self.assertEqual(run.call_count, 5)

    def test_success_with_clamped_bitrate_falls_back_without_repeated_probe(self):
        def clamped(command, **kwargs):
            self.fake_probe(command, **kwargs)
            return subprocess.CompletedProcess(
                command, 0, stderr=b"[aac_at] Bitrate 16000 not allowed; changing to 32000\n",
            )
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio.subprocess, "run", side_effect=clamped) as run, \
                self.assertLogs(level="WARNING"):
            self.assertEqual(self.args(bitrate="16k")[1], "aac")
            self.assertEqual(self.args(bitrate="16k")[1], "aac")
        self.assertEqual(run.call_count, 1)

    def test_unavailable_encoder_and_timeout_fall_back(self):
        for result in (
            subprocess.CompletedProcess([], 1, stderr=b"Unknown encoder 'aac_at'"),
            subprocess.TimeoutExpired([], 5),
            FileNotFoundError("missing ffmpeg"),
        ):
            with studio._AAC_AT_COMPATIBILITY_LOCK:
                studio._AAC_AT_COMPATIBILITY_CACHE.clear()
            kwargs = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
            with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                    mock.patch.object(studio.subprocess, "run", **kwargs), \
                    self.assertLogs(level="WARNING"):
                self.assertEqual(self.args()[1], "aac")

    def test_cancel_before_and_after_probe_is_not_fallback_or_negative_cache(self):
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio.subprocess, "run", side_effect=self.fake_probe) as run:
            with self.assertRaises(InterruptedError):
                self.args(cancelled=lambda: True)
            run.assert_not_called()
            cancellation = iter((False, True))
            with self.assertRaises(InterruptedError):
                self.args(cancelled=lambda: next(cancellation))
            self.assertFalse(studio._AAC_AT_COMPATIBILITY_CACHE)
            self.assertFalse(studio._AAC_AT_COMPATIBILITY_INFLIGHT)

    def test_parallel_same_profile_has_one_probe_and_waiter_can_cancel(self):
        entered = threading.Event()
        release = threading.Event()
        outputs = []

        def probe(command, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.fake_probe(command, **kwargs)

        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio.subprocess, "run", side_effect=probe) as run:
            owner = threading.Thread(target=lambda: outputs.append(self.args()))
            owner.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(InterruptedError):
                    self.args(cancelled=lambda: True)
                follower = threading.Thread(target=lambda: outputs.append(self.args()))
                follower.start()
                release.set()
                owner.join(2)
                follower.join(2)
                self.assertFalse(owner.is_alive() or follower.is_alive())
            finally:
                release.set()
                owner.join(2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual([row[1] for row in outputs], ["aac_at", "aac_at"])

    def test_capability_cache_is_bounded(self):
        for number in range(200):
            studio._remember_aac_at_compatibility((number,), (True, ""))
        self.assertEqual(len(studio._AAC_AT_COMPATIBILITY_CACHE), 128)
        self.assertNotIn((0,), studio._AAC_AT_COMPATIBILITY_CACHE)

    def test_retry_command_retains_metadata_maps_and_native_profile(self):
        command = ["ffmpeg", "-i", "source", "-map", "0:a", "-c:a", "aac_at",
                   "-profile:a", "1", "-aac_at_quality", "0", "-aac_at_mode", "cbr",
                   "-b:a", "96k", "-ar", "48000", "-metadata", "title=Книга", "output"]
        original = list(command)
        retry = studio._native_aac_retry_command(command)
        self.assertEqual(command, original)
        self.assertEqual(retry, ["ffmpeg", "-i", "source", "-map", "0:a", "-c:a", "aac",
                                 "-profile:a", "aac_low", "-b:a", "96k", "-ar", "48000",
                                 "-metadata", "title=Книга", "output"])


class AACRuntimeFallbackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.source = self.base / "source.wav"
        self.source.write_bytes(b"source untouched")
        self.output = self.base / "book.m4a"
        self.output.write_bytes(b"previous published file")
        self.profile = {"codec": "pcm_s16le", "sample_rate": 48000,
                        "channels": 2, "channel_layout": "stereo", "bitrate": None}

    def run_merge(self, popen, **options):
        with mock.patch.object(studio, "_check_aac_at_profile", return_value=(True, "")), \
                mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio, "_can_concat_stream_copy", return_value=False), \
                mock.patch.object(studio, "get_ffmpeg_path", return_value="ffmpeg-test"), \
                mock.patch.object(studio.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(studio, "_ffmpeg_source_sample_counts", return_value=[24000]):
            return studio._export_merged_audio_ffmpeg(
                [self.source], self.output, output_format="m4a", bitrate="96k",
                channels="mono", sample_rate=48000,
                _probed_profiles=[self.profile], aac_encoder="aac_at", **options,
            )

    def process(self, command, kwargs, returncode, detail=b""):
        Path(command[-1]).write_bytes(b"partial" if returncode else b"complete")
        kwargs["stderr"].write(detail)
        return mock.Mock(returncode=returncode, poll=mock.Mock(return_value=returncode))

    def test_encoder_failure_retries_once_preserving_profile_and_chapter_counts(self):
        commands = []
        counts = [7]

        def popen(command, **kwargs):
            commands.append(command)
            native = command[command.index("-c:a") + 1] == "aac"
            return self.process(command, kwargs, 0 if native else 1,
                                b"[aac_at @ 0] AudioToolbox init error: 1718449215")

        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.run_merge(popen, _source_sample_counts=counts), self.output)
        self.assertEqual(len(commands), 2)
        self.assertEqual(counts, [7, 24000])
        for command in commands:
            self.assertEqual(command[command.index("-ac") + 1], "1")
            self.assertEqual(command[command.index("-b:a") + 1], "96k")
        self.assertEqual(self.output.read_bytes(), b"complete")
        self.assertEqual(self.source.read_bytes(), b"source untouched")
        self.assertFalse(list(self.base.glob(".audio_*")))

    def test_input_and_disk_failure_are_not_retried_or_published(self):
        for detail in (b"Invalid data found when processing input",
                       b"[aac_at @ 0] Encode error: 1\nNo space left on device"):
            commands = []

            def popen(command, **kwargs):
                commands.append(command)
                return self.process(command, kwargs, 1, detail)

            with self.assertRaises(RuntimeError):
                self.run_merge(popen)
            self.assertEqual(len(commands), 1)
            self.assertEqual(self.output.read_bytes(), b"previous published file")
            self.assertFalse(list(self.base.glob(".audio_*")))

    def test_cancel_after_encoder_failure_never_retries_or_publishes(self):
        stopped = False
        commands = []

        def popen(command, **kwargs):
            nonlocal stopped
            commands.append(command)
            stopped = True
            return self.process(command, kwargs, 1, b"[aac_at @ 0] Encode error: 1")

        with self.assertRaises(InterruptedError):
            self.run_merge(popen, cancelled=lambda: stopped)
        self.assertEqual(len(commands), 1)
        self.assertEqual(self.output.read_bytes(), b"previous published file")
        self.assertFalse(list(self.base.glob(".audio_*")))

    def test_failed_native_retry_preserves_old_file_and_counts(self):
        commands = []
        counts = [7]

        def popen(command, **kwargs):
            commands.append(command)
            return self.process(command, kwargs, 1, b"[aac_at @ 0] Encode error: 1")

        with self.assertLogs(level="WARNING"), self.assertRaises(RuntimeError):
            self.run_merge(popen, _source_sample_counts=counts)
        self.assertEqual(len(commands), 2)
        self.assertEqual(counts, [7])
        self.assertEqual(self.output.read_bytes(), b"previous published file")
        self.assertFalse(list(self.base.glob(".audio_*")))


    def test_batched_retry_stops_old_feeder_and_commits_only_native_counts(self):
        events = []
        counts = [7]

        def popen(command, **kwargs):
            codec = command[command.index("-c:a") + 1]
            events.append(("process", codec))
            return self.process(command, kwargs, int(codec == "aac_at"),
                                b"[aac_at @ 0] Encode error: 1718449215")

        class Feeder:
            def __init__(self, *args, source_sample_counts=None, **kwargs):
                codec = events[-1][1]
                values = [999] if codec == "aac_at" else [24000, 24000]
                self.error = None
                self.thread = mock.Mock()
                self.thread.start.side_effect = lambda: source_sample_counts.extend(values)
                self.thread.is_alive.return_value = False
                self.thread.join.side_effect = lambda **kwargs: events.append(("joined", codec))
                self.stop = lambda: events.append(("stopped", codec))

        batches = (((self.source, None),), ((self.source, None),))
        with mock.patch.object(studio, "_BatchedAudioFeeder", Feeder), \
                mock.patch.object(studio, "_plan_ffmpeg_decode_batches", return_value=batches), \
                self.assertLogs(level="WARNING"):
            self.run_merge(popen, _source_sample_counts=counts)
        self.assertEqual(counts, [7, 24000, 24000])
        native_start = events.index(("process", "aac"))
        self.assertLess(events.index(("stopped", "aac_at")), native_start)
        self.assertLess(events.index(("joined", "aac_at")), native_start)
        self.assertEqual(self.output.read_bytes(), b"complete")

    def test_direct_m4a_encoder_failure_retries_and_keeps_existing_callbacks(self):
        processor = studio.TTSProcessor.__new__(studio.TTSProcessor)
        processor.cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        processor.cfg.update(output_format="m4a", output_bitrate="96k", aac_encoder="aac_at")
        processor.is_stopped = False
        processor.encode_semaphore = None
        processor._mark_output_status = mock.Mock()
        commands = []
        callback = mock.Mock()

        def run(command, **kwargs):
            commands.append(command)
            self.assertEqual(kwargs["out_path"], Path(command[-1]))
            Path(command[-1]).write_bytes(b"complete")
            if command[command.index("-c:a") + 1] == "aac_at":
                raise RuntimeError("[aac_at @ 0] Encode error: 1718449215")
            return b""

        with mock.patch.object(studio, "_inspect_opus_audio_files", return_value=([self.source], False)), \
                mock.patch.object(studio, "_check_aac_at_profile", return_value=(True, "")), \
                mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio, "_run_ffmpeg_checked", side_effect=run), \
                self.assertLogs(level="WARNING"):
            processor._merge_save_and_notify([self.source], self.output, "book.m4a", False, callback)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[1][commands[1].index("-c:a") + 1], "aac")
        self.assertEqual(processor._last_ffmpeg_save_command, tuple(commands[1]))
        self.assertEqual(self.output.read_bytes(), b"complete")
        callback.assert_called_once_with("book.m4a", "success", str(self.output.resolve()))


class AACEncoderRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.source = self.base / "source.ogg"
        self.source.write_bytes(b"source remains untouched")
        self.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.config["aac_encoder"] = "aac_at"
        self.processor = SimpleNamespace(is_stopped=False, encode_semaphore=None)

    @staticmethod
    def publish(_sources, destination, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"complete AAC")
        if kwargs.get("_source_sample_counts") is not None:
            kwargs["_source_sample_counts"].append(24000)

    def m4b_record(self):
        return {"target": {"format": "m4b", "bitrate": "96k"},
                "path": self.base / "book.m4b", "file_ids": ("chapter",),
                "source_paths": (self.base / "chapter.txt",)}

    def test_source_m4a_chain_forwards_selected_backend(self):
        record = {"target": {"format": "m4a", "bitrate": "96k"},
                  "path": self.base / "chapter.m4a"}
        with mock.patch.object(studio, "_source_synthesis_cache_profiles", return_value=[None]), \
                mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=self.publish) as export:
            result = studio.run_source_synthesis_target_chain(
                self.processor, (self.source,), (record,), self.config,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(export.call_args.kwargs["aac_encoder"], "aac_at")

    def test_source_m4b_preencode_and_final_stage_forward_backend(self):
        record = self.m4b_record()
        stage = self.base / "stage.m4a"
        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=self.publish) as export:
            result = studio.run_source_synthesis_m4b_target(
                self.processor, record, {"chapter": (self.source,)}, self.config,
                audio_stage_path=stage,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["chapter_durations"], {"chapter": 0.5})
        self.assertEqual(export.call_args.kwargs["aac_encoder"], "aac_at")
        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=self.publish) as export:
            result = studio.run_source_synthesis_m4b_target(
                self.processor, record, {"chapter": (self.source,)}, self.config,
                chapter_duration_overrides={"chapter": 0.5}, preencoded_audio=stage,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(export.call_args.kwargs["aac_encoder"], "aac_at")
        self.assertEqual(export.call_args.kwargs["preencoded_audio"], stage)

    def test_export_routes_use_captured_backend_instead_of_mutable_live_settings(self):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        app.config["aac_encoder"] = "aac"
        setting = {"path": str(self.source), "title": "Глава", "duration": 0.5}
        for fmt, helper in (("m4a", "_export_single_audio_ffmpeg"),
                            ("m4b", "_export_m4b_ffmpeg")):
            with mock.patch.object(studio, helper, side_effect=self.publish) as export:
                app._run_output_target_set(
                    ("chapter",), {}, {}, {"chapter": setting},
                    [{"format": fmt}], self.base / fmt,
                    settings_snapshot=self.config,
                )
            self.assertEqual(export.call_args.kwargs["aac_encoder"], "aac_at")
        self.assertEqual(app.config["aac_encoder"], "aac")


if __name__ == "__main__":
    unittest.main()
