"""Переход ru-normalizr: новые этапы, перенос профилей и речевые ключи."""
import copy
import unittest

from test_regressions import studio


class Normalizer130Tests(unittest.TestCase):
    def test_new_stages_follow_tts_and_safe_defaults(self):
        for mode, expected in (("tts", True), ("safe", False)):
            with self.subTest(mode=mode):
                config = {"normalizer_mode": mode}
                options = studio.resolved_normalizer_options(config)
                for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
                    self.assertEqual(options[key], expected)
                normalizer = studio.build_ru_normalizer(config)
                self.assertEqual(normalizer.options.enable_math_normalization, expected)
                self.assertEqual(normalizer.options.cleanup_malformed_punctuation, expected)

    def test_explicit_new_options_survive_settings_and_profile_round_trip(self):
        config = {"normalizer_options": {
            "cleanup_malformed_punctuation": False,
            "enable_math_normalization": False,
            "initials_vowel_mode": "single",
        }}
        profile = studio.normalizer_profile_from_config(config)
        applied = studio.apply_normalizer_profile({}, profile)
        options = studio.resolved_normalizer_options(applied)
        self.assertFalse(options["cleanup_malformed_punctuation"])
        self.assertFalse(options["enable_math_normalization"])
        normalizer = studio.build_ru_normalizer(applied)
        self.assertEqual(normalizer.options.letter_vowel_mode, "single")
        self.assertEqual(normalizer.options.initials_vowel_mode, "single")

    def test_previous_profiles_gain_only_new_fields_without_mutating_input(self):
        for mode in ("tts", "safe"):
            with self.subTest(mode=mode):
                old = studio.normalizer_profile_from_config({"normalizer_mode": mode})
                old["version"] = 1
                for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
                    del old["normalizer"]["options"][key]
                old["normalizer"]["options"]["initials_vowel_mode"] = "single"
                snapshot = copy.deepcopy(old)
                migrated = studio.normalize_normalizer_profile(old)
                self.assertEqual(old, snapshot)
                self.assertEqual(migrated["version"], studio.NORMALIZER_PROFILE_VERSION)
                self.assertEqual(migrated["normalizer"]["options"]["initials_vowel_mode"], "single")
                self.assertEqual(migrated["normalizer"]["options"]["enable_math_normalization"], mode == "tts")

    def test_migration_does_not_hide_old_missing_fields_or_typos(self):
        for bad_key in ("enable_year_normalization", "enable_numeral_normalization"):
            old = studio.normalizer_profile_from_config({})
            old["version"] = 1
            del old["normalizer"]["options"][bad_key]
            with self.subTest(key=bad_key), self.assertRaisesRegex(ValueError, bad_key):
                studio.normalize_normalizer_profile(old)
        old = studio.normalizer_profile_from_config({})
        old["version"] = 1
        old["normalizer"]["options"]["enable_maths_normalization"] = True
        with self.assertRaisesRegex(ValueError, "enable_maths_normalization"):
            studio.normalize_normalizer_profile(old)

    def test_current_profile_requires_new_fields(self):
        for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
            profile = studio.normalizer_profile_from_config({})
            del profile["normalizer"]["options"][key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                studio.normalize_normalizer_profile(profile)

    def test_new_stage_flags_change_profile_identity(self):
        original = studio.normalizer_profile_from_config({})
        for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
            modified = copy.deepcopy(original)
            modified["normalizer"]["options"][key] = False
            with self.subTest(key=key):
                self.assertNotEqual(studio.normalizer_profile_identity(original), studio.normalizer_profile_identity(modified))

    def test_math_stage_can_be_disabled_without_turning_off_other_normalization(self):
        enabled = studio.TTSProcessor.normalization_context({}, {})
        disabled = studio.TTSProcessor.normalization_context({
            "normalizer_options": {"enable_math_normalization": False}
        }, {})
        source = "Это $x^2 + 3$."
        result = enabled.process_sentence_text(source)
        self.assertIn("икс в степени два плюс три", result.casefold())
        self.assertNotEqual(result, disabled.process_sentence_text(source))

    def test_math_plus_keeps_manual_accents_and_verbatim_terms(self):
        processor = studio.TTSProcessor.normalization_context({}, {
            "terms_ignore_case": {"замок": {
                "replacement": "з+амок", "verbatim": True,
            }},
        })
        text = processor.process_sentence_text("Значение $x^2 + 3$, замок и +аура.")
        self.assertIn("икс в степени два плюс три", text.casefold())
        self.assertIn("з+амок и +аура", text)


if __name__ == "__main__":
    unittest.main()
