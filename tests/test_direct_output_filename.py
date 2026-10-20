"""Названия прямого синтеза сохраняют точки, не относящиеся к расширению."""

import unittest

from test_regressions import studio


class DirectOutputFilenameTests(unittest.TestCase):
    def test_dotted_titles_remain_distinct_in_every_output_format(self):
        for output_format in studio.AUDIO_PROFILE_FORMATS:
            for suffix in ("", ".mp3", ".m4a.m4a"):
                with self.subTest(output_format=output_format, suffix=suffix):
                    names = [
                        studio.normalize_output_filename(title + suffix, output_format)
                        for title in ("Книга. Том 1", "Книга. Том 2", "Версия 1.6.2")
                    ]
                    self.assertEqual(names, [
                        "Книга. Том 1." + output_format,
                        "Книга. Том 2." + output_format,
                        "Версия 1.6.2." + output_format,
                    ])
                    self.assertEqual(len(set(names)), 3)

    def test_recognized_audio_extensions_are_replaced(self):
        for suffix in (*studio.AUDIO_PROFILE_FORMATS, "m4b", "mp4"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    studio.normalize_output_filename("Книга. Том 1." + suffix.upper(), "opus"),
                    "Книга. Том 1.opus",
                )

    def test_path_safety_and_fallback_are_preserved(self):
        cases = [
            (r"C:\temp\Книга. Том 1.mp3", "Книга. Том 1.mp3"),
            ("/tmp/Версия 1.6.2.mp3.mp3", "Версия 1.6.2.mp3"),
            ("chapter.notes", "chapter.notes.mp3"),
            ("CON.wav", "_CON.mp3"),
            ("", "direct_output.mp3"),
            ("...", "direct_output.mp3"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(studio.normalize_output_filename(value, "mp3"), expected)


if __name__ == "__main__":
    unittest.main()
