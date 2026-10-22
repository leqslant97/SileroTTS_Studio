"""Настройка озвучки плюсов: перенос профилей и границы речевых сеансов."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_source_session as source_session
from test_regressions import studio


class MemoryVariable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class AutoPlusSettingsTests(unittest.TestCase):
    def test_missing_and_invalid_settings_keep_the_previous_enabled_default(self):
        for raw in ({}, {"auto_plus": None}, {"auto_plus": "unknown"}):
            with self.subTest(raw=raw):
                self.assertTrue(studio.normalize_config(raw)["auto_plus"])
                self.assertTrue(studio.normalizer_settings_snapshot(raw)["auto_plus"])
        for value in (False, "false", "off", 0):
            with self.subTest(value=value):
                self.assertFalse(studio.normalize_config({"auto_plus": value})["auto_plus"])

    def test_previous_profile_versions_gain_enabled_plus_without_mutating_input(self):
        for version in (1, 2):
            for mode in ("tts", "safe"):
                with self.subTest(version=version, mode=mode):
                    old = studio.normalizer_profile_from_config({"normalizer_mode": mode})
                    old["version"] = version
                    del old["preprocessing"]["auto_plus"]
                    if version == 1:
                        for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
                            del old["normalizer"]["options"][key]
                    original = copy.deepcopy(old)

                    migrated = studio.normalize_normalizer_profile(old)
                    applied = studio.apply_normalizer_profile({"auto_plus": False}, old)

                    self.assertEqual(old, original)
                    self.assertEqual(migrated["version"], studio.NORMALIZER_PROFILE_VERSION)
                    self.assertTrue(migrated["preprocessing"]["auto_plus"])
                    self.assertTrue(applied["auto_plus"])

    def test_current_profile_requires_a_boolean_plus_field(self):
        profile = studio.normalizer_profile_from_config({})
        del profile["preprocessing"]["auto_plus"]
        with self.assertRaisesRegex(ValueError, "auto_plus"):
            studio.normalize_normalizer_profile(profile)
        for version in (1, 2, studio.NORMALIZER_PROFILE_VERSION):
            for value in (None, "false", 0, [], {}):
                with self.subTest(version=version, value=value):
                    invalid = studio.normalizer_profile_from_config({})
                    invalid["version"] = version
                    invalid["preprocessing"]["auto_plus"] = value
                    with self.assertRaisesRegex(ValueError, "preprocessing.auto_plus"):
                        studio.normalize_normalizer_profile(invalid)
        profile = studio.normalizer_profile_from_config({})
        profile["preprocessing"]["auto_pluss"] = False
        with self.assertRaisesRegex(ValueError, "auto_pluss"):
            studio.normalize_normalizer_profile(profile)

    def test_disabled_plus_survives_portable_profile_and_library_round_trip(self):
        profile = studio.normalizer_profile_from_config({"auto_plus": False}, name="Без плюсов")
        entry = studio.make_normalizer_profile_entry(profile, profile_id="custom:" + "3" * 32)
        bundle = json.loads(json.dumps(studio.normalizer_profile_library_from_profiles([entry])))

        restored = studio.normalize_normalizer_profile_library(bundle)
        applied = studio.apply_normalizer_profile({"api_token": "preserve"}, restored[0]["profile"])

        self.assertEqual(restored, [entry])
        self.assertFalse(applied["auto_plus"])
        self.assertEqual(applied["api_token"], "preserve")

    def test_previous_unwrapped_profile_keeps_its_selected_identifier(self):
        old = studio.normalizer_profile_from_config({}, name="Старый профиль")
        old["version"] = 2
        del old["preprocessing"]["auto_plus"]
        old_payload = json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        old_id = "custom:" + hashlib.sha256(old_payload.encode("utf-8")).hexdigest()[:32]
        for version in (1, 2):
            with self.subTest(version=version):
                legacy = copy.deepcopy(old)
                legacy["version"] = version
                if version == 1:
                    for key in ("cleanup_malformed_punctuation", "enable_math_normalization"):
                        del legacy["normalizer"]["options"][key]

                restored = studio.normalize_config({
                    "normalizer_profiles": [legacy],
                    "default_normalizer_profile_id": old_id,
                })

                self.assertEqual(restored["default_normalizer_profile_id"], old_id)
                self.assertEqual(restored["normalizer_profiles"][0]["id"], old_id)
                self.assertTrue(restored["normalizer_profiles"][0]["profile"]["preprocessing"]["auto_plus"])

    def test_profile_identity_and_global_summary_include_plus(self):
        enabled = studio.normalizer_profile_from_config({})
        disabled = studio.normalizer_profile_from_config({"auto_plus": False})
        self.assertNotEqual(studio.normalizer_profile_identity(enabled), studio.normalizer_profile_identity(disabled))
        self.assertIsNone(studio.matching_normalizer_profile_entry({"auto_plus": False}))
        self.assertEqual(studio.describe_normalizer_settings({})["profile"], "TTS (для озвучки)")
        self.assertIn("озвучка плюсов", studio.describe_normalizer_settings({})["details"])
        self.assertNotIn("озвучка плюсов", studio.describe_normalizer_settings({"auto_plus": False})["details"])
        self.assertIn("Пользовательский", studio.describe_normalizer_settings({"auto_plus": False})["profile"])
        for entry in studio.builtin_normalizer_profile_entries():
            self.assertTrue(entry["profile"]["preprocessing"]["auto_plus"])

    def test_selective_settings_transfer_includes_plus_only_in_its_groups(self):
        config = studio.normalize_config({"auto_plus": False, "api_token": "secret"})
        for group in ("normalizer", "cache"):
            with self.subTest(group=group):
                exported = studio.select_config_values(config, [group], include_api_token=False)
                restored = studio.merge_config_values({"auto_plus": True, "api_token": "keep"}, exported, [group])
                self.assertFalse(exported["auto_plus"])
                self.assertFalse(restored["auto_plus"])
                self.assertEqual(restored["api_token"], "keep")
        self.assertNotIn("auto_plus", studio.select_config_values(config, ["api", "effects"]))

    def test_identical_final_text_reuses_persistent_keys_in_both_states(self):
        text = "Обычный з+амок."
        for steps in ({}, {"api_steps_enabled": True, "api_steps": 16},
                      {"api_steps_enabled": True, "api_steps": 16, "cache_include_steps": False}):
            with self.subTest(steps=steps):
                enabled = studio.TTSProcessor.normalization_context(dict(steps, auto_plus=True), {})
                disabled = studio.TTSProcessor.normalization_context(dict(steps, auto_plus=False), {})
                self.assertEqual(enabled.get_hash(text), disabled.get_hash(text))


class AutoPlusProcessingTests(unittest.TestCase):
    def test_missing_flag_and_enabled_keep_legacy_preprocessing_results(self):
        cases = (
            ("Два + три.", "Два плюс три."),
            ("Поставь + к ответу.", "Поставь плюс к ответу."),
            ("Знак +.", "Знак плюс."),
            ("1+1=2.", "один плюс один равно два."),
            ("Старинный з+амок и +аура.", "Старинный з+амок и +аура."),
            ("Значение $x^2 + 3$.", "Значение икс в степени два плюс три."),
            ("Звоните +7 (495) 123-45-67.",
             "Звоните плюс семь сто двадцать три — сорок пять — шестьдесят семь."),
        )
        for missing in (True, False):
            context = studio.TTSProcessor.normalization_context({"auto_plus": True}, {})
            if missing:
                context.cfg.pop("auto_plus")
            for source, expected in cases:
                with self.subTest(missing=missing, source=source):
                    self.assertEqual(context.process_sentence_text(source), expected)

    def test_disabled_plus_leaves_supported_number_rules_to_the_library(self):
        phone = "Звоните +7 (495) 123-45-67."
        spoken_phone = (
            "Звоните плюс семь, четыреста девяносто пять, сто двадцать три, "
            "сорок пять, шестьдесят семь."
        )
        profiles = (
            ({}, "один плюс один равно два.", "Температура плюс пять градусов.", spoken_phone),
            ({"normalizer_mode": "safe"}, "один плюс один равно два.", "Температура плюс пять градусов.", spoken_phone),
            ({"normalizer_options": {"enable_math_normalization": False}},
             "один плюс один равно два.", "Температура плюс пять градусов.", spoken_phone),
            ({"normalizer_options": {"enable_numeral_normalization": False}},
             "1+1=2.", "Температура плюс 5 градусов.", "Звоните плюс 7 123-45-67."),
        )
        for config, arithmetic, positive, phone_result in profiles:
            context = studio.TTSProcessor.normalization_context(dict(config, auto_plus=False), {})
            for source, expected in (("1+1=2.", arithmetic),
                                     ("Температура +5 градусов.", positive),
                                     (phone, phone_result),
                                     ("Поставь + к ответу.", "Поставь + к ответу.")):
                with self.subTest(config=config, source=source):
                    self.assertEqual(context._normalizer.normalize(source), expected)
                    self.assertEqual(context.process_sentence_text(source), expected)

    def test_plus_and_library_switches_are_independent_of_other_studio_stages(self):
        source = "И.И. в ур. поставил +."
        for auto_plus, expected in ((True, "И-И в ур поставил плюс ."),
                                    (False, "И-И в ур поставил +.")):
            with self.subTest(auto_plus=auto_plus):
                context = studio.TTSProcessor.normalization_context({
                    "auto_plus": auto_plus, "normalizer_enabled": False,
                }, {})
                self.assertIsNone(context._normalizer)
                self.assertEqual(context.process_sentence_text(source), expected)
        context = studio.TTSProcessor.normalization_context({
            "auto_plus": False, "normalizer_enabled": False,
        }, {})
        self.assertEqual(context.process_sentence_text("1+1=2."), "1+1=2.")
        self.assertEqual(context.process_sentence_text("Звоните +7 (495) 123-45-67."),
                         "Звоните +7 495 123-45-67.")

    def test_manual_stress_and_exact_glossary_replacements_survive_both_states(self):
        glossary = {"terms_ignore_case": {
            "замок": {"replacement": "з+амок", "verbatim": True},
            "убогого": {"replacement": "уб+о'го", "verbatim": True},
        }}
        for config in ({}, {"normalizer_mode": "safe"}, {"normalizer_enabled": False},
                       {"normalizer_options": {"enable_numeral_normalization": False}}):
            for auto_plus in (True, False):
                with self.subTest(config=config, auto_plus=auto_plus):
                    context = studio.TTSProcessor.normalization_context(dict(config, auto_plus=auto_plus), glossary)
                    self.assertEqual(context.process_sentence_text("Старинный з+амок и +аура."),
                                     "Старинный з+амок и +аура.")
                    self.assertEqual(context.process_sentence_text("Вот замок и убогого."),
                                     "Вот з+амок и уб+о'го.")
        for auto_plus in (True, False):
            context = studio.TTSProcessor.normalization_context({"auto_plus": auto_plus}, glossary)
            self.assertEqual(context.process_sentence_text("Значение $x^2 + 3$, замок и +аура."),
                             "Значение икс в степени два плюс три, з+амок и +аура.")

    def test_prepared_text_bypasses_both_plus_states_and_the_library(self):
        source = "Р+еплика 62-й: «уб+о'го». Итог +5."
        for auto_plus in (True, False):
            with self.subTest(auto_plus=auto_plus):
                context = studio.TTSProcessor.normalization_context({
                    "auto_plus": auto_plus, "text_is_prepared": True,
                }, {"terms_ignore_case": {"Итог": "Замена"}})
                self.assertIsNone(context._normalizer)
                context._normalizer = mock.Mock()
                self.assertEqual(context.process_sentence_text(source), source)
                with self.assertRaises(ValueError):
                    context.process_sentence_text("Р+еплика\t+5.")
                context._normalizer.normalize.assert_not_called()

    def test_preview_synthesis_and_cache_use_the_actual_text_in_each_state(self):
        sources = ("Поставь + к ответу.", "Звоните +7 (495) 123-45-67.",
                   "Старинный з+амок и +аура.")
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("sentence", "paragraph", "full"):
                for source in sources:
                    outputs, candidates = {}, {}
                    for auto_plus in (True, False):
                        with self.subTest(mode=mode, source=source, auto_plus=auto_plus):
                            context = studio.TTSProcessor.normalization_context({
                                "auto_plus": auto_plus, "synthesis_mode": mode,
                                "output_dir": directory, "pause_file_start": 0,
                                "pause_file_end": 0,
                            }, {})
                            context.is_stopped = False
                            context._save_cache = mock.Mock()
                            context.synthesize_sentence = mock.Mock(
                                return_value=(Path(directory) / "speech.ogg", True)
                            )
                            context._get_silence_file = mock.Mock(return_value=Path(directory) / "pause.ogg")
                            text = context.process_sentence_text(source)
                            preview = context.preview_normalization(source)
                            hashes = context.get_all_possible_hashes(source)

                            result = context.process_raw_text(
                                source, "chapter.mp3", save_to_disk=False, return_audio_files=True
                            )

                            self.assertEqual(result["status"], "success")
                            self.assertEqual(preview["normalized_text"], text)
                            self.assertEqual([call.args[0] for call in context.synthesize_sentence.call_args_list], [text])
                            content_hash = studio.cache_content_hash(text, context.cfg["speaker"])
                            self.assertEqual(context.get_hash(text), content_hash)
                            self.assertIn(content_hash, hashes)
                            outputs[auto_plus] = text
                            candidates[auto_plus] = hashes
                    if "+ к" in source or source.startswith("Звоните"):
                        self.assertNotEqual(outputs[True], outputs[False])
                        for auto_plus in (True, False):
                            other = studio.cache_content_hash(outputs[not auto_plus], context.cfg["speaker"])
                            self.assertNotIn(other, candidates[auto_plus])
                    else:
                        self.assertEqual(outputs[True], outputs[False])
                        self.assertEqual(candidates[True], candidates[False])

    def test_disabling_the_stage_does_not_make_a_bare_operator_synthesizable(self):
        enabled = studio.TTSProcessor.normalization_context({"auto_plus": True}, {})
        disabled = studio.TTSProcessor.normalization_context({"auto_plus": False}, {})
        self.assertEqual(enabled.preview_normalization("+")["normalized_text"], "плюс.")
        preview = disabled.preview_normalization("+")
        self.assertEqual(preview["normalized_text"], "")
        self.assertEqual(len(preview["skipped"]), 1)
        self.assertEqual(disabled.get_all_possible_hashes("+"), set())


class AutoPlusPreviewSettingsTests(unittest.TestCase):
    def app(self):
        app = object.__new__(studio.TTSApp)
        app.config = studio.normalize_config({})
        for name in ("normalizer_preview_enabled_var", "normalizer_preview_glossary_var",
                     "normalizer_preview_auto_plus_var", "normalizer_preview_auto_abbr_var",
                     "normalizer_preview_short_words_var", "normalizer_ignore_start_var",
                     "normalizer_ignore_end_var", "normalizer_profile_choice"):
            setattr(app, name, MemoryVariable())
        app.normalizer_preview_vars = {
            key: MemoryVariable() for key in studio.resolved_normalizer_options({})
        }
        app._refresh_normalizer_profile_choices = mock.Mock()
        app._refresh_normalizer_scope_status = mock.Mock()
        app._refresh_global_normalizer_settings_summary = mock.Mock()
        app._persist_settings_snapshot = mock.Mock()
        app._show_info = mock.Mock()
        app._show_error = mock.Mock()
        app.settings_vars = {"auto_plus": MemoryVariable(True)}
        app._set_normalizer_preview_config(app.config)
        return app

    def test_loading_and_collecting_plus_is_local_until_global_apply(self):
        app = self.app()
        self.assertTrue(app.normalizer_preview_auto_plus_var.get())
        app.normalizer_preview_auto_plus_var.set(False)

        local = app._collect_normalizer_preview_config()

        self.assertFalse(local["auto_plus"])
        self.assertTrue(app.config["auto_plus"])
        self.assertTrue(app.settings_vars["auto_plus"].get())
        app.apply_normalizer_preview_globally()
        self.assertFalse(app.config["auto_plus"])
        self.assertFalse(app.settings_vars["auto_plus"].get())
        self.assertFalse(app._persist_settings_snapshot.call_args.args[0]["auto_plus"])
        app._show_error.assert_not_called()

    def test_loading_disabled_profile_updates_the_local_checkbox(self):
        app = self.app()
        applied = studio.apply_normalizer_profile(
            app.config, studio.normalizer_profile_from_config({"auto_plus": False})
        )
        app._set_normalizer_preview_config(applied)
        self.assertFalse(app.normalizer_preview_auto_plus_var.get())
        self.assertTrue(app.config["auto_plus"])


class AutoPlusSessionTests(unittest.TestCase):
    def test_temporary_speech_scope_changes_when_plus_is_disabled(self):
        app = object.__new__(studio.TTSApp)
        old = {"glossary_enabled": False}
        baseline = app._session_audio_scope(old, "direct", "Два + три.")
        enabled = app._session_audio_scope(dict(old, auto_plus=True), "direct", "Два + три.")
        disabled = app._session_audio_scope(dict(old, auto_plus=False), "direct", "Два + три.")
        self.assertEqual(baseline, enabled)
        self.assertNotEqual(enabled, disabled)

    def test_default_plus_preserves_the_previous_completed_result_signature(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"glossary_enabled": False}
        previous = app._source_session_settings_signature()
        app.config["auto_plus"] = True
        with mock.patch.object(studio.json, "dumps", wraps=json.dumps) as dumps:
            enabled = app._source_session_settings_signature()
        self.assertNotIn("auto_plus", dumps.call_args.args[0])
        app.config["auto_plus"] = False
        disabled = app._source_session_settings_signature()
        self.assertEqual(previous, enabled)
        self.assertNotEqual(enabled, disabled)

    def test_restored_source_keeps_default_completion_and_invalidates_disabled_plus(self):
        case = source_session.SourceSessionTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        app = case.app()
        app.config.pop("auto_plus")
        case.add_group(app)
        app._remember_source_completed_regular_targets(app._source_plan_revision, [case.record()])
        app._source_plan_dirty = False
        app._save_source_session()

        enabled = case.app({"auto_plus": True})
        self.assertTrue(enabled._source_completed_regular_targets)
        disabled = case.app({"auto_plus": False})
        self.assertEqual(disabled._source_completed_regular_targets, {})
        self.assertIn("m4b_plan:my-group", disabled._source_plan_groups)


if __name__ == "__main__":
    unittest.main()
