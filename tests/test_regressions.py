import hashlib
import importlib.util
import json
import logging
import re
import subprocess
import base64
import copy
import struct
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


def fake_ogg_first_page(codec_packet, *, trailing=b""):
    """Builds the minimum first Ogg page needed by header-only unit tests."""
    if len(codec_packet) > 254:
        raise ValueError("test packet must fit one lacing segment")
    return (
        b"OggS" + b"\x00" + b"\x02" + b"\x00" * 20
        + b"\x01" + bytes([len(codec_packet)]) + codec_packet + trailing
    )


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
        self.assertNotIn("last_audio_profile_dir", exported_keys)
        self.assertNotIn("last_normalizer_text_dir", exported_keys)
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

    def test_old_shared_format_is_migrated_during_selective_import(self):
        merged = studio.merge_config_values(
            {"output_format": "mp3", "export_format": "wav"},
            {"output_format": "opus"},
            ["tags"],
        )

        self.assertEqual(merged["output_format"], "opus")
        self.assertEqual(merged["export_format"], "opus")

    def test_new_separate_format_survives_selective_import(self):
        merged = studio.merge_config_values(
            {"output_format": "wav", "export_format": "wav"},
            {"output_format": "mp3", "export_format": "ogg"},
            ["tags"],
        )

        self.assertEqual(merged["output_format"], "mp3")
        self.assertEqual(merged["export_format"], "ogg")


class ConfigurationValidationTests(unittest.TestCase):
    def test_export_audio_profile_defaults_are_auto(self):
        config = studio.normalize_config({})

        self.assertEqual(config["export_bitrate"], "auto")
        self.assertEqual(config["export_sample_rate"], "auto")
        self.assertEqual(config["export_channels"], "auto")

    def test_export_audio_profile_values_are_normalized(self):
        config = studio.normalize_config(
            {
                "export_bitrate": " 192K ",
                "export_sample_rate": " 44100 ",
                "export_channels": " STEREO ",
            }
        )

        self.assertEqual(config["export_bitrate"], "192k")
        self.assertEqual(config["export_sample_rate"], "44100")
        self.assertEqual(config["export_channels"], "stereo")

    def test_invalid_export_audio_profile_values_fall_back_to_auto(self):
        config = studio.normalize_config(
            {
                "export_bitrate": "lossless",
                "export_sample_rate": "12345",
                "export_channels": "surround",
            }
        )

        self.assertEqual(config["export_bitrate"], "auto")
        self.assertEqual(config["export_sample_rate"], "auto")
        self.assertEqual(config["export_channels"], "auto")

    def test_default_api_url_uses_https(self):
        config = studio.normalize_config({"api_url": ""})

        self.assertEqual(
            studio.DEFAULT_API_URL,
            "https://iq3g.silero.ai/enhanced_voice",
        )
        self.assertEqual(config["api_url"], studio.DEFAULT_API_URL)

    def test_official_http_api_url_is_preserved_verbatim(self):
        user_url = "http://iq3g.silero.ai/enhanced_voice"

        config = studio.normalize_config({"api_url": user_url})

        self.assertEqual(config["api_url"], user_url)

    def test_custom_http_api_url_is_preserved_verbatim(self):
        custom_url = "http://127.0.0.1:8000/enhanced_voice"

        config = studio.normalize_config({"api_url": custom_url})

        self.assertEqual(config["api_url"], custom_url)

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

    def test_opus_is_a_supported_output_format(self):
        config = studio.normalize_config({"output_format": " OPUS "})

        self.assertEqual(config["output_format"], "opus")

    def test_string_booleans_are_normalized(self):
        config = studio.normalize_config(
            {"use_cache": "false", "direct_save": "true"}
        )

        self.assertFalse(config["use_cache"])
        self.assertTrue(config["direct_save"])

    def test_valid_missing_user_directory_is_created_and_preserved(self):
        with tempfile.TemporaryDirectory() as tempdir:
            custom_input = Path(tempdir) / "new" / "texts"
            config = {"input_dir": str(custom_input)}

            returned, recovered = studio.ensure_config_directories(
                config, keys=("input_dir",)
            )

            self.assertIs(returned, config)
            self.assertEqual(recovered, {})
            self.assertEqual(config["input_dir"], str(custom_input))
            self.assertTrue(custom_input.is_dir())

    def test_unusable_user_directory_falls_back_only_that_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            blocked_parent = Path(tempdir) / "not_a_directory"
            blocked_parent.write_text("file", encoding="utf-8")
            output_dir = Path(tempdir) / "valid-output"
            config = {
                "input_dir": str(blocked_parent / "texts"),
                "output_dir": str(output_dir),
            }

            _, recovered = studio.ensure_config_directories(
                config, keys=("input_dir", "output_dir")
            )

            self.assertIn("input_dir", recovered)
            self.assertNotIn("output_dir", recovered)
            self.assertEqual(config["input_dir"], studio.DEFAULT_INPUT_DIR)
            self.assertEqual(config["output_dir"], str(output_dir))
            self.assertTrue(output_dir.is_dir())

    def test_processor_propagates_recovered_paths_to_gui_config(self):
        with tempfile.TemporaryDirectory() as tempdir:
            blocked_parent = Path(tempdir) / "not_a_directory"
            blocked_parent.write_text("file", encoding="utf-8")
            config = studio.DEFAULT_CONFIG.copy()
            config["input_dir"] = str(blocked_parent / "texts")

            studio.TTSProcessor(
                config, shared_cache={}, shared_processing_statuses={}
            )

            self.assertEqual(config["input_dir"], studio.DEFAULT_INPUT_DIR)

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

    def test_physical_and_virtual_paste_callbacks_insert_only_once(self):
        class FakeText:
            def __init__(self):
                self.insert = mock.Mock()
                self.delete = mock.Mock()

            @staticmethod
            def tag_ranges(_tag):
                return ()

        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.clipboard_get.return_value = "термин"
        idle_callbacks = []
        app.root.after_idle.side_effect = idle_callbacks.append
        widget = FakeText()
        event = mock.Mock(widget=widget)

        with mock.patch.object(studio.tk, "Text", FakeText):
            self.assertEqual(app._paste_clipboard_once(event), "break")
            self.assertEqual(app._paste_clipboard_once(event), "break")

        widget.insert.assert_called_once_with(studio.tk.INSERT, "термин")
        self.assertEqual(len(idle_callbacks), 1)

        idle_callbacks[0]()
        with mock.patch.object(studio.tk, "Text", FakeText):
            app._paste_clipboard_once(event)
        self.assertEqual(widget.insert.call_count, 2)

    def test_clipboard_setup_replaces_tk_virtual_paste_binding(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()

        app._fix_cyrillic_clipboard()
        for widget_class in ("Text", "Entry", "TEntry"):
            self.assertIn(
                mock.call(widget_class, "<<Paste>>", mock.ANY),
                app.root.bind_class.call_args_list,
            )

        app.root.reset_mock()
        app._setup_mac_hotkeys()
        for widget_class in ("Text", "Entry", "TEntry"):
            self.assertIn(
                mock.call(
                    widget_class, "<<Paste>>", app._paste_clipboard_once
                ),
                app.root.bind_class.call_args_list,
            )


class ExportGroupingTests(unittest.TestCase):
    def test_xiph_cover_uses_signature_and_flac_picture_block(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cover = Path(tempdir) / "cover.bin"
            image = b"\x89PNG\r\n\x1a\n" + b"picture-data"
            cover.write_bytes(image)

            block = base64.b64decode(
                studio._xiph_metadata_block_picture(cover)
            )

        offset = 0

        def read_u32():
            nonlocal offset
            value = struct.unpack_from(">I", block, offset)[0]
            offset += 4
            return value

        self.assertEqual(read_u32(), 3)
        mime_length = read_u32()
        self.assertEqual(block[offset:offset + mime_length], b"image/png")
        offset += mime_length
        description_length = read_u32()
        offset += description_length
        self.assertEqual(tuple(read_u32() for _ in range(4)), (0, 0, 0, 0))
        image_length = read_u32()
        self.assertEqual(block[offset:offset + image_length], image)

    def test_xiph_cover_is_passed_via_file_not_command_line(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cover = root / "cover.png"
            cover.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            with mock.patch.object(studio, "SESSION_TEMP_DIR", root):
                metadata_path = studio._create_xiph_cover_metadata_file(
                    cover, "book.opus"
                )
            self.addCleanup(metadata_path.unlink, missing_ok=True)

            contents = metadata_path.read_text(encoding="ascii")
            self.assertTrue(contents.startswith(";FFMETADATA1\n"))
            self.assertIn("METADATA_BLOCK_PICTURE=", contents)

    def test_auto_merge_profile_preserves_uniform_mono_inputs(self):
        sources = (Path("one.ogg"), Path("two.wav"))
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            side_effect=(
                {"sample_rate": 44100, "channels": 1},
                {"sample_rate": 44100, "channels": 1},
            ),
        ):
            profile = studio._select_merge_audio_profile(sources, "wav")

        self.assertEqual(
            profile,
            {
                "sample_rate": 44100,
                "channels": 1,
                "channel_layout": "mono",
                "bitrate": None,
            },
        )

    def test_probe_treats_vorbis_unknown_bitrate_sentinel_as_unknown(self):
        response = mock.Mock(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": "vorbis",
                            "sample_rate": "96000",
                            "channels": 2,
                            "bit_rate": "4294967294",
                        }
                    ]
                }
            )
        )
        with mock.patch.object(studio.subprocess, "run", return_value=response):
            profile = studio._probe_audio_stream_profile(Path("music.ogg"))

        self.assertEqual(profile["codec"], "vorbis")
        self.assertEqual(profile["sample_rate"], 96000)
        self.assertEqual(profile["channels"], 2)
        self.assertIsNone(profile["bitrate"])

    def test_auto_merge_profile_uses_highest_rate_and_stereo_when_needed(self):
        sources = (Path("speech.ogg"), Path("music.wav"))
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            side_effect=(
                {"sample_rate": 44100, "channels": 1},
                {"sample_rate": 96000, "channels": 2},
            ),
        ):
            profile = studio._select_merge_audio_profile(sources, "wav")

        self.assertEqual(profile["sample_rate"], 96000)
        self.assertEqual(profile["channels"], 2)
        self.assertEqual(profile["channel_layout"], "stereo")

    def test_auto_mp3_profile_uses_nearest_supported_sample_rate(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={"sample_rate": 37800, "channels": 1},
        ):
            profile = studio._select_merge_audio_profile(
                [Path("speech.wav")], "mp3"
            )

        self.assertEqual(profile["sample_rate"], 44100)
        self.assertEqual(profile["channels"], 1)

    def test_auto_ogg_profile_preserves_high_sample_rate_in_quality_mode(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={"sample_rate": 96000, "channels": 2},
        ):
            profile = studio._select_merge_audio_profile(
                [Path("music.wav")], "ogg"
            )

        self.assertEqual(profile["sample_rate"], 96000)
        self.assertEqual(profile["channels"], 2)
        self.assertIsNone(profile["bitrate"])

    def test_auto_opus_profile_preserves_supported_rate_and_channels(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={
                "codec": "opus",
                "sample_rate": 48000,
                "channels": 1,
                "bitrate": 96000,
            },
        ):
            profile = studio._select_merge_audio_profile(
                [Path("speech.opus")], "opus"
            )

        self.assertEqual(profile["sample_rate"], 48000)
        self.assertEqual(profile["channels"], 1)
        self.assertEqual(profile["bitrate"], "96k")

    def test_explicit_ogg_high_sample_rate_requires_auto_bitrate(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={"sample_rate": 44100, "channels": 1},
        ):
            profile = studio._select_merge_audio_profile(
                [Path("speech.wav")], "ogg", sample_rate="96000"
            )
            with self.assertRaisesRegex(ValueError, "quality-режим"):
                studio._select_merge_audio_profile(
                    [Path("speech.wav")],
                    "ogg",
                    sample_rate="96000",
                    bitrate="128k",
                )

        self.assertEqual(profile["sample_rate"], 96000)
        self.assertEqual(profile["channels"], 1)

    def test_explicit_mp3_high_sample_rate_is_not_silently_changed(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={"sample_rate": 44100, "channels": 2},
        ):
            with self.assertRaisesRegex(ValueError, "не поддерживает"):
                studio._select_merge_audio_profile(
                    [Path("music.wav")], "mp3", sample_rate="96000"
                )

    def test_auto_drops_uniform_bitrate_incompatible_with_codec_profile(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={
                "codec": "vorbis",
                "sample_rate": 22050,
                "channels": 1,
                "bitrate": 128000,
            },
        ):
            profile = studio._select_merge_audio_profile(
                [Path("speech.ogg")], "ogg"
            )

        self.assertEqual(profile["sample_rate"], 22050)
        self.assertEqual(profile["channels"], 1)
        self.assertIsNone(profile["bitrate"])

    def test_explicit_incompatible_vorbis_profile_has_clear_error(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={"sample_rate": 22050, "channels": 1},
        ):
            with self.assertRaisesRegex(ValueError, "не выше 64 кбит/с"):
                studio._select_merge_audio_profile(
                    [Path("speech.wav")],
                    "ogg",
                    sample_rate="22050",
                    channels="mono",
                    bitrate="128k",
                )
            with self.assertRaisesRegex(ValueError, "не выше 32 кбит/с"):
                studio._select_merge_audio_profile(
                    [Path("speech.wav")],
                    "ogg",
                    sample_rate="8000",
                    channels="mono",
                    bitrate="48k",
                )
            with self.assertRaisesRegex(ValueError, "не выше 96 кбит/с"):
                studio._select_merge_audio_profile(
                    [Path("speech.wav")],
                    "ogg",
                    sample_rate="12000",
                    channels="stereo",
                    bitrate="128k",
                )

    def test_explicit_merge_bitrate_overrides_uniform_source_metadata(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={
                "sample_rate": 48000,
                "channels": 2,
                "bitrate": 128000,
            },
        ):
            profile = studio._select_merge_audio_profile(
                [Path("music.mp3")],
                "mp3",
                bitrate="192k",
            )

        self.assertEqual(profile["bitrate"], "192k")

    def test_auto_profile_preserves_uniform_lossy_bitrate(self):
        sources = (Path("one.mp3"), Path("two.mp3"))
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            side_effect=(
                {"sample_rate": 44100, "channels": 1, "bitrate": 128000},
                {"sample_rate": 44100, "channels": 1, "bitrate": 128000},
            ),
        ):
            profile = studio._select_merge_audio_profile(sources, "mp3")

        self.assertEqual(profile["sample_rate"], 44100)
        self.assertEqual(profile["channels"], 1)
        self.assertEqual(profile["channel_layout"], "mono")
        self.assertEqual(profile["bitrate"], "128k")

    def test_auto_profile_does_not_claim_a_mixed_or_unknown_bitrate(self):
        cases = (
            (128000, 192000),
            (128000, None),
        )
        for first_bitrate, second_bitrate in cases:
            with self.subTest(
                first_bitrate=first_bitrate,
                second_bitrate=second_bitrate,
            ), mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                side_effect=(
                    {
                        "sample_rate": 44100,
                        "channels": 1,
                        "bitrate": first_bitrate,
                    },
                    {
                        "sample_rate": 44100,
                        "channels": 1,
                        "bitrate": second_bitrate,
                    },
                ),
            ):
                profile = studio._select_merge_audio_profile(
                    (Path("one.ogg"), Path("two.ogg")), "ogg"
                )

            self.assertIsNone(profile["bitrate"])

    def test_explicit_profile_overrides_probed_rate_channels_and_bitrate(self):
        with mock.patch.object(
            studio,
            "_probe_audio_stream_profile",
            return_value={
                "sample_rate": 96000,
                "channels": 2,
                "bitrate": 320000,
            },
        ):
            profile = studio._select_merge_audio_profile(
                [Path("music.wav")],
                "ogg",
                sample_rate="32000",
                channels="mono",
                bitrate="96k",
            )

        self.assertEqual(
            profile,
            {
                "sample_rate": 32000,
                "channels": 1,
                "channel_layout": "mono",
                "bitrate": "96k",
            },
        )

    def test_streaming_merge_normalizes_mixed_inputs_in_filter_complex(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = (root / "mono.ogg", root / "stereo.mp3")
            for source in sources:
                source.write_bytes(b"audio")
            destination = root / "book.mp3"

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"mp3")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    sources,
                    destination,
                    output_format="mp3",
                    bitrate="192k",
                    pause_ms=250,
                )

            command = popen.call_args.args[0]
            self.assertEqual(command.count("-i"), 2)
            self.assertIn("-filter_complex", command)
            graph = command[command.index("-filter_complex") + 1]
            for index in range(2):
                self.assertIn(
                    f"[{index}:a:0]aresample={studio.CACHE_AUDIO_SAMPLE_RATE}",
                    graph,
                )
            self.assertIn("channel_layouts=stereo", graph)
            self.assertIn("anullsrc=r=48000:cl=stereo", graph)
            self.assertIn("atrim=duration=0.250000", graph)
            self.assertIn("[a0][p0][a1]concat=n=3:v=0:a=1[merged]", graph)
            self.assertEqual(
                command[command.index("-c:a") + 1], "libmp3lame"
            )
            self.assertEqual(command[command.index("-b:a") + 1], "192k")
            self.assertNotIn("copy", command)
            self.assertEqual(destination.read_bytes(), b"mp3")

    def test_windows_command_limit_fails_before_createprocess(self):
        with self.assertRaisesRegex(ValueError, "Разделите группу"):
            studio._validate_windows_command_length(
                ["ffmpeg", "x" * 30000], system_name="Windows"
            )

        studio._validate_windows_command_length(
            ["ffmpeg", "x" * 30000], system_name="Linux"
        )

    def test_mp3_cover_is_written_as_windows_compatible_jpeg_apic(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.ogg"
            cover = root / "cover.png"
            destination = root / "book.mp3"
            source.write_bytes(b"audio")
            cover.write_bytes(b"png")

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"mp3")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    [source],
                    destination,
                    output_format="mp3",
                    cover=cover,
                )

            command = popen.call_args.args[0]
            self.assertEqual(command.count("-i"), 2)
            self.assertEqual(
                command[command.index("-c:v") + 1], "mjpeg"
            )
            self.assertEqual(
                command[command.index("-id3v2_version") + 1], "3"
            )
            self.assertEqual(
                command[command.index("-disposition:v:0") + 1],
                "attached_pic",
            )
            self.assertIn(
                "comment=Cover (front)",
                [
                    command[index + 1]
                    for index, value in enumerate(command[:-1])
                    if value == "-metadata:s:v"
                ],
            )

    def test_streaming_merge_scales_pause_with_speed_only_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = (root / "one.ogg", root / "two.ogg")
            for source in sources:
                source.write_bytes(b"audio")
            destination = root / "book.ogg"

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"ogg")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    sources,
                    destination,
                    output_format="ogg",
                    pause_ms=1000,
                    speed=2.0,
                )

            command = popen.call_args.args[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("atrim=duration=1.000000", graph)
            self.assertIn("[merged]atempo=2[processed]", graph)
            self.assertNotIn("atrim=duration=0.500000", graph)

    def test_streaming_merge_applies_explicit_export_profile(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.wav"
            destination = root / "result.ogg"
            source.write_bytes(b"audio")

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"ogg")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    [source],
                    destination,
                    output_format="ogg",
                    sample_rate="32000",
                    channels="mono",
                    bitrate_mode="96k",
                )

            command = popen.call_args.args[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("aresample=32000", graph)
            self.assertIn("channel_layouts=mono", graph)
            self.assertEqual(command[command.index("-ar") + 1], "32000")
            self.assertEqual(command[command.index("-ac") + 1], "1")
            self.assertEqual(command[command.index("-b:a") + 1], "96k")

    def test_streaming_opus_export_uses_libopus_and_opus_extension(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.wav"
            destination = root / "result.opus"
            source.write_bytes(b"audio")

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"opus")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    [source],
                    destination,
                    output_format="opus",
                    sample_rate="48000",
                    channels="mono",
                    bitrate_mode="96k",
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-c:a") + 1], "libopus")
            self.assertEqual(command[command.index("-b:a") + 1], "96k")
            self.assertTrue(str(command[-1]).endswith(".opus"))
            self.assertEqual(destination.read_bytes(), b"opus")

    def test_streaming_opus_export_embeds_cover_and_keeps_album(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.wav"
            cover = root / "cover.png"
            destination = root / "result.opus"
            source.write_bytes(b"audio")
            cover.write_bytes(b"\x89PNG\r\n\x1a\nimage")

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"opus")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    [source],
                    destination,
                    output_format="opus",
                    bitrate_mode="32k",
                    tags={"title": "Part 01", "album": "Book"},
                    cover=cover,
                )

            command = popen.call_args.args[0]
            metadata = [
                command[index + 1]
                for index, option in enumerate(command[:-1])
                if option == "-metadata"
            ]
            self.assertEqual(command.count("-i"), 2)
            self.assertIn("ffmetadata", command)
            self.assertEqual(command[command.index("-b:a") + 1], "32k")
            self.assertIn("title=Part 01", metadata)
            self.assertIn("album=Book", metadata)
            self.assertNotIn("METADATA_BLOCK_PICTURE", " ".join(command))
            self.assertEqual([
                command[index + 1]
                for index, option in enumerate(command[:-1])
                if option == "-map_metadata"
            ], ["-1", "1"])

    def test_single_file_export_uses_the_same_profile_pipeline(self):
        source = Path("source.mp3")
        destination = Path("result.ogg")

        with mock.patch.object(
            studio,
            "_export_merged_audio_ffmpeg",
            return_value=destination,
        ) as export:
            result = studio._export_single_audio_ffmpeg(
                source,
                destination,
                output_format="ogg",
                sample_rate="44100",
                channels="stereo",
                bitrate_mode="192k",
                speed=1.25,
            )

        self.assertEqual(result, destination)
        export.assert_called_once_with(
            [source],
            destination,
            output_format="ogg",
            sample_rate="44100",
            channels="stereo",
            bitrate_mode="192k",
            speed=1.25,
        )

    def test_export_mode_controls_disable_bitrate_for_wav_only(self):
        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app.export_tags_only_var = mock.Mock(get=mock.Mock(return_value=False))
        app.export_fmt_var = mock.Mock(get=mock.Mock(return_value="wav"))
        widget_names = (
            "btn_export_dir",
            "ent_export_dir",
            "cb_export_fmt",
            "cb_export_bitrate",
            "cb_export_sample_rate",
            "cb_export_channels",
            "chk_export_fx",
            "btn_audio_profiles_export",
            "btn_export_start",
            "btn_export_stop",
            "chk_export_tags_only",
        )
        for name in widget_names:
            setattr(app, name, mock.Mock())

        app._sync_export_mode_controls()

        app.cb_export_bitrate.configure.assert_called_with(
            state=studio.tk.DISABLED
        )
        app.cb_export_sample_rate.configure.assert_called_with(state="readonly")
        app.cb_export_channels.configure.assert_called_with(state="readonly")

        app.export_fmt_var.get.return_value = "mp3"
        app._sync_export_mode_controls()

        app.cb_export_bitrate.configure.assert_called_with(state="readonly")

    def test_update_config_from_ui_persists_export_profile_selections(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        app.settings_vars = {}
        app.export_fmt_var = mock.Mock(get=mock.Mock(return_value=" OGG "))
        app.export_bitrate_var = mock.Mock(get=mock.Mock(return_value=" 96K "))
        app.export_sample_rate_var = mock.Mock(
            get=mock.Mock(return_value=" 32000 ")
        )
        app.export_channels_var = mock.Mock(
            get=mock.Mock(return_value=" MONO ")
        )

        app.update_config_from_ui()

        self.assertEqual(app.config["export_format"], "ogg")
        self.assertNotIn("output_format", app.config)
        self.assertEqual(app.config["export_bitrate"], "96k")
        self.assertEqual(app.config["export_sample_rate"], "32000")
        self.assertEqual(app.config["export_channels"], "mono")

    def test_tags_only_disables_all_export_profile_controls(self):
        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app.export_tags_only_var = mock.Mock(get=mock.Mock(return_value=True))
        app.export_fmt_var = mock.Mock(get=mock.Mock(return_value="mp3"))
        widget_names = (
            "btn_export_dir",
            "ent_export_dir",
            "cb_export_fmt",
            "cb_export_bitrate",
            "cb_export_sample_rate",
            "cb_export_channels",
            "chk_export_fx",
            "btn_audio_profiles_export",
            "btn_export_start",
            "btn_export_stop",
            "chk_export_tags_only",
        )
        for name in widget_names:
            setattr(app, name, mock.Mock())
        effect_controls = tuple(mock.Mock() for _ in range(6))
        app.export_fx_value_controls = effect_controls

        app._sync_export_mode_controls()

        for control in (
            app.cb_export_bitrate,
            app.cb_export_sample_rate,
            app.cb_export_channels,
            app.chk_export_fx,
            app.btn_audio_profiles_export,
            *effect_controls,
        ):
            control.configure.assert_called_with(state=studio.tk.DISABLED)

    def test_streaming_merge_uses_rf64_auto_for_wav(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.mp3"
            destination = root / "book.wav"
            source.write_bytes(b"audio")

            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"RF64")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    [source], destination, output_format="wav"
                )

            command = popen.call_args.args[0]
            self.assertIn("-filter_complex", command)
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("channel_layouts=stereo", graph)
            self.assertEqual(
                command[command.index("-rf64") + 1], "auto"
            )
            self.assertEqual(destination.read_bytes(), b"RF64")

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

    def test_export_merge_plan_flattens_groups_in_visual_tree_order(self):
        plan = studio.plan_export_merge(
            ("root-before", "group-1", "root-after", "group-2"),
            {
                "group-1": ("child-a", "child-b"),
                "group-2": ("child-c", "child-d"),
            },
            ("group-2", "child-b", "root-before", "group-1"),
            ("group-1", "group-2"),
            (
                "root-before",
                "root-after",
                "child-a",
                "child-b",
                "child-c",
                "child-d",
            ),
        )

        self.assertEqual(plan["target_group"], "group-1")
        self.assertEqual(plan["source_groups"], ("group-2",))
        self.assertEqual(
            plan["file_ids"],
            ("root-before", "child-a", "child-b", "child-c", "child-d"),
        )

    def test_export_merge_plan_creates_new_group_for_files_only(self):
        plan = studio.plan_export_merge(
            ("group-1", "root-file", "group-2"),
            {
                "group-1": ("child-a", "child-b"),
                "group-2": ("child-c",),
            },
            ("child-c", "root-file", "child-b"),
            ("group-1", "group-2"),
            ("child-a", "child-b", "root-file", "child-c"),
        )

        self.assertIsNone(plan["target_group"])
        self.assertEqual(plan["source_groups"], ())
        self.assertEqual(
            plan["file_ids"], ("child-b", "root-file", "child-c")
        )

    def test_export_merge_controller_preserves_target_settings_and_moves_files(self):
        class FakeTree:
            def __init__(self):
                self.roots = ["root-file", "unselected", "group-1", "group-2"]
                self.children = {
                    "group-1": ["child-a", "child-b"],
                    "group-2": ["child-c"],
                }
                self.selected = ("group-2", "root-file", "group-1")
                self.deleted = []
                self.focused = None

            def selection(self):
                return self.selected

            def get_children(self, parent=""):
                if parent == "":
                    return tuple(self.roots)
                return tuple(self.children.get(parent, ()))

            def exists(self, item):
                return item in self.roots or any(
                    item in children for children in self.children.values()
                )

            def parent(self, item):
                for group, children in self.children.items():
                    if item in children:
                        return group
                return ""

            def move(self, item, parent, index):
                if item in self.roots:
                    self.roots.remove(item)
                for children in self.children.values():
                    if item in children:
                        children.remove(item)
                if parent == "":
                    self.roots.insert(int(index), item)
                else:
                    self.children.setdefault(parent, []).append(item)

            def delete(self, item):
                self.deleted.append(item)
                if item in self.roots:
                    self.roots.remove(item)
                self.children.pop(item, None)

            def selection_set(self, item):
                self.selected = (item,)

            def focus(self, item):
                self.focused = item

        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app._export_lock = False
        target_settings = {"name": "Том 1", "merge": True}
        app.export_groups = {
            "group-1": target_settings,
            "group-2": {"name": "Том 2", "merge": False},
        }
        app.export_files = {
            file_id: {"title": file_id}
            for file_id in (
                "root-file",
                "unselected",
                "child-a",
                "child-b",
                "child-c",
            )
        }
        app.export_tree = FakeTree()
        app._ask_yes_no = mock.Mock(return_value=True)
        app._show_info = mock.Mock()
        app._show_warning = mock.Mock()
        app.update_group_duration = mock.Mock()
        app.on_export_tree_select = mock.Mock()
        app.current_selected_export_item = "group-2"

        app.merge_selected_export_items()

        self.assertIs(app.export_groups["group-1"], target_settings)
        self.assertNotIn("group-2", app.export_groups)
        self.assertEqual(
            app.export_tree.get_children("group-1"),
            ("root-file", "child-a", "child-b", "child-c"),
        )
        self.assertEqual(app.export_tree.deleted, ["group-2"])
        self.assertEqual(app.export_tree.roots, ["group-1", "unselected"])
        self.assertEqual(app.export_tree.selected, ("group-1",))
        self.assertEqual(app.export_tree.focused, "group-1")
        app.update_group_duration.assert_called_with("group-1")
        app.on_export_tree_select.assert_called_once_with(None)

    def test_file_only_merge_is_inserted_at_first_source_and_keeps_parents(self):
        class FakeTree:
            def __init__(self):
                self.roots = ["group-a", "root-b", "group-c"]
                self.children = {
                    "group-a": ["child-a"],
                    "group-c": ["child-c"],
                }
                self.selected = ("child-c", "root-b", "child-a")

            def selection(self):
                return self.selected

            def get_children(self, parent=""):
                return tuple(
                    self.roots if parent == "" else self.children.get(parent, ())
                )

            def exists(self, item):
                return item in self.roots or any(
                    item in children for children in self.children.values()
                )

            def parent(self, item):
                for group, children in self.children.items():
                    if item in children:
                        return group
                return ""

            def move(self, item, parent, index):
                if item in self.roots:
                    self.roots.remove(item)
                for children in self.children.values():
                    if item in children:
                        children.remove(item)
                if parent == "":
                    self.roots.insert(int(index), item)
                else:
                    self.children.setdefault(parent, []).append(item)

            def selection_set(self, item):
                self.selected = (item,)

            def focus(self, _item):
                return None

        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app._export_lock = False
        app.export_groups = {
            "group-a": {"name": "A"},
            "group-c": {"name": "C"},
        }
        app.export_files = {
            file_id: {"title": file_id}
            for file_id in ("child-a", "root-b", "child-c")
        }
        app.export_tree = FakeTree()
        app._show_info = mock.Mock()
        app._show_warning = mock.Mock()
        app.update_group_duration = mock.Mock()
        app.on_export_tree_select = mock.Mock()
        app.current_selected_export_item = None

        def add_group():
            app.export_groups["merged"] = {"name": "Новая группа"}
            app.export_tree.roots.append("merged")
            app.export_tree.children["merged"] = []
            return "merged"

        app.add_export_group = add_group

        app.merge_selected_export_items()

        self.assertEqual(
            app.export_tree.roots, ["merged", "group-a", "group-c"]
        )
        self.assertEqual(
            app.export_tree.children["merged"],
            ["child-a", "root-b", "child-c"],
        )
        self.assertIn("group-a", app.export_groups)
        self.assertIn("group-c", app.export_groups)

    def test_empty_groups_are_not_reserved_as_export_outputs(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("        if not tags_only:\n            planned_outputs")
        end = source.index("            duplicates = duplicate_paths", start)
        preflight = source[start:end]

        empty_guard = preflight.index("if not children:\n                        continue")
        merged_output = preflight.index(
            'planned_outputs.append(out_dir / f"{group_name}.{fmt}")'
        )
        self.assertLess(empty_guard, merged_output)

    def test_clear_export_project_only_resets_the_in_memory_project(self):
        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app._export_lock = False
        app.export_groups = {"group-1": {"name": "Том 1"}}
        app.export_files = {
            "file-1": {"path": "/audio/one.mp3"},
            "file-2": {"path": "/audio/two.mp3"},
        }
        app.group_counter = 3
        app.current_selected_export_item = "file-1"
        app.export_tree = mock.Mock()
        app.export_tree.get_children.return_value = ("group-1", "file-2")
        app.export_tree.exists.return_value = True
        app.export_progress = {}
        app.lbl_export_status = mock.Mock()
        app._ask_yes_no = mock.Mock(return_value=True)
        app._disable_export_settings = mock.Mock()
        app.update_total_export_duration = mock.Mock()
        app._set_status_label = mock.Mock()

        app.clear_export_project()

        self.assertEqual(app.export_groups, {})
        self.assertEqual(app.export_files, {})
        self.assertEqual(app.group_counter, 0)
        self.assertIsNone(app.current_selected_export_item)
        self.assertEqual(app.export_progress["value"], 0)
        self.assertEqual(
            app.export_tree.delete.call_args_list,
            [mock.call("group-1"), mock.call("file-2")],
        )
        app._set_status_label.assert_called_once_with(
            app.lbl_export_status, "Ожидание...", "info"
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

    def test_next_group_name_preserves_existing_sequence_width(self):
        self.assertEqual(
            studio.next_sequence_name(
                "Том {num}", [f"Том {number:02d}" for number in range(1, 11)]
            ),
            "Том 11",
        )
        self.assertEqual(
            studio.next_sequence_name("Том {num}", ["Том 01", "Том 03"]),
            "Том 02",
        )
        self.assertEqual(
            studio.next_sequence_name(
                "Том {num}", [f"Том {number}" for number in range(1, 10)]
            ),
            "Том 10",
        )
        self.assertEqual(
            studio.next_sequence_name("Часть {num:0}", ["Часть 00", "Часть 02"]),
            "Часть 01",
        )
        self.assertEqual(
            studio.next_sequence_name("Том", ["Том", "Том 2"]),
            "Том 3",
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

    def test_direct_output_name_supports_opus_extension(self):
        self.assertEqual(
            studio.normalize_output_filename("chapter.ogg", "opus"),
            "chapter.opus",
        )

    def test_cover_is_only_forwarded_to_mp3_export(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cover = Path(tempdir) / "cover.png"
            cover.write_bytes(b"image")

            mp3 = studio.audio_export_kwargs("mp3", "128k", {"title": "A"}, cover)
            ogg = studio.audio_export_kwargs("ogg", "128k", {"title": "A"}, cover)
            opus = studio.audio_export_kwargs(
                "opus", "96k", {"title": "A"}, cover
            )

            self.assertEqual(mp3["cover"], str(cover))
            self.assertEqual(mp3["bitrate"], "128k")
            self.assertNotIn("cover", ogg)
            self.assertEqual(ogg["bitrate"], "128k")
            self.assertNotIn("cover", opus)
            self.assertEqual(opus["format"], "opus")
            self.assertEqual(opus["bitrate"], "96k")

    def test_wav_export_never_forwards_a_lossy_bitrate(self):
        kwargs = studio.audio_export_kwargs("wav", "320k")

        self.assertEqual(kwargs, {"format": "wav"})


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
    def test_leading_bom_is_removed_before_regex_but_internal_bom_is_kept(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.separators = []
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []
        processor.glossary_regex = [
            {"pattern": r"^Глава", "repl": "Раздел"}
        ]

        prepared = processor._prepare_raw_text(
            "\ufeffГлава 1. Текст\ufeffвнутри.",
            "___SEPARATOR_TOKEN___",
        )

        self.assertEqual(prepared, "Раздел 1. Текст\ufeffвнутри.")

    def test_numeric_minus_variants_are_kept_or_normalized_safely(self):
        text = (
            "-5\n- 5\n−5\n− 5\n– 5\n— 5\n"
            "-62-й\n- 62-й\n−62-й\n− 62-й\n–62-й\n—62-й"
        )

        normalized = studio.normalize_dialogue_line_starts(text)

        self.assertEqual(
            normalized,
            "-5\n- 5\n-5\n- 5\n— 5\n— 5\n"
            "-62-й\n— 62-й\n-62-й\n- 62-й\n— 62-й\n— 62-й",
        )

    def test_ordinal_at_dialogue_start_is_not_spoken_as_negative(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []

        prepared = studio.normalize_dialogue_line_starts("- 62-й ранг.")
        normalized = processor.process_sentence_text(prepared)

        self.assertNotIn("\ue001", normalized)
        self.assertEqual(normalized, "шестьдесят второй ранг.")

    def test_true_negative_ordinal_keeps_negative_semantics_inside_sentence(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []

        normalized = processor.process_sentence_text("Температура -62-я.")

        self.assertNotIn("\ue001", normalized)
        self.assertEqual(normalized, "Температура минус шестьдесят вторая.")

    def test_compact_negative_ordinal_at_line_start_remains_negative(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []

        normalized = processor.process_sentence_text("-62-я температура.")

        self.assertNotIn("\ue001", normalized)
        self.assertEqual(normalized, "минус шестьдесят вторая температура.")

    def test_synthesizable_text_accepts_supported_and_mixed_scripts(self):
        for text in ("Тест", "Test", "囧...... было такое лицо"):
            with self.subTest(text=text):
                self.assertTrue(studio.contains_synthesizable_text(text))
        for text in ("", "...", "—", "***", "123", "王", "火焰领主", "狐狸"):
            with self.subTest(text=text):
                self.assertFalse(studio.contains_synthesizable_text(text))

    def test_real_chinese_footnotes_keep_only_speakable_phrases(self):
        processor = object.__new__(studio.TTSProcessor)
        processor.cfg = {
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        processor.compiled_strict_case = []
        processor.compiled_ignore_case = []

        cases = (
            ("(王)", "王.", False),
            ("[4] (火焰领主)", "火焰领主.", False),
            ("[6] (狐狸)", "狐狸.", False),
            ('[3] (焱) "пламя"', "焱 пламя.", True),
        )
        for raw_text, expected, accepted in cases:
            with self.subTest(raw_text=raw_text):
                normalized = processor.process_sentence_text(raw_text)
                self.assertEqual(normalized, expected)
                self.assertEqual(
                    studio.contains_synthesizable_text(normalized), accepted
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


class NormalizerProfileAndGlossaryV15Tests(unittest.TestCase):
    def make_context(self, glossary=None, config=None):
        merged = studio.DEFAULT_CONFIG.copy()
        if config:
            merged.update(config)
        return studio.TTSProcessor.normalization_context(
            merged,
            glossary or studio.empty_glossary_data(),
        )

    def test_legacy_default_is_tts_and_keeps_options_compact(self):
        config = studio.normalize_config({})

        self.assertEqual(config["normalizer_mode"], "tts")
        self.assertTrue(config["normalizer_enabled"])
        self.assertTrue(config["glossary_enabled"])
        self.assertEqual(config["normalizer_options"], {})
        self.assertTrue(
            studio.resolved_normalizer_options(config)["enable_latinization"]
        )

    def test_global_normalizer_summary_distinguishes_saved_custom_state(self):
        builtin = studio.describe_normalizer_settings({})
        custom = studio.describe_normalizer_settings(
            {"normalizer_mode": "safe", "glossary_enabled": False}
        )

        self.assertEqual(builtin["profile"], "TTS (для озвучки)")
        self.assertEqual(
            custom["profile"], "Пользовательский (база Safe)"
        )
        self.assertIn("глоссарий: выкл.", custom["details"])

    def test_normalizer_snapshot_treats_compact_and_full_defaults_as_equal(self):
        compact = {"normalizer_mode": "tts", "normalizer_options": {}}
        full = {
            "normalizer_mode": "tts",
            "normalizer_options": studio.normalizer_mode_defaults("tts"),
        }

        self.assertEqual(
            studio.normalizer_settings_snapshot(compact),
            studio.normalizer_settings_snapshot(full),
        )

    def test_profile_roundtrip_is_complete_and_does_not_touch_api(self):
        current = {
            "api_token": "secret",
            "normalizer_mode": "safe",
            "normalizer_enabled": True,
            "normalizer_options": {"enable_latinization": True},
            "auto_abbreviations": False,
            "auto_short_words": True,
            "glossary_enabled": False,
        }

        profile = studio.normalizer_profile_from_config(current, name="Книга")
        applied = studio.apply_normalizer_profile(
            {"api_token": "keep-me", "speaker": "voice"}, profile
        )

        self.assertEqual(profile["schema"], studio.NORMALIZER_PROFILE_SCHEMA)
        self.assertEqual(profile["normalizer"]["mode"], "safe")
        self.assertTrue(profile["normalizer"]["options"]["enable_latinization"])
        self.assertEqual(applied["api_token"], "keep-me")
        self.assertEqual(applied["speaker"], "voice")
        self.assertFalse(applied["glossary_enabled"])

    def test_exported_normalizer_profile_omits_local_dictionary_path(self):
        profile = studio.normalizer_profile_from_config(
            {
                "normalizer_mode": "tts",
                "normalizer_options": {
                    "dictionaries_path": "/private/local/dictionaries"
                },
            }
        )

        self.assertEqual(
            profile["normalizer"]["options"]["dictionaries_path"], ""
        )

    def test_imported_normalizer_profile_rejects_invalid_option_values(self):
        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["normalizer"]["options"]["latinization_backend"] = "unknown"

        with self.assertRaisesRegex(ValueError, "latinization_backend"):
            studio.normalize_normalizer_profile(profile)

        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["normalizer"]["options"]["remove_links_ignore_interval"] = [
            2200,
            1000,
        ]
        with self.assertRaisesRegex(ValueError, "remove_links_ignore_interval"):
            studio.normalize_normalizer_profile(profile)

    def test_imported_normalizer_profile_rejects_wrong_types_and_typos(self):
        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["normalizer"]["enabled"] = "yes"
        with self.assertRaisesRegex(ValueError, "normalizer.enabled"):
            studio.normalize_normalizer_profile(profile)

        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["normalizer"]["options"]["enable_latinisation"] = True
        with self.assertRaisesRegex(ValueError, "enable_latinisation"):
            studio.normalize_normalizer_profile(profile)

    def test_imported_normalizer_profile_requires_complete_versioned_schema(self):
        with self.assertRaisesRegex(ValueError, "обязательные поля"):
            studio.normalize_normalizer_profile({})

        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["version"] = True
        with self.assertRaisesRegex(ValueError, "целым числом"):
            studio.normalize_normalizer_profile(profile)

        profile = studio.normalizer_profile_from_config({}, name="Проверка")
        profile["normalizer"]["enable"] = False
        with self.assertRaisesRegex(ValueError, "неизвестные поля normalizer"):
            studio.normalize_normalizer_profile(profile)

    def test_portable_normalizer_profile_rejects_paths_and_preserves_local_path(self):
        current = {
            "normalizer_mode": "tts",
            "normalizer_options": {"dictionaries_path": "/local/dictionaries"},
        }
        profile = studio.normalizer_profile_from_config({}, name="Переносимый")
        applied = studio.apply_normalizer_profile(current, profile)

        self.assertEqual(
            applied["normalizer_options"]["dictionaries_path"],
            "/local/dictionaries",
        )

        profile["normalizer"]["options"]["dictionaries_path"] = "/foreign"
        with self.assertRaisesRegex(ValueError, "локальный путь"):
            studio.normalize_normalizer_profile(profile)

        profile = studio.normalizer_profile_from_config({}, name="Переносимый")
        profile["normalizer"]["options"]["latin_dictionary_filename"] = (
            "../secret.dic"
        )
        with self.assertRaisesRegex(ValueError, "latin_dictionary_filename"):
            studio.normalize_normalizer_profile(profile)

    def test_verbatim_term_survives_normalizer_and_cleanup_exactly(self):
        processor = self.make_context(
            {
                "terms_ignore_case": {
                    "убого": {
                        "replacement": "уб+о'го",
                        "verbatim": True,
                    }
                }
            }
        )

        normalized = processor.process_sentence_text("Это убого 10 раз.")

        self.assertEqual(normalized, "Это уб+о'го десять раз.")
        self.assertNotIn("\U000f0000", normalized)

    def test_verbatim_keeps_context_for_neighboring_roman_number(self):
        processor = self.make_context(
            {
                "terms_strict_case": {
                    "Глава": {
                        "replacement": "Гл+ава",
                        "verbatim": True,
                    }
                }
            }
        )

        self.assertEqual(
            processor.process_sentence_text("Глава IV."),
            "Гл+ава четвёртая.",
        )

    def test_verbatim_is_case_aware_and_whole_word_by_default(self):
        glossary = {
            "terms_ignore_case": {
                "убого": {
                    "replacement": "уб+о'го",
                    "verbatim": True,
                }
            }
        }
        processor = self.make_context(glossary)

        self.assertEqual(processor.process_sentence_text("УБОГО!"), "УБ+О'ГО!")
        self.assertIn(
            "Преубогословие",
            processor.process_sentence_text("Преубогословие 10 раз."),
        )

    def test_verbatim_substring_protects_the_containing_word(self):
        processor = self.make_context(
            {
                "terms_ignore_case": {
                    "убого": {
                        "replacement": "уб+о'го",
                        "verbatim": True,
                        "whole_word": False,
                    }
                }
            }
        )

        self.assertEqual(
            processor.process_sentence_text("преубогословие 10 раз."),
            "преуб+о'гословие десять раз.",
        )

    def test_verbatim_regex_preserves_expanded_replacement(self):
        processor = self.make_context(
            {
                "regex_rules": [
                    {
                        "pattern": r"API-(\d+)",
                        "repl": r"эйп+и'ай-\1",
                        "verbatim": True,
                    }
                ]
            }
        )

        self.assertEqual(
            processor.process_sentence_text("API-42 и 10 раз."),
            "эйп+и'ай-42 и десять раз.",
        )

    def test_verbatim_substring_replaces_repeated_matches_in_one_word(self):
        processor = self.make_context(
            {
                "terms_ignore_case": {
                    "убого": {
                        "replacement": "X",
                        "verbatim": True,
                        "whole_word": False,
                    }
                }
            }
        )

        self.assertEqual(processor.process_sentence_text("убогоубого"), "XX.")

    def test_verbatim_regex_inside_word_does_not_add_boundaries(self):
        processor = self.make_context(
            {
                "regex_rules": [
                    {"pattern": "убого", "repl": "X", "verbatim": True}
                ]
            }
        )

        self.assertEqual(
            processor.process_sentence_text("преубогословие"),
            "преXсловие.",
        )

    def test_verbatim_regex_replaces_every_match_inside_one_word(self):
        processor = self.make_context(
            {
                "regex_rules": [
                    {"pattern": "убого", "repl": "X", "verbatim": True}
                ]
            }
        )

        self.assertEqual(
            processor.process_sentence_text("убогоубого"),
            "XX.",
        )

    def test_marker_damage_uses_segment_fallback_without_leaking_marker(self):
        class MarkerRemovingNormalizer:
            def normalize(self, text):
                return "".join(char for char in text if ord(char) < 0xF0000)

        processor = self.make_context(
            {
                "terms_ignore_case": {
                    "точно": {"replacement": "т+о'чно", "verbatim": True}
                }
            }
        )
        processor._normalizer = MarkerRemovingNormalizer()

        normalized = processor.process_sentence_text("Это точно.")

        self.assertEqual(normalized, "Это т+о'чно.")

    def test_changed_verbatim_source_uses_logged_opaque_fallback(self):
        class NumberChangingNormalizer:
            def normalize(self, text):
                return text.replace("42", "сорок два")

        processor = self.make_context(
            {
                "regex_rules": [
                    {"pattern": "42", "repl": "XLII", "verbatim": True}
                ]
            }
        )
        processor._normalizer = NumberChangingNormalizer()

        with self.assertLogs(level="WARNING") as captured:
            normalized = processor.process_sentence_text("Код 42.")

        self.assertEqual(normalized, "Код XLII.")
        self.assertIn("грамматический контекст", "\n".join(captured.output))

    def test_preview_uses_chunk_pipeline_and_reports_unsupported_text(self):
        processor = self.make_context()

        result = processor.preview_normalization("Глава 10.\n\n(王)")

        self.assertIn("десятая", result["normalized_text"].lower())
        self.assertEqual(result["sentence_count"], 1)
        self.assertEqual(result["skipped"][0]["source"], "(王)")

    def test_preview_file_text_restores_a_real_separator(self):
        processor = self.make_context()

        result = processor.preview_normalization(
            "Глава 10.\n***\nПродолжение 20."
        )

        self.assertIn("[ПАУЗА РАЗДЕЛИТЕЛЯ]", result["normalized_text"])
        self.assertNotIn(
            "[ПАУЗА РАЗДЕЛИТЕЛЯ]", result["normalized_text_for_file"]
        )
        self.assertIn("☆☆☆", result["normalized_text_for_file"])

    def test_prepared_text_is_literal_and_bypasses_semantic_pipeline(self):
        processor = self.make_context(
            glossary={
                "terms_ignore_case": {"убого": "ИЗМЕНЕНО"},
                "regex_rules": [{"pattern": "10", "repl": "десять"}],
            },
            config={
                "text_is_prepared": True,
                "auto_abbreviations": True,
                "auto_short_words": True,
            },
        )
        processor.apply_regex_rules = mock.Mock(
            side_effect=AssertionError("RegEx must not run for prepared text")
        )
        processor.apply_glossary_segments = mock.Mock(
            side_effect=AssertionError("glossary must not run for prepared text")
        )
        processor._normalizer = mock.Mock()
        processor._normalizer.normalize.side_effect = AssertionError(
            "ru-normalizr must not run for prepared text"
        )

        prepared = processor._prepare_raw_text(
            "уб+о'го 10", "___SEPARATOR_TOKEN___"
        )
        normalized = processor.process_sentence_text(prepared)

        self.assertEqual(normalized, "уб+о'го 10")
        processor.apply_regex_rules.assert_not_called()
        processor.apply_glossary_segments.assert_not_called()
        processor._normalizer.normalize.assert_not_called()

    def test_prepared_text_removes_only_structural_dialogue_prefix(self):
        processor = self.make_context(config={"text_is_prepared": True})

        self.assertEqual(
            processor.process_sentence_text("— уб+о'го 10"),
            "уб+о'го 10",
        )

    def test_prepared_text_rejects_control_and_private_use_markers(self):
        processor = self.make_context(config={"text_is_prepared": True})

        for unsafe_text in ("текст\x00", "текст\U000f0000"):
            with self.subTest(unsafe_text=repr(unsafe_text)):
                with self.assertRaisesRegex(
                    ValueError, "служебный или управляющий символ"
                ):
                    processor.process_sentence_text(unsafe_text)

    def test_normal_pipeline_still_normalizes_when_text_is_not_prepared(self):
        processor = self.make_context(config={"text_is_prepared": False})

        self.assertEqual(processor.process_sentence_text("10"), "десять.")

    def test_preview_file_roundtrip_keeps_separator_and_dialogue_pause(self):
        processor = self.make_context()

        first_pass = processor.preview_normalization(
            "— Глава 10.\n***\nПродолжение 20."
        )
        saved_text = first_pass["normalized_text_for_file"]
        saved_paragraphs = saved_text.split("\n\n")

        self.assertTrue(saved_paragraphs[0].startswith("— "))
        self.assertEqual(saved_paragraphs[1], processor.separators[0])
        self.assertNotIn("[ПАУЗА РАЗДЕЛИТЕЛЯ]", saved_text)

        prepared_processor = self.make_context(
            config={"text_is_prepared": True}
        )
        second_pass = prepared_processor.preview_normalization(saved_text)

        self.assertEqual(second_pass["normalized_text_for_file"], saved_text)

    def test_hash_scan_can_include_exact_prepared_payloads(self):
        processor = self.make_context(config={"text_is_prepared": False})
        raw_text = "уб+о'го 10"

        ordinary_hashes = processor.get_all_possible_hashes(raw_text)
        hashes_with_prepared = processor.get_all_possible_hashes(
            raw_text, include_prepared=True
        )
        exact_hash = studio.cache_content_hash(
            raw_text, processor.cfg["speaker"]
        )

        self.assertNotIn(exact_hash, ordinary_hashes)
        self.assertTrue(ordinary_hashes.issubset(hashes_with_prepared))
        self.assertIn(exact_hash, hashes_with_prepared)

    def test_scheduling_preview_immediately_invalidates_stale_result(self):
        app = object.__new__(studio.TTSApp)
        app.normalizer_source_text = mock.Mock()
        app.btn_save_normalized_text = mock.Mock()
        app.root = mock.Mock()
        app.root.after.return_value = "scheduled-preview"
        app._normalizer_preview_after_id = None
        app._normalizer_preview_generation = 7
        app._normalizer_last_preview_result = {
            "normalized_text_for_file": "устаревший текст"
        }

        app._schedule_normalizer_preview(delay_ms=125)

        self.assertIsNone(app._normalizer_last_preview_result)
        app.btn_save_normalized_text.configure.assert_called_once_with(
            state=studio.tk.DISABLED
        )
        self.assertGreater(app._normalizer_preview_generation, 7)
        app.root.after.assert_called_once_with(125, app.run_normalizer_preview)

    def test_skipped_preview_requires_confirmation_before_saving(self):
        with tempfile.TemporaryDirectory() as tempdir:
            destination = Path(tempdir) / "incomplete.txt"
            app = object.__new__(studio.TTSApp)
            app.config = {
                "input_dir": tempdir,
                "last_normalizer_text_dir": "",
            }
            app._normalizer_last_preview_result = {
                "normalized_text_for_file": "Глава десятая.",
                "skipped": [{"source": "(王)", "normalized": ""}],
            }
            app._ask_yes_no = mock.Mock(return_value=False)
            app.save_settings = mock.Mock(return_value=True)
            app._show_info = mock.Mock()
            app._show_warning = mock.Mock()
            app._show_error = mock.Mock()

            with mock.patch.object(
                studio.filedialog,
                "asksaveasfilename",
                return_value=str(destination),
            ) as save_dialog:
                app.save_normalized_text_to_file()

            app._ask_yes_no.assert_called_once()
            save_dialog.assert_not_called()
            self.assertFalse(destination.exists())

    def test_normalized_text_is_saved_atomically_as_utf8(self):
        with tempfile.TemporaryDirectory() as tempdir:
            destination = Path(tempdir) / "prepared.txt"
            app = object.__new__(studio.TTSApp)
            app.config = {
                "input_dir": tempdir,
                "last_normalizer_text_dir": "",
            }
            app._normalizer_last_preview_result = {
                "normalized_text_for_file": "Гл+ава десятая.\n\n☆☆☆"
            }
            app.save_settings = mock.Mock(return_value=True)
            app._show_info = mock.Mock()
            app._show_warning = mock.Mock()
            app._show_error = mock.Mock()

            with mock.patch.object(
                studio.filedialog,
                "asksaveasfilename",
                return_value=str(destination),
            ):
                app.save_normalized_text_to_file()

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "Гл+ава десятая.\n\n☆☆☆",
            )
            self.assertEqual(
                app.config["last_normalizer_text_dir"], tempdir
            )
            app.save_settings.assert_called_once_with()
            app._show_info.assert_called_once()

    def test_failed_global_profile_write_does_not_change_live_config(self):
        app = object.__new__(studio.TTSApp)
        original = {
            "normalizer_enabled": True,
            "normalizer_mode": "tts",
            "normalizer_options": {},
            "glossary_enabled": True,
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        app.config = copy.deepcopy(original)
        app.settings_vars = {
            "auto_abbreviations": mock.Mock(),
            "auto_short_words": mock.Mock(),
        }
        app._collect_normalizer_preview_config = mock.Mock(
            return_value={
                "normalizer_enabled": False,
                "normalizer_mode": "safe",
                "normalizer_options": {},
                "glossary_enabled": False,
                "auto_abbreviations": False,
                "auto_short_words": False,
            }
        )
        app._persist_settings_snapshot = mock.Mock(
            side_effect=OSError("disk full")
        )
        app._show_error = mock.Mock()
        app._show_info = mock.Mock()

        app.apply_normalizer_preview_globally()

        self.assertEqual(app.config, original)
        app.settings_vars["auto_abbreviations"].set.assert_not_called()
        app._show_error.assert_called_once()
        app._show_info.assert_not_called()

    def test_glossary_edit_invalidates_preview_and_marks_editor_dirty(self):
        app = object.__new__(studio.TTSApp)
        app.txt_glossary = mock.Mock()
        app.txt_glossary.edit_modified.return_value = True
        app._glossary_ui_loading = False
        app._glossary_dirty = False
        app.normalizer_source_text = mock.Mock()
        app.normalizer_preview_glossary_var = mock.Mock()
        app.normalizer_preview_glossary_var.get.return_value = True
        app._schedule_normalizer_preview = mock.Mock()

        app._on_glossary_modified()

        self.assertTrue(app._glossary_dirty)
        app.txt_glossary.edit_modified.assert_called_with(False)
        app._schedule_normalizer_preview.assert_called_once_with()

    def test_stale_glossary_editor_cannot_feed_or_overwrite_new_cache(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(root / "new-cache")}
            app._glossary_loaded_path = root / "old-cache" / "glossary.json"
            app.txt_glossary = mock.Mock()
            app._write_json_atomic = mock.Mock()
            app._show_error = mock.Mock()

            with self.assertRaisesRegex(ValueError, "папка кэша изменилась"):
                app._normalizer_glossary_snapshot(True)
            self.assertFalse(app.save_glossary_ui())
            app._write_json_atomic.assert_not_called()

    def test_declined_glossary_reload_immediately_invalidates_old_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(root / "new-cache")}
            app._glossary_loaded_path = root / "old-cache" / "glossary.json"
            app._glossary_dirty = True
            app.txt_glossary = mock.Mock()
            app.normalizer_source_text = mock.Mock()
            app.lbl_normalizer_preview_status = mock.Mock()
            app._ask_yes_no = mock.Mock(return_value=False)
            app._set_status_label = mock.Mock()
            app._schedule_normalizer_preview = mock.Mock()

            synced = app._sync_glossary_editor_cache(prompt_if_dirty=True)

            self.assertFalse(synced)
            app._schedule_normalizer_preview.assert_called_once_with(delay_ms=10)

    def test_synthesis_preflight_saves_dirty_glossary_used_by_preview(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"glossary_enabled": True}
        app.txt_glossary = mock.Mock()
        app.txt_glossary.edit_modified.return_value = True
        app._glossary_dirty = True
        app._sync_glossary_editor_cache = mock.Mock(return_value=True)
        app._ask_yes_no = mock.Mock(return_value=True)
        app.save_glossary_ui = mock.Mock(return_value=True)

        self.assertTrue(app._prepare_glossary_for_synthesis())

        app._ask_yes_no.assert_called_once()
        app.save_glossary_ui.assert_called_once_with(show_popup=False)

    def test_prepared_text_does_not_require_glossary_save(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"glossary_enabled": True}
        app.txt_glossary = mock.Mock()
        app._sync_glossary_editor_cache = mock.Mock()

        self.assertTrue(
            app._prepare_glossary_for_synthesis(prepared_text=True)
        )
        app._sync_glossary_editor_cache.assert_not_called()

    def test_glossary_reload_path_error_keeps_current_editor_text(self):
        with tempfile.TemporaryDirectory() as tempdir:
            blocked_cache = Path(tempdir) / "cache-is-a-file"
            blocked_cache.write_text("not a directory", encoding="utf-8")
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(blocked_cache)}
            app.txt_glossary = mock.Mock()
            app._show_error = mock.Mock()

            self.assertFalse(app.load_glossary_ui())

            app.txt_glossary.delete.assert_not_called()
            app.txt_glossary.insert.assert_not_called()
            app._show_error.assert_called_once()

    def test_glossary_ui_recovers_from_structurally_invalid_primary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            primary = cache_dir / "glossary.json"
            backup = cache_dir / "glossary.json.bak"
            primary.write_text(
                json.dumps({"terms_ignore_case": []}), encoding="utf-8"
            )
            backup.write_text(
                json.dumps({"terms_ignore_case": {"API": "эй-пи-ай"}}),
                encoding="utf-8",
            )
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(cache_dir)}
            app.txt_glossary = mock.Mock()
            app._write_json_atomic = mock.Mock()
            app._show_error = mock.Mock()

            self.assertTrue(app.load_glossary_ui())

            loaded = json.loads(app.txt_glossary.insert.call_args.args[1])
            self.assertEqual(
                loaded["terms_ignore_case"], {"API": "эй-пи-ай"}
            )
            app._write_json_atomic.assert_called_once()
            app._show_error.assert_not_called()

    def test_all_invalid_glossary_candidates_preserve_editor(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            (cache_dir / "glossary.json").write_text("{", encoding="utf-8")
            (cache_dir / "glossary.json.bak").write_text(
                json.dumps({"regex_rules": "not-a-list"}), encoding="utf-8"
            )
            app = object.__new__(studio.TTSApp)
            app.config = {"cache_dir": str(cache_dir)}
            app.txt_glossary = mock.Mock()
            app._show_error = mock.Mock()

            self.assertFalse(app.load_glossary_ui())

            app.txt_glossary.delete.assert_not_called()
            app.txt_glossary.insert.assert_not_called()
            app._show_error.assert_called_once()

    def test_manual_glossary_reload_can_keep_unsaved_editor(self):
        app = object.__new__(studio.TTSApp)
        app._glossary_dirty = True
        app.txt_glossary = mock.Mock()
        app.txt_glossary.edit_modified.return_value = False
        app._ask_yes_no = mock.Mock(return_value=False)
        app.load_glossary_ui = mock.Mock(return_value=True)

        self.assertFalse(app.reload_glossary_ui())

        app.load_glossary_ui.assert_not_called()

    def test_glossary_rule_records_cover_all_sections_and_flags(self):
        data = {
            "accents_ignore_case": ["молок+о"],
            "accents_strict_case": ["Зам+ок"],
            "terms_ignore_case": {
                "убого": {
                    "replacement": "уб+о'го",
                    "verbatim": True,
                    "whole_word": True,
                }
            },
            "terms_strict_case": {"API": "эй-пи-ай"},
            "regex_rules": [
                {"pattern": r"\bX\b", "repl": "икс", "verbatim": True}
            ],
        }

        records = studio.glossary_rule_records(data)

        self.assertEqual(len(records), 5)
        self.assertEqual(
            {record["group"] for record in records},
            {"accents", "terms", "regex"},
        )
        verbatim_term = next(
            record for record in records if record["source"] == "убого"
        )
        self.assertEqual(verbatim_term["replacement"], "уб+о'го")
        self.assertIn("verbatim", verbatim_term["flags"])
        self.assertIn("убого", verbatim_term["search_text"])

    def test_term_priority_over_shadowed_accent_is_visible_and_logged(self):
        data = {
            "accents_ignore_case": ["Sil+ero"],
            "accents_strict_case": ["уб+ого"],
            "terms_ignore_case": {"silero": "силеро"},
            "terms_strict_case": {"убого": "уб+о'го"},
        }

        conflicts = studio.glossary_shadowed_accent_rules(data)
        self.assertEqual(len(conflicts), 2)
        records = studio.glossary_rule_records(data)
        accent_flags = [
            record["flags"]
            for record in records
            if record["group"] == "accents"
        ]
        self.assertTrue(
            all("приоритет термина" in flags for flags in accent_flags)
        )

        processor = object.__new__(studio.TTSProcessor)
        with self.assertLogs(level="WARNING") as captured:
            processor.load_glossary_data(data)
        self.assertEqual(processor.glossary_strict_case["убого"], "уб+о'го")
        self.assertIn("2 правил ударения", "\n".join(captured.output))

    def test_glossary_deletion_is_exact_and_preserves_future_fields(self):
        data = {
            "accents_ignore_case": ["дубль", "дубль"],
            "terms_ignore_case": {"one": "один", "two": "два"},
            "regex_rules": [
                {"pattern": "x", "repl": "first"},
                {"pattern": "x", "repl": "second"},
            ],
            "future_metadata": {"owner": "user"},
        }

        updated, removed = studio.remove_glossary_rules(
            data,
            {
                ("accents_ignore_case", 1),
                ("terms_ignore_case", "two"),
                ("regex_rules", 0),
            },
        )

        self.assertEqual(removed, 3)
        self.assertEqual(updated["accents_ignore_case"], ["дубль"])
        self.assertEqual(updated["terms_ignore_case"], {"one": "один"})
        self.assertEqual(
            updated["regex_rules"], [{"pattern": "x", "repl": "second"}]
        )
        self.assertEqual(updated["future_metadata"], {"owner": "user"})

        cleared, cleared_count = studio.clear_glossary_rules(data)
        self.assertEqual(cleared_count, 6)
        for section, default in studio.GLOSSARY_SECTION_DEFAULTS.items():
            self.assertEqual(cleared[section], default)
        self.assertEqual(cleared["future_metadata"], {"owner": "user"})

    def test_programmatic_glossary_replacement_marks_preview_dirty(self):
        app = object.__new__(studio.TTSApp)
        app.txt_glossary = mock.Mock()
        app._glossary_ui_loading = False
        app._mark_glossary_editor_dirty = mock.Mock()

        app._replace_glossary_editor_data(
            {"terms_ignore_case": {"test": "т+ест"}}
        )

        self.assertFalse(app._glossary_ui_loading)
        app._mark_glossary_editor_dirty.assert_called_once_with()
        inserted_json = app.txt_glossary.insert.call_args.args[1]
        self.assertEqual(
            json.loads(inserted_json)["terms_ignore_case"],
            {"test": "т+ест"},
        )

    def test_glossary_merge_keeps_personal_conflicts_by_default(self):
        current = {
            "terms_ignore_case": {"Silero": "моё"},
            "regex_rules": [{"pattern": "x", "repl": "mine"}],
        }
        imported = {
            "terms_ignore_case": {
                "silero": {"replacement": "общее", "verbatim": True},
                "убого": {"replacement": "уб+о'го", "verbatim": True},
            },
            "regex_rules": [
                {"pattern": "x", "repl": "central"},
                {"pattern": "y", "repl": "new"},
            ],
        }

        merged, stats = studio.merge_glossary_data(current, imported)

        self.assertEqual(merged["terms_ignore_case"]["Silero"], "моё")
        self.assertTrue(
            merged["terms_ignore_case"]["убого"]["verbatim"]
        )
        self.assertEqual(merged["regex_rules"][0]["repl"], "mine")
        self.assertEqual(merged["regex_rules"][1]["pattern"], "y")
        self.assertEqual(stats["added"], 2)
        self.assertEqual(stats["kept"], 2)

    def test_glossary_merge_can_explicitly_replace_conflicts(self):
        merged, stats = studio.merge_glossary_data(
            {"terms_strict_case": {"API": "старое"}},
            {"terms_strict_case": {"API": "новое"}},
            replace_existing=True,
        )

        self.assertEqual(merged["terms_strict_case"]["API"], "новое")
        self.assertEqual(stats["replaced"], 1)

    def test_malformed_accent_sections_are_not_split_into_characters(self):
        merged, _stats = studio.merge_glossary_data(
            {"accents_ignore_case": "аб"},
            {"accents_ignore_case": "вг"},
        )

        self.assertEqual(merged["accents_ignore_case"], [])

    def test_glossary_validation_rejects_wrong_section_type(self):
        with self.assertRaisesRegex(ValueError, "accents_ignore_case"):
            studio.canonicalize_glossary_data(
                {"accents_ignore_case": "не массив"}
            )

    def test_glossary_validation_rejects_invalid_regex(self):
        with self.assertRaisesRegex(ValueError, "regex_rules\\[0\\]"):
            studio.canonicalize_glossary_data(
                {"regex_rules": [{"pattern": "(", "repl": "x"}]}
            )

    def test_glossary_validation_preserves_future_root_fields(self):
        result = studio.canonicalize_glossary_data(
            {"future_metadata": {"version": 2}}
        )

        self.assertEqual(result["future_metadata"], {"version": 2})
        self.assertEqual(result["terms_ignore_case"], {})


class AudioProfileV15Tests(unittest.TestCase):
    def test_profile_roundtrip_contains_format_layout_and_effects(self):
        profile = studio.make_audio_profile(
            "Моя речь",
            output_format="opus",
            bitrate="32k",
            sample_rate="48000",
            channels="mono",
            effects_enabled=True,
            speed=1.15,
            pitch=0.95,
        )

        self.assertEqual(profile["schema"], studio.AUDIO_PROFILE_SCHEMA)
        self.assertEqual(profile["audio"]["format"], "opus")
        self.assertEqual(profile["audio"]["bitrate"], "32k")
        self.assertTrue(profile["audio"]["effects"]["enabled"])
        self.assertEqual(
            studio.normalize_audio_profile(profile),
            profile,
        )

    def test_profile_summary_reports_exact_match_and_decodes_parameters(self):
        profile = studio.make_audio_profile(
            "Мой Opus",
            output_format="opus",
            bitrate="48k",
            sample_rate="48000",
            channels="mono",
        )

        self.assertEqual(
            studio.matching_audio_profile_name(profile),
            "Opus · речь 48 кбит/с",
        )
        duplicate = dict(profile)
        duplicate["name"] = "Такие же параметры"
        self.assertIsNone(
            studio.matching_audio_profile_name(profile, [duplicate])
        )
        description = studio.describe_audio_profile(profile)
        self.assertIn("Opus (Ogg)", description)
        self.assertIn("48 кбит/с", description)
        self.assertIn("48 кГц", description)
        self.assertIn("эффекты: выкл.", description)

    def test_disabled_effect_values_do_not_break_profile_equivalence(self):
        saved = studio.make_audio_profile(
            "Шаблон",
            bitrate="96k",
            effects_enabled=False,
            speed=1.7,
            pitch=1.2,
            echo=True,
        )
        current = studio.make_audio_profile(
            "Текущий",
            bitrate="96k",
            effects_enabled=False,
        )

        self.assertEqual(
            studio.matching_audio_profile_name(current, [saved]),
            "Шаблон",
        )

    def test_direct_run_snapshot_keeps_local_effects_out_of_global_config(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "fx_speed": 1.0,
            "fx_pitch": 1.0,
            "fx_echo": False,
        }
        app.dir_speed_var = mock.Mock(get=mock.Mock(return_value=1.25))
        app.dir_pitch_var = mock.Mock(get=mock.Mock(return_value=0.9))
        app.dir_echo_var = mock.Mock(get=mock.Mock(return_value=True))
        app.dir_echo_delay_var = mock.Mock(get=mock.Mock(return_value=250))
        app.dir_echo_decay_var = mock.Mock(get=mock.Mock(return_value=0.4))

        direct = app._direct_processing_config(
            prepared_text=True,
            apply_direct_tags=False,
            output_dir="direct",
        )

        self.assertEqual(direct["fx_speed"], 1.25)
        self.assertEqual(direct["fx_pitch"], 0.9)
        self.assertTrue(direct["fx_echo"])
        self.assertEqual(app.config["fx_speed"], 1.0)
        self.assertFalse(app.config["fx_echo"])

    def test_audio_profile_can_update_target_without_implicit_save(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        app.settings_vars = {}
        for name in (
            "export_fmt_var",
            "export_bitrate_var",
            "export_sample_rate_var",
            "export_channels_var",
            "export_apply_fx_var",
            "exp_speed_var",
            "exp_pitch_var",
            "exp_echo_var",
            "exp_delay_var",
            "exp_decay_var",
        ):
            setattr(app, name, mock.Mock())
        for name in (
            "lbl_exp_speed",
            "lbl_exp_pitch",
            "lbl_exp_delay",
            "lbl_exp_decay",
        ):
            setattr(app, name, mock.Mock())
        app._sync_export_mode_controls = mock.Mock()
        app._refresh_audio_profile_summaries = mock.Mock()
        app.save_settings = mock.Mock(return_value=True)
        profile = studio.make_audio_profile(
            "Тест", output_format="opus", bitrate="48k"
        )

        app._apply_audio_profile_to_ui(profile, "export", save=False)

        app.save_settings.assert_not_called()
        app.export_fmt_var.set.assert_called_once_with("opus")
        app._refresh_audio_profile_summaries.assert_called_once_with()

    def test_audio_profile_manager_stages_list_changes_until_commit(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def open_audio_profiles_dialog(")
        end = source.index("# --- Вкладка \"Кэш\" ---", start)
        dialog_source = source[start:end]
        save_start = dialog_source.index("        def stage_current_editor(")
        delete_start = dialog_source.index("        def delete_custom():")
        export_start = dialog_source.index("        def staged_initial_dir():")
        staged_actions = dialog_source[save_start:export_start]
        add_start = dialog_source.index("        def add_custom():")
        add_end = dialog_source.index("        ttk.Button(\n            list_actions", add_start)
        add_block = dialog_source[add_start:add_end]

        self.assertIn("staged_profiles = []", dialog_source)
        self.assertIn("staged_profiles.append(profile)", staged_actions)
        self.assertIn("del staged_profiles[index]", staged_actions)
        self.assertNotIn("staged_profiles.append(draft)", add_block)
        self.assertNotIn('self.config["audio_profiles"]', staged_actions)
        self.assertNotIn("self.save_settings()", staged_actions)
        self.assertLess(save_start, delete_start)

    def test_audio_profile_manager_cancel_and_close_discard_staged_state(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def open_audio_profiles_dialog(")
        end = source.index("# --- Вкладка \"Кэш\" ---", start)
        dialog_source = source[start:end]

        self.assertIn('text="Отмена"', dialog_source)
        self.assertIn("command=close_dialog", dialog_source)
        self.assertIn('dialog.protocol("WM_DELETE_WINDOW", close_dialog)', dialog_source)
        self.assertIn('dialog.bind("<Escape>"', dialog_source)
        self.assertIn('parent=dialog', dialog_source)

    def test_audio_profile_manager_keeps_commit_actions_visible_and_transactional(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def open_audio_profiles_dialog(")
        end = source.index("# --- Вкладка \"Кэш\" ---", start)
        dialog_source = source[start:end]
        commit_start = dialog_source.index("        def commit_dialog(")
        commit_end = dialog_source.index("        ttk.Button(\n            apply_buttons", commit_start)
        commit_block = dialog_source[commit_start:commit_end]

        self.assertIn("footer_hint.grid(row=0", dialog_source)
        self.assertIn("apply_buttons.grid(\n            row=1", dialog_source)
        self.assertIn("close_buttons.grid(\n            row=2", dialog_source)
        self.assertIn('footer.bind("<Configure>"', dialog_source)
        self.assertIn('text="➕ Добавить"', dialog_source)
        self.assertIn('text="🗑 Удалить"', dialog_source)
        self.assertIn('text="Сохранить и закрыть"', dialog_source)
        self.assertNotIn('text="ОК"', dialog_source)
        self.assertNotIn("нажмите ОК", dialog_source)
        self.assertIn('candidate_config["audio_profiles"] = copy.deepcopy(staged_profiles)', commit_block)
        self.assertIn("stage_current_editor(refresh=False)", commit_block)
        self.assertIn("self._persist_settings_snapshot(candidate_config)", commit_block)
        self.assertNotIn("self.save_settings()", commit_block)
        self.assertNotIn("self.set_ui_from_config()", commit_block)

    def test_profile_targets_are_independent(self):
        profile = studio.make_audio_profile(
            "Opus",
            output_format="opus",
            bitrate="48k",
            sample_rate="24000",
            channels="stereo",
        )

        book = studio.audio_profile_config_values(profile, "book")
        export = studio.audio_profile_config_values(profile, "export")

        self.assertEqual(book["output_format"], "opus")
        self.assertNotIn("export_format", book)
        self.assertEqual(export["export_format"], "opus")
        self.assertNotIn("output_format", export)

    def test_disabled_profile_effects_produce_clean_book_and_export(self):
        profile = studio.make_audio_profile(
            "Без эффектов",
            effects_enabled=False,
            speed=1.3,
            pitch=1.2,
            echo=True,
        )

        book = studio.audio_profile_config_values(profile, "book")
        export = studio.audio_profile_config_values(profile, "export")

        self.assertEqual(book["fx_speed"], 1.0)
        self.assertEqual(book["fx_pitch"], 1.0)
        self.assertFalse(book["fx_echo"])
        self.assertFalse(export["export_apply_fx"])

    def test_old_config_migrates_shared_format_once(self):
        migrated = studio.normalize_config({"output_format": "opus"})
        separated = studio.normalize_config(
            {"output_format": "mp3", "export_format": "ogg"}
        )

        self.assertEqual(migrated["export_format"], "opus")
        self.assertEqual(separated["output_format"], "mp3")
        self.assertEqual(separated["export_format"], "ogg")

    def test_invalid_custom_profile_is_skipped_during_config_recovery(self):
        config = studio.normalize_config(
            {
                "audio_profiles": [
                    {"name": "broken", "audio": {"sample_rate": "12345"}},
                    studio.make_audio_profile("valid"),
                ]
            }
        )

        self.assertEqual([item["name"] for item in config["audio_profiles"]], ["valid"])

    def test_book_auto_profile_resolves_to_canonical_cache_layout(self):
        profile = studio._select_book_audio_profile(
            "opus",
            sample_rate="auto",
            channels="auto",
            bitrate="48k",
        )

        self.assertEqual(profile["sample_rate"], 48000)
        self.assertEqual(profile["channels"], 1)

    def test_book_profile_rejects_unsupported_explicit_mp3_rate(self):
        with self.assertRaisesRegex(ValueError, "MP3"):
            studio._select_book_audio_profile(
                "mp3",
                sample_rate="96000",
                channels="stereo",
                bitrate="128k",
            )

    def test_profile_rejects_empty_name_and_non_finite_effect(self):
        with self.assertRaisesRegex(ValueError, "имя"):
            studio.normalize_audio_profile({"name": "", "audio": {}})
        with self.assertRaisesRegex(ValueError, "скорость"):
            studio.make_audio_profile("NaN", speed=float("nan"))
        with self.assertRaisesRegex(ValueError, "schema"):
            studio.normalize_audio_profile(
                {"name": "Нет конверта", "audio": {}},
                require_envelope=True,
            )
        envelope_without_audio = {
            "schema": studio.AUDIO_PROFILE_SCHEMA,
            "version": studio.AUDIO_PROFILE_VERSION,
            "name": "Нет audio",
        }
        with self.assertRaisesRegex(ValueError, "audio"):
            studio.normalize_audio_profile(
                envelope_without_audio, require_envelope=True
            )
        invalid_version = studio.make_audio_profile("Версия")
        invalid_version["version"] = True
        with self.assertRaisesRegex(ValueError, "версия"):
            studio.normalize_audio_profile(
                invalid_version, require_envelope=True
            )
        with self.assertRaisesRegex(ValueError, "8–320"):
            studio.make_audio_profile(
                "Слишком большой MP3",
                output_format="mp3",
                bitrate="999999k",
            )

    def test_wav_and_ogg_auto_keep_codec_specific_book_policy(self):
        wav = studio.audio_profile_config_values(
            studio.make_audio_profile("WAV", output_format="wav"),
            "book",
        )
        ogg = studio._select_book_audio_profile(
            "ogg",
            sample_rate="auto",
            channels="auto",
            bitrate="auto",
        )

        self.assertEqual(wav["output_bitrate"], "auto")
        self.assertIsNone(ogg["bitrate"])

    def test_profile_names_are_unique_across_builtin_and_custom(self):
        custom = [studio.make_audio_profile("Личный")]

        self.assertTrue(
            studio.audio_profile_name_conflict("личный", custom)
        )
        self.assertTrue(
            studio.audio_profile_name_conflict("WAV · без потерь", custom)
        )
        self.assertEqual(
            studio.unique_audio_profile_name("Личный", custom),
            "Личный (2)",
        )

    def test_book_profile_preflight_reports_error_before_processing(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "output_format": "mp3",
            "output_bitrate": "128k",
            "output_sample_rate": "96000",
            "output_channels": "stereo",
        }
        app._show_error = mock.Mock()

        self.assertFalse(app._validate_book_output_profile())
        app._show_error.assert_called_once()


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

    def test_corrupt_primary_never_replaces_valid_json_backup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "settings.json"
            backup = path.with_suffix(".json.bak")
            path.write_text("{broken", encoding="utf-8")
            backup.write_text('{"version": 1}', encoding="utf-8")
            app = object.__new__(studio.TTSApp)

            app._write_json_atomic(path, {"version": 2}, backup=True)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {"version": 1})

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

    def test_closing_can_be_cancelled_for_unsaved_glossary(self):
        app = object.__new__(studio.TTSApp)
        app._is_closing = False
        app.is_cache_operation_running = mock.Mock(return_value=False)
        app._import_running = False
        app._export_lock = False
        app._export_running = False
        app._glossary_dirty = True
        app.txt_glossary = mock.Mock()
        app.txt_glossary.edit_modified.return_value = True
        app._ask_yes_no_cancel = mock.Mock(return_value=None)
        app.save_glossary_ui = mock.Mock(return_value=True)
        app.root = mock.Mock()

        app.on_closing()

        self.assertFalse(app._is_closing)
        app.save_glossary_ui.assert_not_called()
        app.root.destroy.assert_not_called()

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
    def test_txt_chapter_regex_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "book.txt"
            source.write_bytes(
                b"\xef\xbb\xbf" + "Глава 1\nТекст главы.".encode("utf-8")
            )

            chapters = studio.BookExtractor.split_txt_by_regex(
                source, r"^Глава \d+"
            )

            self.assertEqual(chapters, [("Глава 1", "Глава 1\nТекст главы.")])

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


class MacSubprocessPolicyTests(unittest.TestCase):
    def test_pydub_converter_uses_the_pre_resolved_ffmpeg_path(self):
        self.assertEqual(studio.AudioSegment.converter, studio.get_ffmpeg_path())

    def test_macos_wrapper_requests_posix_spawn_compatible_options(self):
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
             mock.patch.object(studio.sys, "platform", "darwin"), \
             mock.patch.object(studio, "_ORIGINAL_SUBPROCESS_POPEN") as original:
            studio._patched_popen(
                ["/usr/bin/true"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        kwargs = original.call_args.kwargs
        self.assertIs(kwargs["close_fds"], False)
        self.assertNotIn("preexec_fn", kwargs)

    def test_macos_wrapper_resolves_bare_command_for_posix_spawn(self):
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
             mock.patch.object(studio.sys, "platform", "darwin"), \
             mock.patch.object(studio.shutil, "which", return_value="/usr/bin/pbpaste"), \
             mock.patch.object(studio, "_ORIGINAL_SUBPROCESS_POPEN") as original:
            studio._patched_popen(
                ["pbpaste"], stdout=subprocess.PIPE
            )

        self.assertEqual(original.call_args.args[0][0], "/usr/bin/pbpaste")
        self.assertIs(original.call_args.kwargs["close_fds"], False)

    def test_macos_binary_lookup_falls_back_to_both_homebrew_prefixes(self):
        arm_binary = Path("/opt/homebrew/bin/ffprobe")
        with mock.patch.object(studio.sys, "platform", "darwin"), \
             mock.patch.object(studio.shutil, "which", return_value=None), \
             mock.patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == arm_binary), \
             mock.patch.object(studio.os, "access", return_value=True):
            self.assertEqual(
                studio._resolve_external_binary("ffprobe"), str(arm_binary)
            )

    def test_pydub_prober_uses_resolved_absolute_path(self):
        import pydub.utils

        with mock.patch.object(
            studio, "get_ffprobe_path", return_value="/absolute/ffprobe"
        ):
            self.assertEqual(
                pydub.utils.get_prober_name(), "/absolute/ffprobe"
            )

    def test_macos_wrapper_rejects_preexec_fn(self):
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
             mock.patch.object(studio.sys, "platform", "darwin"):
            with self.assertRaises(ValueError):
                studio._patched_popen(
                    ["/usr/bin/true"], preexec_fn=lambda: None
                )

    def test_pydub_uses_the_platform_popen_policy(self):
        import pydub.audio_segment
        import pydub.utils

        expected_popen = (
            studio._patched_popen
            if studio.platform.system() in {"Windows", "Darwin"}
            else studio._ORIGINAL_SUBPROCESS_POPEN
        )
        self.assertIs(pydub.audio_segment.subprocess.Popen, expected_popen)
        self.assertIs(pydub.utils.Popen, expected_popen)


class AppClickFocusTests(unittest.TestCase):
    def make_app(self, current_focus=None):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.focus_get.return_value = current_focus
        app.root.winfo_exists.return_value = True
        app.root.state.return_value = "normal"
        app._is_closing = False
        return app

    def test_click_restores_missing_local_focus_to_clicked_widget(self):
        app = self.make_app()
        widget = mock.Mock()
        widget.winfo_toplevel.return_value = app.root
        widget.winfo_exists.return_value = True

        app._restore_focus_on_app_click(mock.Mock(widget=widget))
        restore_focus = app.root.after_idle.call_args.args[0]
        restore_focus()

        widget.focus_set.assert_called_once_with()
        app.root.focus_force.assert_not_called()

    def test_click_reasserts_existing_widget_focus_without_redirecting_it(self):
        focused = mock.Mock()
        app = self.make_app(focused)
        focused.winfo_toplevel.return_value = app.root
        focused.winfo_exists.return_value = True
        widget = mock.Mock()
        widget.winfo_toplevel.return_value = app.root

        app._restore_focus_on_app_click(mock.Mock(widget=widget))
        restore_focus = app.root.after_idle.call_args.args[0]
        restore_focus()

        focused.focus_set.assert_called_once_with()
        widget.focus_set.assert_not_called()
        app.root.focus_force.assert_not_called()

    def test_macos_click_refreshes_inactive_native_controls(self):
        app = self.make_app()
        app._schedule_mac_restore_refresh = mock.Mock()
        widget = mock.Mock()
        widget.winfo_toplevel.return_value = app.root
        widget.winfo_exists.return_value = True

        with mock.patch.object(studio.sys, "platform", "darwin"):
            app._restore_focus_on_app_click(mock.Mock(widget=widget))
            app.root.after_idle.call_args.args[0]()

        widget.focus_set.assert_called_once_with()
        app._schedule_mac_restore_refresh.assert_called_once_with()
        app.root.focus_force.assert_not_called()

    def test_macos_active_click_does_not_reload_theme_again(self):
        app = self.make_app()
        app._mac_window_active = True
        app._schedule_mac_restore_refresh = mock.Mock()
        widget = mock.Mock()
        widget.winfo_toplevel.return_value = app.root
        widget.winfo_exists.return_value = True

        with mock.patch.object(studio.sys, "platform", "darwin"):
            app._restore_focus_on_app_click(mock.Mock(widget=widget))
            app.root.after_idle.call_args.args[0]()

        widget.focus_set.assert_called_once_with()
        app._schedule_mac_restore_refresh.assert_not_called()

    def test_click_in_child_toplevel_is_not_redirected_to_root(self):
        app = self.make_app()
        widget = mock.Mock()
        widget.winfo_toplevel.return_value = mock.Mock()

        app._restore_focus_on_app_click(mock.Mock(widget=widget))

        widget.focus_set.assert_not_called()
        app.root.after_idle.assert_not_called()


class MessageboxFocusTests(unittest.TestCase):
    def make_app(self, previous_focus=None):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.focus_get.return_value = previous_focus
        app.root.winfo_exists.return_value = True
        app._is_closing = False
        return app

    def test_dialog_has_root_owner_and_preserves_result(self):
        previous_focus = mock.Mock()
        previous_focus.winfo_exists.return_value = True
        app = self.make_app(previous_focus)
        dialog_function = mock.Mock(return_value=False)

        result = app._run_messagebox(
            dialog_function, "Подтверждение", "Продолжить?", icon="question"
        )

        self.assertFalse(result)
        dialog_function.assert_called_once_with(
            "Подтверждение",
            "Продолжить?",
            icon="question",
            parent=app.root,
        )
        restore_focus = app.root.after_idle.call_args.args[0]
        restore_focus()
        previous_focus.focus_set.assert_called_once_with()
        app.root.focus_force.assert_not_called()

    def test_destroyed_previous_widget_falls_back_to_root(self):
        previous_focus = mock.Mock()
        previous_focus.winfo_exists.return_value = False
        app = self.make_app(previous_focus)

        app._schedule_focus_after_messagebox(previous_focus)
        restore_focus = app.root.after_idle.call_args.args[0]
        restore_focus()

        app.root.focus_set.assert_called_once_with()

    def test_dialog_exception_still_schedules_focus_restore(self):
        app = self.make_app()
        dialog_function = mock.Mock(side_effect=studio.tk.TclError("dialog failed"))

        with self.assertRaises(studio.tk.TclError):
            app._run_messagebox(dialog_function, "Ошибка", "Текст")

        app.root.after_idle.assert_called_once()


class MacAquaRestoreTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = True
        app.root.state.return_value = "normal"
        app.root.after.return_value = "restore-after"
        app._mac_startup_focus_done = True
        app._mac_restore_refresh_after_id = None
        app._is_closing = False
        app._check_system_appearance = mock.Mock()
        return app

    def test_setup_observes_map_and_aqua_activation(self):
        app = self.make_app()
        app.root.bind.side_effect = (
            "initial-map",
            "restore-map",
            "activate",
            "deactivate",
        )

        with mock.patch.object(studio.sys, "platform", "darwin"):
            app._setup_mac_startup_focus()

        sequences = [call.args[0] for call in app.root.bind.call_args_list]
        self.assertEqual(
            sequences, ["<Map>", "<Map>", "<Activate>", "<Deactivate>"]
        )
        self.assertEqual(app._mac_restore_activate_bind_id, "activate")
        self.assertEqual(app._mac_restore_deactivate_bind_id, "deactivate")

    def test_deactivate_marks_native_window_inactive_without_forcing_focus(self):
        app = self.make_app()
        app._mac_window_active = True

        app._on_mac_restore_deactivate(mock.Mock(widget=app.root))

        self.assertFalse(app._mac_window_active)
        app.root.focus_force.assert_not_called()

    def test_startup_activation_also_refreshes_native_progressbars(self):
        app = self.make_app()
        app._mac_startup_focus_done = False
        app._mac_startup_focus_after_id = "startup-after"
        app._mac_startup_map_bind_id = None
        app._force_mac_focus = mock.Mock()
        app._schedule_mac_restore_refresh = mock.Mock()

        app._run_mac_startup_focus()

        app._force_mac_focus.assert_called_once_with()
        app._schedule_mac_restore_refresh.assert_called_once_with()

    def test_map_and_activate_share_one_debounced_refresh(self):
        app = self.make_app()
        event = mock.Mock(widget=app.root)

        app._on_mac_restore_map(event)
        app._on_mac_restore_activate(event)

        app.root.after_cancel.assert_called_once_with("restore-after")
        self.assertEqual(app.root.after.call_count, 2)
        self.assertEqual(
            app.root.after.call_args.args,
            (25, app._refresh_mac_after_restore),
        )

    @mock.patch.object(studio.ttk, "Style")
    def test_restore_reapplies_auto_appearance_and_redraws_all_ttk_widgets(
        self, style_class
    ):
        app = self.make_app()
        style = style_class.return_value
        style.theme_use.side_effect = ("aqua", None)

        app._refresh_mac_after_restore()

        style_class.assert_called_once_with(app.root)
        self.assertEqual(
            style.theme_use.call_args_list,
            [mock.call(), mock.call("aqua")],
        )
        app.root.tk.call.assert_any_call(
            "wm", "attributes", app.root._w, "-appearance", "auto"
        )
        app.root.event_generate.assert_called_with("<Expose>", when="tail")
        app.root.after_idle.assert_called_once_with(app.root.update_idletasks)
        app._check_system_appearance.assert_called_once_with(reschedule=False)

    @mock.patch.object(studio.ttk, "Style")
    def test_appearance_change_redraws_ttk_descendants(self, style_class):
        app = self.make_app()
        style = style_class.return_value
        style.theme_use.side_effect = ("aqua", None)
        app._is_dark_appearance = False
        app._detect_dark_appearance = mock.Mock(return_value=True)
        app._refresh_status_colors = mock.Mock()
        app._check_system_appearance = (
            studio.TTSApp._check_system_appearance.__get__(app, studio.TTSApp)
        )

        app._check_system_appearance(reschedule=False)

        self.assertTrue(app._is_dark_appearance)
        app._refresh_status_colors.assert_called_once_with()
        self.assertEqual(
            style.theme_use.call_args_list,
            [mock.call(), mock.call("aqua")],
        )


class ExportLayoutContractTests(unittest.TestCase):
    def test_export_selection_is_reloaded_after_import_or_build_unlocks(self):
        for method_name in ("_set_export_ui_state", "_set_export_running_state"):
            with self.subTest(method=method_name):
                app = object.__new__(studio.TTSApp)
                app._export_lock = False
                app._export_running = method_name == "_set_export_running_state"
                app.export_mid_frame = mock.Mock()
                app.export_frame = mock.Mock()
                app.group_settings_frame = mock.Mock()
                app.export_tree = mock.Mock()
                app.export_tree.selection.return_value = ("group-1",)
                app.btn_export_start = mock.Mock()
                app.btn_export_stop = mock.Mock()
                app._set_descendant_state = mock.Mock()
                app._sync_export_mode_controls = mock.Mock()
                app.on_export_tree_select = mock.Mock()
                app._disable_export_settings = mock.Mock()

                if method_name == "_set_export_ui_state":
                    app._set_export_ui_state(studio.tk.NORMAL)
                else:
                    app._set_export_running_state(False)

                app.on_export_tree_select.assert_called_once_with(None)
                app._disable_export_settings.assert_not_called()

    def test_locked_export_tree_ignores_macos_selection_fallbacks(self):
        app = object.__new__(studio.TTSApp)
        app._export_lock = True
        app._export_running = False
        app.export_tree = mock.Mock()
        event = mock.Mock(widget=app.export_tree, x=10, y=10)

        self.assertEqual(
            app._mac_multiselect(event, app.export_tree),
            "break",
        )
        self.assertEqual(
            app._mac_tree_button_press(event, app.export_tree),
            "break",
        )
        self.assertEqual(app._tree_select_all(event), "break")
        app._mac_ensure_tree_plain_click(app.export_tree, "file-1")

        app.export_tree.focus_set.assert_not_called()
        app.export_tree.selection_set.assert_not_called()
        app.export_tree.after_idle.assert_not_called()

    def test_export_selection_update_flag_is_released_on_widget_error(self):
        app = object.__new__(studio.TTSApp)
        app._export_lock = False
        app._export_running = False
        app.export_tree = mock.Mock()
        app.export_tree.selection.return_value = ("file-1",)
        app.export_groups = {}
        app.export_files = {"file-1": {"title": "Глава"}}
        app.grp_name_var = mock.Mock()
        app.grp_name_var.set.side_effect = RuntimeError("widget destroyed")
        app._enable_export_settings = mock.Mock()
        app._disable_export_settings = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "widget destroyed"):
            app.on_export_tree_select(None)

        self.assertFalse(app._is_updating_ui)

    def test_basic_settings_use_compact_ttk_grid_without_plain_canvas(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_utils_tab(self):")
        end = source.index("    def _sync_export_mode_controls(self):", start)
        setup_source = source[start:end]
        basic_start = setup_source.index("self.grp_tab_basic = ttk.Frame(")
        tags_start = setup_source.index("# -- Вкладка: Теги --")
        basic_block = setup_source[basic_start:tags_start]

        self.assertNotIn("tk.Canvas", basic_block)
        self.assertNotIn("Scrollbar", basic_block)
        self.assertIn("self.grp_basic_content.columnconfigure(1, weight=1)", basic_block)
        self.assertIn("self.lbl_grp_name.grid(row=0", basic_block)
        self.assertIn("flags_row = ttk.Frame(self.grp_basic_content)", basic_block)
        self.assertIn("self.chk_merge.pack(side=tk.LEFT)", basic_block)
        self.assertIn(
            "self.chk_subfolder.pack(side=tk.LEFT, padx=(6, 0))", basic_block
        )
        self.assertIn("self.btn_mass_apply_basic.grid(", basic_block)

    def test_format_stays_compact_and_actions_use_separate_row(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_utils_tab(self):")
        end = source.index("    def _sync_export_mode_controls(self):", start)
        setup_source = source[start:end]

        format_start = setup_source.index("self.cb_export_fmt = ttk.Combobox(")
        format_end = setup_source.index(
            "self.cb_export_fmt.pack(side=tk.LEFT)", format_start
        )
        format_block = setup_source[format_start:format_end]
        self.assertIn('values=["mp3", "wav", "ogg", "opus"]', format_block)
        self.assertIn("width=5", format_block)

        effects_row = setup_source.index("row3 = ttk.Frame(export_frame)")
        summary_row = setup_source.index(
            "profile_summary_row = ttk.Frame(export_frame)"
        )
        actions_row = setup_source.index("row4 = ttk.Frame(export_frame)")
        middle_panel = setup_source.index("self.export_mid_frame = ttk.Frame(frame)")
        profile_button = setup_source.index(
            "self.btn_audio_profiles_export = ttk.Button("
        )
        effects_checkbox = setup_source.index(
            "self.chk_export_fx = ttk.Checkbutton("
        )
        self.assertLess(profile_button, effects_row)
        self.assertGreater(effects_checkbox, effects_row)
        self.assertLess(effects_checkbox, summary_row)
        self.assertLess(effects_row, summary_row)
        self.assertLess(summary_row, actions_row)
        self.assertLess(actions_row, middle_panel)

        actions_block = setup_source[actions_row:middle_panel]
        tags_var = setup_source.index("self.export_tags_only_var = tk.BooleanVar")
        tags_widget = setup_source.index(
            "self.chk_export_tags_only = ttk.Checkbutton("
        )
        self.assertLess(tags_var, effects_row)
        self.assertGreater(tags_widget, actions_row)
        self.assertIn(
            'text="Только обновить теги в исходных файлах"',
            actions_block,
        )
        self.assertNotIn(
            "Только обновить теги (в исходных файлах)",
            setup_source,
        )
        self.assertIn(
            'values=["auto", "32k", "48k", "64k", "96k", "128k", "192k", "256k", "320k"]',
            setup_source,
        )
        self.assertIn("export_actions = ttk.Frame(row4)", actions_block)
        self.assertIn("export_actions.pack(side=tk.RIGHT)", actions_block)
        self.assertIn(
            "self.btn_export_start = ttk.Button(\n            export_actions,",
            actions_block,
        )
        self.assertIn(
            "self.btn_export_stop = ttk.Button(\n            export_actions,",
            actions_block,
        )
        self.assertNotIn("self.btn_audio_profiles_export", actions_block)
        summary_block = setup_source[summary_row:actions_row]
        self.assertIn("self.lbl_export_audio_profile_summary", summary_block)
        self.assertNotIn("self.btn_export_start", summary_block)
        self.assertIn('profile_summary_row.bind("<Configure>"', summary_block)
        self.assertIn("wraplength=max(320, event.width - 10)", summary_block)

        self.assertIn("self.root.minsize(", source)
        status_start = setup_source.index("self.lbl_export_status = ttk.Label(")
        status_end = setup_source.index(
            "self._status_label_kinds[self.lbl_export_status]", status_start
        )
        status_block = setup_source[status_start:status_end]
        self.assertNotIn("width=", status_block)
        self.assertIn(
            "self.export_progress.pack(side=tk.RIGHT, fill=tk.X, expand=True",
            setup_source,
        )

    def test_normalizer_actions_are_reserved_below_the_editors(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_normalizer_tab(self):")
        end = source.index("    def _open_normalizer_tab_from_settings", start)
        setup_source = source[start:end]

        controls_pack = setup_source.index(
            "preview_controls.pack(side=tk.BOTTOM, fill=tk.X"
        )
        editor_pack = setup_source.index(
            "editor_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)"
        )
        self.assertLess(controls_pack, editor_pack)
        self.assertIn('text="💾 Сохранить TXT…"', setup_source)
        self.assertIn("self.lbl_normalizer_scope_status", setup_source)
        self.assertIn("ttk.Frame(main_pane, width=440)", setup_source)
        self.assertIn("main_pane.add(text_pane, weight=2)", setup_source)
        self.assertIn("main_pane.add(settings_pane, weight=3)", setup_source)
        self.assertEqual(setup_source.count("\n            width=44,\n"), 2)
        self.assertIn("self.normalizer_font_combobox", setup_source)
        self.assertIn("textvariable=self.font_size_var", setup_source)
        self.assertIn("self.root.after(10, self.update_fonts)", setup_source)

    def test_settings_explain_global_normalizer_and_profile_copy_semantics(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_settings_tab(self):")
        end = source.index("    # --- Вкладка \"Глоссарий\" ---", start)
        settings_source = source[start:end]

        self.assertIn('text="Нормализация"', settings_source)
        self.assertIn("Сохранено глобально", settings_source)
        self.assertIn("Применить глобально", settings_source)
        self.assertIn("Профиль копирует параметры", settings_source)
        self.assertIn("связанным с полями", settings_source)
        self.assertIn("У вкладки «Экспорт и сборка» свои эффекты", settings_source)

    def test_output_settings_offer_32k_bitrate(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("        # 7. Вывод и Теги")
        end = source.index("    # --- Вкладка \"Глоссарий\" ---", start)
        output_settings = source[start:end]
        self.assertIn(
            '["auto", "32k", "48k", "64k", "96k", "128k", "192k", "256k", "320k"]',
            output_settings,
        )

    def test_basic_group_settings_stay_compact_without_canvas_artifacts(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_utils_tab(self):")
        end = source.index("    def _sync_export_mode_controls(self):", start)
        setup_source = source[start:end]

        basic_start = setup_source.index("self.grp_basic_content = ttk.Frame(")
        tags_start = setup_source.index("# -- Вкладка: Теги --")
        basic_block = setup_source[basic_start:tags_start]
        self.assertNotIn("Canvas", basic_block)
        self.assertNotIn("Scrollbar", basic_block)
        self.assertIn("self.grp_basic_content.pack(fill=tk.BOTH, expand=True)", basic_block)
        self.assertIn("flags_row = ttk.Frame(self.grp_basic_content)", basic_block)
        self.assertIn(
            "row=1, column=0, columnspan=4, sticky=tk.W", basic_block
        )
        self.assertIn("self.chk_merge.pack(side=tk.LEFT)", basic_block)
        self.assertIn(
            "self.chk_subfolder.pack(side=tk.LEFT, padx=(6, 0))", basic_block
        )
        self.assertIn(
            "self.btn_mass_apply_basic = ttk.Button(self.grp_basic_content,",
            basic_block,
        )
        self.assertIn(
            "row=4, column=0, columnspan=4, sticky=tk.W", basic_block
        )

        disable_start = source.index("    def _disable_export_settings(self):")
        disable_end = source.index("    def on_export_tree_select(self, event):")
        state_block = source[disable_start:disable_end]
        self.assertGreaterEqual(
            state_block.count(
                "for tab in (self.grp_tab_basic, self.grp_tab_tags):"
            ),
            2,
        )

    def test_export_and_glossary_bulk_actions_are_visible_and_transactional(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        export_start = source.index("    def setup_utils_tab(self):")
        export_end = source.index(
            "    def _sync_export_mode_controls(self):", export_start
        )
        export_setup = source[export_start:export_end]
        self.assertIn('text="🔗 Объединить"', export_setup)
        self.assertIn('text="🧹 Очистить всё"', export_setup)
        self.assertIn("command=self.merge_selected_export_items", export_setup)
        self.assertIn("command=self.clear_export_project", export_setup)

        glossary_start = source.index("    def setup_glossary_tab(self):")
        glossary_end = source.index("    def toggle_glos_fields(self):", glossary_start)
        glossary_setup = source[glossary_start:glossary_end]
        self.assertIn('text="🗑 Удалить правила…"', glossary_setup)
        self.assertIn("command=self.open_glossary_delete_dialog", glossary_setup)

        manager_start = source.index("    def open_glossary_delete_dialog(self):")
        manager_end = source.index(
            '    # --- Вкладка "Нормализатор"', manager_start
        )
        manager = source[manager_start:manager_end]
        self.assertIn("notebook = ttk.Notebook(dialog)", manager)
        self.assertIn('search_var = tk.StringVar()', manager)
        self.assertIn('text="Выбрать результаты поиска"', manager)
        self.assertIn('text="Выбрать все в разделе"', manager)
        self.assertIn('text="Снять всё в разделе"', manager)
        self.assertIn("показано в разделе", manager)
        self.assertIn('"<<NotebookTabChanged>>"', manager)
        self.assertIn('text="🔥 Очистить весь глоссарий…"', manager)
        self.assertIn("self._replace_glossary_editor_data(updated)", manager)

    def test_export_activity_status_is_compact_and_keeps_both_name_ends(self):
        short = studio.format_export_activity_status(
            "Экспорт", "Глава 01.opus"
        )
        self.assertEqual(short, "Экспорт: Глава 01.opus")

        long_name = "Очень длинное начало " + "фрагмент " * 20 + "том 99.opus"
        compact = studio.format_export_activity_status(
            "Экспорт", long_name, max_subject_chars=40
        )
        subject = compact.removeprefix("Экспорт: ")
        self.assertEqual(len(subject), 40)
        self.assertTrue(subject.startswith("Очень длинное начало"))
        self.assertTrue(subject.endswith("том 99.opus"))
        self.assertIn("…", subject)

        normalized = studio.middle_ellipsize("  Глава\n\t01\x00.opus  ", 40)
        self.assertEqual(normalized, "Глава 01.opus")

        wide_measure = lambda value: sum(
            2 if ord(character) > 127 else 1 for character in value
        )
        fitted = studio.middle_ellipsize_to_width(
            "Начало очень длинного имени 章节 99.opus",
            24,
            wide_measure,
        )
        self.assertLessEqual(wide_measure(fitted), 24)
        self.assertTrue(fitted.startswith("Начало"))
        self.assertTrue(fitted.endswith("99.opus"))

    def test_export_activity_restores_full_name_after_resize(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.lbl_export_status = mock.Mock()
        app.lbl_export_status.master.winfo_width.return_value = 360
        app.lbl_export_status.cget.return_value = "TkDefaultFont"
        app._set_status_label = mock.Mock()
        fake_font = mock.Mock()
        fake_font.measure.side_effect = lambda value: len(value) * 10
        full_name = "Начало " + "очень-длинное-имя-" * 8 + "том-99.opus"

        with mock.patch.object(
            studio.tkfont, "nametofont", return_value=fake_font
        ):
            app._set_export_activity_status("Экспорт", full_name)
            compact = app._set_status_label.call_args.args[1]
            self.assertIn("…", compact)

            app.lbl_export_status.master.winfo_width.return_value = 4000
            app._render_export_activity_status()
            expanded = app._set_status_label.call_args.args[1]

        self.assertEqual(expanded, f"Экспорт: {full_name}")
        app._set_export_status("Готово!", "success")
        self.assertIsNone(app._export_status_activity)

        wide = studio.middle_ellipsize_to_width("Глава 01.opus", 50, len)
        self.assertEqual(wide, "Глава 01.opus")
        fitted = studio.middle_ellipsize_to_width(
            "Очень длинное начало и окончание.opus", 18, len
        )
        self.assertLessEqual(len(fitted), 18)
        self.assertIn("…", fitted)
        self.assertTrue(fitted.startswith("Очень"))
        self.assertTrue(fitted.endswith("opus"))

    def test_export_worker_uses_short_activity_labels(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def start_export_process(self):")
        end = source.index("    def add_separator_row", start)
        worker = source[start:end]

        self.assertIn(
            'self._post_export_activity_status(\n                                    "Склейка"',
            worker,
        )
        self.assertIn(
            'self._post_export_activity_status(\n                                        "Экспорт"',
            worker,
        )
        self.assertIn('f_set["title"]', worker)
        self.assertNotIn("Потоковая склейка, эффекты и сохранение", worker)
        self.assertNotIn("Конвертация:", worker)

    def test_stopped_export_resets_status_and_progress_after_worker_finishes(self):
        app = studio.TTSApp.__new__(studio.TTSApp)
        app._export_thread = mock.Mock()
        app._set_export_running_state = mock.Mock()
        app.export_progress = mock.Mock()
        app.lbl_export_status = mock.Mock()
        app._set_status_label = mock.Mock()
        app.is_export_stopped = True

        app._finish_export_process_ui("stopped")

        self.assertIsNone(app._export_thread)
        app._set_export_running_state.assert_called_once_with(False)
        app.export_progress.configure.assert_called_once_with(value=0)
        app._set_status_label.assert_called_once_with(
            app.lbl_export_status,
            "Ожидание...",
            "info",
        )
        self.assertFalse(app.is_export_stopped)

    def test_completed_or_failed_export_keeps_diagnostic_ui(self):
        for outcome in ("success", "warning", "error"):
            with self.subTest(outcome=outcome):
                app = studio.TTSApp.__new__(studio.TTSApp)
                app._export_thread = mock.Mock()
                app._set_export_running_state = mock.Mock()
                app.export_progress = mock.Mock()
                app.lbl_export_status = mock.Mock()
                app._set_status_label = mock.Mock()
                app.is_export_stopped = False

                app._finish_export_process_ui(outcome)

                app.export_progress.configure.assert_not_called()
                app._set_status_label.assert_not_called()
                self.assertFalse(app.is_export_stopped)


class BuildWorkflowContractTests(unittest.TestCase):
    def test_release_python_and_macos_tk_are_pinned_and_verified(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('PYTHON_VERSION: "3.13.15"', workflow)
        self.assertIn('MACOS_PYTHON_SERIES: "3.13"', workflow)
        self.assertIn('MACOS_TK_SERIES: "9.0"', workflow)
        self.assertIn(
            'brew install --skip-link "python@$python_series"', workflow
        )
        self.assertIn(
            'brew install --skip-link "python-tk@$python_series"', workflow
        )
        self.assertIn('HOMEBREW_NO_PATH_SHADOW_CHECK: "1"', workflow)
        self.assertIn("brew update", workflow)
        self.assertNotIn(
            'brew upgrade "python@$python_series" "python-tk@$python_series"',
            workflow,
        )
        self.assertNotIn("brew link --overwrite", workflow)
        self.assertNotIn("python-tk@3.13 --overwrite", workflow)
        self.assertIn('MACOS_PYTHON_VERSION=$installed_python_version', workflow)
        self.assertIn('"$py" -m venv .venv', workflow)
        self.assertIn("actual_python[:2] != expected_series", workflow)
        self.assertIn("actual_arch != expected_arch", workflow)
        self.assertIn('EXPECTED_TARGET_ARCH: ${{ matrix.target_arch }}', workflow)
        self.assertIn("tkinter.TclVersion != 9.0", workflow)
        self.assertIn("tkinter.TkVersion != 9.0", workflow)

        runtime_start = workflow.index("- name: Verify Python runtime dependencies")
        ffmpeg_start = workflow.index("- name: Set up FFmpeg")
        runtime_block = workflow[runtime_start:ffmpeg_start]
        self.assertIn("import tkinter", runtime_block)
        self.assertIn("sys.version_info[:2] == expected_series", runtime_block)
        self.assertIn(
            'platform.python_version() == os.environ["MACOS_PYTHON_VERSION"]',
            runtime_block,
        )
        self.assertIn("assert tkinter.TclVersion == 9.0", runtime_block)
        self.assertIn("assert tkinter.TkVersion == 9.0", runtime_block)

        verify_start = workflow.index("- name: Verify macOS bundle contents")
        upload_start = workflow.index("- name: Upload full build artifact")
        verify_block = workflow[verify_start:upload_start]
        self.assertIn("Python.framework", verify_block)
        self.assertIn("CFBundleVersion", verify_block)
        self.assertIn('"$MACOS_PYTHON_VERSION"', verify_block)
        self.assertIn("_tkinter*.so", verify_block)
        self.assertIn("libtcl9.0.dylib", verify_block)
        self.assertIn("libtcl9tk9.0.dylib", verify_block)
        self.assertIn("otool -L \"$media_binary\"", verify_block)
        self.assertIn("Homebrew paths", verify_block)
        self.assertIn(
            'for media_binary in "$ffmpeg_path" "$ffprobe_path"; do',
            verify_block,
        )
        self.assertGreaterEqual(verify_block.count("lipo"), 4)

    def test_help_and_readme_describe_audio_and_macos_contracts(self):
        readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
        source = MODULE_PATH.read_text(encoding="utf-8")
        help_start = source.index('help_text = r"""')
        help_end = source.index(
            '        help_text = help_text.replace', help_start
        )
        help_text = source[help_start:help_end]

        for document in (readme, help_text):
            self.assertIn("Opus → PCM", document)
            self.assertIn("Python 3.13.x", document)
            self.assertIn("Python 3.13.15", document)
            self.assertIn("Tk 9", document)
            self.assertIn("FFprobe", document)

        self.assertIn("первую страницу контейнера", readme)
        for document in (readme, help_text):
            self.assertIn("Ogg/Opus (.opus)", document)
            self.assertIn("Ogg/Vorbis (.ogg)", document)
            self.assertIn("128 кбит/с", document)
            self.assertIn("32 кбит/с", document)
            self.assertIn("METADATA_BLOCK_PICTURE", document)
        self.assertIn("Промежуточные WAV и Vorbis", help_text)

    def test_release_matrix_keeps_required_portable_artifacts(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        matrix_start = workflow.index("      matrix:\n        include:\n")
        matrix_end = workflow.index("\n    steps:", matrix_start)
        matrix_block = workflow[matrix_start:matrix_end]
        entries = []
        for chunk in re.split(r"(?m)^          - name: ", matrix_block)[1:]:
            lines = chunk.splitlines()
            entry = {"name": lines[0].strip()}
            for line in lines[1:]:
                match = re.match(r"^            ([a-z_]+): (.+)$", line)
                if match:
                    entry[match.group(1)] = match.group(2).strip('"')
            entries.append(entry)

        expected_entries = [
            {
                "name": "Windows",
                "os": "windows-latest",
                "target_arch": "x86_64",
                "artifact_name": "SileroTTS_Studio_Windows.zip",
                "portable_artifact_name": (
                    "SileroTTS_Studio_Windows_Portable.exe"
                ),
            },
            {
                "name": "Linux x86_64",
                "os": "ubuntu-latest",
                "target_arch": "x86_64",
                "artifact_name": "SileroTTS_Studio_Linux_x86_64.zip",
                "portable_artifact_name": (
                    "SileroTTS_Studio_Linux_x86_64_Portable.zip"
                ),
            },
            {
                "name": "Linux ARM64",
                "os": "ubuntu-24.04-arm",
                "target_arch": "arm64",
                "artifact_name": "SileroTTS_Studio_Linux_arm64.zip",
                "portable_artifact_name": (
                    "SileroTTS_Studio_Linux_arm64_Portable.zip"
                ),
            },
            {
                "name": "macOS Apple Silicon ARM64",
                "os": "macos-15",
                "target_arch": "arm64",
                "artifact_name": (
                    "SileroTTS_Studio_macOS_AppleSilicon_ARM64.zip"
                ),
            },
            {
                "name": "macOS Intel x86_64",
                "os": "macos-15-intel",
                "target_arch": "x86_64",
                "artifact_name": "SileroTTS_Studio_macOS_Intel_x86_64.zip",
            },
        ]
        keys = {
            "name",
            "os",
            "target_arch",
            "artifact_name",
            "portable_artifact_name",
        }
        self.assertEqual(
            [{key: entry[key] for key in keys if key in entry} for entry in entries],
            expected_entries,
        )

        matrix_assets = {
            entry[key]
            for entry in entries
            for key in ("artifact_name", "portable_artifact_name")
            if key in entry
        }
        self.assertEqual(len(matrix_assets), 8)

        release_block = workflow[workflow.index("  release:"):]
        expected_match = re.search(
            r"expected=\(\n(?P<body>.*?)\n\s+\)", release_block, re.DOTALL
        )
        self.assertIsNotNone(expected_match)
        expected_assets = re.findall(
            r"(?m)^\s+(SileroTTS_Studio_\S+)$",
            expected_match.group("body"),
        )
        published_assets = re.findall(
            r"(?m)^\s+release-assets/(SileroTTS_Studio_\S+)$",
            release_block,
        )
        self.assertEqual(len(expected_assets), 8)
        self.assertEqual(len(set(expected_assets)), 8)
        self.assertEqual(set(expected_assets), matrix_assets)
        self.assertEqual(len(published_assets), 8)
        self.assertEqual(len(set(published_assets)), 8)
        self.assertEqual(set(published_assets), matrix_assets)

        self.assertIn("--onefile", workflow)
        self.assertIn("Upload portable build artifact", workflow)
        self.assertNotIn("macOS_arm64_Portable", workflow)
        self.assertEqual(
            workflow.count("name: ${{ matrix.artifact_name }}-artifact"), 1
        )
        self.assertEqual(
            workflow.count(
                "name: ${{ matrix.portable_artifact_name }}-artifact"
            ),
            1,
        )
        self.assertIn('pattern: "*-artifact"', release_block)

    def test_release_is_published_once_only_after_complete_integrity_check(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(workflow.count("softprops/action-gh-release@v2"), 1)
        self.assertIn("name: Verify and publish complete release", workflow)
        self.assertIn("needs: build", workflow)
        self.assertIn(
            "if: success() && startsWith(github.ref, 'refs/tags/')",
            workflow,
        )
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("merge-multiple: true", workflow)
        self.assertIn('test -s "$artifact"', workflow)
        self.assertIn("zipped.testzip()", workflow)
        self.assertIn('unzip -tqq "release-assets/$filename"', workflow)
        self.assertIn("sha256sum", workflow)
        self.assertIn("release-assets/SHA256SUMS.txt", workflow)
        self.assertIn("fail_on_unmatched_files: true", workflow)
        verify_build = workflow.index("- name: Verify packaged artifacts")
        upload_build = workflow.index("- name: Upload full build artifact")
        verify_release = workflow.index(
            "- name: Verify complete release set and write SHA256SUMS"
        )
        publish_release = workflow.index(
            "- name: Publish release only after every platform passed"
        )
        self.assertLess(verify_build, upload_build)
        self.assertLess(verify_release, publish_release)

    def test_release_ffmpeg_is_checked_for_opus_support(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Verify required FFmpeg codecs", workflow)
        self.assertIn("libopus", workflow)
        self.assertIn("libvorbis", workflow)

    def test_macos_bundle_version_is_set_before_codesigning(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "1.5.0"', source)
        self.assertNotIn("APP_VERSION_FALLBACK", workflow)
        self.assertNotIn("1.4.1", workflow)
        self.assertIn('source_version="$(sed -nE', workflow)
        self.assertIn('version="$source_version"', workflow)
        self.assertIn(
            'IFS=. read -r version_major version_minor version_patch', workflow
        )
        self.assertIn('expected_tag="v$public_version"', workflow)
        self.assertIn(
            '"$GITHUB_REF_NAME" != "$expected_tag"', workflow
        )
        self.assertNotIn(
            '"$GITHUB_REF_NAME" != "v$source_version"', workflow
        )
        self.assertNotIn('version="${GITHUB_REF_NAME#v}"', workflow)
        self.assertNotIn('version="${version%%-*}"', workflow)

        build_start = workflow.index("- name: Build macOS app")
        verify_start = workflow.index("- name: Verify macOS bundle contents")
        upload_start = workflow.index("- name: Upload full build artifact")
        build_block = workflow[build_start:verify_start]
        verify_block = workflow[verify_start:upload_start]
        signing_position = build_block.index("codesign --force --deep --sign -")

        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            self.assertLess(
                build_block.index(f"plutil -replace {key}"),
                signing_position,
            )
            self.assertIn(f"plutil -extract {key}", verify_block)
            self.assertNotIn(f"plutil -replace {key}", verify_block)
            self.assertNotIn(f"plutil -insert {key}", verify_block)

    def test_windows_binaries_embed_and_verify_release_version(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        resource_start = workflow.index("- name: Create Windows version resources")
        linux_start = workflow.index("- name: Build Linux apps")
        windows_block = workflow[resource_start:linux_start]

        self.assertIn("windows-full-version.txt", windows_block)
        self.assertIn("windows-portable-version.txt", windows_block)
        self.assertEqual(windows_block.count('--version-file "windows-'), 2)
        self.assertEqual(windows_block.count("filevers=($versionTuple)"), 1)
        self.assertEqual(windows_block.count("prodvers=($versionTuple)"), 1)
        self.assertIn("StringStruct('FileVersion', '$env:APP_VERSION')", windows_block)
        self.assertIn("StringStruct('ProductVersion', '$env:APP_VERSION')", windows_block)
        self.assertIn("StringStruct('OriginalFilename', '$OriginalFilename')", windows_block)
        self.assertIn("- name: Verify Windows executable versions", windows_block)
        self.assertIn("$info.FileMajorPart", windows_block)
        self.assertIn("$info.ProductVersion", windows_block)


class AtomicOutputTests(unittest.TestCase):
    def test_codec_detector_reads_only_the_ogg_header(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "cached.ogg"
            source.write_bytes(fake_ogg_first_page(b"OpusHead") + b"x" * 10000)

            wrapped_file = mock.MagicMock()
            wrapped_file.__enter__.return_value = wrapped_file
            wrapped_file.read.return_value = fake_ogg_first_page(b"OpusHead")
            wrapped_file.__exit__.return_value = False

            with mock.patch("builtins.open", return_value=wrapped_file) as opened:
                codec = studio._detect_ogg_audio_codec(source)

            self.assertEqual(codec, "opus")
            opened.assert_called_once_with(source, "rb")
            wrapped_file.read.assert_called_once_with(4096)

    def test_codec_detector_rejects_marker_outside_identification_packet(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "misleading.ogg"
            source.write_bytes(
                fake_ogg_first_page(b"not-a-codec", trailing=b"OpusHead")
            )

            self.assertIsNone(studio._detect_ogg_audio_codec(source))

    def test_codec_detector_rejects_truncated_or_invalid_ogg_page(self):
        invalid_headers = (
            b"not-ogg OpusHead",
            b"OggS\x01" + b"\x00" * 40 + b"OpusHead",
            b"OggS\x00\x02" + b"\x00" * 20 + b"\x01\xffOpusHead",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "broken.ogg"
            for header in invalid_headers:
                with self.subTest(header=header[:8]):
                    source.write_bytes(header)
                    self.assertIsNone(studio._detect_ogg_audio_codec(source))

    def test_physical_opus_check_rejects_stale_vorbis_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "cached.ogg"
            source.write_bytes(fake_ogg_first_page(b"\x01vorbis"))

            with self.assertRaisesRegex(ValueError, "Ogg/Opus"):
                studio._require_opus_audio_file(source)

    def test_known_ogg_codec_decodes_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "cached.ogg"
            source.write_bytes(fake_ogg_first_page(b"OpusHead"))
            sentinel = object()

            with mock.patch.object(
                studio.AudioSegment,
                "from_file",
                return_value=sentinel,
            ) as from_file:
                decoded = studio._load_audio_segment(source)

            self.assertIs(decoded, sentinel)
            from_file.assert_called_once_with(
                source, format="ogg", codec="opus"
            )

    def test_known_vorbis_codec_decodes_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "legacy.ogg"
            source.write_bytes(fake_ogg_first_page(b"\x01vorbis"))
            sentinel = object()

            with mock.patch.object(
                studio.AudioSegment,
                "from_file",
                return_value=sentinel,
            ) as from_file:
                decoded = studio._load_audio_segment(source)

            self.assertIs(decoded, sentinel)
            from_file.assert_called_once_with(
                source, format="ogg", codec="vorbis"
            )

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
    def test_missing_saved_path_recovers_without_discarding_other_settings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            blocked_parent = root / "not_a_directory"
            blocked_parent.write_text("file", encoding="utf-8")
            path = root / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "input_dir": str(blocked_parent / "texts"),
                        "speaker": "saved_voice",
                    }
                ),
                encoding="utf-8",
            )

            app = object.__new__(studio.TTSApp)
            config = app.load_settings(path)

            self.assertEqual(config["input_dir"], studio.DEFAULT_INPUT_DIR)
            self.assertEqual(config["speaker"], "saved_voice")

    def test_save_validates_fresh_ui_path_before_writing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            blocked_parent = root / "not_a_directory"
            blocked_parent.write_text("file", encoding="utf-8")
            settings_path = root / "settings.json"

            app = object.__new__(studio.TTSApp)
            app.config = studio.DEFAULT_CONFIG.copy()
            app.settings_vars = {
                "input_dir": mock.Mock(
                    get=mock.Mock(
                        return_value=str(blocked_parent / "fresh-ui-value")
                    ),
                    set=mock.Mock(),
                )
            }
            app.shared_rate_limiter = mock.Mock()

            self.assertTrue(app.save_settings(settings_path))

            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["input_dir"], studio.DEFAULT_INPUT_DIR)
            app.settings_vars["input_dir"].set.assert_called_with(
                studio.DEFAULT_INPUT_DIR
            )

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
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"speaker": "backup_voice"},
            )

    def test_save_settings_returns_false_when_target_parent_is_not_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            blocked_parent = Path(tempdir) / "not-a-directory"
            blocked_parent.write_text("file", encoding="utf-8")
            app = object.__new__(studio.TTSApp)
            app.config = {}
            app.settings_vars = {}
            app.shared_rate_limiter = mock.Mock()
            app.update_config_from_ui = mock.Mock()
            app.ensure_dirs = mock.Mock()
            app._show_error = mock.Mock()

            saved = app.save_settings(
                blocked_parent / "settings.json",
                show_popup=True,
            )

            self.assertFalse(saved)
            app._show_error.assert_called_once()


class SettingsTransactionContractTests(unittest.TestCase):
    def test_config_import_and_reset_persist_before_replacing_live_config(self):
        source = MODULE_PATH.read_text(encoding="utf-8")

        import_start = source.index("    def import_config(self):")
        import_end = source.index("    def reset_config(self):", import_start)
        import_block = source[import_start:import_end]
        self.assertIn("ensure_config_directories(candidate_config)", import_block)
        self.assertLess(
            import_block.index("self._persist_settings_snapshot(candidate_config)"),
            import_block.index("self.config = candidate_config"),
        )

        reset_start = import_end
        reset_end = source.index('# --- Вкладка "Синтез из папки" ---', reset_start)
        reset_block = source[reset_start:reset_end]
        self.assertLess(
            reset_block.index("self._persist_settings_snapshot(candidate_config)"),
            reset_block.index("self.config = candidate_config"),
        )


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

    def test_hyphen_before_ordinal_at_line_start_is_dialogue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(processor)
            speech_texts = []

            pauses = self.collect_silence_durations(
                processor,
                "Авторский текст.\n- 62-й ранг.",
                speech_texts=speech_texts,
            )

            self.assertEqual(pauses, [700])
            self.assertEqual(speech_texts[-1], "шестьдесят второй ранг.")

    def test_standalone_unsupported_chunk_is_removed_after_sentence_split(self):
        raw_text = (
            "Светлые волосы в мгновение ока сменились на белые и чёрные, "
            "белых было больше, но чёрные локоны были видны особенно чётко. "
            "На его лбу появились четыре линии, три горизонтальные и одна "
            "вертикальная, как раз, чтобы образовывать иероглиф \"король\". "
            "(王)"
        )
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            speech_texts = []

            with self.assertLogs(level=logging.INFO) as captured:
                self.collect_silence_durations(
                    processor, raw_text, speech_texts=speech_texts
                )

        self.assertEqual(len(speech_texts), 2)
        self.assertTrue(all("王" not in text for text in speech_texts))
        self.assertTrue(any("иероглиф король" in text for text in speech_texts))
        log_text = "\n".join(captured.output)
        self.assertIn("source='(王)'", log_text)
        self.assertIn("normalized='王.'", log_text)

    def test_standalone_unsupported_chunk_is_skipped_but_mixed_text_is_kept(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            speech_texts = []

            self.collect_silence_durations(
                processor,
                'Слово «король».\n(王)\nВнутри фразы 王 остаётся.',
                speech_texts=speech_texts,
            )

            self.assertEqual(
                speech_texts,
                ["Слово король.", "Внутри фразы 王 остаётся."],
            )
            self.assertNotIn("王.", speech_texts)

    def test_hash_scan_skips_same_unsupported_chunk_as_synthesis(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            raw_text = (
                'Слово «король».\n(王)\n'
                'Внутри фразы 王 остаётся.'
            )

            hashes = processor.get_all_possible_hashes(raw_text)
            unsupported_hash = studio.cache_content_hash(
                "王.", processor.cfg["speaker"]
            )

            self.assertNotIn(unsupported_hash, hashes)
            self.assertIn(
                studio.cache_content_hash(
                    "Внутри фразы 王 остаётся.",
                    processor.cfg["speaker"],
                ),
                hashes,
            )

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

    def test_regex_generated_separator_becomes_pause_not_empty_fragment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            processor.separators = ["***"]
            processor.cfg["separator_symbols"] = "***"
            processor.cfg["pause_separator"] = 1234
            processor.glossary_regex = [
                {
                    "pattern": r"^(Глава\s+\d+\.)\s*(.+)$",
                    "repl": r"\1\n- \2\n***",
                }
            ]
            speech_texts = []

            with mock.patch.object(studio.logging, "info") as app_info:
                pauses = self.collect_silence_durations(
                    processor,
                    "Глава 1. Заголовок",
                    speech_texts=speech_texts,
                )

            self.assertFalse(
                any(
                    call.args
                    and "Пропущен самостоятельный фрагмент" in str(call.args[0])
                    for call in app_info.call_args_list
                )
            )
            # Первая пауза относится к оформленной RegEx реплике, вторая —
            # к самому разделителю. Важно, что ``***`` не был отброшен как
            # неподдерживаемый текст и сохранил настроенную длительность.
            self.assertEqual(pauses, [processor.cfg["pause_speech"], 1234])
            self.assertEqual(speech_texts, ["Глава первая.", "Заголовок."])
            self.assertEqual(
                processor._prepare_raw_text(
                    "Глава 1. Заголовок", "___SEPARATOR_TOKEN___"
                ),
                "Глава 1.\n- Заголовок\n___SEPARATOR_TOKEN___",
            )

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


class InplaceTagCoverTests(unittest.TestCase):
    @staticmethod
    def _make_app():
        app = object.__new__(studio.TTSApp)
        app.lbl_export_status = object()
        app._post_status_label = mock.Mock()
        return app

    def test_mp3_cover_update_uses_jpeg_id3v23_apic(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.mp3"
            cover = root / "cover.png"
            source.write_bytes(b"audio")
            cover.write_bytes(b"png")
            captured = []

            def fake_run(command, **_kwargs):
                captured.append(command)
                Path(command[-1]).write_bytes(b"tagged")
                return _FakeCompletedProcess()

            app = self._make_app()
            with mock.patch.object(
                studio.subprocess, "run", side_effect=fake_run
            ):
                result = app._update_file_tags_inplace(
                    source, {"title": "Chapter"}, cover, "Chapter"
                )

            self.assertTrue(result)
            command = captured[0]
            self.assertEqual(command[command.index("-c:v") + 1], "mjpeg")
            self.assertEqual(
                command[command.index("-id3v2_version") + 1], "3"
            )
            self.assertEqual(
                command[command.index("-disposition:v:0") + 1],
                "attached_pic",
            )
            self.assertEqual(source.read_bytes(), b"tagged")

    def test_opus_update_cover_through_xiph_picture_comment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.opus"
            cover = root / "cover.png"
            source.write_bytes(b"audio")
            cover.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            captured = []

            def fake_run(command, **_kwargs):
                captured.append(command)
                Path(command[-1]).write_bytes(b"tagged")
                return _FakeCompletedProcess()

            app = self._make_app()
            with mock.patch.object(
                studio.subprocess, "run", side_effect=fake_run
            ):
                result = app._update_file_tags_inplace(
                    source, {"title": "Chapter"}, cover, "Chapter"
                )

            self.assertTrue(result)
            command = captured[0]
            self.assertEqual(command.count("-i"), 1)
            self.assertNotIn("-c:v", command)
            self.assertNotIn("-disposition:v:0", command)
            self.assertIn("0:a:0", command)
            metadata = [
                command[index + 1]
                for index, option in enumerate(command[:-1])
                if option == "-metadata"
            ]
            self.assertTrue(any(
                value.startswith("METADATA_BLOCK_PICTURE=")
                for value in metadata
            ))

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "FFmpeg and FFprobe are required for the integration test",
    )
    def test_opus_inplace_cover_and_album_round_trip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.opus"
            cover = root / "cover.png"
            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=duration=0.05",
                    "-c:a", "libopus", str(source),
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

            app = self._make_app()
            self.assertTrue(app._update_file_tags_inplace(
                source,
                {"title": "Part 01", "album": "Test Book"},
                cover,
                "Part 01",
            ))

            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error",
                    "-show_entries", "stream=codec_name,codec_type:stream_tags",
                    "-of", "json", str(source),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            audio = next(
                stream for stream in streams
                if stream.get("codec_type") == "audio"
            )
            pictures = [
                stream for stream in streams
                if stream.get("codec_type") == "video"
            ]
            self.assertEqual(audio["tags"]["title"], "Part 01")
            self.assertEqual(audio["tags"]["album"], "Test Book")
            self.assertEqual(len(pictures), 1)

    def test_wav_still_skips_unsupported_cover_stream(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.wav"
            cover = root / "cover.png"
            source.write_bytes(b"audio")
            cover.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            captured = []

            def fake_run(command, **_kwargs):
                captured.append(command)
                Path(command[-1]).write_bytes(b"tagged")
                return _FakeCompletedProcess()

            app = self._make_app()
            with mock.patch.object(
                studio.subprocess, "run", side_effect=fake_run
            ), self.assertLogs(level=logging.WARNING):
                result = app._update_file_tags_inplace(
                    source, {"title": "Chapter"}, cover, "Chapter"
                )

            self.assertTrue(result)
            command = captured[0]
            self.assertEqual(command.count("-i"), 1)
            self.assertNotIn("METADATA_BLOCK_PICTURE", " ".join(command))


class AudioMetadataImportTests(unittest.TestCase):
    def test_opus_stream_tags_include_album_when_format_tags_are_empty(self):
        app = object.__new__(studio.TTSApp)
        probe_data = {
            "format": {"duration": "12.5", "tags": {}},
            "streams": [
                {
                    "codec_type": "audio",
                    "tags": {
                        "title": "Part 01",
                        "artist": "Reader",
                        "album": "Test Book",
                        "album_artist": "Author",
                        "date": "2026",
                    },
                }
            ],
        }
        with mock.patch.object(
            studio.subprocess,
            "check_output",
            return_value=json.dumps(probe_data).encode("utf-8"),
        ):
            metadata = app.get_audio_metadata("part.opus")

        self.assertEqual(metadata["title"], "Part 01")
        self.assertEqual(metadata["artist"], "Reader")
        self.assertEqual(metadata["album"], "Test Book")
        self.assertEqual(metadata["album_artist"], "Author")
        self.assertEqual(metadata["year"], "2026")
        self.assertEqual(metadata["duration"], 12.5)


class FfmpegSaveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.audio_file = self.root / "input.ogg"
        # Финальная сборка принимает только внутренние канонические фрагменты.
        # Для теста построения команды достаточно сигнатуры Ogg/Opus: сам
        # subprocess ниже подменён и содержимое декодироваться не будет.
        self.audio_file.write_bytes(fake_ogg_first_page(b"OpusHead-test-audio"))
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

    def test_mp3_cover_is_jpeg_encoded_with_explicit_maps(self):
        processor = self.make_processor()
        _output, command, callbacks = self.run_save(processor, "chapter.mp3")

        self.assertEqual(self.values_after(command, "-map"), ["0:a:0", "1:v:0"])
        self.assertEqual(self.values_after(command, "-c:v"), ["mjpeg"])
        self.assertEqual(
            self.values_after(command, "-disposition:v:0"),
            ["attached_pic"],
        )
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

    def test_opus_embeds_cover_as_xiph_picture_and_preserves_tags(self):
        self.cover_file.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        processor = self.make_processor(output_format="opus")
        _output, command, callbacks = self.run_save(processor, "chapter.opus")

        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("-map", command)
        self.assertNotIn("-c:v", command)
        self.assertNotIn("-disposition:v", command)
        metadata = self.values_after(command, "-metadata")
        self.assertIn("album=Book", metadata)
        self.assertTrue(any(
            value.startswith("METADATA_BLOCK_PICTURE=")
            for value in metadata
        ))
        self.assertEqual(callbacks[-1][1], "success")

    def test_wav_uses_rf64_auto_for_large_books(self):
        processor = self.make_processor(
            output_format="wav",
            tag_cover="",
        )

        _output, command, callbacks = self.run_save(processor, "chapter.wav")

        self.assertEqual(self.values_after(command, "-c:a"), ["pcm_s16le"])
        self.assertEqual(self.values_after(command, "-rf64"), ["auto"])
        self.assertEqual(callbacks[-1][1], "success")

    def test_book_profile_writes_explicit_sample_rate_and_channels(self):
        processor = self.make_processor(
            output_sample_rate="24000",
            output_channels="stereo",
        )

        _output, command, callbacks = self.run_save(processor, "chapter.mp3")

        self.assertEqual(self.values_after(command, "-ar"), ["24000"])
        self.assertEqual(self.values_after(command, "-ac"), ["2"])
        self.assertEqual(callbacks[-1][1], "success")

    def test_book_ogg_profile_uses_selected_bitrate(self):
        processor = self.make_processor(
            output_format="ogg",
            output_bitrate="96k",
            tag_cover="",
        )

        _output, command, callbacks = self.run_save(processor, "chapter.ogg")

        self.assertEqual(self.values_after(command, "-c:a"), ["libvorbis"])
        self.assertEqual(self.values_after(command, "-b:a"), ["96k"])
        self.assertEqual(callbacks[-1][1], "success")

    def test_book_ogg_auto_uses_encoder_quality_mode_without_bitrate(self):
        processor = self.make_processor(
            output_format="ogg",
            output_bitrate="auto",
            tag_cover="",
        )

        _output, command, callbacks = self.run_save(processor, "chapter.ogg")

        self.assertEqual(self.values_after(command, "-c:a"), ["libvorbis"])
        self.assertNotIn("-b:a", command)
        self.assertEqual(callbacks[-1][1], "success")

    def test_book_opus_auto_uses_speech_bitrate_and_layout(self):
        processor = self.make_processor(
            output_format="opus",
            output_bitrate="auto",
            output_sample_rate="48000",
            output_channels="mono",
            tag_cover="",
        )

        _output, command, callbacks = self.run_save(processor, "chapter.opus")

        self.assertEqual(self.values_after(command, "-c:a"), ["libopus"])
        self.assertEqual(self.values_after(command, "-b:a"), ["48k"])
        self.assertEqual(self.values_after(command, "-ar"), ["48000"])
        self.assertEqual(self.values_after(command, "-ac"), ["1"])
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
        def __init__(self, response=None):
            self.calls = []
            self.response = response or CacheBehaviorTests.FakeResponse()

        def post(self, url, json, timeout):
            self.calls.append((url, json, timeout))
            return self.response

    class Fake422Response:
        status_code = 422

        def raise_for_status(self):
            raise studio.requests.exceptions.HTTPError(
                "422 Client Error: Unprocessable Entity",
                response=self,
            )

        @staticmethod
        def json():
            return {"detail": "Your text is empty!"}

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
            destination = Path(destination)
            # Имитируем контракт реального _prepare_api_audio_file: наружу
            # публикуется только физический Ogg/Opus, а не произвольный ответ.
            destination.write_bytes(fake_ogg_first_page(b"OpusHead-new audio"))
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
        self.assertEqual(returned_file.read_bytes(), fake_ogg_first_page(b"OpusHead-new audio"))
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
        self.assertEqual(returned_file.read_bytes(), fake_ogg_first_page(b"OpusHead-new audio"))
        self.assertEqual(len(processor.session.calls), 1)
        self.assertEqual(processor.cache[text_hash]["usage_count"], 1)
        self.assertEqual(processor.unsaved_cache_items, 1)

    def test_synthesis_uses_configured_api_url_verbatim(self):
        processor = self.make_processor(use_cache=False)
        configured_url = "http://127.0.0.1:8000/enhanced_voice"
        processor.cfg["api_url"] = configured_url

        returned_file, success = self.synthesize_without_decoding(processor)

        self.assertTrue(success)
        self.assertTrue(returned_file.exists())
        self.assertEqual(len(processor.session.calls), 1)
        self.assertEqual(processor.session.calls[0][0], configured_url)

    def test_local_decode_failure_does_not_repeat_successful_api_request(self):
        processor = self.make_processor(use_cache=True)
        processor.cfg["max_retries"] = 5
        fallback = self.root / "fallback.ogg"
        processor._get_silence_file = mock.Mock(return_value=fallback)

        with mock.patch.object(
            studio,
            "_prepare_api_audio_file",
            side_effect=FileNotFoundError("ffprobe"),
        ), self.assertLogs(level=logging.ERROR) as captured:
            returned_file, success = processor.synthesize_sentence(
                "Фраза.", "Фраза."
            )

        self.assertFalse(success)
        self.assertEqual(returned_file, fallback)
        self.assertEqual(len(processor.session.calls), 1)
        self.assertIn(
            "Ошибка локальной подготовки ответа API",
            "\n".join(captured.output),
        )

    def test_http_422_logs_detail_and_is_not_retried(self):
        processor = self.make_processor(use_cache=True)
        processor.cfg["max_retries"] = 5
        processor.session = self.FakeSession(self.Fake422Response())
        fallback = self.root / "fallback.ogg"
        processor._get_silence_file = mock.Mock(return_value=fallback)

        with self.assertLogs(level=logging.WARNING) as captured:
            returned_file, success = processor.synthesize_sentence("王.", "(王)")

        self.assertFalse(success)
        self.assertEqual(returned_file, fallback)
        self.assertEqual(len(processor.session.calls), 1)
        log_text = "\n".join(captured.output)
        self.assertIn("Your text is empty!", log_text)
        self.assertIn("source='(王)'", log_text)
        self.assertIn("normalized='王.'", log_text)
        self.assertIn("без повторной попытки", log_text)


class CacheOpusMigrationTests(unittest.TestCase):
    def test_migration_updates_metadata_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            audio_dir = cache_dir / "audio"
            audio_dir.mkdir()
            vorbis = audio_dir / ("a" * 32 + ".ogg")
            opus = audio_dir / ("b" * 32 + ".ogg")
            vorbis.write_bytes(fake_ogg_first_page(b"\x01vorbis-old"))
            opus.write_bytes(fake_ogg_first_page(b"OpusHead-new"))
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
                path.write_bytes(fake_ogg_first_page(b"OpusHead-converted"))
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
                (audio_dir / filename).write_bytes(fake_ogg_first_page(b"\x01vorbis"))
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
            audio_file.write_bytes(fake_ogg_first_page(b"\x01vorbis"))
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
            audio_file.write_bytes(fake_ogg_first_page(b"\x01vorbis"))
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
            (audio_dir / filename).write_bytes(fake_ogg_first_page(b"\x01vorbis"))
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
    Path(studio.get_ffmpeg_path()).is_file()
    and Path(studio.get_ffprobe_path()).is_file(),
    "FFmpeg and FFprobe are required for the integration test",
)
class FfmpegIntegrationTests(unittest.TestCase):
    def test_opus_cover_and_album_round_trip_through_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.wav"
            cover = root / "cover.png"
            output = root / "book.opus"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.05",
                    "-ar", "48000", "-ac", "1", str(source),
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

            studio._export_single_audio_ffmpeg(
                source,
                output,
                output_format="opus",
                bitrate_mode="32k",
                sample_rate="48000",
                channels="mono",
                tags={"title": "Part 01", "album": "Test Book"},
                cover=cover,
            )

            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error",
                    "-show_entries",
                    "stream=codec_name,codec_type:stream_disposition=attached_pic:stream_tags",
                    "-of", "json", str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            audio = next(
                stream for stream in streams
                if stream.get("codec_type") == "audio"
            )
            pictures = [
                stream for stream in streams
                if stream.get("codec_type") == "video"
            ]
            self.assertEqual(audio["codec_name"], "opus")
            self.assertEqual(audio["tags"]["title"], "Part 01")
            self.assertEqual(audio["tags"]["album"], "Test Book")
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0]["codec_name"], "png")
            self.assertEqual(pictures[0]["disposition"]["attached_pic"], 1)

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

    def test_stereo_api_opus_is_downmixed_even_without_trimming(self):
        """Fast path must not let a non-canonical Opus layout into cache."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "api-stereo.ogg"
            destination = root / "cache-mono.ogg"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.1:sample_rate=48000",
                    "-filter_complex", "[0:a]pan=stereo|c0=c0|c1=c0[out]",
                    "-map", "[out]", "-c:a", "libopus", str(source),
                ],
                check=True,
            )

            self.assertEqual(
                studio._inspect_ogg_audio_header(source), ("opus", 2)
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
            self.assertNotEqual(source.read_bytes(), destination.read_bytes())

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

    def test_png_cover_is_converted_to_jpeg_for_windows_mp3(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            audio = root / "audio.ogg"
            cover = root / "cover.png"
            output = root / "book.mp3"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.05",
                    "-ar", "48000", "-ac", "1", "-c:a", "libopus",
                    str(audio),
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
            self.assertEqual(pictures[0]["codec_name"], "mjpeg")
            self.assertEqual(pictures[0]["disposition"]["attached_pic"], 1)


if __name__ == "__main__":
    unittest.main()
