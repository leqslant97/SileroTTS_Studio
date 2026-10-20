"""Заголовок переиспользуется только вместе с проверенной физической версией Ogg."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    import test_regressions as regression
except ModuleNotFoundError:
    from tests import test_regressions as regression

studio = regression.studio


class OggValidationProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "fragment.ogg"
        self.good = regression.CacheBehaviorTests.complete_cached_opus()
        self.path.write_bytes(self.good)
        self.header = studio._inspect_ogg_audio_header_details(self.path)
        self.cache_patch = mock.patch.object(studio, "_OGG_VALIDATION_CACHE", {})
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def header_for(self, path=None):
        return studio._validate_cached_ogg_audio_file(path or self.path, return_header=True)

    def test_cold_scan_returns_header_and_warm_hit_needs_no_second_open(self):
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio, "_inspect_ogg_audio_header_details",
                               side_effect=AssertionError("unexpected header reopen")), \
                mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                                  wraps=studio._validate_complete_ogg_audio_file) as complete:
            self.assertEqual(self.header_for(), self.header)
            self.assertEqual(self.header_for(), self.header)
        self.assertEqual(complete.call_count, 1)

    def test_published_without_header_reads_it_once_without_rescanning_audio(self):
        studio._validate_complete_ogg_audio_file(self.path)
        studio._remember_validated_ogg_audio_file(self.path)
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio, "_inspect_ogg_audio_header_details",
                               wraps=studio._inspect_ogg_audio_header_details) as header, \
                mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                                  side_effect=AssertionError("unexpected full reread")):
            self.assertEqual(self.header_for(), self.header)
            self.assertEqual(self.header_for(), self.header)
        self.assertEqual(header.call_count, 1)

    def test_same_size_and_restored_mtime_do_not_hide_corruption(self):
        self.header_for()
        original = self.path.stat()
        damaged = bytearray(self.good)
        damaged[-1] ^= 1
        self.path.write_bytes(damaged)
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(ValueError):
            self.header_for()

    def test_windows_warm_hit_rechecks_crc_instead_of_assuming_change_time(self):
        self.header_for()
        with mock.patch.object(studio.platform, "system", return_value="Windows"), \
                mock.patch.object(studio, "_validate_complete_ogg_audio_file",
                                  wraps=studio._validate_complete_ogg_audio_file) as complete:
            self.assertEqual(self.header_for(), self.header)
            self.assertEqual(self.header_for(), self.header)
        self.assertEqual(complete.call_count, 2)

    def test_windows_unchanged_stat_cannot_hide_payload_corruption(self):
        fingerprint = studio._ogg_audio_file_fingerprint(self.path)
        with mock.patch.object(studio.platform, "system", return_value="Windows"), \
                mock.patch.object(studio, "_ogg_audio_file_fingerprint", return_value=fingerprint):
            self.header_for()
            damaged = bytearray(self.good)
            damaged[-1] ^= 1
            self.path.write_bytes(damaged)
            for return_header in (False, True):
                with self.subTest(return_header=return_header), self.assertRaises(ValueError):
                    studio._validate_cached_ogg_audio_file(self.path, return_header=return_header)

    def test_atomic_replacement_does_not_reuse_old_header(self):
        self.header_for()
        original = self.path.stat()
        replacement = self.root / "replacement.ogg"
        damaged = bytearray(self.good)
        damaged[-1] ^= 1
        replacement.write_bytes(damaged)
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, self.path)
        with self.assertRaises(ValueError):
            self.header_for()

    def test_replacement_during_full_scan_is_not_remembered(self):
        complete = studio._validate_complete_ogg_audio_file
        replacement = self.root / "replacement.ogg"
        replacement.write_bytes(self.good)
        def replace_after_scan(path, **kwargs):
            result = complete(path, **kwargs)
            os.replace(replacement, path)
            return result
        with mock.patch.object(studio, "_validate_complete_ogg_audio_file", side_effect=replace_after_scan):
            with self.assertRaisesRegex(ValueError, "изменился"):
                self.header_for()
        self.assertEqual(studio._OGG_VALIDATION_CACHE, {})
        self.assertEqual(self.header_for(), self.header)

    def test_replacement_during_header_fill_is_rejected_and_revalidated(self):
        studio._validate_complete_ogg_audio_file(self.path)
        studio._remember_validated_ogg_audio_file(self.path)
        inspect = studio._inspect_ogg_audio_header_details
        replacement = self.root / "replacement.ogg"
        replacement.write_bytes(self.good[:-1])
        def replace_after_header(path):
            result = inspect(path)
            os.replace(replacement, path)
            return result
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio, "_inspect_ogg_audio_header_details", side_effect=replace_after_header):
            with self.assertRaisesRegex(ValueError, "изменился"):
                self.header_for()
        with self.assertRaises(ValueError):
            self.header_for()


if __name__ == '__main__':
    unittest.main()
