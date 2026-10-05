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


class NormalizerEntryPreviewAuditTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = object.__new__(studio.TTSApp)
        self.app.root = self.root
        self.app.config = {}
        self.app.settings_vars = {}
        self.app._status_label_kinds = {}
        self.app.font_size_var = studio.tk.IntVar(master=self.root, value=12)
        self.app.get_status_color = lambda _kind: "#333333"
        self.app.tab_normalizer = studio.ttk.Frame(self.root)
        self.app.run_normalizer_preview = mock.Mock()
        self.app.setup_normalizer_tab()

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from NormalizerEntryPreviewAuditTests.descendants(child)

    def entry_for(self, key):
        variable = self.app.normalizer_preview_vars[key]
        return next(
            widget for widget in self.descendants(self.app.tab_normalizer)
            if widget.winfo_class() == "TEntry"
            and str(widget.cget("textvariable")) == str(variable)
        )

    def mark_completed_preview(self):
        self.app._normalizer_last_preview_result = {
            "normalized_text_for_file": "Проверенный текст."
        }
        self.app.btn_save_normalized_text.configure(state="normal")
        return self.app._normalizer_preview_generation

    def assert_preview_invalidated(self, generation):
        self.assertIsNone(self.app._normalizer_last_preview_result)
        self.assertIn("disabled", self.app.btn_save_normalized_text.state())
        self.assertGreater(self.app._normalizer_preview_generation, generation)
        # Инвалидация синхронна; отложенный пересчёт ещё не запускался.
        self.app.run_normalizer_preview.assert_not_called()

    def test_virtual_paste_and_cut_invalidate_completed_preview_immediately(self):
        for key in (
            "latin_dictionary_filename",
            "dictionary_include_files",
            "dictionary_exclude_files",
            "dictionaries_path",
        ):
            with self.subTest(key=key):
                entry = self.entry_for(key)
                variable = self.app.normalizer_preview_vars[key]
                variable.set("before.dic")
                entry.selection_range(0, "end")
                self.root.clipboard_clear()
                self.root.clipboard_append("after.dic")
                generation = self.mark_completed_preview()
                entry.event_generate("<<Paste>>")

                self.assertEqual(variable.get(), "after.dic")
                self.assert_preview_invalidated(generation)

                entry.selection_range(0, "end")
                generation = self.mark_completed_preview()
                entry.event_generate("<<Cut>>")

                self.assertEqual(variable.get(), "")
                self.assert_preview_invalidated(generation)


if __name__ == "__main__":
    unittest.main()
