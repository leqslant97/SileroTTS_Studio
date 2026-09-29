"""Проверки точных замен глоссария среди похожих и неоднозначных фрагментов."""

import importlib.util
import sys
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


class VerbatimOccurrenceAuditTests(unittest.TestCase):
    def term_context(self, source="раз", replacement="р+аз"):
        return studio.TTSProcessor.normalization_context(
            {},
            {"terms_ignore_case": {
                source: {"replacement": replacement, "verbatim": True}
            }},
        )

    def test_whole_word_term_does_not_replace_substring_in_earlier_word(self):
        processor = self.term_context()

        self.assertEqual(
            processor.process_sentence_text("Сразу раз."), "Сразу р+аз."
        )

    def test_repeated_protected_terms_keep_order_and_case(self):
        processor = self.term_context()

        self.assertEqual(
            processor.process_sentence_text("Раз, сразу раз."),
            "Р+аз, сразу р+аз.",
        )

    def test_contextual_regex_does_not_replace_unprotected_identical_word(self):
        processor = studio.TTSProcessor.normalization_context(
            {},
            {"regex_rules": [{
                "pattern": r"(?<=точно )раз", "repl": "р+аз", "verbatim": True
            }]},
        )

        with self.assertLogs(level="WARNING") as captured:
            result = processor.process_sentence_text("раз, точно раз.")

        self.assertEqual(result, "раз, точно р+аз.")
        self.assertIn("резервную защиту", "\n".join(captured.output))

    def test_new_identical_word_from_number_expansion_is_not_protected(self):
        processor = self.term_context("два", "дв+а")

        with self.assertLogs(level="WARNING"):
            result = processor.process_sentence_text("2 два.")

        self.assertEqual(result, "два дв+а.")

    def test_unambiguous_term_keeps_roman_numeral_context_in_single_pass(self):
        processor = self.term_context("Глава", "Гл+ава")
        normalizer = processor._normalizer
        with mock.patch.object(
            normalizer, "normalize", wraps=normalizer.normalize
        ) as normalize:
            result = processor.process_sentence_text("Глава IV.")

        self.assertEqual(result, "Гл+ава четвёртая.")
        self.assertEqual(normalize.call_count, 1)


if __name__ == "__main__":
    unittest.main()
