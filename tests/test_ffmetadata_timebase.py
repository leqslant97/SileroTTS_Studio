"""Единицы главы соответствуют указанной временной шкале FFmpeg."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_regressions import studio


class FFMetadataTimebaseTests(unittest.TestCase):
    def test_fractional_timebase_preserves_real_chapter_times_in_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for timebase in ("1/1000", "1/48000", "1001/30000"):
                with self.subTest(timebase=timebase):
                    metadata = base / "chapters.ffmetadata"
                    metadata.write_text(studio.build_ffmetadata_chapters([
                        {"title": "Начало", "start": 0, "end": 1.001},
                        {"title": "Продолжение", "start": 1.001, "end": 2.002},
                    ], timebase=timebase))
                    target = base / "book.m4b"
                    subprocess.run([
                        studio.get_ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=mono", "-f", "ffmetadata", "-i", str(metadata),
                        "-map_metadata", "1", "-map_chapters", "1", "-t", "2.002", "-c:a", "aac", str(target),
                    ], capture_output=True, check=True)
                    probe = subprocess.run([studio.get_ffprobe_path(), "-v", "error", "-show_chapters", "-of", "json", str(target)], capture_output=True, text=True, check=True)
                    chapters = json.loads(probe.stdout)["chapters"]
                    tolerance = 0.001 if timebase != "1001/30000" else 0.034
                    self.assertAlmostEqual(float(chapters[1]["start_time"]), 1.001, delta=tolerance)
                    self.assertAlmostEqual(float(chapters[1]["end_time"]), 2.002, delta=tolerance)
                    self.assertEqual(chapters[1]["tags"]["title"], "Продолжение")

    def test_invalid_timebase_is_rejected_before_metadata_publication(self):
        for timebase in ("0/1000", "1/0", "-1/1000", "1/-1000", "nan", "1/1000\nSTART=1"):
            with self.subTest(timebase=timebase), self.assertRaisesRegex(ValueError, "TIMEBASE"):
                studio.build_ffmetadata_chapters([("Глава", 1.0)], timebase=timebase)


if __name__ == "__main__":
    unittest.main()
