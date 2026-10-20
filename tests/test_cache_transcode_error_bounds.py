"""Large damaged JSON caches retain exact counters and a bounded error report."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py")
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class CacheTranscodeErrorBoundsTests(unittest.TestCase):
    def test_invalid_metadata_and_paths_count_all_errors_without_unbounded_details(self):
        records = {f"invalid-{n}": None for n in range(2000)}
        records.update({f"unsafe-{n}": {"file_name": "../outside.ogg"} for n in range(2000)})
        with tempfile.TemporaryDirectory() as folder:
            stats = studio.transcode_cache_entries_to_opus(folder, records, max_workers=1)
        self.assertEqual(stats["failed"], 4000)
        self.assertEqual(len(stats["errors"]), 100)
        self.assertEqual(stats["converted"], 0)
        self.assertFalse(stats["index_changed"])

    def test_encoder_failures_are_bounded_and_do_not_prevent_successful_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            (cache / "audio").mkdir()
            records = {}
            for n in range(151):
                name = f"{n}.ogg"
                (cache / "audio" / name).write_bytes(b"fixture")
                records[str(n)] = {"file_name": name, "unknown": n}

            def encode(path):
                if path.name == "150.ogg":
                    return "already_opus", 7, 7, 4096, 4096
                raise OSError("failed audio")

            with mock.patch.object(studio, "_transcode_cache_audio_to_opus", side_effect=encode):
                stats = studio.transcode_cache_entries_to_opus(cache, records, max_workers=2)
            self.assertEqual(stats["failed"], 150)
            self.assertEqual(len(stats["errors"]), 100)
            self.assertEqual(stats["already_opus"], 1)
            self.assertTrue(stats["index_changed"])
            self.assertEqual(records["150"]["audio_codec"], studio.CACHE_AUDIO_CODEC)
            self.assertEqual(records["150"]["unknown"], 150)
            self.assertNotIn("audio_codec", records["0"])


if __name__ == "__main__":
    unittest.main()
