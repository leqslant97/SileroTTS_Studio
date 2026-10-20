"""Real child-process coverage for bounded stderr and cancellation."""

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_regressions import studio


class FfmpegCheckedLoggingTests(unittest.TestCase):
    @staticmethod
    def noisy_child(exit_code=0):
        return [sys.executable, "-c", (
            "import os,sys; "
            "[os.write(2,b'x'*65536) for _ in range(20)]; "
            "os.write(2,b'\\nFINAL_ENCODER_DIAGNOSTIC\\n'); "
            f"sys.exit({exit_code})"
        )]

    def test_success_reads_bounded_tail_without_pipe_deadlock(self):
        stderr = studio._run_ffmpeg_checked(self.noisy_child())
        self.assertLessEqual(len(stderr), 262144)
        self.assertGreater(len(stderr), 250000)
        self.assertTrue(stderr.endswith(b"FINAL_ENCODER_DIAGNOSTIC\n"))

    def test_failure_keeps_final_diagnostic_and_bounds_exception(self):
        with self.assertRaisesRegex(RuntimeError, "FINAL_ENCODER_DIAGNOSTIC") as failure:
            studio._run_ffmpeg_checked(self.noisy_child(7), error_prefix="Encoder failed")
        self.assertTrue(str(failure.exception).startswith("Encoder failed:"))
        self.assertLessEqual(len(str(failure.exception)), 262144 + 32)

    def test_cancellation_stops_real_child_after_large_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            command = [sys.executable, "-c", (
                "import os,time; from pathlib import Path; "
                "[os.write(2,b'x'*65536) for _ in range(20)]; "
                "Path(" + repr(str(ready)) + ").touch(); time.sleep(30)"
            )]
            children = []
            actual_popen = subprocess.Popen
            def start(*args, **kwargs):
                child = actual_popen(*args, **kwargs)
                children.append(child)
                return child
            started = time.monotonic()
            with mock.patch.object(studio.subprocess, "Popen", side_effect=start):
                with self.assertRaises(InterruptedError):
                    studio._run_ffmpeg_checked(command, cancelled=ready.exists)
            self.assertTrue(ready.exists())
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertLess(time.monotonic() - started, 8)


if __name__ == "__main__":
    unittest.main()
