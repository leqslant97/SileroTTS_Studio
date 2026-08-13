import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_DIR / "SileroTTS_Studio.py"


def load_studio_module():
    """Imports the single-file application without starting Tk's mainloop."""
    module_name = "silero_tts_studio_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


studio = load_studio_module()


class ApiStepsTests(unittest.TestCase):
    def test_disabled_and_legacy_config_omit_steps(self):
        for config in ({}, {"api_steps_enabled": False, "api_steps": 16}):
            with self.subTest(config=config):
                self.assertIsNone(studio.resolve_api_steps(config))

    def test_custom_value_in_current_api_range_is_supported(self):
        self.assertEqual(
            studio.resolve_api_steps(
                {"api_steps_enabled": "true", "api_steps": "72"}
            ),
            72,
        )

    def test_invalid_values_are_rejected_only_when_enabled(self):
        invalid_values = (True, 0, -1, 73, 1024, 1.5, "", "1.5", "eight")
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    studio.resolve_api_steps(
                        {"api_steps_enabled": True, "api_steps": value}
                    )

        # A stale invalid value in an old/disabled config must not break startup.
        self.assertIsNone(
            studio.resolve_api_steps(
                {"api_steps_enabled": False, "api_steps": "unfinished"}
            )
        )

    def test_warning_thresholds(self):
        self.assertIsNone(studio.get_api_steps_warning(16))
        self.assertIn("выше 16", studio.get_api_steps_warning(17))
        self.assertIn("32", studio.get_api_steps_warning(32))

    def test_enabled_steps_default_to_openapi_value_16(self):
        self.assertEqual(
            studio.resolve_api_steps({"api_steps_enabled": True}),
            16,
        )

    def test_payload_contains_no_reserved_emotion_fields(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "api_token": "token",
            "speaker": "voice",
            "api_steps_enabled": True,
            "api_steps": 12,
            "api_emotion_enabled": True,
            "api_emotion": "happy",
        }

        payload = processor.build_api_payload("Тест.")

        self.assertEqual(payload["steps"], 12)
        self.assertNotIn("emotion", payload)
        self.assertNotIn("api_emotion", payload)

    def test_payload_omits_steps_when_disabled(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "api_token": "token",
            "speaker": "voice",
            "api_steps_enabled": False,
            "api_steps": 16,
        }
        self.assertNotIn("steps", processor.build_api_payload("Тест."))

    def test_hash_is_legacy_compatible_when_steps_are_disabled(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "speaker": "voice",
            "api_steps_enabled": False,
            "api_steps": 16,
        }
        expected = hashlib.md5("Фраза_voice".encode("utf-8")).hexdigest()
        self.assertEqual(processor.get_hash("Фраза"), expected)

    def test_hash_separates_steps_by_default(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "speaker": "voice",
            "api_steps_enabled": True,
            "api_steps": 8,
        }
        hash_8 = processor.get_hash("Фраза")
        processor.cfg["api_steps"] = 16
        hash_16 = processor.get_hash("Фраза")
        self.assertNotEqual(hash_8, hash_16)

    def test_explicit_legacy_cache_flag_keeps_shared_hash(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "speaker": "voice",
            "api_steps_enabled": True,
            "api_steps": 8,
            "cache_include_steps": False,
        }
        hash_8 = processor.get_hash("Фраза")
        processor.cfg["api_steps"] = 16
        self.assertEqual(hash_8, processor.get_hash("Фраза"))

    def test_legacy_cache_mode_checks_known_step_mismatches(self):
        match = studio.TTSProcessor._steps_match_cache_entry
        self.assertTrue(match({}, 8))
        self.assertTrue(match({"steps": "8"}, 8))
        self.assertFalse(match({"steps": 16}, 8))
        self.assertFalse(match({"steps": 8}, None))
        self.assertTrue(match({}, None))


class CacheVariantPolicyTests(unittest.TestCase):
    def test_variant_statistics_distinguish_legacy_and_steps(self):
        stats = studio.analyze_cache_step_variants(
            {
                "legacy": {},
                "step8": {"steps": 8, "steps_in_cache_key": True},
                "shared16": {"steps": "16", "steps_in_cache_key": False},
            }
        )

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["legacy"], 1)
        self.assertEqual(stats["steps"], 2)
        self.assertEqual(stats["steps_by_value"], {8: 1, 16: 1})
        self.assertEqual(stats["shared_steps"], 1)

    def test_safe_policies_keep_expected_variants(self):
        keep = studio.should_keep_cache_variant
        legacy = {}
        step8 = {"steps": 8}
        step16 = {"steps": "16"}

        self.assertTrue(keep(step8, studio.CACHE_VARIANT_KEEP_ALL, 16))
        self.assertTrue(keep(legacy, studio.CACHE_VARIANT_KEEP_LEGACY, None))
        self.assertFalse(keep(step8, studio.CACHE_VARIANT_KEEP_LEGACY, None))
        self.assertTrue(
            keep(legacy, studio.CACHE_VARIANT_KEEP_LEGACY_CURRENT, 16)
        )
        self.assertTrue(
            keep(step16, studio.CACHE_VARIANT_KEEP_LEGACY_CURRENT, 16)
        )
        self.assertFalse(
            keep(step8, studio.CACHE_VARIANT_KEEP_LEGACY_CURRENT, 16)
        )
        self.assertTrue(keep(step16, studio.CACHE_VARIANT_KEEP_CURRENT, 16))
        self.assertFalse(keep(legacy, studio.CACHE_VARIANT_KEEP_CURRENT, 16))

    def test_choice_is_skipped_when_cache_has_no_explicit_steps(self):
        choose = studio.TTSApp._default_cache_variant_policy
        stats = studio.analyze_cache_step_variants({"legacy": {}})

        self.assertEqual(
            choose(None, True, stats),
            (studio.CACHE_VARIANT_KEEP_ALL, False),
        )

    def test_enabled_steps_default_preserves_legacy_and_current(self):
        choose = studio.TTSApp._default_cache_variant_policy
        stats = studio.analyze_cache_step_variants(
            {"legacy": {}, "step8": {"steps": 8}}
        )

        self.assertEqual(
            choose(16, True, stats),
            (studio.CACHE_VARIANT_KEEP_LEGACY_CURRENT, True),
        )

    def test_steps_entry_matches_through_canonical_content_hash(self):
        normalized = "Тест."
        speaker = "voice"
        content_hash = studio.cache_content_hash(normalized, speaker)
        shared_entry = {
            "normalized_text": normalized,
            "speaker": speaker,
            "steps": 16,
            "steps_in_cache_key": False,
        }

        self.assertTrue(
            studio.cache_entry_matches_required_text(
                shared_entry, {content_hash}
            )
        )

    def test_entry_without_content_metadata_is_stale_even_if_key_matches(self):
        content_hash = studio.cache_content_hash("Тест.", "voice")

        self.assertFalse(
            studio.cache_entry_matches_required_text({}, {content_hash})
        )


class CacheIndexFormatTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def make_processor(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {"speaker": "voice"}
        processor.cache_dir = self.root / "cache"
        processor.cache_audio_dir = processor.cache_dir / "audio"
        processor.cache_audio_dir.mkdir(parents=True)
        processor.cache_index_path = processor.cache_dir / "sentence_cache.json"
        return processor

    def test_loads_current_metadata_format_without_rewriting_it(self):
        processor = self.make_processor()
        current_entry = {
            "file_name": "current.ogg",
            "original_text": "Тест.",
            "normalized_text": "Тест.",
            "speaker": "voice",
            "created_at": 1.0,
            "last_accessed": 2.0,
            "usage_count": 3,
        }
        processor.cache_index_path.write_text(
            json.dumps({"hash": current_entry}), encoding="utf-8"
        )

        self.assertEqual(processor._load_cache(), {"hash": current_entry})

    def test_rejects_removed_string_only_cache_format(self):
        processor = self.make_processor()
        processor.cache_index_path.write_text(
            json.dumps({"hash": "audio/old.ogg"}), encoding="utf-8"
        )

        with self.assertLogs(level=logging.ERROR) as captured:
            loaded = processor._load_cache()

        self.assertEqual(loaded, {})
        self.assertIn("не является JSON-объектом", "\n".join(captured.output))


class DirectTagSessionSettingTests(unittest.TestCase):
    def test_direct_tag_setting_is_not_a_persistent_default(self):
        self.assertNotIn("direct_apply_tags", studio.DEFAULT_CONFIG)

    def test_old_persisted_value_is_discarded_on_load(self):
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Path(tempdir) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "direct_apply_tags": True,
                        "api_emotion_enabled": True,
                        "api_emotion": "happy",
                        "output_bitrate": "192k",
                    }
                ),
                encoding="utf-8",
            )
            app = object.__new__(studio.TTSApp)

            config = app.load_settings(settings)

            self.assertNotIn("direct_apply_tags", config)
            self.assertNotIn("api_emotion_enabled", config)
            self.assertNotIn("api_emotion", config)
            self.assertEqual(config["output_bitrate"], "192k")

    def test_old_disabled_steps_cache_flag_is_preserved(self):
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Path(tempdir) / "settings.json"
            settings.write_text(
                json.dumps({"cache_include_steps": False}),
                encoding="utf-8",
            )
            app = object.__new__(studio.TTSApp)

            config = app.load_settings(settings)

            self.assertFalse(config["cache_include_steps"])

    def test_ui_update_never_persists_session_checkbox(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"direct_apply_tags": True}
        app.settings_vars = {
            "direct_apply_tags": mock.Mock(get=mock.Mock(return_value=True)),
            "output_bitrate": mock.Mock(get=mock.Mock(return_value="256k")),
        }

        app.update_config_from_ui()

        self.assertNotIn("direct_apply_tags", app.config)
        self.assertEqual(app.config["output_bitrate"], "256k")


class DirectOutputDirectoryTests(unittest.TestCase):
    def test_direct_output_directory_has_an_independent_default(self):
        self.assertIn("direct_output_dir", studio.DEFAULT_CONFIG)
        self.assertNotEqual(
            studio.DEFAULT_CONFIG["direct_output_dir"],
            studio.DEFAULT_CONFIG["output_dir"],
        )

    def test_direct_output_directory_belongs_to_folder_import_group(self):
        app = object.__new__(studio.TTSApp)
        self.assertIn(
            "direct_output_dir",
            app._config_group_rules()["folders"],
        )

    def test_direct_tab_path_is_written_to_config(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"direct_output_dir": "old"}
        app.settings_vars = {}
        app.direct_output_dir_var = mock.Mock(
            get=mock.Mock(return_value="new-direct")
        )

        app.update_config_from_ui()

        self.assertEqual(app.config["direct_output_dir"], "new-direct")


class ConfigurationProfileTests(unittest.TestCase):
    def test_workspace_defaults_are_exportable_as_a_separate_group(self):
        rules = studio._config_group_rules_data()

        self.assertEqual(
            rules["workspace"],
            {
                "direct_filename",
                "direct_save",
                "direct_force",
                "direct_autoplay",
                "import_template",
                "import_regex",
                "import_single_file",
            },
        )

    def test_ui_history_and_font_are_not_exported_with_any_profile_group(self):
        exported_keys = set().union(*studio._config_group_rules_data().values())

        self.assertNotIn("last_browse_dir", exported_keys)
        self.assertNotIn("last_config_dir", exported_keys)
        self.assertNotIn("last_glossary_dir", exported_keys)
        self.assertNotIn("ui_font_size", exported_keys)

    def test_direct_output_path_stays_in_folder_group_despite_single_ui_field(self):
        self.assertIn(
            "direct_output_dir", studio._config_group_rules_data()["folders"]
        )

    def test_import_without_folder_group_never_changes_any_path(self):
        current = {
            "input_dir": "local-input",
            "output_dir": "local-output",
            "direct_output_dir": "local-direct",
            "cache_dir": "local-cache",
            "export_dir": "local-export",
            "import_outdir": "local-import",
            "speaker": "old-voice",
        }
        imported = {
            "input_dir": "foreign-input",
            "output_dir": "foreign-output",
            "direct_output_dir": "foreign-direct",
            "cache_dir": "foreign-cache",
            "export_dir": "foreign-export",
            "import_outdir": "foreign-import",
            "speaker": "new-voice",
        }

        merged = studio.merge_config_values(current, imported, ["api"])

        for key in studio._config_group_rules_data()["folders"]:
            self.assertEqual(merged[key], current[key])
        self.assertEqual(merged["speaker"], "new-voice")

    def test_export_profile_never_contains_ui_history(self):
        profile = studio.select_config_values(
            {
                **studio.DEFAULT_CONFIG,
                "last_browse_dir": "/private",
                "last_config_dir": "/private/config",
                "last_glossary_dir": "/private/glossary",
            },
            studio._config_group_rules_data().keys(),
        )

        self.assertNotIn("last_browse_dir", profile)
        self.assertNotIn("last_config_dir", profile)
        self.assertNotIn("last_glossary_dir", profile)

    def test_api_token_requires_explicit_profile_opt_in(self):
        config = {"api_token": "secret", "speaker": "voice"}

        public_profile = studio.select_config_values(
            config, ["api"], include_api_token=False
        )
        secret_profile = studio.select_config_values(
            config, ["api"], include_api_token=True
        )

        self.assertEqual(public_profile, {"speaker": "voice"})
        self.assertEqual(secret_profile["api_token"], "secret")

    def test_api_token_is_not_imported_without_explicit_opt_in(self):
        merged = studio.merge_config_values(
            {"api_token": "local", "speaker": "old"},
            {"api_token": "foreign", "speaker": "new"},
            ["api"],
            include_api_token=False,
        )

        self.assertEqual(merged["api_token"], "local")
        self.assertEqual(merged["speaker"], "new")


class ConfigurationValidationTests(unittest.TestCase):
    def test_official_http_api_url_is_preserved_verbatim(self):
        api_url = "http://iq3g.silero.ai/enhanced_voice"

        config = studio.normalize_config({"api_url": api_url})

        self.assertEqual(studio.DEFAULT_API_URL, api_url)
        self.assertEqual(config["api_url"], api_url)

    def test_invalid_numeric_values_fall_back_without_losing_unknown_keys(self):
        with self.assertLogs(level=logging.WARNING):
            config = studio.normalize_config(
                {
                    "api_max_requests": "many",
                    "max_parallel_encodes": -4,
                    "fx_speed": 0,
                    "future_setting": {"enabled": True},
                }
            )

        self.assertEqual(
            config["api_max_requests"], studio.DEFAULT_CONFIG["api_max_requests"]
        )
        self.assertEqual(
            config["max_parallel_encodes"],
            studio.DEFAULT_CONFIG["max_parallel_encodes"],
        )
        self.assertEqual(config["fx_speed"], studio.DEFAULT_CONFIG["fx_speed"])
        self.assertEqual(config["future_setting"], {"enabled": True})

    def test_invalid_enum_and_empty_required_paths_use_defaults(self):
        config = studio.normalize_config(
            {
                "output_format": "flac",
                "synthesis_mode": "unknown",
                "input_dir": "",
                "cache_dir": None,
            }
        )

        self.assertEqual(config["output_format"], "mp3")
        self.assertEqual(config["synthesis_mode"], "sentence")
        self.assertEqual(config["input_dir"], studio.DEFAULT_INPUT_DIR)
        self.assertEqual(config["cache_dir"], studio.DEFAULT_CACHE_DIR)

    def test_string_booleans_are_normalized(self):
        config = studio.normalize_config(
            {"use_cache": "false", "direct_save": "true"}
        )

        self.assertFalse(config["use_cache"])
        self.assertTrue(config["direct_save"])

    def test_config_bool_handles_json_tk_and_invalid_strings(self):
        truthy = (True, 1, "1", "TRUE", " yes ", "on")
        falsy = (False, 0, "0", "FALSE", " no ", "off", "")

        for value in truthy:
            with self.subTest(value=value):
                self.assertTrue(studio._config_bool(value))
        for value in falsy:
            with self.subTest(value=value):
                self.assertFalse(studio._config_bool(value, default=True))

        self.assertTrue(studio._config_bool(None, default=True))
        self.assertTrue(studio._config_bool("unknown", default=True))
        self.assertFalse(studio._config_bool("unknown", default=False))

    def test_processor_normalizes_string_booleans_without_legacy_cache_logic(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = studio.DEFAULT_CONFIG.copy()
            config.update(
                input_dir=str(root / "input"),
                output_dir=str(root / "output"),
                cache_dir=str(root / "cache"),
                use_cache="false",
                auto_trim_silence="false",
                enable_cache_lru="false",
                enable_cache_ttl="false",
                fx_echo="false",
            )

            processor = studio.TTSProcessor(
                config,
                shared_cache={},
                shared_processing_statuses={},
            )

            self.assertFalse(processor.cfg["use_cache"])
            self.assertFalse(processor.cfg["auto_trim_silence"])
            self.assertFalse(processor.cfg["enable_cache_lru"])
            self.assertFalse(processor.cfg["enable_cache_ttl"])
            self.assertFalse(processor.cfg["fx_echo"])


class DialogPathTests(unittest.TestCase):
    def test_empty_or_missing_paths_fall_back_to_project_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            missing = Path(tempdir) / "missing"
            self.assertEqual(
                studio.resolve_dialog_initial_dir("", missing),
                str(studio.BASE_DIR),
            )

    def test_first_existing_candidate_wins_and_file_path_uses_parent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            existing = root / "folder"
            existing.mkdir()
            selected_file = existing / "profile.json"
            selected_file.write_text("{}", encoding="utf-8")

            self.assertEqual(
                studio.resolve_dialog_initial_dir(
                    root / "missing", existing, studio.BASE_DIR
                ),
                str(existing.resolve()),
            )
            self.assertEqual(
                studio.resolve_dialog_initial_dir(
                    selected_file, file_path=True
                ),
                str(existing.resolve()),
            )



class ClipboardNormalizationTests(unittest.TestCase):
    def test_plain_text_keeps_whitespace_and_percent_sequences(self):
        values = (
            "  Скидка 50%20 сегодня.\n",
            "Cafe\u0301",
            "file://[повреждённый адрес",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(studio.normalize_clipboard_text(value), value)

    def test_file_url_is_unquoted(self):
        self.assertEqual(
            studio.normalize_clipboard_text(
                "file:///Users/test/%D0%9C%D0%BE%D1%8F%20%D0%BA%D0%BD%D0%B8%D0%B3%D0%B0.txt"
            ),
            "/Users/test/Моя книга.txt",
        )

    def test_quoted_absolute_path_is_unquoted(self):
        self.assertEqual(
            studio.normalize_clipboard_text('"/tmp/My%20Book.txt"'),
            "/tmp/My Book.txt",
        )


class ExportGroupingTests(unittest.TestCase):
    def test_new_export_paths_are_deduplicated_before_batching(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.mp3"
            second = root / "two.mp3"
            existing = root / "existing.mp3"

            result = studio.unique_new_file_paths(
                [first, first, second, existing],
                [existing],
            )

            self.assertEqual(result, (str(first), str(second)))

    def test_root_files_and_group_children_are_all_preserved_in_tree_order(self):
        ordered = studio.ordered_export_file_ids(
            ("root-a", "group-1", "root-b"),
            {"group-1": ("child-a", "child-b")},
            ("root-a", "root-b", "child-a", "child-b"),
        )

        self.assertEqual(
            ordered, ["root-a", "child-a", "child-b", "root-b"]
        )

    def test_split_works_without_preexisting_groups(self):
        groups = studio.split_export_file_ids(
            ["a", "b", "c"], {"a": 40, "b": 30, "c": 20}, 60
        )

        self.assertEqual(groups, [["a"], ["b", "c"]])

    def test_split_rejects_nonpositive_duration_limit(self):
        with self.assertRaises(ValueError):
            studio.split_export_file_ids(["a"], {"a": 1}, 0)

    def test_sequence_padding_is_adaptive(self):
        self.assertEqual(studio.format_sequence_number(1, 9), "1")
        self.assertEqual(studio.format_sequence_number(1, 10), "01")
        self.assertEqual(studio.format_sequence_number(10, 10), "10")
        self.assertEqual(
            studio.format_sequence_number(8, 3, start_index=8), "08"
        )

    def test_subfolder_is_effective_only_for_unmerged_groups(self):
        self.assertFalse(studio.effective_group_subfolder(True, True))
        self.assertTrue(studio.effective_group_subfolder(False, True))
        self.assertFalse(studio.effective_group_subfolder(False, False))

    def test_filename_component_is_safe_on_all_supported_platforms(self):
        self.assertEqual(
            studio.sanitize_filename_component('  Том: 1 / "Финал".  '),
            'Том_ 1 _ _Финал_',
        )
        self.assertEqual(studio.sanitize_filename_component("CON"), "_CON")
        self.assertEqual(studio.sanitize_filename_component("CON.txt"), "_CON.txt")
        self.assertEqual(
            studio.sanitize_filename_component("<>:\\/?*", fallback="Группа"),
            "_______",
        )

    def test_filename_component_uses_fallback_for_blank_name(self):
        self.assertEqual(
            studio.sanitize_filename_component(" ... ", fallback="Глава 1"),
            "Глава 1",
        )

    def test_duplicate_output_paths_are_reported_once(self):
        self.assertEqual(
            studio.duplicate_paths(["a.mp3", "b.mp3", "a.mp3", "a.mp3"]),
            ["a.mp3"],
        )

    def test_direct_output_name_is_cross_platform_safe(self):
        self.assertEqual(
            studio.normalize_output_filename(r"C:\\temp\\CON.wav", "mp3"),
            "_CON.mp3",
        )

    def test_cover_is_only_forwarded_to_mp3_export(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cover = Path(tempdir) / "cover.png"
            cover.write_bytes(b"image")

            mp3 = studio.audio_export_kwargs("mp3", "128k", {"title": "A"}, cover)
            ogg = studio.audio_export_kwargs("ogg", "128k", {"title": "A"}, cover)

            self.assertEqual(mp3["cover"], str(cover))
            self.assertEqual(mp3["bitrate"], "128k")
            self.assertNotIn("cover", ogg)
            self.assertNotIn("bitrate", ogg)


class CacheFileSafetyTests(unittest.TestCase):
    def test_cache_audio_path_rejects_escape_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self.assertIsNone(studio.resolve_cache_audio_path(root, "../secret"))
            self.assertIsNone(
                studio.resolve_cache_audio_path(root, str(root / "absolute.ogg"))
            )
            self.assertEqual(
                studio.resolve_cache_audio_path(root, "safe.ogg"),
                root / "audio" / "safe.ogg",
            )

    def test_cache_clear_preserves_glossary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "audio").mkdir()
            (root / "silences").mkdir()
            owned_audio = root / "audio" / ("a" * 32 + ".ogg")
            foreign_audio = root / "audio" / "entry.ogg"
            owned_silence = root / "silences" / "silence_100ms.ogg"
            foreign_silence = root / "silences" / "pause.ogg"
            owned_audio.write_bytes(b"audio")
            foreign_audio.write_bytes(b"foreign audio")
            owned_silence.write_bytes(b"pause")
            foreign_silence.write_bytes(b"foreign pause")
            (root / "sentence_cache.json").write_text("{}", encoding="utf-8")
            glossary = root / "glossary.json"
            glossary.write_text('{"terms": {}}', encoding="utf-8")

            studio.clear_cache_storage(root)

            self.assertTrue(glossary.exists())
            self.assertTrue((root / "audio").is_dir())
            self.assertFalse(owned_audio.exists())
            self.assertTrue(foreign_audio.exists())
            self.assertFalse(owned_silence.exists())
            self.assertTrue(foreign_silence.exists())
            self.assertTrue((root / "silences").is_dir())
            self.assertFalse((root / "sentence_cache.json").exists())


class TextNormalizationTests(unittest.TestCase):
    def test_numeric_minus_variants_are_kept_or_normalized_safely(self):
        text = "-5\n- 5\n−5\n− 5\n– 5\n— 5"

        normalized = studio.normalize_dialogue_line_starts(text)

        self.assertEqual(
            normalized,
            "-5\n- 5\n-5\n- 5\n— 5\n— 5",
        )

    def test_dialogue_and_separator_lines_keep_their_roles(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.separators = ["–––"]
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []
        processor.glossary_regex = []

        self.assertEqual(
            processor._prepare_raw_text(
                "— реплика\n–––", "___SEPARATOR_TOKEN___"
            ),
            "— реплика\n___SEPARATOR_TOKEN___",
        )

    def test_quotes_are_recognized_as_speech_paragraph_openers(self):
        for text in (
            '"Мысль',
            "'Мысль",
            '«Мысль',
            '“Мысль',
            '„Мысль',
            '— Реплика',
        ):
            with self.subTest(text=text):
                self.assertTrue(studio.paragraph_starts_with_speech(text))

        self.assertFalse(studio.paragraph_starts_with_speech("Авторский текст"))

    def test_colon_is_recognized_before_closing_quotes_and_brackets(self):
        for text in (
            "Автор сказал:",
            '«Автор сказал:»',
            '“Автор сказал:”',
            "(Автор сказал:)",
        ):
            with self.subTest(text=text):
                self.assertTrue(studio.paragraph_ends_with_colon(text))

        self.assertFalse(studio.paragraph_ends_with_colon("Автор сказал."))

    def test_boundary_pause_uses_maximum_instead_of_sum(self):
        config = {
            "pause_paragraph": 350,
            "pause_speech": 700,
            "pause_colon": 900,
        }

        self.assertEqual(
            studio.paragraph_boundary_pause(
                config,
                '«Ответ».',
                previous_ended_with_colon=True,
            ),
            900,
        )


class AtomicWriteTests(unittest.TestCase):
    def test_json_backup_is_previous_complete_document(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "settings.json"
            path.write_text('{"version": 1}', encoding="utf-8")
            app = object.__new__(studio.TTSApp)

            app._write_json_atomic(path, {"version": 2}, backup=True)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
            self.assertEqual(
                json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")),
                {"version": 1},
            )

    def test_dry_run_returns_hashes_without_mutating_cache_setting(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "chapter.txt"
            path.write_text("Тест.", encoding="utf-8")
            processor = object.__new__(studio.TTSProcessor)
            processor.cfg = {"use_cache": True}
            processor.get_all_possible_hashes = mock.Mock(return_value={"hash"})

            result = processor.process_text_file(path, dry_run=True)

            self.assertEqual(result, {"hash"})
            self.assertTrue(processor.cfg["use_cache"])
            processor.get_all_possible_hashes.assert_called_once_with("Тест.")

    def test_closing_flushes_cache_and_resume_statuses_before_destroy(self):
        app = object.__new__(studio.TTSApp)
        app._is_closing = False
        app.batch_processor = mock.Mock(is_stopped=False)
        app.direct_processor = None
        app.is_cache_operation_running = mock.Mock(return_value=False)
        app._import_running = False
        app._export_lock = False
        app._export_running = False
        app._appearance_check_after_id = None
        app._settings_save_after_id = None
        app.save_settings = mock.Mock()
        app.root = mock.Mock()

        app.on_closing()

        app.batch_processor.stop.assert_called_once_with()
        app.batch_processor.flush_cache.assert_called_once_with()
        app.batch_processor._save_processing_statuses.assert_called_once_with()
        app.save_settings.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_resume_statuses_are_saved_and_empty_state_removes_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = object.__new__(studio.TTSProcessor)
            processor.cache_lock = studio.threading.RLock()
            processor.processing_statuses_ram = {
                "/tmp/chapter.mp3": "warning",
                "/tmp/finished.mp3": "success",
            }

            with mock.patch.object(studio, "APP_DATA_DIR", root):
                processor._save_processing_statuses()
                status_file = root / "processing_statuses.json"
                self.assertEqual(
                    json.loads(status_file.read_text(encoding="utf-8")),
                    {"/tmp/chapter.mp3": "warning"},
                )

                processor.processing_statuses_ram.clear()
                processor._save_processing_statuses()
                self.assertFalse(status_file.exists())

    @unittest.skipIf(sys.platform == "win32", "Windows locks an open log file")
    def test_deleted_log_is_recreated_on_next_record(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log_path = Path(tempdir) / "processor.log"
            handler = studio.ReopeningFileHandler(log_path, encoding="utf-8")
            try:
                before = logging.LogRecord(
                    "test", logging.INFO, __file__, 1, "before", (), None
                )
                handler.emit(before)
                handler.flush()
                log_path.unlink()

                after = logging.LogRecord(
                    "test", logging.INFO, __file__, 1, "after", (), None
                )
                handler.emit(after)
                handler.flush()

                self.assertEqual(log_path.read_text(encoding="utf-8"), "after\n")
            finally:
                handler.close()


class BookImportTests(unittest.TestCase):
    def test_duplicate_chapter_names_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tempdir:
            saved = studio.BookExtractor.save_chapters(
                [("Глава", "one"), ("Глава", "two")],
                tempdir,
                "book.fb2",
                "{title}",
            )

            self.assertEqual(saved, ["Глава.txt", "Глава (2).txt"])
            self.assertEqual(
                (Path(tempdir) / "Глава.txt").read_text(encoding="utf-8"),
                "one",
            )
            self.assertEqual(
                (Path(tempdir) / "Глава (2).txt").read_text(encoding="utf-8"),
                "two",
            )


class BuildWorkflowContractTests(unittest.TestCase):
    def test_release_matrix_keeps_required_portable_artifacts(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("SileroTTS_Studio_Windows_Portable.exe", workflow)
        self.assertIn("SileroTTS_Studio_Linux_x86_64_Portable.zip", workflow)
        self.assertIn("SileroTTS_Studio_Linux_arm64_Portable.zip", workflow)
        self.assertIn("--onefile", workflow)
        self.assertIn("Upload portable release asset", workflow)
        self.assertNotIn("macOS_arm64_Portable", workflow)

    def test_release_ffmpeg_is_checked_for_opus_support(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Verify required FFmpeg codecs", workflow)
        self.assertIn("libopus", workflow)
        self.assertIn("libvorbis", workflow)


class AtomicOutputTests(unittest.TestCase):
    def test_failed_audio_export_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "result.wav"
            output.write_bytes(b"old-good-file")
            audio = studio.AudioSegment.silent(duration=20, frame_rate=8000)

            with mock.patch.object(
                audio, "export", side_effect=RuntimeError("encoder failed")
            ):
                with self.assertRaises(RuntimeError):
                    studio._export_audio_atomic(audio, output, format="wav")

            self.assertEqual(output.read_bytes(), b"old-good-file")
            self.assertEqual(list(output.parent.glob(".*.tmp.wav")), [])

    def test_successful_audio_export_replaces_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "result.wav"
            output.write_bytes(b"old")
            audio = studio.AudioSegment.silent(duration=20, frame_rate=8000)

            studio._export_audio_atomic(audio, output, format="wav")

            with wave.open(str(output), "rb") as wav_file:
                self.assertGreater(wav_file.getnframes(), 0)

    def test_silence_file_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = object.__new__(studio.TTSProcessor)
            processor.cache_dir = Path(tempdir)

            silence = processor._get_silence_file(25)

            self.assertTrue(silence.exists())
            self.assertGreater(silence.stat().st_size, 0)
            self.assertEqual(studio._detect_ogg_audio_codec(silence), "opus")
            self.assertEqual(
                list((Path(tempdir) / "silences").glob(".*.tmp.ogg")), []
            )


class AudioEffectsTests(unittest.TestCase):
    def test_strict_effect_mode_reports_ffmpeg_failure(self):
        segment = studio.AudioSegment.silent(duration=20, frame_rate=8000)
        with mock.patch.object(
            studio.subprocess, "run", side_effect=OSError("ffmpeg missing")
        ):
            with self.assertRaises(RuntimeError):
                studio.AudioEffects.apply_effects(
                    segment, speed=1.1, strict=True
                )

    def test_preview_effect_mode_keeps_original_on_ffmpeg_failure(self):
        segment = studio.AudioSegment.silent(duration=20, frame_rate=8000)
        with mock.patch.object(
            studio.subprocess, "run", side_effect=OSError("ffmpeg missing")
        ):
            result = studio.AudioEffects.apply_effects(segment, speed=1.1)

        self.assertIs(result, segment)


class SettingsRecoveryTests(unittest.TestCase):
    def test_corrupt_settings_fall_back_to_valid_backup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            path.with_suffix(".json.bak").write_text(
                json.dumps({"speaker": "backup_voice"}), encoding="utf-8"
            )

            app = object.__new__(studio.TTSApp)
            config = app.load_settings(path)

            self.assertEqual(config["speaker"], "backup_voice")


class ApiStepsUiConfigTests(unittest.TestCase):
    def test_empty_custom_steps_does_not_reuse_previous_preset(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"api_steps": 16}
        app.settings_vars = {
            "api_steps_choice": mock.Mock(
                get=mock.Mock(return_value="Другое")
            ),
            "api_steps_custom": mock.Mock(get=mock.Mock(return_value="")),
        }

        app.update_config_from_ui()

        self.assertEqual(app.config["api_steps"], "")


class EmptySynthesisTests(unittest.TestCase):
    def make_processor(self, root):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = studio.DEFAULT_CONFIG.copy()
        processor.cfg.update(
            {
                "output_dir": str(root),
                "pause_file_start": 500,
                "pause_file_end": 500,
                "separator_symbols": "---",
                "synthesis_mode": "sentence",
            }
        )
        processor.separators = ["---"]
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []
        processor.glossary_regex = []
        processor.processing_statuses_ram = {}
        processor.cache_lock = studio.threading.RLock()
        processor.is_stopped = False
        return processor

    def collect_silence_durations(self, processor, raw_text, *, speech_texts=None):
        """Runs the planner without network/FFmpeg and returns pause requests."""
        silence_durations = []
        audio = Path(processor.cfg["output_dir"]) / "speech.ogg"
        processor.cfg["pause_file_start"] = 0
        processor.cfg["pause_file_end"] = 0
        processor.synthesize_sentence = mock.Mock(return_value=(audio, True))
        processor._get_silence_file = mock.Mock(
            side_effect=lambda duration: silence_durations.append(duration) or audio
        )
        processor._run_ffmpeg_concat = mock.Mock(return_value=audio)
        processor._save_cache = mock.Mock()

        processor.process_raw_text(raw_text, "test.mp3", save_to_disk=False)
        if speech_texts is not None:
            speech_texts.extend(
                call.args[0] for call in processor.synthesize_sentence.call_args_list
            )
        return silence_durations

    @staticmethod
    def set_pause_config(processor, **overrides):
        values = {
            "pause_paragraph": 300,
            "pause_speech": 700,
            "pause_colon": 500,
        }
        values.update(overrides)
        processor.cfg.update(values)

    def test_quoted_thought_uses_same_pause_as_dash_dialogue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor)

            quoted = self.collect_silence_durations(
                processor, 'Авторский текст.\n«Это мысль».'
            )

            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor)
            dialogue = self.collect_silence_durations(
                processor, "Авторский текст.\n— Это реплика."
            )

            self.assertEqual(quoted, [700])
            self.assertEqual(dialogue, quoted)

    def test_colon_pause_is_larger_than_regular_paragraph_pause(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor, pause_speech=500, pause_colon=900)

            pauses = self.collect_silence_durations(
                processor, "Автор сказал:\nПродолжение."
            )

            self.assertEqual(pauses, [900])

    def test_colon_and_dialogue_pauses_are_not_added_together(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor, pause_colon=900)

            pauses = self.collect_silence_durations(
                processor, "Автор сказал:\n«Ответ»."
            )

            self.assertEqual(pauses, [900])

    def test_colon_before_closing_quote_affects_next_paragraph(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor, pause_speech=500, pause_colon=900)

            pauses = self.collect_silence_durations(
                processor, '«Автор подумал:»\nПродолжение.'
            )

            self.assertEqual(pauses, [900])

    def test_separator_and_dialogue_pauses_collapse_to_one_maximum(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor, pause_separator=400)

            pauses = self.collect_silence_durations(
                processor, "Авторский текст.\n---\n— Реплика."
            )

            self.assertEqual(pauses, [700])

    def test_separator_larger_than_dialogue_pause_is_kept_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor, pause_separator=1200)

            pauses = self.collect_silence_durations(
                processor, "Авторский текст.\n---\n— Реплика."
            )

            self.assertEqual(pauses, [1200])

    def test_full_mode_keeps_paragraphs_in_one_request_without_fake_pause(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(
                processor, synthesis_mode="full", pause_colon=900
            )
            speech_texts = []

            pauses = self.collect_silence_durations(
                processor,
                "Автор сказал:\n«Ответ».",
                speech_texts=speech_texts,
            )

            self.assertEqual(pauses, [])
            self.assertEqual(len(speech_texts), 1)
            self.assertIn("\n", speech_texts[0])

    def test_full_mode_separator_and_dialogue_use_one_maximum_pause(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(
                processor, synthesis_mode="full", pause_separator=400
            )

            pauses = self.collect_silence_durations(
                processor, "Авторский текст.\n---\n— Реплика."
            )

            self.assertEqual(pauses, [700])

    def test_full_mode_safe_limit_break_uses_boundary_maximum(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(
                processor, synthesis_mode="full", pause_colon=900
            )

            with mock.patch.object(studio, "SAFE_LIMIT", 18):
                pauses = self.collect_silence_durations(
                    processor, "Автор сказал:\n«Ответ»."
                )

            self.assertEqual(pauses, [900])

    def test_punctuation_only_text_reports_empty_and_creates_no_silence_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            callbacks = []

            processor.process_raw_text(
                ".",
                "empty.mp3",
                completion_callback=lambda *args: callbacks.append(args),
            )

            self.assertEqual(callbacks, [("empty.mp3", "empty", None)])
            self.assertFalse((root / "empty.mp3").exists())
            self.assertEqual(processor.processing_statuses_ram, {})

    def test_all_failed_speech_does_not_create_silence_only_output(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            callbacks = []
            fallback = root / "fallback-silence.ogg"
            processor.synthesize_sentence = mock.Mock(
                return_value=(fallback, False)
            )
            processor._get_silence_file = mock.Mock(return_value=fallback)
            processor._save_cache = mock.Mock()
            processor._merge_save_and_notify = mock.Mock()

            with self.assertLogs(level=logging.ERROR) as captured:
                processor.process_raw_text(
                    "Первое предложение. Второе предложение.",
                    "failed.mp3",
                    completion_callback=lambda *args: callbacks.append(args),
                )

            self.assertEqual(callbacks, [("failed.mp3", "error", None)])
            processor._merge_save_and_notify.assert_not_called()
            self.assertIn("файл из одной тишины не создан", "\n".join(captured.output))

    def test_explicit_separator_can_still_create_intentional_silence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            processor.cfg["pause_file_start"] = 0
            processor.cfg["pause_file_end"] = 0
            callbacks = []
            silence = root / "silence.ogg"
            joined = root / "joined.ogg"
            processor._get_silence_file = mock.Mock(return_value=silence)
            processor._run_ffmpeg_concat = mock.Mock(return_value=joined)
            processor._save_cache = mock.Mock()

            processor.process_raw_text(
                "---",
                "silence.mp3",
                save_to_disk=False,
                completion_callback=lambda *args: callbacks.append(args),
            )

            processor._get_silence_file.assert_called_once_with(
                processor.cfg["pause_separator"]
            )
            self.assertEqual(
                callbacks, [("silence.mp3", "success", str(joined))]
            )

    def test_explicit_separator_reports_visible_progress_label(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            processor.cfg["pause_file_start"] = 0
            processor.cfg["pause_file_end"] = 0
            silence = root / "silence.ogg"
            processor._get_silence_file = mock.Mock(return_value=silence)
            processor._run_ffmpeg_concat = mock.Mock(return_value=root / "joined.ogg")
            processor._save_cache = mock.Mock()
            progress = []

            processor.process_raw_text(
                "---",
                "silence.mp3",
                save_to_disk=False,
                progress_callback=lambda current, total, text: progress.append(
                    (current, total, text)
                ),
            )

            self.assertEqual(progress, [(1, 1, "[ПАУЗА РАЗДЕЛИТЕЛЯ]")])

    def test_consecutive_separators_keep_two_full_pauses(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            processor.cfg["pause_separator"] = 1200

            pauses = self.collect_silence_durations(
                processor, "Авторский текст.\n---\n---\nПродолжение."
            )

            self.assertEqual(pauses, [1200, 1200])

    def test_en_dash_separator_with_spaces_is_protected_before_typography(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            processor.separators = ["–––"]

            prepared = processor._prepare_raw_text(
                "\t  –––  \t", "___SEPARATOR_TOKEN___"
            )

            self.assertEqual(prepared, "___SEPARATOR_TOKEN___")

    def test_separator_must_occupy_the_entire_line(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))

            prepared = processor._prepare_raw_text(
                "--- примечание", "___SEPARATOR_TOKEN___"
            )

            self.assertNotIn("___SEPARATOR_TOKEN___", prepared)

    def test_separator_text_is_treated_literally_not_as_regex(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            processor.separators = ["[pause]+"]

            prepared = processor._prepare_raw_text(
                "\t[pause]+\t", "___SEPARATOR_TOKEN___"
            )

            self.assertEqual(prepared, "___SEPARATOR_TOKEN___")

    def test_hash_collection_uses_one_content_identity_for_all_steps(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            processor.cfg["speaker"] = "voice"
            processor.cfg["api_steps_enabled"] = True
            processor.cfg["api_steps"] = 16
            processor.cfg["cache_include_steps"] = True

            hashes_at_16 = processor.get_all_possible_hashes("Тест.")
            processor.cfg["api_steps"] = 72
            hashes_at_72 = processor.get_all_possible_hashes("Тест.")

            self.assertEqual(
                hashes_at_16,
                {studio.cache_content_hash("Тест.", "voice")},
            )
            self.assertEqual(hashes_at_72, hashes_at_16)


class _FakeCompletedProcess:
    returncode = 0
    stderr = b""


class FfmpegSaveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.audio_file = self.root / "input.ogg"
        self.audio_file.write_bytes(b"audio")
        self.cover_file = self.root / "cover.png"
        self.cover_file.write_bytes(b"image")

    def make_processor(self, **overrides):
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "output_dir": str(self.root),
                "output_format": "mp3",
                "output_bitrate": "128k",
                "apply_output_tags": True,
                "tag_title": "{filename}",
                "tag_artist": "Writer",
                "tag_album_artist": "",
                "tag_album": "Book",
                "tag_genre": "",
                "tag_composer": "",
                "tag_year": "",
                "tag_cover": str(self.cover_file),
                "fx_speed": 1.0,
                "fx_pitch": 1.0,
                "fx_echo": False,
            }
        )
        config.update(overrides)

        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = config
        processor.encode_semaphore = None
        processor.is_stopped = False
        processor._last_ffmpeg_save_command = None
        processor.processing_statuses_ram = {}
        processor.cache_lock = studio.threading.RLock()
        return processor

    def run_save(self, processor, output_name):
        output_path = self.root / output_name
        callbacks = []

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"encoded")
            return _FakeCompletedProcess()

        with mock.patch.object(studio.subprocess, "run", side_effect=fake_run):
            processor._merge_save_and_notify(
                [self.audio_file],
                output_path,
                output_name,
                False,
                lambda *args: callbacks.append(args),
            )
        return output_path, list(processor._last_ffmpeg_save_command), callbacks

    @staticmethod
    def values_after(command, option):
        return [command[index + 1] for index, value in enumerate(command[:-1]) if value == option]

    def test_mp3_cover_is_stream_copied_with_explicit_maps(self):
        processor = self.make_processor()
        _output, command, callbacks = self.run_save(processor, "chapter.mp3")

        self.assertEqual(self.values_after(command, "-map"), ["0:a:0", "1:v:0"])
        self.assertEqual(self.values_after(command, "-c:v"), ["copy"])
        self.assertEqual(self.values_after(command, "-map_metadata"), ["-1"])
        self.assertIn("title=chapter", self.values_after(command, "-metadata"))
        self.assertIn("artist=Writer", self.values_after(command, "-metadata"))
        self.assertIn("album=Book", self.values_after(command, "-metadata"))
        self.assertEqual(callbacks[-1][1], "success")

    def test_direct_default_has_no_tag_metadata_or_cover_input(self):
        processor = self.make_processor(apply_output_tags=False)
        _output, command, _callbacks = self.run_save(processor, "direct_output.mp3")

        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("-map", command)
        self.assertNotIn("-metadata", command)
        self.assertNotIn("-metadata:s:v", command)
        self.assertEqual(self.values_after(command, "-map_metadata"), ["-1"])

    def test_ogg_does_not_map_unsupported_jpeg_or_png_stream(self):
        processor = self.make_processor(output_format="ogg")
        with self.assertLogs(level=logging.WARNING) as captured:
            _output, command, callbacks = self.run_save(processor, "chapter.ogg")

        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("-map", command)
        self.assertNotIn("-c:v", command)
        self.assertNotIn("-disposition:v", command)
        self.assertIn("пропущена для OGG", "\n".join(captured.output))
        self.assertEqual(callbacks[-1][1], "success")


class CacheBehaviorTests(unittest.TestCase):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            import base64

            return {
                "results": [
                    {"audio": base64.b64encode(b"new audio").decode("ascii")}
                ]
            }

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json, timeout):
            self.calls.append((url, json, timeout))
            return CacheBehaviorTests.FakeResponse()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def make_processor(self, *, use_cache):
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "cache_dir": str(self.root / "cache"),
                "input_dir": str(self.root / "input"),
                "output_dir": str(self.root / "output"),
                "use_cache": use_cache,
                "auto_trim_silence": False,
                "max_retries": 1,
                "api_max_requests": 100,
                "api_time_window": 0,
            }
        )
        processor = studio.TTSProcessor(
            config,
            shared_cache={},
            shared_processing_statuses={},
        )
        processor.session = self.FakeSession()
        return processor

    def synthesize_without_decoding(self, processor, *, force_new=False):
        def fake_prepare(source, destination, **_kwargs):
            source = Path(source)
            destination = Path(destination)
            destination.write_bytes(source.read_bytes())
            return destination

        with mock.patch.object(
            studio, "_prepare_api_audio_file", side_effect=fake_prepare
        ):
            return processor.synthesize_sentence(
                "Фраза.", "Фраза.", force_new=force_new
            )

    def test_disabled_cache_neither_reads_nor_indexes_new_audio(self):
        processor = self.make_processor(use_cache=False)
        text_hash = processor.get_hash("Фраза.")
        cache_file = processor.cache_audio_dir / f"{text_hash}.ogg"
        cache_file.write_bytes(b"old audio")
        processor.cache[text_hash] = {
            "file_name": cache_file.name,
            "speaker": processor.cfg["speaker"],
        }

        returned_file, success = self.synthesize_without_decoding(processor)

        self.assertTrue(success)
        self.assertEqual(returned_file.read_bytes(), b"new audio")
        self.assertNotEqual(returned_file, cache_file)
        self.assertEqual(cache_file.read_bytes(), b"old audio")
        self.assertEqual(len(processor.session.calls), 1)
        self.assertEqual(processor.unsaved_cache_items, 0)
        self.assertEqual(processor.cache[text_hash]["file_name"], cache_file.name)

    def test_force_new_bypasses_cache_but_keeps_entry_until_success(self):
        processor = self.make_processor(use_cache=True)
        text_hash = processor.get_hash("Фраза.")
        cache_file = processor.cache_audio_dir / f"{text_hash}.ogg"
        cache_file.write_bytes(b"old audio")
        processor.cache[text_hash] = {
            "file_name": cache_file.name,
            "speaker": processor.cfg["speaker"],
            "usage_count": 9,
        }

        returned_file, success = self.synthesize_without_decoding(
            processor, force_new=True
        )

        self.assertTrue(success)
        self.assertEqual(returned_file.read_bytes(), b"new audio")
        self.assertEqual(len(processor.session.calls), 1)
        self.assertEqual(processor.cache[text_hash]["usage_count"], 1)
        self.assertEqual(processor.unsaved_cache_items, 1)


class CacheOpusMigrationTests(unittest.TestCase):
    def test_migration_updates_metadata_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            vorbis = audio_dir / ("a" * 32 + ".ogg")
            opus = audio_dir / ("b" * 32 + ".ogg")
            vorbis.write_bytes(b"OggS\x00\x01vorbis-old")
            opus.write_bytes(b"OggS\x00OpusHead-new")
            cache_data = {
                "first": {"file_name": vorbis.name},
                "second": {"file_name": opus.name, "audio_codec": "opus"},
            }

            def fake_transcode(path):
                path = Path(path)
                if path == opus:
                    size = path.stat().st_size
                    return "already_opus", size, size, size, size
                old_size = path.stat().st_size
                path.write_bytes(b"OggS\x00OpusHead-converted")
                new_size = path.stat().st_size
                return "converted", old_size, new_size, old_size, new_size

            with mock.patch.object(
                studio,
                "_transcode_cache_audio_to_opus",
                side_effect=fake_transcode,
            ):
                stats = studio.transcode_cache_entries_to_opus(
                    cache_dir, cache_data, max_workers=2
                )

            self.assertEqual(stats["converted"], 1)
            self.assertEqual(stats["already_opus"], 1)
            self.assertEqual(stats["failed"], 0)
            self.assertTrue(stats["index_changed"])
            self.assertEqual(cache_data["first"]["audio_codec"], "opus")
            self.assertEqual(cache_data["second"]["audio_codec"], "opus")
            self.assertEqual(studio._detect_ogg_audio_codec(vorbis), "opus")

    def test_cancel_stops_submitting_unstarted_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            cache_data = {}
            for index in range(10):
                filename = f"{index:032x}.ogg"
                (audio_dir / filename).write_bytes(b"OggS\x00\x01vorbis")
                cache_data[str(index)] = {"file_name": filename}
            cancel_event = studio.threading.Event()
            calls = []

            def fake_transcode(path):
                calls.append(Path(path).name)
                cancel_event.set()
                size = Path(path).stat().st_size
                return "converted", size, size, size, size

            with mock.patch.object(
                studio,
                "_transcode_cache_audio_to_opus",
                side_effect=fake_transcode,
            ):
                stats = studio.transcode_cache_entries_to_opus(
                    cache_dir,
                    cache_data,
                    cancel_event=cancel_event,
                    max_workers=1,
                )

            self.assertTrue(stats["cancelled"])
            self.assertLess(len(calls), len(cache_data))

    def test_duplicate_index_references_are_all_marked_opus(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            filename = "c" * 32 + ".ogg"
            audio_file = audio_dir / filename
            audio_file.write_bytes(b"OggS\x00\x01vorbis")
            cache_data = {
                "one": {"file_name": filename},
                "two": {"file_name": filename},
            }
            size = audio_file.stat().st_size

            with mock.patch.object(
                studio,
                "_transcode_cache_audio_to_opus",
                return_value=("converted", size, size, size, size),
            ) as transcode:
                stats = studio.transcode_cache_entries_to_opus(
                    cache_dir, cache_data, max_workers=1
                )

            transcode.assert_called_once_with(audio_file)
            self.assertEqual(stats["converted"], 1)
            self.assertEqual(cache_data["one"]["audio_codec"], "opus")
            self.assertEqual(cache_data["two"]["audio_codec"], "opus")

    def test_checkpoint_callback_receives_updated_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            filename = "d" * 32 + ".ogg"
            audio_file = audio_dir / filename
            audio_file.write_bytes(b"OggS\x00\x01vorbis")
            cache_data = {"one": {"file_name": filename}}
            checkpoints = []
            size = audio_file.stat().st_size

            with mock.patch.object(
                studio,
                "_transcode_cache_audio_to_opus",
                return_value=("converted", size, size, size, size),
            ):
                stats = studio.transcode_cache_entries_to_opus(
                    cache_dir,
                    cache_data,
                    max_workers=1,
                    checkpoint_callback=lambda current: checkpoints.append(
                        json.loads(json.dumps(current))
                    ),
                )

            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["one"]["audio_codec"], "opus")
            self.assertFalse(stats["index_dirty"])

    def test_pre_cancelled_migration_starts_no_ffmpeg_work(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            filename = "e" * 32 + ".ogg"
            (audio_dir / filename).write_bytes(b"OggS\x00\x01vorbis")
            cache_data = {"one": {"file_name": filename}}
            cancel_event = studio.threading.Event()
            cancel_event.set()

            with mock.patch.object(
                studio, "_transcode_cache_audio_to_opus"
            ) as transcode:
                stats = studio.transcode_cache_entries_to_opus(
                    cache_dir,
                    cache_data,
                    cancel_event=cancel_event,
                    max_workers=1,
                )

            transcode.assert_not_called()
            self.assertTrue(stats["cancelled"])


@unittest.skipUnless(
    studio.shutil.which("ffmpeg") and studio.shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for the integration test",
)
class FfmpegIntegrationTests(unittest.TestCase):
    def test_existing_vorbis_cache_file_is_canonicalized_to_opus_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_file = Path(tempdir) / "cached.ogg"
            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=0.1",
                    "-ar", "48000", "-ac", "1", "-c:a", "libvorbis",
                    str(cache_file),
                ],
                check=True,
            )

            self.assertEqual(studio._detect_ogg_audio_codec(cache_file), "vorbis")
            self.assertEqual(
                studio._canonicalize_cached_audio_if_needed(cache_file),
                "opus",
            )
            self.assertEqual(studio._detect_ogg_audio_codec(cache_file), "opus")

            with mock.patch.object(
                studio, "_transcode_cache_audio_to_opus"
            ) as transcode_again:
                self.assertEqual(
                    studio._canonicalize_cached_audio_if_needed(
                        cache_file, known_codec="vorbis"
                    ),
                    "opus",
                )
                transcode_again.assert_not_called()

    def test_vorbis_migration_does_not_overwrite_parallel_new_opus(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cache_file = root / "cached.ogg"
            replacement = root / "replacement.ogg"
            for path, frequency, codec in (
                (cache_file, 330, "libvorbis"),
                (replacement, 660, "libopus"),
            ):
                subprocess.run(
                    [
                        studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration=0.1",
                        "-ar", "48000", "-ac", "1", "-c:a", codec,
                        str(path),
                    ],
                    check=True,
                )
            replacement_bytes = replacement.read_bytes()
            real_transcode = studio._transcode_cache_audio_to_opus

            def transcode_then_publish_new(path, publish_lock=None):
                result = real_transcode(path, publish_lock=publish_lock)
                cache_file.write_bytes(replacement_bytes)
                return result

            with mock.patch.object(
                studio,
                "_transcode_cache_audio_to_opus",
                side_effect=transcode_then_publish_new,
            ):
                codec = studio._canonicalize_cached_audio_if_needed(cache_file)

            self.assertEqual(codec, "opus")
            self.assertEqual(cache_file.read_bytes(), replacement_bytes)

    def test_api_opus_is_preserved_without_trimming(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "api-opus.ogg"
            destination = root / "cache-opus.ogg"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
                    "-ar", "48000", "-ac", "1", "-c:a", "libopus",
                    str(source),
                ],
                check=True,
            )

            studio._prepare_api_audio_file(
                source,
                destination,
                trim_silence=False,
            )

            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error", "-of", "json",
                    "-show_entries", "stream=codec_name,sample_rate,channels",
                    str(destination),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "opus")
            self.assertEqual(stream["sample_rate"], "48000")
            self.assertEqual(stream["channels"], 1)
            self.assertEqual(source.read_bytes(), destination.read_bytes())

    def test_vorbis_transcode_reduces_real_speech_sample_and_keeps_duration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_file = Path(tempdir) / "speech.ogg"
            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    "anoisesrc=color=pink:duration=3:amplitude=0.04",
                    "-ar", "48000", "-ac", "1", "-c:a", "libvorbis",
                    str(cache_file),
                ],
                check=True,
            )
            before_size = cache_file.stat().st_size
            before_probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error",
                    "-show_entries", "format=duration", "-of", "json",
                    str(cache_file),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )

            result = studio._transcode_cache_audio_to_opus(cache_file)

            after_probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error",
                    "-show_entries", "format=duration", "-of", "json",
                    str(cache_file),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            before_duration = float(json.loads(before_probe.stdout)["format"]["duration"])
            after_duration = float(json.loads(after_probe.stdout)["format"]["duration"])
            self.assertEqual(result[0], "converted")
            self.assertEqual(studio._detect_ogg_audio_codec(cache_file), "opus")
            self.assertLess(cache_file.stat().st_size, before_size)
            self.assertAlmostEqual(after_duration, before_duration, delta=0.03)

    def test_mixed_opus_fragments_and_generated_pause_concat_to_audible_opus(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "first.ogg"
            second = root / "second.ogg"
            for path, frequency in ((first, 330), (second, 660)):
                subprocess.run(
                    [
                        studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration=0.2",
                        "-ar", "48000", "-ac", "1", "-c:a", "libopus",
                        "-b:a", studio.CACHE_AUDIO_BITRATE,
                        str(path),
                    ],
                    check=True,
                )
            processor = object.__new__(studio.TTSProcessor)
            processor.cache_dir = root / "cache"
            silence = processor._get_silence_file(100)

            output = processor._run_ffmpeg_concat([first, silence, second])
            self.addCleanup(lambda: output and output.unlink(missing_ok=True))

            self.assertIsNotNone(output)
            self.assertEqual(studio._detect_ogg_audio_codec(output), "opus")
            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error",
                    "-show_entries", "format=duration", "-of", "json",
                    str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            self.assertGreater(duration, 0.45)
            self.assertGreater(output.stat().st_size, 1000)

    def test_png_cover_remains_png_in_mp3(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            audio = root / "audio.ogg"
            cover = root / "cover.png"
            output = root / "book.mp3"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.05",
                    "-c:a", "libvorbis", str(audio),
                ],
                check=True,
            )
            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=16x16",
                    "-frames:v", "1", str(cover),
                ],
                check=True,
            )

            config = studio.DEFAULT_CONFIG.copy()
            config.update(
                {
                    "output_dir": str(root),
                    "output_format": "mp3",
                    "output_bitrate": "64k",
                    "apply_output_tags": True,
                    "tag_title": "Integration",
                    "tag_artist": "",
                    "tag_album_artist": "",
                    "tag_album": "",
                    "tag_genre": "",
                    "tag_composer": "",
                    "tag_year": "",
                    "tag_cover": str(cover),
                }
            )
            processor = object.__new__(studio.TTSProcessor)
            processor.cfg = config
            processor.encode_semaphore = None
            processor.is_stopped = False
            processor._last_ffmpeg_save_command = None
            processor.processing_statuses_ram = {}
            processor.cache_lock = studio.threading.RLock()

            processor._merge_save_and_notify(
                [audio], output, output.name, False, None
            )
            self.assertTrue(output.exists())

            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error", "-of", "json",
                    "-show_entries",
                    "stream=codec_name,codec_type:stream_disposition=attached_pic",
                    str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            pictures = [
                stream for stream in streams
                if stream.get("codec_type") == "video"
            ]
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0]["codec_name"], "png")
            self.assertEqual(pictures[0]["disposition"]["attached_pic"], 1)


if __name__ == "__main__":
    unittest.main()
