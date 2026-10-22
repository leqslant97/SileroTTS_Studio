"""Проверки предварительной обработки и line ending вариантов текста."""

import importlib.util
import sys
import tempfile
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


class TextLineEndingPipelineTests(unittest.TestCase):
    def context(
        self,
        *,
        prepared=False,
        mode="sentence",
        normalizer_enabled=False,
        auto_abbreviations=False,
        auto_short_words=False,
        glossary_enabled=False,
    ):
        return studio.TTSProcessor.normalization_context({
            "normalizer_enabled": normalizer_enabled,
            "glossary_enabled": glossary_enabled,
            "auto_abbreviations": auto_abbreviations,
            "auto_short_words": auto_short_words,
            "text_is_prepared": prepared,
            "synthesis_mode": mode,
            "separator_symbols": "---",
            "pause_file_start": 0,
            "pause_file_end": 0,
            "pause_sentence": 11,
            "pause_paragraph": 19,
            "pause_speech": 23,
            "pause_colon": 29,
            "pause_separator": 47,
        })

    def test_disabling_both_custom_abbreviation_stages_uses_library_results(self):
        context = self.context(
            normalizer_enabled=True,
            auto_abbreviations=False,
            auto_short_words=False,
        )
        self.assertIsNotNone(context._normalizer)

        for text in (
            "И.И. и к.п.д. работают.",
            "г. Москва, ул. Ленина.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    context.process_sentence_text(text),
                    context._normalizer.normalize(text),
                )

    def test_custom_abbreviation_flags_have_independent_overlapping_effects(self):
        initials_context = self.context(
            normalizer_enabled=True,
            auto_abbreviations=False,
            auto_short_words=True,
        )
        initials = "И.И. и к.п.д. работают."
        self.assertEqual(
            initials_context.process_sentence_text(initials),
            "ИИ и кпд работают.",
        )
        self.assertNotEqual(
            initials_context.process_sentence_text(initials),
            initials_context._normalizer.normalize(initials),
        )

        short_words_context = self.context(
            normalizer_enabled=True,
            auto_abbreviations=True,
            auto_short_words=False,
        )
        short_words = "г. Москва, ул. Ленина."
        self.assertEqual(
            short_words_context.process_sentence_text(short_words),
            short_words_context._normalizer.normalize(short_words),
        )
        self.assertEqual(
            short_words_context.process_sentence_text(short_words),
            "город Москва, улица Ленина.",
        )

    def test_custom_abbreviation_stages_still_run_when_normalizer_is_off(self):
        context = self.context(
            normalizer_enabled=False,
            auto_abbreviations=True,
            auto_short_words=True,
        )
        self.assertIsNone(context._normalizer)
        self.assertEqual(
            context.process_sentence_text("И.И. и к.п.д. работают."),
            "И-И и к-п-д работают.",
        )
        self.assertEqual(
            context.process_sentence_text("г. Москва, ул. Ленина."),
            "г Москва, ул Ленина.",
        )

    def test_prepared_text_bypasses_custom_abbreviation_stages(self):
        context = self.context(
            prepared=True,
            normalizer_enabled=True,
            auto_abbreviations=True,
            auto_short_words=True,
        )
        self.assertIsNone(context._normalizer)
        self.assertEqual(
            context.process_sentence_text("— И.И. и г. Москва"),
            "И.И. и г. Москва",
        )

    def test_final_cleanup_belongs_to_studio_with_or_without_the_library(self):
        for enabled in (True, False):
            context = self.context(normalizer_enabled=enabled)
            with self.subTest(normalizer_enabled=enabled):
                self.assertEqual(context.process_sentence_text("«Слово!»"), "Слово!")
                self.assertEqual(context.process_sentence_text("путь_имени."), "путьимени.")
                if enabled:
                    self.assertIn("_", context._normalizer.normalize("путь_имени."))
                    self.assertNotEqual(
                        context._normalizer.normalize("«Слово!»"), "Слово!"
                    )
                prepared = self.context(prepared=True, normalizer_enabled=enabled)
                self.assertEqual(
                    prepared.process_sentence_text("«путь_имени!»"), "«путь_имени!»"
                )

    def test_studio_adds_missing_terminal_punctuation_and_keeps_existing_marks(self):
        for enabled in (True, False):
            context = self.context(normalizer_enabled=enabled)
            with self.subTest(normalizer_enabled=enabled):
                self.assertEqual(context.process_sentence_text("Слово"), "Слово.")
                if enabled:
                    self.assertEqual(context._normalizer.normalize("Слово"), "Слово")
                for ending in (".", "!", "?", "?!", "…"):
                    self.assertEqual(
                        context.process_sentence_text("Слово" + ending), "Слово" + ending
                    )
                self.assertEqual(
                    self.context(prepared=True).process_sentence_text("Слово"), "Слово"
                )

    def collecting_context(self, directory, *, prepared=False, mode="full"):
        context = self.context(prepared=prepared, mode=mode)
        context.is_stopped = False
        context.cfg["output_dir"] = str(directory)
        context._save_cache = mock.Mock()
        context.synthesize_sentence = mock.Mock(
            return_value=(Path(directory) / "speech.ogg", True)
        )
        context._get_silence_file = mock.Mock(
            return_value=Path(directory) / "pause.ogg"
        )
        return context

    @staticmethod
    def collect(context, raw):
        return context.process_raw_text(
            raw, "chapter.mp3", save_to_disk=False, return_audio_files=True
        )

    def test_preview_recognizes_separators_for_all_line_endings(self):
        for prepared in (False, True):
            context = self.context(prepared=prepared)
            expected = context.preview_normalization(
                "Фраза.\n \t---\t \nОтвет."
            )
            self.assertIn("[ПАУЗА РАЗДЕЛИТЕЛЯ]", expected["normalized_text"])
            for ending in ("\r\n", "\r"):
                with self.subTest(prepared=prepared, ending=repr(ending)):
                    actual = context.preview_normalization(ending.join((
                        "Фраза.", " \t---\t ", "Ответ."
                    )))
                    for key in ("prepared", "normalized_text", "normalized_text_for_file",
                                "sentence_count", "skipped"):
                        self.assertEqual(actual[key], expected[key])

    def test_regex_inserted_line_endings_keep_separators_requests_and_hashes(self):
        raw = "Первая фраза.МЕТКАВторая фраза."
        with tempfile.TemporaryDirectory() as directory:
            plans = []
            for ending in ("\n", "\r\n", "\r"):
                context = self.collecting_context(directory, mode="full")
                context.cfg["glossary_enabled"] = True
                context.load_glossary_data({"regex_rules": [{
                    "pattern": "МЕТКА", "repl": ending + "---" + ending,
                }]})
                preview = context.preview_normalization(raw)
                self.assertIn("[ПАУЗА РАЗДЕЛИТЕЛЯ]", preview["normalized_text"])
                hashes = context.get_all_possible_hashes(raw)
                result = self.collect(context, raw)
                self.assertEqual(result["status"], "success")
                self.assertIn(mock.call(47), context._get_silence_file.call_args_list)
                plans.append((preview["normalized_text"], hashes,
                              context.synthesize_sentence.call_args_list,
                              context._get_silence_file.call_args_list))
            self.assertEqual(plans[1:], [plans[0], plans[0]])

    def test_requests_and_boundary_pauses_match_in_every_synthesis_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "speech.ogg"
            pause = root / "pause.ogg"
            audio.write_bytes(b"fixture speech")
            pause.write_bytes(b"fixture pause")
            for prepared in (False, True):
                for mode in ("sentence", "paragraph", "full"):
                    plans = []
                    for ending in ("\n", "\r\n", "\r"):
                        with self.subTest(prepared=prepared, mode=mode, ending=repr(ending)):
                            context = self.context(prepared=prepared, mode=mode)
                            context.is_stopped = False
                            context.cfg["output_dir"] = str(root / "unused")
                            context._save_cache = mock.Mock()
                            context._get_silence_file = mock.Mock(return_value=pause)
                            context.synthesize_sentence = mock.Mock(return_value=(audio, True))
                            result = context.process_raw_text(
                                ending.join(("Вступление:", "— Р+еплика. Ответ.",
                                             "---", "Конец.")),
                                "chapter.mp3", save_to_disk=False, return_audio_files=True,
                            )
                            self.assertEqual(result["status"], "success")
                            plans.append((context.synthesize_sentence.call_args_list,
                                          context._get_silence_file.call_args_list,
                                          result["audio_files"]))
                    self.assertEqual(plans[1:], [plans[0], plans[0]])
                    self.assertIn(mock.call(47), plans[0][1])

    def test_cache_hashes_match_direct_synthesis_line_endings(self):
        for prepared in (False, True):
            context = self.context(prepared=prepared)
            expected = context.get_all_possible_hashes(
                "Вступление:\n— Р+еплика. Ответ.\n---\nКонец."
            )
            for ending in ("\r\n", "\r"):
                with self.subTest(prepared=prepared, ending=repr(ending)):
                    actual = context.get_all_possible_hashes(ending.join((
                        "Вступление:", "— Р+еплика. Ответ.", "---", "Конец."
                    )))
                    self.assertEqual(actual, expected)

    def test_prepared_text_preserves_accents_numbers_and_punctuation(self):
        context = self.context(prepared=True)
        preview = context.preview_normalization(
            "— Р+еплика 62-й: «уб+о'го».\r\n---\r\nИтог +5."
        )
        self.assertEqual(preview["normalized_text_for_file"],
                         "— Р+еплика 62-й: «уб+о'го».\n\n---\n\nИтог +5.")

    def test_full_splits_large_single_paragraph_at_whole_sentences(self):
        raw = ("Проверка. " * 3334).strip()
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            result = self.collect(context, raw)
        requests = [call.args[0] for call in context.synthesize_sentence.call_args_list]
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(len(text) <= studio.SAFE_LIMIT for text in requests))
        self.assertEqual(" ".join(requests), raw)
        context._get_silence_file.assert_not_called()
        hashes = context.get_all_possible_hashes(raw)
        for text in requests:
            self.assertIn(studio.cache_content_hash(text, context.cfg["speaker"]), hashes)

    def test_full_guard_keeps_original_paragraph_and_separator_pauses(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            context.cfg.update({"pause_paragraph": 3, "pause_speech": 5,
                                "pause_colon": 7, "pause_separator": 9})
            raw = "Начало:\n— Реплика. Ответ. Конец.\n---\nФинал."
            with mock.patch.object(studio, "SAFE_LIMIT", 18):
                self.collect(context, raw)
                hashes = context.get_all_possible_hashes(raw)
        self.assertEqual(context._get_silence_file.call_args_list,
                         [mock.call(7), mock.call(9)])
        self.assertEqual([call.args[0] for call in context.synthesize_sentence.call_args_list],
                         ["Начало:", "Реплика. Ответ.", "Конец.", "Финал."])
        for call in context.synthesize_sentence.call_args_list:
            self.assertIn(studio.cache_content_hash(call.args[0], context.cfg["speaker"]),
                          hashes)

    def test_full_splits_long_single_sentence_at_whitespace_without_added_pause(self):
        raw = ("Слово " * 12).strip() + "."
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            with mock.patch.object(studio, "SAFE_LIMIT", 18):
                self.collect(context, raw)
                hashes = context.get_all_possible_hashes(raw)
        requests = [call.args[0] for call in context.synthesize_sentence.call_args_list]
        self.assertGreater(len(requests), 1)
        self.assertTrue(all(len(text) <= 18 for text in requests))
        self.assertEqual(" ".join(requests), raw)
        context._get_silence_file.assert_not_called()
        for text in requests:
            self.assertIn(studio.cache_content_hash(text, context.cfg["speaker"]), hashes)

    def test_full_prepared_whitespace_split_keeps_literal_accents_and_internal_spaces(self):
        raw = "П+ервое\u00a0  сл+ово  вт+орое\u00a0  третье  четвёртое."
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory, prepared=True)
            with mock.patch.object(studio, "SAFE_LIMIT", 20):
                self.collect(context, raw)
        requests = [call.args[0] for call in context.synthesize_sentence.call_args_list]
        self.assertEqual(requests, ["П+ервое\u00a0  сл+ово", "вт+орое\u00a0  третье", "четвёртое."])
        context._get_silence_file.assert_not_called()

    def test_full_rejects_oversized_word_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            with mock.patch.object(studio, "SAFE_LIMIT", 12):
                with self.assertRaisesRegex(ValueError, "защитного предела Studio"):
                    self.collect(context, "Начало.\nСверхдлинноеслово.")
        context.synthesize_sentence.assert_not_called()
        context._get_silence_file.assert_not_called()

    def test_sentence_and_paragraph_remain_available_for_long_word(self):
        raw = "Сверхдлинноеслово."
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("sentence", "paragraph"):
                with self.subTest(mode=mode):
                    context = self.collecting_context(directory, mode=mode)
                    with mock.patch.object(studio, "SAFE_LIMIT", 12):
                        result = self.collect(context, raw)
                        hashes = context.get_all_possible_hashes(raw)
                    self.assertEqual(result["status"], "success")
                    context.synthesize_sentence.assert_called_once()
                    self.assertIn(studio.cache_content_hash(raw, context.cfg["speaker"]),
                                  hashes)

    def test_full_small_paragraph_preserves_request_and_cache_identity(self):
        raw = "Первая фраза. Вторая фраза.\nПродолжение."
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            self.collect(context, raw)
        context.synthesize_sentence.assert_called_once()
        self.assertEqual(context.synthesize_sentence.call_args.args[:2], (raw, raw))
        context._get_silence_file.assert_not_called()
        self.assertIn(studio.cache_content_hash(raw, context.cfg["speaker"]),
                      context.get_all_possible_hashes(raw))

    def test_stop_during_large_paragraph_preparation_does_not_send_partial_text(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.collecting_context(directory)
            original = context.process_sentence_text
            count = 0

            def prepare(text):
                nonlocal count
                count += 1
                if count == 3:
                    context.is_stopped = True
                return original(text)

            context.process_sentence_text = prepare
            with mock.patch.object(studio, "SAFE_LIMIT", 18):
                result = self.collect(context, "Проверка. " * 12)
        self.assertEqual(result["status"], "error")
        context.synthesize_sentence.assert_not_called()
        context._get_silence_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
