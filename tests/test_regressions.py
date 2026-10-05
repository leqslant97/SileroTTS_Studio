import hashlib
import importlib.util
import json
import logging
import math
import re
import subprocess
import base64
import copy
import struct
import sys
import tempfile
import textwrap
import threading
import unittest
import wave
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_DIR / "SileroTTS_Studio.py"


def load_studio_module():
    """Импортирует приложение из одного файла без запуска цикла Tk."""
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
    """Создаёт минимальную первую страницу Ogg для тестов заголовков."""
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

        # Устаревшее неверное значение в старой/отключённой конфигурации
        # не должно мешать запуску.
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


class CacheOptimizationSourceTests(unittest.TestCase):
    def test_nested_texts_protect_their_cache_entries(self):
        for root_text in (True, False):
            with self.subTest(root_text=root_text), tempfile.TemporaryDirectory() as temp:
                source_dir = Path(temp) / "texts"
                nested_dir = source_dir / "part"
                nested_dir.mkdir(parents=True)
                (nested_dir / "nested.txt").write_text("Nested text", encoding="utf-8")
                if root_text:
                    (source_dir / "root.txt").write_text("Root text", encoding="utf-8")

                entries = {"nested": {"text": "Nested text"}, "stale": {"text": "Old text"}}
                if root_text:
                    entries["root"] = {"text": "Root text"}
                processor = mock.Mock()
                processor.cache = entries.copy()
                processor.cache_dir = Path(temp) / "cache"
                processor.get_all_possible_hashes.side_effect = (
                    lambda raw_text, include_prepared: {raw_text}
                )

                app = object.__new__(studio.TTSApp)
                app.config = {"input_dir": str(source_dir), "include_subdirs": True}
                app.is_cache_operation_running = mock.Mock(return_value=False)
                app.is_synthesis_running = mock.Mock(return_value=False)
                app._validate_api_steps_ui = mock.Mock(return_value=True)
                app.save_settings = mock.Mock()
                popup = mock.Mock()
                app._begin_cache_operation = mock.Mock(return_value=popup)
                app._end_cache_operation = mock.Mock()
                app._choose_cache_variant_policy = mock.Mock(
                    return_value=studio.CACHE_VARIANT_KEEP_ALL
                )
                app._ask_yes_no = mock.Mock(return_value=True)
                app.is_cache_optimization_running = mock.Mock(return_value=True)
                app._post_to_ui = mock.Mock()
                app._show_info = mock.Mock()
                app._show_warning = mock.Mock()
                app._finish_cache_optimization = mock.Mock()

                with ExitStack() as patches:
                    patches.enter_context(mock.patch.object(studio, "TTSProcessor", return_value=processor))
                    patches.enter_context(mock.patch.object(studio, "cache_entry_matches_required_text", side_effect=lambda entry, hashes: entry["text"] in hashes))
                    patches.enter_context(mock.patch.object(studio, "unreferenced_cache_audio_paths", return_value=[]))
                    write_index = patches.enter_context(mock.patch.object(studio, "write_cache_index_atomic"))
                    thread_class = patches.enter_context(mock.patch.object(studio.threading, "Thread"))
                    thread_class.return_value.start.side_effect = (
                        lambda: thread_class.call_args.kwargs["target"]()
                    )

                    app.optimize_cache()

                expected = {key: value for key, value in entries.items() if key != "stale"}
                write_index.assert_called_once_with(processor.cache_dir, expected)
                self.assertEqual(processor.cache, expected)
                self.assertFalse(any(
                    call.args[0] is app._show_warning
                    for call in app._post_to_ui.call_args_list
                ))


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

    def test_invalid_numeric_metadata_is_normalized_without_mutating_input(self):
        source = {
            "hash": {
                "file_name": "fragment.ogg",
                "created_at": "not-a-number",
                "last_accessed": float("nan"),
                "usage_count": "-7",
            }
        }

        normalized = studio.validate_cache_index(source)

        self.assertIsNot(normalized, source)
        self.assertIsNot(normalized["hash"], source["hash"])
        self.assertIsInstance(normalized["hash"]["created_at"], (int, float))
        self.assertIsInstance(normalized["hash"]["last_accessed"], (int, float))
        self.assertTrue(math.isfinite(normalized["hash"]["created_at"]))
        self.assertTrue(math.isfinite(normalized["hash"]["last_accessed"]))
        self.assertGreaterEqual(normalized["hash"]["usage_count"], 0)
        self.assertIsInstance(normalized["hash"]["usage_count"], int)
        self.assertEqual(source["hash"]["created_at"], "not-a-number")
        self.assertEqual(source["hash"]["usage_count"], "-7")


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

    def test_manual_direct_path_keeps_settings_and_direct_tab_variables_in_sync(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"direct_output_dir": "old"}
        app.settings_vars = {}
        app._is_updating_ui = False
        app._is_closing = False
        settings_variable = mock.Mock(get=mock.Mock(return_value="new-direct"))
        direct_variable = mock.Mock(get=mock.Mock(return_value="old"))
        app.settings_vars["direct_output_dir"] = settings_variable
        app.direct_output_dir_var = direct_variable

        app._path_var_changed("direct_output_dir", settings_variable)

        self.assertEqual(app.config["direct_output_dir"], "new-direct")
        direct_variable.set.assert_called_once_with("new-direct")


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

    def test_export_parallel_flag_round_trips_in_portable_output_settings(self):
        self.assertIn(
            "export_parallel_enabled",
            studio._config_group_rules_data()["tags"],
        )
        profile = studio.select_config_values(
            {**studio.DEFAULT_CONFIG, "export_parallel_enabled": False},
            ["tags"],
        )

        self.assertIn("export_parallel_enabled", profile)
        self.assertFalse(profile["export_parallel_enabled"])

        merged = studio.merge_config_values(
            {**studio.DEFAULT_CONFIG, "export_parallel_enabled": True},
            profile,
            ["tags"],
        )
        self.assertFalse(merged["export_parallel_enabled"])

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
    def test_source_book_name_is_bound_to_its_input_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "Первая папка"
            second = root / "Вторая папка"
            config = studio.normalize_config(
                {
                    "input_dir": str(first),
                    "source_book_name": "Серия / Том 1",
                    "source_book_name_input_dir": str(first),
                    "tag_album": "Альбом из настроек",
                }
            )

            self.assertEqual(
                studio.resolve_source_book_name(config, first),
                "Серия / Том 1",
            )
            self.assertEqual(
                studio.resolve_source_book_name(config, second),
                "Альбом из настроек",
            )
            config["tag_album"] = "{filename}"
            self.assertEqual(
                studio.resolve_source_book_name(config, second),
                "Вторая папка",
            )

    def test_source_book_name_migrates_from_internal_m4b_alias(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_root = Path(tempdir) / "Исходная папка"
            config = studio.normalize_config(
                {
                    "input_dir": str(source_root),
                    "source_m4b_book_name": "Старое имя книги",
                }
            )
            self.assertEqual(config["source_book_name"], "Старое имя книги")
            self.assertEqual(
                Path(config["source_book_name_input_dir"]),
                source_root,
            )
            self.assertEqual(
                studio.resolve_source_book_name(
                    config,
                    source_root.parent / "Другая папка",
                ),
                "Другая папка",
            )

    def test_source_m4b_templates_have_independent_defaults_and_empty_name_is_no_range(self):
        config = studio.normalize_config({})
        self.assertEqual(
            config["synthesis_m4b_template"],
            studio.DEFAULT_SOURCE_M4B_TEMPLATE,
        )
        self.assertEqual(
            config["synthesis_m4b_album_template"],
            studio.DEFAULT_SOURCE_M4B_ALBUM_TEMPLATE,
        )
        self.assertEqual(
            studio.normalize_source_m4b_template("  "),
            studio.SOURCE_M4B_NO_RANGE_TEMPLATE,
        )
        custom = studio.normalize_config(
            {
                "synthesis_m4b_template": "{book}",
                "synthesis_m4b_album_template": "Серия {book}",
            }
        )
        self.assertEqual(custom["synthesis_m4b_template"], "{book}")
        self.assertEqual(custom["synthesis_m4b_album_template"], "Серия {book}")

    def test_source_m4b_template_alias_migrates_without_resurrecting_range(self):
        config = studio.normalize_config({"source_m4b_template": ""})
        self.assertEqual(
            config["synthesis_m4b_template"],
            studio.SOURCE_M4B_NO_RANGE_TEMPLATE,
        )

    def test_source_m4b_template_validation_recovers_fields_independently(self):
        config = studio.normalize_config(
            {
                "synthesis_m4b_template": "{book} {part}",
                "synthesis_m4b_album_template": "{unknown}",
            }
        )
        self.assertEqual(config["synthesis_m4b_template"], "{book} {part}")
        self.assertEqual(
            config["synthesis_m4b_album_template"],
            studio.DEFAULT_SOURCE_M4B_ALBUM_TEMPLATE,
        )

    def test_m4b_safe_duration_limit_caps_unlimited_and_preserves_smaller(self):
        # Общий лимит экспорта намеренно не задан фиксированным значением:
        # предел 23:50 включается только для измеренной книги длиннее суток.
        # Явное значение из импортированной/старой конфигурации не меняется.
        normalized_defaults = studio.normalize_config({})
        self.assertEqual(
            normalized_defaults["export_m4b_max_duration_hours"],
            studio.DEFAULT_M4B_MAX_DURATION_HOURS,
        )
        # Значение, явно сохранённое старой версией, является выбором
        # пользователя, а не пропущенным параметром, и совместимо со старым
        # планировщиком.
        self.assertEqual(
            studio.normalize_config(
                {"export_m4b_max_duration_hours": 23.0}
            )["export_m4b_max_duration_hours"],
            23.0,
        )
        self.assertEqual(
            studio.plan_m4b_parts(
                [("A", 12 * 3600), ("B", 11 * 3600), ("C", 12 * 3600)],
                max_duration_seconds=normalized_defaults[
                    "export_m4b_max_duration_hours"
                ]
                * 3600,
                auto_split_long=True,
            )[0].total,
            2,
        )
        self.assertEqual(
            studio.effective_m4b_duration_limit(0),
            0.0,
        )
        self.assertEqual(
            studio.effective_m4b_duration_limit(0, cap_unlimited=True),
            studio.M4B_SAFE_VOLUME_SECONDS,
        )
        self.assertEqual(studio.effective_m4b_duration_limit(3600), 3600)
        self.assertEqual(
            studio.effective_m4b_duration_limit(24 * 3600),
            studio.M4B_SAFE_VOLUME_SECONDS,
        )

    def test_source_m4b_actual_reflow_defaults_and_numeric_limit(self):
        config = studio.normalize_config(
            {
                "synthesis_m4b_reflow_actual_duration": "true",
                "synthesis_m4b_reflow_minutes": "90",
            }
        )

        self.assertTrue(config["synthesis_m4b_reflow_actual_duration"])
        self.assertEqual(config["synthesis_m4b_reflow_minutes"], 90.0)

        invalid = studio.normalize_config(
            {"synthesis_m4b_reflow_minutes": "not-a-number"}
        )
        self.assertEqual(
            invalid["synthesis_m4b_reflow_minutes"],
            studio.DEFAULT_CONFIG["synthesis_m4b_reflow_minutes"],
        )

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

    def test_export_parallel_processing_is_opt_in(self):
        config = studio.normalize_config({})

        self.assertIs(studio.DEFAULT_CONFIG["export_parallel_enabled"], False)
        self.assertIs(config["export_parallel_enabled"], False)

    def test_export_parallel_processing_normalizes_false_values(self):
        for raw_value in (False, 0, "0", "false", "off", "no"):
            with self.subTest(raw_value=raw_value):
                config = studio.normalize_config(
                    {"export_parallel_enabled": raw_value}
                )
                self.assertIs(config["export_parallel_enabled"], False)

    def test_export_parallel_processing_normalizes_true_values(self):
        for raw_value in (True, 1, "1", "true", "on", "yes"):
            with self.subTest(raw_value=raw_value):
                config = studio.normalize_config(
                    {"export_parallel_enabled": raw_value}
                )
                self.assertIs(config["export_parallel_enabled"], True)

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
    def test_process_queue_delayed_callbacks_keep_their_source_file(self):
        """Фоновые кодировщики не должны менять последнюю строку очереди."""

        class DeferredProcessor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}
                self.callbacks = []

            def process_text_file(self, filepath, **kwargs):
                # Настоящий кодировщик вызывает их из другого потока. Оставляем
                # вызовы отложенными до завершения обеих итераций очереди —
                # именно такой порядок выявляет ошибку позднего связывания.
                self.callbacks.append(
                    (kwargs["encoding_callback"], kwargs["completion_callback"])
                )

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")

            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {
                "one-id": first,
                "two-id": second,
            }
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            status_events = []

            def record_status(*args):
                status_events.append(args)

            app.update_file_status = record_status
            # Для этого точечного теста выполняем очередь интерфейса синхронно.
            # Обратные вызовы обработчика при этом остаются отложенными,
            # как описано выше.
            app._post_to_ui = lambda callback, *args: callback(*args)

            processor = DeferredProcessor()
            app.process_queue(
                processor,
                ("one-id", "two-id"),
                {
                    "input_dir": str(root),
                    "output_dir": str(root / "out"),
                    "output_format": "mp3",
                    "include_subdirs": False,
                },
                False,
            )

            for encoding_callback, completion_callback in processor.callbacks:
                encoding_callback("ignored-name")
                completion_callback("ignored-name", "success", None)

            delayed = [
                (args[0], args[1])
                for args in status_events
                if len(args) >= 2 and args[1] in {"encoding", "success"}
            ]
            self.assertEqual(
                delayed,
                [
                    ("one-id", "encoding"),
                    ("one-id", "success"),
                    ("two-id", "encoding"),
                    ("two-id", "success"),
                ],
            )


class SourceSynthesisRuntimeTests(unittest.TestCase):
    """Функциональные контракты однопроходного синтеза из папки с несколькими целями."""

    def test_source_group_metadata_selects_target_pipeline_for_legacy_output(self):
        """Даже явная очистка тега требует применить снимок к основному формату."""
        self.assertFalse(
            studio.source_groups_have_metadata_overrides(
                {"volume": {"file_ids": ("chapter",)}}
            )
        )
        self.assertTrue(
            studio.source_groups_have_metadata_overrides(
                {
                    "volume": {
                        "file_ids": ("chapter",),
                        "metadata_overrides": {"album": ""},
                    }
                }
            )
        )
        self.assertTrue(
            studio.source_groups_have_metadata_overrides(
                (
                    {
                        "source_group": {
                            "metadata_overrides": {"artist": "Автор тома"}
                        }
                    },
                )
            )
        )
        legacy_target = studio.source_synthesis_targets_from_config(
            {
                "output_format": "opus",
                "output_bitrate": "48k",
                "output_sample_rate": "auto",
                "output_channels": "auto",
                "output_dir": "/tmp/audio",
            }
        )
        self.assertEqual(len(legacy_target), 1)
        self.assertEqual(legacy_target[0]["format"], "opus")
        self.assertEqual(legacy_target[0]["bitrate"], "48k")

    def test_source_group_metadata_commit_is_atomic_and_keeps_explicit_clear(self):
        app = object.__new__(studio.TTSApp)
        app._source_plan_groups = {
            "group-1": {
                "name": "Том 1",
                "source_group": {"id": "group-1", "unrelated": "keep"},
            }
        }
        app._source_plan_dirty = False
        app._invalidate_source_runtime_m4b_plan = mock.Mock()

        applied = app._commit_source_group_metadata_overrides(
            "group-1",
            {
                "title": "Первый том",
                "album": "",
                "year": "2026",
                "unknown": "игнорируется",
            },
        )

        expected = {
            "title": "Первый том",
            "album": "",
            "year": "2026",
        }
        self.assertTrue(applied)
        self.assertEqual(
            app._source_plan_groups["group-1"]["metadata_overrides"],
            expected,
        )
        self.assertEqual(
            app._source_plan_groups["group-1"]["source_group"][
                "metadata_overrides"
            ],
            expected,
        )
        self.assertEqual(
            app._source_plan_groups["group-1"]["source_group"]["unrelated"],
            "keep",
        )
        self.assertTrue(app._source_plan_dirty)
        app._invalidate_source_runtime_m4b_plan.assert_called_once_with()

        app._invalidate_source_runtime_m4b_plan.reset_mock()
        self.assertFalse(
            app._commit_source_group_metadata_overrides(
                "missing", {"artist": "Автор"}
            )
        )
        app._invalidate_source_runtime_m4b_plan.assert_not_called()

    def test_source_planner_propagates_group_tags_to_every_regular_target(self):
        """Локальные теги части принадлежат TXT, а не только итоговому M4B."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            first = root / "001.txt"
            second = root / "002.txt"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            first_overrides = {
                "artist": "Автор первого тома",
                "album": "Том 1",
                "cover": "cover-one.jpg",
            }
            second_overrides = {
                "artist": "Автор второго тома",
                "album": "",
            }

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                (first, second),
                targets=(
                    studio.OutputTarget(format="mp3", bitrate="128k"),
                    studio.OutputTarget(format="opus", bitrate="48k"),
                    studio.OutputTarget(format="m4a", bitrate="64k"),
                ),
                source_m4b_groups=(
                    {
                        "id": "volume-one",
                        "file_ids": (str(first),),
                        "metadata_overrides": first_overrides,
                    },
                    {
                        "id": "volume-two",
                        "file_ids": (str(second),),
                        "metadata_overrides": second_overrides,
                    },
                ),
            )

            by_target_and_source = {
                (record["target"]["format"], record["item_id"]): record
                for record in planned
            }
            for fmt in ("mp3", "opus", "m4a"):
                self.assertEqual(
                    by_target_and_source[(fmt, str(first))][
                        "metadata_overrides"
                    ],
                    first_overrides,
                )
                self.assertEqual(
                    by_target_and_source[(fmt, str(second))][
                        "metadata_overrides"
                    ],
                    second_overrides,
                )

    def test_regular_target_chain_applies_group_tags_and_cover_last(self):
        """Экспортёр каждого обычного формата получает локальные теги части."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "fragment.ogg"
            opus_head = b"OpusHead" + bytes((1, 1)) + b"\x00" * 9
            source.write_bytes(fake_ogg_first_page(opus_head))
            global_cover = root / "global.jpg"
            local_cover = root / "volume.jpg"
            global_cover.write_bytes(b"global cover")
            local_cover.write_bytes(b"local cover")
            overrides = {
                "title": "Локальная глава",
                "artist": "Локальный автор",
                "album": "",
                "year": "2026",
                "cover": str(local_cover),
            }
            records = []
            for target_index, fmt in enumerate(
                ("mp3", "opus", "ogg", "m4a", "wav")
            ):
                records.append(
                    {
                        "target_index": target_index,
                        "target": studio.normalize_output_target(
                            {"format": fmt, "bitrate": "auto"}
                        ),
                        "path": root / f"chapter.{fmt}",
                        "metadata_overrides": overrides,
                    }
                )
            calls = []

            def fake_export(_audio_files, output_path, **kwargs):
                calls.append((Path(output_path).suffix.lstrip("."), kwargs))
                Path(output_path).write_bytes(b"encoded")

            config = {
                "apply_output_tags": True,
                "tag_title": "Глобальное название",
                "tag_artist": "Глобальный автор",
                "tag_album_artist": "Глобальный автор альбома",
                "tag_album": "Глобальный альбом",
                "tag_year": "2020",
                "tag_cover": str(global_cover),
            }
            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_export
            ):
                result = studio.run_source_synthesis_target_chain(
                    Processor(), audio_files=(source,), records=records,
                    config=config,
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(
                [fmt for fmt, _kwargs in calls],
                ["mp3", "opus", "ogg", "m4a", "wav"],
            )
            for _fmt, kwargs in calls:
                self.assertEqual(kwargs["tags"]["title"], "Локальная глава")
                self.assertEqual(kwargs["tags"]["artist"], "Локальный автор")
                self.assertEqual(
                    kwargs["tags"]["album_artist"],
                    "Глобальный автор альбома",
                )
                self.assertNotIn("album", kwargs["tags"])
                self.assertEqual(kwargs["tags"]["date"], "2026")
                self.assertEqual(kwargs["cover"], str(local_cover))

    def test_source_group_tag_selection_accepts_txt_inside_virtual_group(self):
        class FakeTree:
            parents = {
                "folder": "group-1",
                "chapter.txt": "folder",
                "other.txt": "group-2",
            }

            def selection(self):
                return ("chapter.txt",)

            def exists(self, item_id):
                return item_id in {
                    "group-1", "group-2", "folder", "chapter.txt", "other.txt"
                }

            def parent(self, item_id):
                return self.parents.get(item_id, "")

            def get_children(self, item_id=""):
                if item_id == "":
                    return ("group-1", "group-2")
                return ()

        app = object.__new__(studio.TTSApp)
        app.tree = FakeTree()
        app._source_plan_groups = {
            "group-1": {"file_ids": ("chapter.txt",)},
            "group-2": {"file_ids": ("other.txt",)},
        }

        self.assertEqual(
            app._source_selected_virtual_group_ids(),
            ("group-1",),
        )

    def test_source_cache_profiles_are_header_only_and_keep_opus_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.ogg"
            second = root / "two.ogg"
            opus_head = b"OpusHead" + bytes((1, 1)) + b"\x00" * 9
            first.write_bytes(fake_ogg_first_page(opus_head))
            second.write_bytes(fake_ogg_first_page(opus_head))

            with mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                side_effect=AssertionError("ffprobe must not run"),
            ):
                profiles = studio._source_synthesis_cache_profiles(
                    (first, second)
                )

            self.assertEqual(
                [(item["codec"], item["sample_rate"], item["channels"])
                 for item in profiles],
                [("opus", 48000, 1), ("opus", 48000, 1)],
            )
            self.assertEqual(
                profiles[0]["extradata_hash"],
                profiles[1]["extradata_hash"],
            )

    def test_stopped_target_chain_skips_remaining_fragment_headers(self):
        processor = mock.Mock(is_stopped=True)
        records = [{
            "target_index": 0,
            "target": {"format": "mp3"},
            "path": "out.mp3",
        }]
        fragments = ("first.ogg", "second.ogg")
        with mock.patch.object(studio, "_inspect_ogg_audio_header_details") as inspect:
            with self.assertRaises(InterruptedError):
                studio.run_source_synthesis_target_chain(
                    processor, fragments, records, {}
                )
            inspect.assert_not_called()

            processor.is_stopped = False
            def stop_after_first(_path):
                processor.is_stopped = True
                return "opus", 1, b"OpusHead"

            inspect.side_effect = stop_after_first
            with self.assertRaises(InterruptedError):
                studio.run_source_synthesis_target_chain(
                    processor, fragments, records, {}
                )
            self.assertEqual(inspect.call_count, 1)

    def test_merge_accepts_reusable_profiles_without_reprobing_sources(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.ogg"
            source.write_bytes(b"audio")
            destination = root / "book.mp3"
            profile = {
                "codec": "opus",
                "sample_rate": 48000,
                "channels": 1,
                "bitrate": None,
                "format_name": "ogg",
                "channel_layout": "mono",
            }
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **_kwargs):
                Path(command[-1]).write_bytes(b"mp3")
                return process

            with mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                side_effect=AssertionError("profile snapshot must be reused"),
            ), mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ):
                studio._export_merged_audio_ffmpeg(
                    (source,),
                    destination,
                    output_format="mp3",
                    bitrate="128k",
                    _probed_profiles=(profile,),
                )

            self.assertEqual(destination.read_bytes(), b"mp3")

    def test_target_chain_encodes_formats_sequentially_and_reports_format(self):
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "fragment.ogg"
            source.write_bytes(b"canonical audio")
            targets = [
                studio.normalize_output_target(
                    {"format": "mp3", "bitrate": "128k"}
                ),
                studio.normalize_output_target(
                    {"format": "opus", "bitrate": "48k"}
                ),
            ]
            records = [
                {
                    "target_index": index,
                    "target": target,
                    "path": root / f"out-{target['format']}.{target['format']}",
                }
                for index, target in enumerate(targets)
            ]
            calls = []
            events = []

            def fake_export(audio_files, output_path, **kwargs):
                calls.append((tuple(audio_files), kwargs["output_format"]))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"encoded")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_export
            ):
                result = studio.run_source_synthesis_target_chain(
                    Processor(),
                    (source,),
                    records,
                    {},
                    status_callback=lambda fmt, path, status: events.append(
                        (fmt, status)
                    ),
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual([fmt for _files, fmt in calls], ["mp3", "opus"])
            self.assertEqual(
                [event for event in events if event[1] == "encoding"],
                [("mp3", "encoding"), ("opus", "encoding")],
            )
            self.assertEqual(
                [event for event in events if event[1] == "success"],
                [("mp3", "success"), ("opus", "success")],
            )

    def test_source_m4b_auto_bitrate_uses_source_setting_before_export_setting(self):
        """Автоматический битрейт M4B для исходников не должен наследовать профиль экспорта."""

        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            chapter = root / "chapter.ogg"
            chapter.write_bytes(b"canonical audio")
            output = root / "book.m4b"
            record = {
                "target": studio.normalize_output_target(
                    {"format": "m4b", "bitrate": "auto"}
                ),
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (root / "chapter.txt",),
            }
            config = {
                "synthesis_m4b_bitrate": "48k",
                "export_m4b_bitrate": "192k",
                "apply_output_tags": False,
            }

            def fake_m4b(_audio_files, output_path, **kwargs):
                Path(output_path).write_bytes(b"m4b")
                self.assertEqual(kwargs["bitrate"], "48k")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ) as export:
                result = studio.run_source_synthesis_m4b_target(
                    Processor(),
                    record,
                    {"chapter-id": (chapter,)},
                    config,
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(export.call_args.kwargs["bitrate"], "48k")
            self.assertTrue(output.is_file())

    def test_source_m4b_recreates_deleted_output_directory(self):
        """Каталог плана мог быть удалён после подготовки, но до записи M4B."""

        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            chapter = root / "chapter.ogg"
            chapter.write_bytes(b"canonical audio")
            deleted_dir = root / "removed-after-planning" / "m4b"
            deleted_dir.mkdir(parents=True)
            deleted_dir.rmdir()
            deleted_dir.parent.rmdir()
            output = deleted_dir / "book.m4b"
            record = {
                "target": studio.normalize_output_target(
                    {"format": "m4b", "bitrate": "64k"}
                ),
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (root / "chapter.txt",),
            }

            def fake_m4b(_audio_files, output_path, **_kwargs):
                output_path = Path(output_path)
                self.assertTrue(output_path.parent.is_dir())
                output_path.write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                result = studio.run_source_synthesis_m4b_target(
                    Processor(),
                    record,
                    {"chapter-id": (chapter,)},
                    {
                        "input_dir": str(root),
                        "apply_output_tags": False,
                        "synthesis_m4b_bitrate": "64k",
                    },
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(output.read_bytes(), b"m4b")

    def test_source_m4b_chapter_counter_start_is_consistent_across_volumes(self):
        """Финальный этап источников сохраняет сквозной старт и сбрасывает старт тома."""

        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            fragments = {}
            for chapter_id in ("chapter-7", "chapter-8", "chapter-9"):
                fragment = root / f"{chapter_id}.ogg"
                fragment.write_bytes(b"canonical audio")
                fragments[chapter_id] = (fragment,)
            target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "chapter_title_template": (
                        "{global_index:10}/{volume_index:20}"
                    ),
                }
            )
            records = (
                {
                    "target": target,
                    "path": root / "book-1.m4b",
                    "file_ids": ("chapter-7", "chapter-8"),
                    "source_paths": (root / "chapter-7.txt", root / "chapter-8.txt"),
                    "group_index": 1,
                    "parts": 2,
                    "chapter_start": 7,
                    "chapter_end": 8,
                    "chapter_count": 9,
                },
                {
                    "target": target,
                    "path": root / "book-2.m4b",
                    "file_ids": ("chapter-9",),
                    "source_paths": (root / "chapter-9.txt",),
                    "group_index": 2,
                    "parts": 2,
                    "chapter_start": 9,
                    "chapter_end": 9,
                    "chapter_count": 9,
                },
            )
            captured = []

            def fake_m4b(_audio_files, output_path, **kwargs):
                captured.append(kwargs["chapters"])
                Path(output_path).write_bytes(b"m4b")

            config = {
                "input_dir": str(root),
                "apply_output_tags": False,
                "synthesis_m4b_bitrate": "64k",
            }
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                for record in records:
                    result = studio.run_source_synthesis_m4b_target(
                        Processor(), record, fragments, config
                    )
                    self.assertEqual(result["status"], "success")

            self.assertEqual(
                [[chapter["title"] for chapter in volume] for volume in captured],
                [["16/20", "17/21"], ["18/20"]],
            )

    def test_m4b_export_reports_progress_for_long_post_stage(self):
        """M4B сообщает об активности даже без побайтового прогресса API/FFmpeg."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.ogg"
            second = root / "two.ogg"
            first.write_bytes(b"audio")
            second.write_bytes(b"audio")
            output = root / "book.m4b"
            events = []

            merge_kwargs = []

            def fake_merge(_sources, destination, **kwargs):
                merge_kwargs.append(kwargs)
                Path(destination).write_bytes(b"stage m4a")

            def fake_checked(_command, *, out_path=None, **_kwargs):
                Path(out_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_merge
            ), mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=fake_checked
            ):
                result = studio._export_m4b_ffmpeg(
                    (first, second),
                    output,
                    chapters=(
                        {"title": "One", "duration": 10},
                        {"title": "Two", "duration": 20},
                    ),
                    progress_callback=lambda current, total, text: events.append(
                        (current, total, text)
                    ),
                )

            self.assertEqual(result, output)
            self.assertTrue(output.is_file())
            self.assertFalse(merge_kwargs[0]["faststart"])
            self.assertTrue(any(current < 0 for current, _total, _text in events))
            self.assertEqual(events[-1][0], events[-1][1])
            self.assertIn("готово", events[-1][2])

    def test_m4b_export_accepts_auto_channels_as_canonical_mono(self):
        """Прямой вызов M4B поддерживает тот же режим ``auto``, что и аудиопрофиль."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.ogg"
            source.write_bytes(b"audio")
            output = root / "book.m4b"
            merge_kwargs = []

            def fake_merge(_sources, destination, **kwargs):
                merge_kwargs.append(kwargs)
                Path(destination).write_bytes(b"stage m4a")

            def fake_checked(_command, *, out_path=None, **_kwargs):
                Path(out_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_merge
            ), mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=fake_checked
            ):
                result = studio._export_m4b_ffmpeg(
                    (source,),
                    output,
                    chapters=({"title": "Chapter", "duration": 1},),
                    channels="auto",
                )

            self.assertEqual(result, output)
            self.assertEqual(merge_kwargs[0]["channels"], "mono")
            self.assertTrue(output.is_file())

    def test_m4b_export_writes_disk_pair_into_final_ffmpeg_command(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "one.ogg"
            source.write_bytes(b"audio")
            output = root / "book.m4b"
            commands = []

            def fake_merge(_sources, destination, **_kwargs):
                Path(destination).write_bytes(b"stage m4a")

            def fake_checked(command, *, out_path=None, **_kwargs):
                commands.append(tuple(command))
                Path(out_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_merge
            ), mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=fake_checked
            ):
                studio._export_m4b_ffmpeg(
                    (source,),
                    output,
                    chapters=({"title": "One", "duration": 10},),
                    tags={"album": "Book", "language": "eng"},
                    disk_number=2,
                    disk_total=3,
                )

            self.assertEqual(len(commands), 1)
            # Мультиплексор MOV в FFmpeg принимает ключ метаданных ``disc`` и
            # записывает его в атом ``disk`` контейнера QuickTime. Сам ключ ``disk``
            # текущие сборки FFmpeg молча игнорируют.
            self.assertIn("disc=2/3", commands[0])
            self.assertIn("track=2/3", commands[0])
            self.assertEqual(
                commands[0][commands[0].index("-metadata:s:a:0") + 1],
                "language=eng",
            )
            self.assertNotIn("disk=2/3", commands[0])
            self.assertNotIn("disc=Disk 2 of 3", commands[0])
            self.assertEqual(
                commands[0][commands[0].index("-map_chapters") + 1], "1"
            )

    def test_m4b_export_copies_jpeg_but_converts_png_cover(self):
        """Финальная команда M4B не должна повторно сжимать готовый JPEG."""
        for suffix, signature, expected_codec in (
            ("jpg", b"\xff\xd8\xff\xe0jpeg", "copy"),
            ("png", b"\x89PNG\r\n\x1a\npng", "mjpeg"),
        ):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                source = root / "chapter.ogg"
                source.write_bytes(b"audio")
                cover = root / f"cover.{suffix}"
                cover.write_bytes(signature)
                output = root / "book.m4b"
                commands = []

                def fake_merge(_sources, destination, **_kwargs):
                    Path(destination).write_bytes(b"stage m4a")

                def fake_checked(command, *, out_path=None, **_kwargs):
                    commands.append(tuple(command))
                    Path(out_path).write_bytes(b"m4b")

                with mock.patch.object(
                    studio, "_export_merged_audio_ffmpeg", side_effect=fake_merge
                ), mock.patch.object(
                    studio, "_run_ffmpeg_checked", side_effect=fake_checked
                ):
                    studio._export_m4b_ffmpeg(
                        (source,),
                        output,
                        chapters=({"title": "Chapter", "duration": 1},),
                        cover=cover,
                    )

                command = commands[0]
                self.assertEqual(
                    command[command.index("-c:v") + 1], expected_codec
                )
                self.assertEqual(
                    command[command.index("-disposition:v:0") + 1],
                    "attached_pic",
                )

    def test_source_m4b_duration_probe_reports_progress(self):
        """Второй проход по кэшу не оставляет прогресс в состоянии ожидания."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.ogg"
            second = root / "two.ogg"
            first.write_bytes(b"audio")
            second.write_bytes(b"audio")
            records = (
                {"file_ids": ("chapter-1",)},
                {"file_ids": ("chapter-2",)},
            )
            events = []
            with mock.patch.object(
                studio, "_probe_audio_duration", side_effect=(12.5, 7.25)
            ):
                durations = studio._measure_source_m4b_chapter_durations(
                    records,
                    {
                        "chapter-1": (first,),
                        "chapter-2": (second,),
                    },
                    progress_callback=lambda current, total, text: events.append(
                        (current, total, text)
                    ),
                )

            self.assertEqual(durations, {"chapter-1": 12.5, "chapter-2": 7.25})
            self.assertEqual(events[0][0], 0)
            self.assertTrue(any(current < 0 for current, _total, _text in events))
            self.assertEqual(events[-1][:2], (2, 2))

    def test_source_m4b_duration_probe_memoizes_shared_pause_files(self):
        """Один физический фрагмент кэша не проверяется повторно."""
        with tempfile.TemporaryDirectory() as tempdir:
            shared = Path(tempdir) / "shared-silence.ogg"
            shared.write_bytes(b"audio")
            records = (
                {"file_ids": ("chapter-1",)},
                {"file_ids": ("chapter-2",)},
            )
            with mock.patch.object(
                studio, "_probe_audio_duration", return_value=2.5
            ) as probe:
                durations = studio._measure_source_m4b_chapter_durations(
                    records,
                    {
                        "chapter-1": (shared,),
                        "chapter-2": (shared,),
                    },
                )

            self.assertEqual(durations, {"chapter-1": 2.5, "chapter-2": 2.5})
            probe.assert_called_once_with(shared)

    def test_source_m4b_duration_probe_reuses_background_results_and_path_cache(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "first.ogg"
            second = root / "second.ogg"
            first.write_bytes(b"audio")
            second.write_bytes(b"audio")
            path_cache = {}
            with mock.patch.object(
                studio, "_probe_audio_duration", side_effect=(4.0, 6.0)
            ) as probe:
                background = studio._measure_source_m4b_chapter_durations(
                    ({"file_ids": ("one",)},),
                    {"one": (first,)},
                    duration_by_path=path_cache,
                )
                final = studio._measure_source_m4b_chapter_durations(
                    (
                        {"file_ids": ("one",)},
                        {"file_ids": ("two",)},
                    ),
                    {"one": (first,), "two": (second,)},
                    known_durations=background,
                    duration_by_path=path_cache,
                )

            self.assertEqual(final, {"one": 4.0, "two": 6.0})
            self.assertEqual(probe.call_count, 2)

    def test_source_m4b_duration_probe_retries_invalid_background_value(self):
        with tempfile.TemporaryDirectory() as tempdir:
            fragment = Path(tempdir) / "chapter.ogg"
            fragment.write_bytes(b"audio")
            path_cache = {}
            records = ({"file_ids": ("chapter",)},)
            audio = {"chapter": (fragment,)}

            with mock.patch.object(
                studio, "_probe_audio_duration", side_effect=(0.0, 5.0)
            ) as probe:
                with self.assertRaisesRegex(
                    ValueError,
                    "Не удалось определить фактическую длительность",
                ):
                    studio._measure_source_m4b_chapter_durations(
                        records,
                        audio,
                        duration_by_path=path_cache,
                    )
                self.assertEqual(path_cache, {})
                durations = studio._measure_source_m4b_chapter_durations(
                    records,
                    audio,
                    duration_by_path=path_cache,
                )

            self.assertEqual(durations, {"chapter": 5.0})
            self.assertEqual(probe.call_count, 2)

    def test_source_book_name_is_default_album_for_folder_synthesis(self):
        tags = studio._source_synthesis_tag_values(
            {
                "apply_output_tags": True,
                "source_book_name": "Название книги",
                "tag_album": "",
            },
            "chapter.mp3",
        )
        self.assertEqual(tags["album"], "Название книги")

        explicit = studio._source_synthesis_tag_values(
            {
                "apply_output_tags": True,
                "source_book_name": "Название книги",
                "tag_album": "Пользовательский альбом",
            },
            "chapter.mp3",
        )
        self.assertEqual(explicit["album"], "Пользовательский альбом")

    def test_target_chain_skip_existing_does_not_reencode(self):
        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "fragment.ogg"
            source.write_bytes(b"canonical audio")
            output = root / "already.mp3"
            output.write_bytes(b"encoded")
            target = studio.normalize_output_target(
                {"format": "mp3", "bitrate": "128k"}
            )
            processor = Processor()
            events = []
            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg"
            ) as export:
                result = studio.run_source_synthesis_target_chain(
                    processor,
                    (source,),
                    [{"target_index": 0, "target": target, "path": output}],
                    {},
                    skip_existing=True,
                    status_callback=lambda fmt, path, status: events.append(
                        (fmt, status)
                    ),
                )

            export.assert_not_called()
            self.assertEqual(result["status"], "success")
            self.assertEqual(events, [("mp3", "success")])

    def test_process_queue_collects_once_then_fans_out_target(self):
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}
                self.process_calls = 0
                self.flush_calls = 0

            def process_text_file(self, filepath, **kwargs):
                self.process_calls += 1
                self.assert_return_audio = kwargs.get("return_audio_files")
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def flush_cache(self):
                self.flush_calls += 1

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = source.with_suffix(".ogg")
            fragment.write_bytes(b"canonical audio")
            output = root / "opus" / "chapter.opus"
            target = studio.normalize_output_target(
                {"format": "opus", "bitrate": "48k"}
            )
            record = {
                "kind": "file",
                "target_index": 0,
                "target": target,
                "item_id": "chapter-id",
                "path": output,
                "source_path": source,
                "source_paths": (source,),
                "file_ids": ("chapter-id",),
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)

            def fake_export(audio_files, output_path, **kwargs):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"encoded")

            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
            }
            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fake_export
            ):
                app.process_queue(processor, ("chapter-id",), config, False)

            self.assertEqual(processor.process_calls, 1)
            self.assertTrue(processor.assert_return_audio)
            self.assertTrue(output.is_file())
            self.assertIn(
                ("chapter-id", "encoding", "opus"), status_events
            )
            self.assertIn(("chapter-id", "success"), status_events)

    def test_legacy_single_output_defers_cache_eviction_until_final_flush(self):
        """Одиночный вывод не теряет фрагменты до фоновой склейки FFmpeg."""
        events = []

        class Processor:
            def __init__(self):
                self.cfg = {"use_cache": True}
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}

            def defer_cache_eviction(self):
                events.append("defer")

            def process_text_file(self, _filepath, **_kwargs):
                events.append("process")

            def resume_cache_eviction(self):
                events.append("resume")

            def flush_cache(self):
                events.append("flush")

            def cleanup_transient_audio_files(self):
                events.append("cleanup")

            def _save_processing_statuses(self):
                events.append("statuses")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)

            app.process_queue(
                Processor(),
                ("chapter-id",),
                {
                    "input_dir": str(root),
                    "output_dir": str(root / "output"),
                    "output_format": "mp3",
                    "include_subdirs": False,
                },
                False,
            )

        self.assertEqual(
            events,
            ["defer", "process", "resume", "flush", "cleanup", "statuses"],
        )

    def test_m4b_only_queue_keeps_source_rows_active_until_post_stage(self):
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            output = root / "book.m4b"
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "m4b-group",
                "path": output,
                "file_ids": ("one-id", "two-id"),
                "source_paths": (first, second),
                "group_name": "Book",
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"one-id": first, "two-id": second},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "export_m4b_bitrate": "64k",
                "source_m4b_auto_split_long": False,
            }

            def fake_m4b(audio_files, output_path, **kwargs):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app.process_queue(processor, ("one-id", "two-id"), config, False)

            self.assertTrue(output.is_file())
            self.assertNotIn(("one-id", "empty"), status_events)
            self.assertNotIn(("two-id", "empty"), status_events)
            self.assertIn(("one-id", "success"), status_events)
            self.assertIn(("two-id", "success"), status_events)

    def test_cache_off_reflow_reuses_temporary_audio_then_cleans_it(self):
        """Второй проход M4B не вызывает синтез повторно и удаляет временные OGG в конце."""

        class Processor:
            def __init__(self, root):
                self.cfg = {"use_cache": False}
                self.root = root
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}
                self.process_calls = 0
                self.transient_paths = []
                self.cleanup_called = False

            def process_text_file(self, filepath, **kwargs):
                self.process_calls += 1
                self.assert_return_audio = kwargs.get("return_audio_files")
                fragment = self.root / f"synth_{self.process_calls}.ogg"
                fragment.write_bytes(b"temporary opus")
                self.transient_paths.append(fragment)
                return {"status": "success", "audio_files": (fragment,)}

            def defer_cache_eviction(self):
                raise AssertionError(
                    "выключенный постоянный кэш не должен запускать TTL/LRU"
                )

            def flush_cache(self):
                pass

            def cleanup_transient_audio_files(self):
                self.cleanup_called = True
                for path in self.transient_paths:
                    path.unlink(missing_ok=True)

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            output = root / "book.m4b"
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "m4b-group",
                "path": output,
                "file_ids": ("one-id", "two-id"),
                "source_paths": (first, second),
                "group_name": "Book",
            }
            processor = Processor(root)
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            app._stage_source_m4b_ui_plan = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "use_cache": False,
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_m4b_reflow_actual_duration": True,
                "source_m4b_reflow_limit_seconds": 3600,
                "source_m4b_force_rebuild": True,
                "source_m4b_auto_split_long": True,
            }
            observed_paths = []

            def fake_probe(path):
                path = Path(path)
                self.assertTrue(path.is_file())
                observed_paths.append(path)
                return 1.0

            def fake_m4b(_processor, m4b_record, audio_map, _config, **_kwargs):
                for file_id in m4b_record["file_ids"]:
                    self.assertTrue(all(path.is_file() for path in audio_map[file_id]))
                Path(m4b_record["path"]).write_bytes(b"m4b")
                return {
                    "status": "success",
                    "path": m4b_record["path"],
                    "format": "m4b",
                }

            with mock.patch.object(
                studio, "_probe_audio_duration", side_effect=fake_probe
            ), mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(
                    processor,
                    ("one-id", "two-id"),
                    config,
                    False,
                )

            self.assertEqual(processor.process_calls, 2)
            self.assertTrue(processor.assert_return_audio)
            self.assertEqual(set(observed_paths), set(processor.transient_paths))
            self.assertTrue(output.is_file())
            self.assertTrue(processor.cleanup_called)
            self.assertTrue(all(not path.exists() for path in processor.transient_paths))

    def test_fixed_m4b_group_starts_before_next_group_finishes_synthesis(self):
        """Готовая фиксированная часть собирается параллельно следующему TXT."""

        first_m4b_started = threading.Event()
        second_collector_saw_pipeline = []

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                path = Path(filepath)
                if path.name == "two.txt":
                    second_collector_saw_pipeline.append(
                        first_m4b_started.wait(timeout=1)
                    )
                fragment = path.with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            records = tuple(
                {
                    "kind": "m4b",
                    "target_index": 0,
                    "target": target,
                    "item_id": f"group-{number}",
                    "path": root / f"part-{number}.m4b",
                    "file_ids": (file_id,),
                    "source_paths": (source,),
                    "group_name": f"Часть {number}",
                    "group_index": number,
                    "parts": 2,
                }
                for number, file_id, source in (
                    (1, "one-id", first),
                    (2, "two-id", second),
                )
            )
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": records,
                "source_virtual_group_ids": ("group-1", "group-2"),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            planned_numbers = []

            def fake_m4b(_processor, record, _audio, _config, **_kwargs):
                planned_numbers.append(
                    (record.get("group_index"), record.get("parts"))
                )
                if record["item_id"] == "group-1":
                    first_m4b_started.set()
                Path(record["path"]).write_bytes(b"m4b")
                return {
                    "status": "success",
                    "path": record["path"],
                    "format": "m4b",
                }

            with mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(
                    processor,
                    ("one-id", "two-id"),
                    config,
                    False,
                )

            self.assertEqual(second_collector_saw_pipeline, [True])
            self.assertEqual(sorted(planned_numbers), [(1, 2), (2, 2)])
            self.assertTrue(all(Path(record["path"]).is_file() for record in records))

    def test_m4b_target_duration_limit_prepares_aac_before_final_plan(self):
        """Лимит M4B допускает ранний AAC, но итог публикуется после проверки."""

        m4b_started = threading.Event()
        duration_started = threading.Event()
        second_collector_saw_m4b = []
        second_collector_saw_duration = []

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                path = Path(filepath)
                if path.name == "two.txt":
                    second_collector_saw_m4b.append(
                        m4b_started.wait(timeout=1)
                    )
                    second_collector_saw_duration.append(
                        duration_started.wait(timeout=1)
                    )
                fragment = path.with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "max_duration_seconds": 60,
                }
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "group-1",
                "path": root / "part-1.m4b",
                "file_ids": ("one-id",),
                "source_paths": (first,),
                "group_name": "Часть 1",
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_virtual_group_ids": ("group-1",),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            final_used_stage = []

            def fake_m4b(_processor, current_record, _audio, _config, **kwargs):
                stage_path = kwargs.get("audio_stage_path")
                if stage_path is not None:
                    stage_path = Path(stage_path)
                    stage_path.parent.mkdir(parents=True, exist_ok=True)
                    stage_path.write_bytes(b"aac-stage")
                    m4b_started.set()
                    return {
                        "status": "success",
                        "path": current_record["path"],
                        "format": "m4b",
                        "stage_path": stage_path,
                    }
                prepared = kwargs.get("preencoded_audio")
                final_used_stage.append(
                    bool(prepared and Path(prepared).is_file())
                )
                Path(current_record["path"]).write_bytes(b"m4b")
                return {
                    "status": "success",
                    "path": current_record["path"],
                    "format": "m4b",
                }

            reflow_result = {
                "records": (record,),
                "durations": {"one-id": 10.0},
                "diagnostics": (),
                "changed": False,
            }

            def fake_probe(_path):
                duration_started.set()
                return 10.0

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                return_value=reflow_result,
            ) as reflow_mock, mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ), mock.patch.object(
                studio,
                "_probe_audio_duration",
                side_effect=fake_probe,
            ):
                app.process_queue(
                    processor,
                    ("one-id", "two-id"),
                    config,
                    False,
                )

            self.assertEqual(second_collector_saw_m4b, [True])
            self.assertEqual(second_collector_saw_duration, [True])
            reflow_mock.assert_called_once()
            self.assertEqual(
                reflow_mock.call_args.kwargs["chapter_duration_overrides"],
                {"one-id": 10.0},
            )
            self.assertTrue(m4b_started.is_set())
            self.assertEqual(final_used_stage, [True])

    def test_preencoded_m4b_with_known_durations_does_not_remerge_chapters(self):
        """Готовая AAC-основа не требует повторной склейки фрагментов глав."""

        class Processor:
            is_stopped = False
            encode_semaphore = None

            def __init__(self):
                self.processing_statuses_ram = {}
                self._run_ffmpeg_concat = mock.Mock(
                    side_effect=AssertionError(
                        "фрагменты главы не должны склеиваться повторно"
                    )
                )

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_one = root / "Глава 1.txt"
            source_two = root / "Глава 2.txt"
            source_one.write_text("Один", encoding="utf-8")
            source_two.write_text("Два", encoding="utf-8")
            fragments = []
            for name in ("one-a.ogg", "one-b.ogg", "two-a.ogg", "two-b.ogg"):
                fragment = root / name
                fragment.write_bytes(b"canonical audio")
                fragments.append(fragment)
            prepared = root / "prepared.m4a"
            prepared.write_bytes(b"aac stage")
            output = root / "book.m4b"
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "group",
                "path": output,
                "file_ids": ("one", "two"),
                "source_paths": (source_one, source_two),
                "group_name": "Книга",
            }
            processor = Processor()

            with mock.patch.object(studio, "_export_m4b_ffmpeg") as export:
                result = studio.run_source_synthesis_m4b_target(
                    processor,
                    record,
                    {
                        "one": tuple(fragments[:2]),
                        "two": tuple(fragments[2:]),
                    },
                    {"apply_output_tags": False},
                    chapter_duration_overrides={"one": 1.25, "two": 2.5},
                    preencoded_audio=prepared,
                )

            self.assertEqual(result["status"], "success")
            processor._run_ffmpeg_concat.assert_not_called()
            export.assert_called_once()
            self.assertEqual(
                Path(export.call_args.kwargs["preencoded_audio"]), prepared
            )
            self.assertEqual(
                [item["duration"] for item in export.call_args.kwargs["chapters"]],
                [1.25, 2.5],
            )

    def test_corrupt_m4b_draft_falls_back_without_failing_queue(self):
        """После ошибки AAC-основы обычная сборка успешно завершает запуск."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            output = root / "book.m4b"
            target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "max_duration_seconds": 60,
                }
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "group",
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Книга",
            }
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_virtual_group_ids": ("group",),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }
            calls = []

            def fake_m4b(_processor, current_record, _audio, _config, **kwargs):
                stage_path = kwargs.get("audio_stage_path")
                if stage_path is not None:
                    calls.append("stage")
                    stage_path = Path(stage_path)
                    stage_path.parent.mkdir(parents=True, exist_ok=True)
                    stage_path.write_bytes(b"not a valid AAC stream")
                    return {
                        "status": "success",
                        "path": current_record["path"],
                        "format": "m4b",
                        "stage_path": stage_path,
                    }
                if kwargs.get("preencoded_audio") is not None:
                    calls.append("prepared-error")
                    return {
                        "status": "error",
                        "path": current_record["path"],
                        "format": "m4b",
                        "error": "corrupt draft",
                    }
                calls.append("fallback")
                Path(current_record["path"]).write_bytes(b"rebuilt m4b")
                return {
                    "status": "success",
                    "path": current_record["path"],
                    "format": "m4b",
                }

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                return_value={
                    "records": (record,),
                    "durations": {"chapter-id": 10.0},
                    "diagnostics": (),
                    "changed": False,
                },
            ), mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(Processor(), ("chapter-id",), config, False)

            self.assertEqual(calls, ["stage", "prepared-error", "fallback"])
            self.assertEqual(output.read_bytes(), b"rebuilt m4b")
            self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_partial_m4b_resume_keeps_audio_for_duration_limited_target(self):
        """Частичный повторный запуск измеряет главы всех M4B-целей."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}
                self.processed = []

            def process_text_file(self, filepath, **_kwargs):
                path = Path(filepath)
                self.processed.append(path.name)
                fragment = path.with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            fixed_target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            limited_target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "max_duration_seconds": 60,
                }
            )
            existing_output = root / "existing.m4b"
            existing_output.write_bytes(b"old m4b")
            missing_output = root / "missing.m4b"
            records = (
                {
                    "kind": "m4b",
                    "target_index": 0,
                    "target": fixed_target,
                    "item_id": "group-1",
                    "path": existing_output,
                    "file_ids": ("one-id",),
                    "source_paths": (first,),
                },
                {
                    "kind": "m4b",
                    "target_index": 1,
                    "target": limited_target,
                    "item_id": "group-2",
                    "path": missing_output,
                    "file_ids": ("two-id",),
                    "source_paths": (second,),
                },
            )
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [fixed_target, limited_target],
                "source_target_records": records,
                "source_virtual_group_ids": ("group-1", "group-2"),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }
            captured_audio = {}

            def fake_reflow(current_records, audio_by_file_id, *_args, **_kwargs):
                captured_audio.update(audio_by_file_id)
                return {
                    "records": tuple(current_records),
                    "durations": {"one-id": 10.0, "two-id": 10.0},
                    "diagnostics": (),
                    "changed": False,
                }

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                side_effect=fake_reflow,
            ), mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                return_value={"status": "success", "format": "m4b"},
            ):
                app.process_queue(
                    processor,
                    ("one-id", "two-id"),
                    config,
                    True,
                )

            self.assertEqual(processor.processed, ["one.txt", "two.txt"])
            self.assertTrue(captured_audio["one-id"])
            self.assertTrue(captured_audio["two-id"])

    def test_m4b_group_status_waits_for_all_pipeline_targets(self):
        """Одна строка группы получает итог только после всех M4B-целей."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "one.txt"
            source.write_text("Один", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            records = tuple(
                {
                    "kind": "m4b",
                    "target_index": target_index,
                    "target": target,
                    "item_id": "group-1",
                    "path": root / f"part-{target_index + 1}.m4b",
                    "file_ids": ("one-id",),
                    "source_paths": (source,),
                    "group_name": "Часть 1",
                }
                for target_index in range(2)
            )
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target, target],
                "source_target_records": records,
                "source_virtual_group_ids": ("group-1",),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }
            calls = []

            def fake_m4b(_processor, record, _audio, _config, **kwargs):
                calls.append(record["target_index"])
                kwargs["status_callback"]("m4b", record["path"], "encoding")
                if len(calls) == 2:
                    self.assertNotIn(("group-1", "success"), status_events)
                Path(record["path"]).write_bytes(b"m4b")
                return {
                    "status": "success" if len(calls) == 1 else "warning",
                    "path": record["path"],
                    "format": "m4b",
                }

            with mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(processor, ("one-id",), config, False)

            self.assertEqual(calls, [0, 1])
            self.assertNotIn(("group-1", "success"), status_events)
            self.assertIn(("group-1", "warning"), status_events)

    def test_ungrouped_m4b_pipeline_uses_activity_status_not_first_file(self):
        """Сборка M4B без виртуальной группы не помечает первую главу."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            output = root / "book.m4b"
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "source_m4b:1",
                "path": output,
                "file_ids": ("one-id", "two-id"),
                "source_paths": (first, second),
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {
                "one-id": first,
                "two-id": second,
            }
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_virtual_group_ids": (),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            def fake_m4b(_processor, current_record, _audio, _config, **kwargs):
                kwargs["status_callback"](
                    "m4b", current_record["path"], "encoding"
                )
                Path(current_record["path"]).write_bytes(b"m4b")
                return {"status": "success", "format": "m4b"}

            with mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(processor, ("one-id", "two-id"), config, False)

            self.assertFalse(
                any(
                    event[:2] == ("one-id", "encoding")
                    for event in status_events
                )
            )
            activity_calls = [
                call
                for call in app.update_progress_ui.call_args_list
                if call.args and call.args[0] == studio.PROGRESS_STATUS_ONLY
            ]
            self.assertTrue(activity_calls)
            self.assertIn("book.m4b", str(activity_calls[-1].args[1]))
            self.assertIn(("one-id", "success"), status_events)
            self.assertIn(("two-id", "success"), status_events)

    def test_ungrouped_m4b_final_stage_uses_activity_status_not_first_file(self):
        """Заключительная сборка M4B без группы также не помечает первую главу."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "one.txt"
            source.write_text("Один", encoding="utf-8")
            target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "max_duration_seconds": 60,
                }
            )
            output = root / "book.m4b"
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "source_m4b:1",
                "path": output,
                "file_ids": ("one-id",),
                "source_paths": (source,),
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"one-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_virtual_group_ids": (),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            def fake_reflow(current_records, *_args, **_kwargs):
                return {
                    "records": tuple(current_records),
                    "durations": {"one-id": 10.0},
                    "diagnostics": (),
                    "changed": False,
                }

            def fake_m4b(_processor, current_record, _audio, _config, **kwargs):
                kwargs["status_callback"](
                    "m4b", current_record["path"], "encoding"
                )
                stage_path = kwargs.get("audio_stage_path")
                if stage_path:
                    Path(stage_path).write_bytes(b"aac stage")
                    return {
                        "status": "success",
                        "format": "m4b",
                        "stage_path": stage_path,
                    }
                Path(current_record["path"]).write_bytes(b"m4b")
                return {"status": "success", "format": "m4b"}

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                side_effect=fake_reflow,
            ), mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(processor, ("one-id",), config, False)

            self.assertFalse(
                any(
                    event[:2] == ("one-id", "encoding")
                    for event in status_events
                )
            )
            activity_calls = [
                call
                for call in app.update_progress_ui.call_args_list
                if call.args and call.args[0] == studio.PROGRESS_STATUS_ONLY
            ]
            self.assertTrue(activity_calls)
            self.assertIn("book.m4b", str(activity_calls[-1].args[1]))
            self.assertIn(("one-id", "success"), status_events)

    def test_disjoint_fixed_m4b_groups_can_build_in_parallel(self):
        """Непересекающиеся части используют доступный пул сборок FFmpeg."""

        first_started = threading.Event()
        second_started = threading.Event()
        overlap_observed = []

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = threading.Semaphore(2)
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = []
            for name in ("one", "two"):
                source = root / f"{name}.txt"
                source.write_text(name, encoding="utf-8")
                sources.append(source)
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            records = tuple(
                {
                    "kind": "m4b",
                    "target_index": 0,
                    "target": target,
                    "item_id": f"group-{number}",
                    "path": root / f"part-{number}.m4b",
                    "file_ids": (file_id,),
                    "source_paths": (source,),
                }
                for number, file_id, source in (
                    (1, "one-id", sources[0]),
                    (2, "two-id", sources[1]),
                )
            )
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {
                "one-id": sources[0],
                "two-id": sources[1],
            }
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": records,
                "source_virtual_group_ids": ("group-1", "group-2"),
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
                "max_parallel_encodes": 2,
            }

            def fake_m4b(_processor, record, _audio, _config, **_kwargs):
                if record["item_id"] == "group-1":
                    first_started.set()
                    overlap_observed.append(second_started.wait(timeout=1))
                else:
                    self.assertTrue(first_started.wait(timeout=1))
                    second_started.set()
                Path(record["path"]).write_bytes(b"m4b")
                return {
                    "status": "success",
                    "path": record["path"],
                    "format": "m4b",
                }

            with mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                side_effect=fake_m4b,
            ):
                app.process_queue(
                    processor,
                    ("one-id", "two-id"),
                    config,
                    False,
                )

            self.assertEqual(overlap_observed, [True])

    def test_failed_m4b_post_stage_does_not_report_completed_progress(self):
        """Ошибка цели M4B не должна публиковать вводящий в заблуждение кадр 100 %."""
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            output = root / "book.m4b"
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "m4b-group",
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Book",
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                # Проверяем заключительный этап напрямую; для этого контракта
                # прогресса переразбиение по длительности и ``ffprobe`` не нужны.
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=RuntimeError("boom")
            ):
                app.process_queue(processor, ("chapter-id",), config, False)

            # При отсутствии виртуальной строки M4B сообщает активность в общей
            # строке, а не помечает первую главу. Ошибка всё равно не публикует
            # успешный процент завершения.
            activity_calls = [
                call
                for call in app.update_progress_ui.call_args_list
                if call.args and call.args[0] == studio.PROGRESS_STATUS_ONLY
            ]
            self.assertTrue(activity_calls)
            self.assertIn("book.m4b", str(activity_calls[-1].args[1]))
            self.assertTrue(app.finish_processing.call_args.args[-1])

    def test_changed_source_plan_forces_m4b_rebuild_with_skip_existing(self):
        """Изменённая виртуальная группа не переиспользует старый M4B."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}
                self.collect_calls = 0

            def process_text_file(self, filepath, **kwargs):
                self.collect_calls += 1
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = root / "chapter.ogg"
            fragment.write_bytes(b"canonical audio")
            output = root / "book.m4b"
            output.write_bytes(b"old plan")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b", "target_index": 0, "target": target,
                "item_id": "group", "path": output,
                "file_ids": ("chapter-id",), "source_paths": (source,),
            }
            processor = Processor()
            processor.processing_statuses_ram[str(output.resolve())] = "success"
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root), "output_dir": str(root / "output"),
                "output_format": "mp3", "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_m4b_force_rebuild": True,
                "source_m4b_auto_split_long": False,
            }

            def fake_m4b(_audio_files, output_path, **_kwargs):
                Path(output_path).write_bytes(b"new plan")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ) as export:
                app.process_queue(processor, ("chapter-id",), config, True)

            export.assert_called_once()
            self.assertEqual(processor.collect_calls, 1)
            self.assertEqual(output.read_bytes(), b"new plan")

    def test_source_m4b_auto_guard_does_not_force_resumable_output(self):
        """Включённая по умолчанию защита M4B не отключает ``skip_existing``."""

        class Processor:
            def __init__(self, output):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {
                    str(Path(output).resolve()): "success"
                }
                self.collect_calls = 0

            def process_text_file(self, _filepath, **_kwargs):
                self.collect_calls += 1
                raise AssertionError("готовый M4B не должен заново собирать кэш")

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            output = root / "book.m4b"
            output.write_bytes(b"already-built")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "book",
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Book",
            }
            processor = Processor(output)
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                # Это обычное значение по умолчанию и должно быть защитной
                # политикой, а не безусловным переключателем пересборки.
                "source_m4b_auto_split_long": True,
                "source_m4b_reflow_actual_duration": False,
            }

            with mock.patch.object(
                studio,
                "_measure_source_m4b_chapter_durations",
                side_effect=AssertionError("resumable M4B must not be probed"),
            ), mock.patch.object(
                studio,
                "_export_m4b_ffmpeg",
                side_effect=AssertionError("resumable M4B must not be encoded"),
            ):
                app.process_queue(processor, ("chapter-id",), config, True)

            self.assertEqual(processor.collect_calls, 0)
            self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_source_m4b_explicit_reflow_collects_even_existing_outputs(self):
        """Явная перестройка не должна быть подавлена ``skip_existing``."""

        class Processor:
            def __init__(self, output):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {
                    str(Path(output).resolve()): "success"
                }
                self.collect_calls = 0

            def process_text_file(self, filepath, **kwargs):
                self.collect_calls += 1
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical")
                self.assert_return_audio = kwargs.get("return_audio_files")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            output = root / "book.m4b"
            output.write_bytes(b"old")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "book",
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Book",
            }
            processor = Processor(output)
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_m4b_auto_split_long": True,
                "source_m4b_reflow_actual_duration": True,
                "source_m4b_reflow_limit_seconds": 3600,
                "source_m4b_force_rebuild": True,
            }
            captured = {}

            def fake_reflow(records, audio_by_file_id, *_args, **kwargs):
                captured["audio"] = dict(audio_by_file_id)
                captured["duration_overrides"] = dict(
                    kwargs.get("chapter_duration_overrides") or {}
                )
                return {
                    "records": tuple(records),
                    "durations": {"chapter-id": 12.0},
                    "diagnostics": (),
                    "changed": False,
                }

            def fake_m4b(_audio, path, **_kwargs):
                Path(path).write_bytes(b"new")

            with mock.patch.object(
                studio, "reflow_source_m4b_target_records", side_effect=fake_reflow
            ), mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ), mock.patch.object(
                studio, "_probe_audio_duration", return_value=12.0
            ):
                app.process_queue(processor, ("chapter-id",), config, True)

            self.assertEqual(processor.collect_calls, 1)
            self.assertEqual(tuple(captured["audio"]), ("chapter-id",))
            self.assertEqual(
                captured["duration_overrides"], {"chapter-id": 12.0}
            )
            self.assertEqual(output.read_bytes(), b"new")

    def test_changed_automatic_m4b_reflow_forces_rebuilt_paths(self):
        """Изменившийся автоматический план не должен пропустить старый файл по имени."""

        class Processor:
            def __init__(self, ready_output):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {
                    str(Path(ready_output).resolve()): "success"
                }
                self.collect_calls = []

            def process_text_file(self, filepath, **kwargs):
                self.collect_calls.append(Path(filepath).name)
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical")
                return {
                    "status": "success",
                    "audio_files": (fragment,),
                }

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = []
            for number in range(1, 4):
                source = root / f"{number}.txt"
                source.write_text(str(number), encoding="utf-8")
                sources.append(source)
            output_dir = root / "m4b"
            old_one = output_dir / "Book Часть 1.m4b"
            old_two = output_dir / "Book Часть 2.m4b"
            old_one.parent.mkdir(parents=True)
            old_one.write_bytes(b"old-1")
            # Второго старого тома нет, поэтому автоматический проход по
            # длительности собирает полную карту глав.
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            old_records = (
                {
                    "kind": "m4b", "target_index": 0, "target": target,
                    "item_id": "part-1", "path": old_one,
                    "file_ids": ("one", "two"),
                    "source_paths": (sources[0], sources[1]),
                    "group_name": "Book",
                },
                {
                    "kind": "m4b", "target_index": 0, "target": target,
                    "item_id": "part-2", "path": old_two,
                    "file_ids": ("three",),
                    "source_paths": (sources[2],),
                    "group_name": "Book",
                },
            )
            # Переразбиение по длительности переносит вторую главу в путь,
            # который раньше обозначал второй том. Поэтому все три записи,
            # включая уже существующие пути, должны обойти ``skip_existing``.
            new_records = tuple(
                dict(record, file_ids=(file_id,), source_paths=(source,))
                for record, file_id, source in (
                    (old_records[0], "one", sources[0]),
                    (old_records[1], "two", sources[1]),
                    (dict(old_records[1], path=output_dir / "Book Часть 3.m4b"), "three", sources[2]),
                )
            )
            processor = Processor(old_one)
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {
                "one": sources[0], "two": sources[1], "three": sources[2]
            }
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root), "output_dir": str(output_dir),
                "output_format": "mp3", "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": old_records,
                "source_m4b_auto_split_long": True,
                "source_m4b_reflow_actual_duration": False,
            }
            skip_values = []
            staged_groups = []
            prepared_by_group = {}

            def fake_m4b(_processor, record, _audio, _config, **kwargs):
                stage_path = kwargs.get("audio_stage_path")
                if stage_path is not None:
                    stage_path = Path(stage_path)
                    stage_path.parent.mkdir(parents=True, exist_ok=True)
                    stage_path.write_bytes(b"aac-stage")
                    staged_groups.append(tuple(record["file_ids"]))
                    return {
                        "status": "success",
                        "path": record["path"],
                        "format": "m4b",
                        "stage_path": stage_path,
                    }
                skip_values.append(bool(kwargs["skip_existing"]))
                prepared_by_group[tuple(record["file_ids"])] = bool(
                    kwargs.get("preencoded_audio")
                    and Path(kwargs["preencoded_audio"]).is_file()
                )
                Path(record["path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(record["path"]).write_bytes(b"rebuilt")
                return {"status": "success", "path": record["path"], "format": "m4b"}

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                return_value={
                    "records": new_records,
                    "durations": {"one": 10.0, "two": 20.0, "three": 30.0},
                    "diagnostics": (),
                    "changed": True,
                },
            ), mock.patch.object(
                studio, "run_source_synthesis_m4b_target", side_effect=fake_m4b
            ):
                app.process_queue(processor, ("one", "two", "three"), config, True)

            self.assertEqual(processor.collect_calls, ["1.txt", "2.txt", "3.txt"])
            # Существующий первый том не готовится заранее. После измерения
            # изменившиеся границы всё равно требуют его новой сборки ниже.
            self.assertEqual(staged_groups, [("three",)])
            self.assertEqual(skip_values, [False, False, False])
            self.assertEqual(
                prepared_by_group,
                {
                    ("one",): False,
                    ("two",): False,
                    # Эта граница не изменилась: новый путь и метаданные можно
                    # применить при финальной перепаковке той же AAC-основы.
                    ("three",): True,
                },
            )
            self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_source_runtime_m4b_snapshot_is_reused_only_for_same_source_set(self):
        """Принятый план переразбиения не применяется к выбранной подвыборке."""
        app = object.__new__(studio.TTSApp)
        source = Path("/tmp/chapter.txt")
        target = studio.normalize_output_target({"format": "m4b", "bitrate": "64k"})
        record = {
            "kind": "m4b",
            "target_index": 0,
            "target": target,
            "path": Path("/tmp/book Часть 1.m4b"),
            "file_ids": ("one", "two"),
            "source_paths": (source, Path("/tmp/two.txt")),
        }
        app._source_runtime_m4b_records = (record,)
        app._source_plan_dirty = False
        planned = dict(record, path=Path("/tmp/old.m4b"))
        reused = app._reuse_source_runtime_m4b_records(
            (planned,),
            ("one", "two"),
            {"one": source, "two": Path("/tmp/two.txt")},
        )
        self.assertEqual(reused[0]["path"], record["path"])
        subset = app._reuse_source_runtime_m4b_records(
            (planned,),
            ("one",),
            {"one": source},
        )
        self.assertEqual(subset[0]["path"], planned["path"])

    def test_m4b_post_stage_updates_virtual_group_and_leaf_rows(self):
        """Оценочная группа не оставляет дочерние TXT в статусе ``processing``."""
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"canonical audio")
                return {"status": "success", "audio_files": (fragment,)}

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        class Tree:
            def exists(self, item_id):
                return item_id in {"m4b-group", "one-id", "two-id"}

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "one.txt"
            second = root / "two.txt"
            first.write_text("Один", encoding="utf-8")
            second.write_text("Два", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b", "target_index": 0, "target": target,
                "item_id": "m4b-group", "path": root / "book.m4b",
                "file_ids": ("one-id", "two-id"),
                "source_paths": (first, second),
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app.tree = Tree()
            app._source_path_by_id = {"one-id": first, "two-id": second}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root), "output_dir": str(root / "output"),
                "output_format": "mp3", "include_subdirs": False,
                "source_path_by_id": {"one-id": first, "two-id": second},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "export_m4b_bitrate": "64k",
                "source_virtual_group_ids": ("m4b-group",),
                "source_m4b_auto_split_long": False,
            }

            def fake_m4b(audio_files, output_path, **kwargs):
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app.process_queue(processor, ("one-id", "two-id"), config, False)

            self.assertTrue((root / "book.m4b").is_file())
            self.assertIn(("m4b-group", "success"), status_events)
            self.assertIn(("one-id", "success"), status_events)
            self.assertIn(("two-id", "success"), status_events)

    def test_collector_error_does_not_fan_out_partial_silence(self):
        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.encode_semaphore = None
                self.processing_statuses_ram = {}
                self.chain_called = False

            def process_text_file(self, filepath, **kwargs):
                fragment = Path(filepath).with_suffix(".ogg")
                fragment.write_bytes(b"fallback silence")
                return {
                    "status": "error",
                    "audio_files": (fragment,),
                    "file_has_errors": True,
                }

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            target = studio.normalize_output_target(
                {"format": "mp3", "bitrate": "128k"}
            )
            output = root / "chapter.mp3"
            record = {
                "kind": "file",
                "target_index": 0,
                "target": target,
                "item_id": "chapter-id",
                "path": output,
                "source_path": source,
                "source_paths": (source,),
                "file_ids": ("chapter-id",),
            }
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
            }
            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg"
            ) as export:
                app.process_queue(processor, ("chapter-id",), config, False)

            export.assert_not_called()
            self.assertFalse(output.exists())
            self.assertIn(("chapter-id", "error"), status_events)
            self.assertTrue(app.finish_processing.call_args.args[-1])

    def test_m4b_target_status_keeps_worst_result_for_duplicate_targets(self):
        class Processor:
            is_stopped = False
            active_threads = []
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = root / "chapter.ogg"
            fragment.write_bytes(b"canonical audio")
            m4b_a = root / "a.m4b"
            m4b_b = root / "b.m4b"
            target_a = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            target_b = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "96k"}
            )
            records = (
                {
                    "kind": "m4b", "target_index": 0, "target": target_a,
                    "item_id": "group", "path": m4b_a,
                    "file_ids": ("chapter-id",),
                    "source_paths": (source,),
                },
                {
                    "kind": "m4b", "target_index": 1, "target": target_b,
                    "item_id": "group", "path": m4b_b,
                    "file_ids": ("chapter-id",),
                    "source_paths": (source,),
                },
            )
            processor = Processor()
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root), "output_dir": str(root / "output"),
                "output_format": "mp3", "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [target_a, target_b],
                "source_target_records": records,
                "source_m4b_auto_split_long": False,
            }

            def fake_m4b(audio_files, output_path, **kwargs):
                if Path(output_path) == m4b_a:
                    raise RuntimeError("first target failed")
                Path(output_path).write_bytes(b"m4b")

            class CollectorProcessor(Processor):
                def process_text_file(self, filepath, **kwargs):
                    return {"status": "success", "audio_files": (fragment,)}

            processor = CollectorProcessor()
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app.process_queue(processor, ("chapter-id",), config, False)

            self.assertTrue(m4b_b.is_file())
            # Дублирующие цели M4B используют одну виртуальную строку, поэтому ошибка
            # должна иметь приоритет над последующим успехом, а не затираться.
            self.assertIn(("chapter-id", "error"), status_events)
            self.assertTrue(app.finish_processing.call_args.args[-1])

    def test_m4b_success_does_not_overwrite_failed_ordinary_target(self):
        """Ошибка MP3 остаётся видимой, даже если заключительный этап M4B успешен."""

        class Processor:
            is_stopped = False
            active_threads = []
            encode_semaphore = None
            processing_statuses_ram = {}

            def process_text_file(self, filepath, **kwargs):
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = root / "chapter.ogg"
            fragment.write_bytes(b"canonical audio")
            mp3_target = studio.normalize_output_target(
                {"format": "mp3", "bitrate": "128k"}
            )
            m4b_target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            records = (
                {
                    "kind": "file", "target_index": 0,
                    "target": mp3_target, "item_id": "chapter-id",
                    "path": root / "chapter.mp3",
                    "source_path": source, "source_paths": (source,),
                    "file_ids": ("chapter-id",),
                },
                {
                    "kind": "m4b", "target_index": 1,
                    "target": m4b_target, "item_id": "chapter-id",
                    "path": root / "chapter.m4b",
                    "source_paths": (source,),
                    "file_ids": ("chapter-id",),
                },
            )
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            status_events = []
            app.update_file_status = lambda *args: status_events.append(args)
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root), "output_dir": str(root / "output"),
                "output_format": "mp3", "include_subdirs": False,
                "source_path_by_id": {"chapter-id": source},
                "source_multi_output": True,
                "synthesis_targets": [mp3_target, m4b_target],
                "source_target_records": records,
                "synthesis_m4b_bitrate": "64k",
                "source_m4b_auto_split_long": False,
            }

            def fail_mp3(*_args, **_kwargs):
                raise RuntimeError("simulated MP3 failure")

            def succeed_m4b(_audio_files, output_path, **_kwargs):
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fail_mp3
            ), mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=succeed_m4b
            ):
                app.process_queue(Processor(), ("chapter-id",), config, False)

            self.assertTrue((root / "chapter.m4b").is_file())
            # Итоговый статус общей строки должен остаться ошибочным: успешный
            # этап M4B не должен скрывать ошибку MP3.
            chapter_statuses = [
                event[1]
                for event in status_events
                if event and event[0] == "chapter-id"
            ]
            self.assertEqual(chapter_statuses[-1], "error")
            self.assertTrue(app.finish_processing.call_args.args[-1])

    def test_changed_source_plan_forces_regular_target_rebuild(self):
        """Локальные правки плана не переиспользуют старый MP3/Opus."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}
                self.collect_calls = 0

            def process_text_file(self, filepath, **kwargs):
                self.collect_calls += 1
                self.assert_return_audio = kwargs.get("return_audio_files")
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            source.with_suffix(".ogg").write_bytes(b"canonical")
            output = root / "chapter.opus"
            output.write_bytes(b"old metadata")
            target = studio.normalize_output_target(
                {"format": "opus", "bitrate": "48k"}
            )
            record = {
                "kind": "file",
                "target_index": 0,
                "target": target,
                "item_id": "chapter-id",
                "path": output,
                "source_path": source,
                "source_paths": (source,),
                "file_ids": ("chapter-id",),
                "metadata_overrides": {"album": "Новый том"},
            }
            processor = Processor()
            processor.processing_statuses_ram[str(output.resolve())] = "success"
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_targets_force_rebuild": True,
            }

            with mock.patch.object(
                studio,
                "run_source_synthesis_target_chain",
                return_value={"status": "success"},
            ) as target_chain:
                app.process_queue(processor, ("chapter-id",), config, True)

            self.assertEqual(processor.collect_calls, 1)
            self.assertTrue(processor.assert_return_audio)
            target_chain.assert_called_once()
            self.assertFalse(target_chain.call_args.kwargs["skip_existing"])
            self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_changed_source_plan_forces_early_m4b_rebuild(self):
        """Фиксированная M4B-группа получает принудительную пересборку в конвейере."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            source.with_suffix(".ogg").write_bytes(b"canonical")
            output = root / "book.m4b"
            output.write_bytes(b"old plan")
            target = studio.normalize_output_target(
                {
                    "format": "m4b",
                    "bitrate": "64k",
                    "max_duration_seconds": 0,
                }
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "book",
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Книга",
            }
            processor = Processor()
            processor.processing_statuses_ram[str(output.resolve())] = "success"
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_m4b_force_rebuild": True,
                "source_m4b_auto_split_long": False,
                "source_m4b_reflow_actual_duration": False,
            }

            with mock.patch.object(
                studio,
                "run_source_synthesis_m4b_target",
                return_value={"status": "success", "path": output},
            ) as m4b_target:
                app.process_queue(processor, ("chapter-id",), config, True)

            m4b_target.assert_called_once()
            self.assertFalse(m4b_target.call_args.kwargs["skip_existing"])
            self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_rebuilt_regular_target_forces_existing_m4b_rebuild(self):
        """M4B не переиспользует старую книгу после нового вывода её главы."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}
                self.collect_calls = 0

            def process_text_file(self, filepath, **_kwargs):
                self.collect_calls += 1
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                key = str(Path(path).resolve())
                if status in {"warning", "error"}:
                    self.processing_statuses_ram[key] = status
                else:
                    self.processing_statuses_ram.pop(key, None)

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        for regular_state in ("missing", "error"):
            with self.subTest(regular_state=regular_state), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                source = root / "chapter.txt"
                source.write_text("Текст", encoding="utf-8")
                source.with_suffix(".ogg").write_bytes(b"canonical")
                regular_output = root / "chapter.opus"
                m4b_output = root / "book.m4b"
                m4b_output.write_bytes(b"old m4b")
                regular_target = studio.normalize_output_target(
                    {"format": "opus", "bitrate": "48k"}
                )
                m4b_target = studio.normalize_output_target(
                    {"format": "m4b", "bitrate": "64k"}
                )
                records = (
                    {
                        "kind": "file",
                        "target_index": 0,
                        "target": regular_target,
                        "item_id": "chapter-id",
                        "path": regular_output,
                        "source_path": source,
                        "source_paths": (source,),
                        "file_ids": ("chapter-id",),
                    },
                    {
                        "kind": "m4b",
                        "target_index": 1,
                        "target": m4b_target,
                        "item_id": "book",
                        "path": m4b_output,
                        "source_paths": (source,),
                        "file_ids": ("chapter-id",),
                    },
                )
                processor = Processor()
                if regular_state == "error":
                    regular_output.write_bytes(b"failed output")
                    processor.processing_statuses_ram[
                        str(regular_output.resolve())
                    ] = "error"
                app = object.__new__(studio.TTSApp)
                app._source_path_by_id = {"chapter-id": source}
                app.finish_processing = mock.Mock()
                app.update_total_ui = mock.Mock()
                app.update_progress_ui = mock.Mock()
                app.update_file_status = mock.Mock()
                app._post_to_ui = lambda callback, *args: callback(*args)
                config = {
                    "input_dir": str(root),
                    "output_dir": str(root / "output"),
                    "output_format": "mp3",
                    "include_subdirs": False,
                    "source_path_by_id": app._source_path_by_id,
                    "source_multi_output": True,
                    "synthesis_targets": [regular_target, m4b_target],
                    "source_target_records": records,
                    "source_m4b_auto_split_long": False,
                    "source_m4b_reflow_actual_duration": False,
                }
                m4b_skip_values = []

                def rebuild_regular(_processor, _audio, target_records, *_args, **_kwargs):
                    Path(target_records[0]["path"]).write_bytes(b"new opus")
                    return {"status": "success"}

                def rebuild_m4b(_processor, record, _audio, _config, **kwargs):
                    m4b_skip_values.append(bool(kwargs["skip_existing"]))
                    Path(record["path"]).write_bytes(b"new m4b")
                    return {
                        "status": "success",
                        "path": record["path"],
                        "format": "m4b",
                    }

                with mock.patch.object(
                    studio,
                    "run_source_synthesis_target_chain",
                    side_effect=rebuild_regular,
                ) as regular_chain, mock.patch.object(
                    studio,
                    "run_source_synthesis_m4b_target",
                    side_effect=rebuild_m4b,
                ) as m4b_chain:
                    app.process_queue(
                        processor, ("chapter-id",), config, True
                    )

                self.assertEqual(processor.collect_calls, 1)
                regular_chain.assert_called_once()
                m4b_chain.assert_called_once()
                self.assertEqual(m4b_skip_values, [False])
                self.assertEqual(regular_output.read_bytes(), b"new opus")
                self.assertEqual(m4b_output.read_bytes(), b"new m4b")
                self.assertFalse(app.finish_processing.call_args.args[-1])

    def test_regular_source_targets_use_bounded_worker_pool(self):
        """Число одновременных цепочек целей не превышает настройку кодирования."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = studio.normalize_output_target(
                {"format": "opus", "bitrate": "48k"}
            )
            path_by_id = {}
            records = []
            item_ids = []
            for index in range(6):
                item_id = f"chapter-{index}"
                source = root / f"chapter-{index}.txt"
                source.write_text(str(index), encoding="utf-8")
                source.with_suffix(".ogg").write_bytes(b"canonical")
                item_ids.append(item_id)
                path_by_id[item_id] = source
                records.append(
                    {
                        "kind": "file",
                        "target_index": 0,
                        "target": target,
                        "item_id": item_id,
                        "path": root / f"chapter-{index}.opus",
                        "source_path": source,
                        "source_paths": (source,),
                        "file_ids": (item_id,),
                    }
                )
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = path_by_id
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": tuple(records),
                "max_parallel_encodes": 2,
            }
            lock = threading.Lock()
            two_active = threading.Event()
            observations = {
                "active": 0,
                "peak": 0,
                "calls": 0,
                "threads": set(),
            }

            def slow_target_chain(*_args, **_kwargs):
                with lock:
                    observations["active"] += 1
                    observations["calls"] += 1
                    observations["peak"] = max(
                        observations["peak"], observations["active"]
                    )
                    observations["threads"].add(threading.current_thread().name)
                    if observations["active"] == 2:
                        two_active.set()
                two_active.wait(timeout=1)
                threading.Event().wait(0.01)
                with lock:
                    observations["active"] -= 1
                return {"status": "success"}

            with mock.patch.object(
                studio,
                "run_source_synthesis_target_chain",
                side_effect=slow_target_chain,
            ):
                app.process_queue(Processor(), tuple(item_ids), config, False)

            self.assertEqual(observations["calls"], len(item_ids))
            self.assertEqual(observations["peak"], 2)
            self.assertLessEqual(len(observations["threads"]), 2)
            self.assertTrue(
                all(
                    name.startswith("source-target-worker-")
                    for name in observations["threads"]
                )
            )
            self.assertFalse(
                any(
                    thread.name.startswith("source-target-worker-")
                    and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )

    def test_reflow_plan_publish_keeps_group_names_and_metadata(self):
        """Принятое переразбиение не теряет ручные имена и локальные теги."""
        app = object.__new__(studio.TTSApp)
        first = Path("/tmp/first.txt")
        second = Path("/tmp/second.txt")
        app._source_path_by_id = {"first": first, "second": second}
        app._source_tree_file_ids = lambda: ("first", "second")
        app._current_source_m4b_template = lambda: "{book} {part}"
        app._source_plan_album_template = "{book}"
        app._remember_source_runtime_m4b_records = mock.Mock()
        app._finish_m4b_source_plan = mock.Mock()
        records = (
            {
                "target_index": 0,
                "file_ids": ("first",),
                "group_name": "Том первый",
                "name_template": "{book} {part}",
                "metadata_overrides": {
                    "album": "Серия",
                    "artist": "Автор 1",
                },
            },
            {
                "target_index": 0,
                "file_ids": ("second",),
                "group_name": "Том второй",
                "name_template": "{book} {part}",
                "metadata_overrides": {
                    "album": "Серия",
                    "artist": "Автор 2",
                },
            },
        )
        app._pending_source_m4b_ui_plan = {
            "records": records,
            "durations": {"first": 10.0, "second": 12.0},
            "source_ids": ("first", "second"),
            "source_paths": {"first": first, "second": second},
        }

        app._apply_pending_source_m4b_ui_plan()

        app._finish_m4b_source_plan.assert_called_once()
        self.assertEqual(
            app._finish_m4b_source_plan.call_args.kwargs["group_overrides"],
            {
                ("first",): {
                    "name": "Том первый",
                    "name_template": "{book} {part}",
                    "metadata_overrides": {
                        "album": "Серия",
                        "artist": "Автор 1",
                    },
                },
                ("second",): {
                    "name": "Том второй",
                    "name_template": "{book} {part}",
                    "metadata_overrides": {
                        "album": "Серия",
                        "artist": "Автор 2",
                    },
                },
            },
        )

    def test_flush_failure_still_cleans_up_and_finishes_queue(self):
        """Ошибка записи индекса не должна пропускать очистку и завершение UI."""
        events = []

        class Processor:
            cfg = {"use_cache": False}
            is_stopped = False
            active_threads = []

            def flush_cache(self):
                events.append("flush")
                raise OSError("disk full")

            def cleanup_transient_audio_files(self):
                events.append("cleanup")

            def _save_processing_statuses(self):
                events.append("statuses")

        app = object.__new__(studio.TTSApp)
        app._source_path_by_id = {}
        app.finish_processing = mock.Mock()
        app.update_total_ui = mock.Mock()
        app.update_file_status = mock.Mock()
        app._post_to_ui = lambda callback, *args: callback(*args)

        app.process_queue(
            Processor(),
            (),
            {
                "input_dir": "/tmp",
                "output_dir": "/tmp",
                "output_format": "mp3",
                "include_subdirs": False,
            },
            False,
        )

        self.assertEqual(events, ["flush", "cleanup", "statuses"])
        app.finish_processing.assert_called_once()
        self.assertTrue(app.finish_processing.call_args.args[-1])

    def test_reflow_failure_does_not_build_original_m4b_plan(self):
        """Ошибка проверки длительности не должна обходить заданный лимит M4B."""

        class Processor:
            def __init__(self):
                self.is_stopped = False
                self.active_threads = []
                self.processing_statuses_ram = {}

            def process_text_file(self, filepath, **_kwargs):
                return {
                    "status": "success",
                    "audio_files": (Path(filepath).with_suffix(".ogg"),),
                }

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            source.with_suffix(".ogg").write_bytes(b"canonical")
            target = studio.normalize_output_target(
                {"format": "m4b", "bitrate": "64k"}
            )
            record = {
                "kind": "m4b",
                "target_index": 0,
                "target": target,
                "item_id": "book",
                "path": root / "book.m4b",
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_name": "Книга",
            }
            app = object.__new__(studio.TTSApp)
            app._source_path_by_id = {"chapter-id": source}
            app.finish_processing = mock.Mock()
            app.update_total_ui = mock.Mock()
            app.update_progress_ui = mock.Mock()
            app.update_file_status = mock.Mock()
            app._post_to_ui = lambda callback, *args: callback(*args)
            app._stage_source_m4b_ui_plan = mock.Mock()
            app._remember_source_runtime_m4b_records = mock.Mock()
            config = {
                "input_dir": str(root),
                "output_dir": str(root / "output"),
                "output_format": "mp3",
                "include_subdirs": False,
                "source_path_by_id": app._source_path_by_id,
                "source_multi_output": True,
                "synthesis_targets": [target],
                "source_target_records": (record,),
                "source_m4b_reflow_actual_duration": True,
                "source_m4b_reflow_limit_seconds": 3600,
                "source_m4b_force_rebuild": True,
                "source_m4b_auto_split_long": True,
            }

            with mock.patch.object(
                studio,
                "reflow_source_m4b_target_records",
                side_effect=RuntimeError("ffprobe failed"),
            ), mock.patch.object(
                studio, "run_source_synthesis_m4b_target"
            ) as m4b_target:
                app.process_queue(Processor(), ("chapter-id",), config, True)

            m4b_target.assert_not_called()
            app._stage_source_m4b_ui_plan.assert_not_called()
            app._remember_source_runtime_m4b_records.assert_not_called()
            self.assertTrue(app.finish_processing.call_args.args[-1])

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
            self.assertEqual(
                command[command.index("-map_chapters") + 1], "-1"
            )
            self.assertEqual(destination.read_bytes(), b"mp3")

    def test_virtual_audio_range_is_trimmed_and_drops_source_chapters(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "book.m4b"
            source.write_bytes(b"audio")
            destination = root / "chapter.opus"
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0

            def fake_popen(command, **_kwargs):
                Path(command[-1]).write_bytes(b"opus")
                return process

            with mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen, mock.patch.object(
                studio, "_probe_audio_stream_profile", return_value=None
            ):
                studio._export_single_audio_ffmpeg(
                    source,
                    destination,
                    clip_start=5.25,
                    clip_end=7.75,
                    output_format="opus",
                    bitrate="48k",
                    bitrate_mode="48k",
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-ss") + 1], "5.250000000")
            self.assertEqual(command[command.index("-t") + 1], "2.500000000")
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("atrim=duration=2.500000000", graph)
            self.assertEqual(
                command[command.index("-map_chapters") + 1], "-1"
            )

    def test_streaming_merge_uses_concat_stream_copy_for_uniform_inputs(self):
        """Однородный уже готовый поток не должен кодироваться второй раз.

        Это намеренно проверяет именно команду, а не продолжительность/размер
        результата: реальный FFmpeg проверяется отдельными интеграционными тестами,
        а здесь важно, что быстрый путь не возвращается к ``filter_complex`` и не
        получает лишние параметры перекодирования.
        """
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = (root / "one.mp3", root / "two.mp3")
            for source in sources:
                source.write_bytes(b"audio")
            destination = root / "book.mp3"

            profile = {
                "codec": "mp3",
                "sample_rate": 44100,
                "channels": 1,
                "bitrate": 128000,
            }
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"copied")
                return process

            with mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                return_value=profile,
            ), mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    sources,
                    destination,
                    output_format="mp3",
                    bitrate_mode="auto",
                    tags={"title": "Book"},
                )

            command = popen.call_args.args[0]
            self.assertIn("-f", command)
            self.assertEqual(command[command.index("-f") + 1], "concat")
            self.assertIn("-safe", command)
            self.assertEqual(command[command.index("-safe") + 1], "0")
            self.assertIn("-c:a", command)
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertNotIn("-filter_complex", command)
            self.assertNotIn("-af", command)
            self.assertNotIn("-ar", command)
            self.assertNotIn("-ac", command)
            metadata = [
                command[index + 1]
                for index, option in enumerate(command[:-1])
                if option == "-metadata"
            ]
            self.assertIn("title=Book", metadata)
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), b"copied")

            manifest = Path(command[command.index("-i") + 1])
            self.assertFalse(
                manifest.exists(),
                "concat-манифест должен удаляться только после завершения FFmpeg",
            )

    def test_streaming_merge_all_unknown_opus_bitrate_uses_stream_copy(self):
        """Одинаковые VBR-потоки Opus без номинального ``bit_rate`` не перекодируются."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = (root / "one.opus", root / "two.opus")
            for source in sources:
                source.write_bytes(b"audio")
            destination = root / "book.opus"
            profile = {
                "codec": "opus",
                "sample_rate": 48000,
                "channels": 1,
                "bitrate": None,
            }
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stderr.read.return_value = b""

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b"copied")
                return process

            with mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                side_effect=[dict(profile), dict(profile)],
            ), mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ) as popen:
                studio._export_merged_audio_ffmpeg(
                    sources,
                    destination,
                    output_format="opus",
                    bitrate_mode="auto",
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertNotIn("-filter_complex", command)
            self.assertNotIn("-ar", command)
            self.assertNotIn("-ac", command)
            self.assertEqual(destination.read_bytes(), b"copied")

    def test_streaming_merge_rejects_mixed_unknown_bitrate_or_opus_headers(self):
        """Неизвестный или отличающийся физический профиль использует резервное кодирование."""
        cases = (
            (
                "mixed_known_unknown_bitrate",
                {"bitrate": 48000},
                {"bitrate": None},
            ),
            (
                "different_opus_headers",
                {"bitrate": None, "extradata_hash": "sha256:first"},
                {"bitrate": None, "extradata_hash": "sha256:second"},
            ),
        )
        for case_name, first_overrides, second_overrides in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                sources = (root / "one.opus", root / "two.opus")
                for source in sources:
                    source.write_bytes(b"audio")
                destination = root / "book.opus"
                first = {
                    "codec": "opus",
                    "sample_rate": 48000,
                    "channels": 1,
                    "bitrate": None,
                }
                second = dict(first)
                first.update(first_overrides)
                second.update(second_overrides)
                process = mock.Mock()
                process.poll.return_value = 0
                process.returncode = 0
                process.stderr.read.return_value = b""

                def fake_popen(command, **kwargs):
                    Path(command[-1]).write_bytes(b"encoded")
                    return process

                with mock.patch.object(
                    studio,
                    "_probe_audio_stream_profile",
                    side_effect=[first, second],
                ), mock.patch.object(
                    studio.subprocess, "Popen", side_effect=fake_popen
                ) as popen:
                    studio._export_merged_audio_ffmpeg(
                        sources,
                        destination,
                        output_format="opus",
                        bitrate_mode="auto",
                    )

                command = popen.call_args.args[0]
                self.assertIn(
                    "-filter_complex",
                    command,
                    f"case {case_name} unexpectedly selected stream copy",
                )
                self.assertEqual(
                    command[command.index("-c:a") + 1], "libopus"
                )
                self.assertEqual(destination.read_bytes(), b"encoded")

    def test_streaming_merge_falls_back_when_stream_copy_is_not_safe(self):
        """Любое изменение профиля или обработки сохраняет безопасный резервный путь."""
        cases = (
            ("different_codec", {"second_codec": "vorbis"}, {}),
            ("different_stream_profile", {"different_profile": True}, {}),
            ("unknown_profile", {"second_unknown": True}, {}),
            ("explicit_resample", {}, {"sample_rate": "48000"}),
            ("pause", {}, {"pause_ms": 100}),
            ("speed_effect", {}, {"speed": 1.25}),
            ("cover", {}, {"cover": "cover.png"}),
            ("single_source", {}, {"single": True}),
        )
        for case_name, profile_options, call_options in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                sources = [root / "one.mp3"]
                if not call_options.get("single"):
                    sources.append(root / "two.mp3")
                for source in sources:
                    source.write_bytes(b"audio")
                cover = root / "cover.png"
                cover.write_bytes(b"image")
                destination = root / "book.mp3"

                first = {
                    "codec": "mp3",
                    "sample_rate": 44100,
                    "channels": 1,
                    "bitrate": 128000,
                }
                second = dict(first)
                second["codec"] = profile_options.get("second_codec", "mp3")
                if profile_options.get("different_profile"):
                    first["profile"] = "layer 3"
                    second["profile"] = "layer 2"
                if profile_options.get("second_unknown"):
                    second = None
                process = mock.Mock()
                process.poll.return_value = 0
                process.returncode = 0
                process.stderr.read.return_value = b""

                def fake_popen(command, **kwargs):
                    Path(command[-1]).write_bytes(b"encoded")
                    return process

                probe_result = [first, second] if len(sources) > 1 else [first]
                kwargs = {
                    "output_format": "mp3",
                    "bitrate_mode": "auto",
                }
                if "sample_rate" in call_options:
                    kwargs["sample_rate"] = call_options["sample_rate"]
                if "pause_ms" in call_options:
                    kwargs["pause_ms"] = call_options["pause_ms"]
                if "speed" in call_options:
                    kwargs["speed"] = call_options["speed"]
                if "cover" in call_options:
                    kwargs["cover"] = cover

                with mock.patch.object(
                    studio,
                    "_probe_audio_stream_profile",
                    side_effect=probe_result,
                ), mock.patch.object(
                    studio.subprocess, "Popen", side_effect=fake_popen
                ) as popen:
                    studio._export_merged_audio_ffmpeg(
                        sources,
                        destination,
                        **kwargs,
                    )

                command = popen.call_args.args[0]
                self.assertIn(
                    "-filter_complex",
                    command,
                    f"case {case_name} unexpectedly selected stream copy",
                )
                self.assertNotEqual(
                    command[command.index("-c:a") + 1], "copy"
                )

    def test_streaming_merge_retries_with_filter_after_copy_rejection(self):
        """Несовместимость отдельных пакетов не должна делать оптимизацию фатальной."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = (root / "one.mp3", root / "two.mp3")
            for source in sources:
                source.write_bytes(b"audio")
            destination = root / "book.mp3"
            profile = {
                "codec": "mp3",
                "sample_rate": 44100,
                "channels": 1,
                "bitrate": 128000,
            }
            failed_copy = mock.Mock()
            failed_copy.poll.return_value = 1
            failed_copy.returncode = 1
            failed_copy.stderr.read.return_value = b"incompatible packets"
            successful_encode = mock.Mock()
            successful_encode.poll.return_value = 0
            successful_encode.returncode = 0
            successful_encode.stderr.read.return_value = b""
            commands = []

            def fake_popen(command, **kwargs):
                commands.append(command)
                if len(commands) == 1:
                    return failed_copy
                Path(command[-1]).write_bytes(b"encoded")
                return successful_encode

            with mock.patch.object(
                studio,
                "_probe_audio_stream_profile",
                return_value=profile,
            ), mock.patch.object(
                studio.subprocess, "Popen", side_effect=fake_popen
            ), self.assertLogs(level=logging.WARNING) as logs:
                studio._export_merged_audio_ffmpeg(
                    sources,
                    destination,
                    output_format="mp3",
                    bitrate_mode="auto",
                )

            self.assertEqual(len(commands), 2)
            self.assertEqual(
                commands[0][commands[0].index("-c:a") + 1], "copy"
            )
            self.assertNotIn("-filter_complex", commands[0])
            self.assertIn("-filter_complex", commands[1])
            self.assertEqual(
                commands[1][commands[1].index("-c:a") + 1], "libmp3lame"
            )
            self.assertTrue(any(
                "повтор через безопасный filter_complex" in message
                for message in logs.output
            ))
            self.assertEqual(destination.read_bytes(), b"encoded")

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

    def test_generic_vorbis_and_m4a_exports_embed_cover(self):
        """Общий FFmpeg-путь сохраняет обложки в обоих контейнерах."""
        for fmt, suffix in (("ogg", ".ogg"), ("m4a", ".m4a")):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                source = root / "source.wav"
                cover = root / "cover.png"
                destination = root / f"result{suffix}"
                source.write_bytes(b"audio")
                cover.write_bytes(b"\x89PNG\r\n\x1a\nimage")
                process = mock.Mock()
                process.poll.return_value = 0
                process.returncode = 0

                def fake_popen(command, **_kwargs):
                    Path(command[-1]).write_bytes(b"encoded")
                    return process

                with mock.patch.object(
                    studio.subprocess, "Popen", side_effect=fake_popen
                ) as popen:
                    studio._export_merged_audio_ffmpeg(
                        (source,),
                        destination,
                        output_format=fmt,
                        bitrate_mode="96k",
                        cover=cover,
                        _probed_profiles=(
                            {
                                "codec": "pcm_s16le",
                                "sample_rate": 48000,
                                "channels": 1,
                                "bitrate": None,
                            },
                        ),
                    )

                command = popen.call_args.args[0]
                maps = [
                    command[index + 1]
                    for index, option in enumerate(command[:-1])
                    if option == "-map"
                ]
                metadata_maps = [
                    command[index + 1]
                    for index, option in enumerate(command[:-1])
                    if option == "-map_metadata"
                ]
                self.assertEqual(command.count("-i"), 2)
                if fmt == "ogg":
                    self.assertIn("ffmetadata", command)
                    self.assertEqual(command[command.index("-c:a") + 1], "libvorbis")
                    self.assertEqual(maps, ["[merged]"])
                    self.assertEqual(metadata_maps, ["-1", "1"])
                    self.assertNotIn("-c:v", command)
                    self.assertNotIn("METADATA_BLOCK_PICTURE", " ".join(command))
                else:
                    self.assertEqual(command[command.index("-c:a") + 1], "aac")
                    self.assertEqual(maps, ["[merged]", "1:v:0"])
                    self.assertEqual(metadata_maps, ["-1"])
                    self.assertEqual(command[command.index("-c:v") + 1], "mjpeg")
                    self.assertEqual(
                        command[command.index("-disposition:v:0") + 1],
                        "attached_pic",
                    )
                self.assertEqual(destination.read_bytes(), b"encoded")

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

    def test_export_mode_controls_disable_effects_for_m4b(self):
        app = object.__new__(studio.TTSApp)
        app._export_running = False
        app.export_tags_only_var = mock.Mock(get=mock.Mock(return_value=False))
        app.export_fmt_var = mock.Mock(get=mock.Mock(return_value="m4b"))
        app.export_apply_fx_var = mock.Mock()
        app.chk_export_fx = mock.Mock()
        app.btn_export_dir = mock.Mock()
        app.ent_export_dir = mock.Mock()
        app.cb_export_fmt = mock.Mock()
        app.cb_export_bitrate = mock.Mock()
        app.cb_export_sample_rate = mock.Mock()
        app.cb_export_channels = mock.Mock()
        app.btn_audio_profiles_export = mock.Mock()
        app.btn_export_start = mock.Mock()
        app.btn_export_stop = mock.Mock()
        app.chk_export_tags_only = mock.Mock()
        app.export_fx_value_controls = tuple(mock.Mock() for _ in range(5))

        app._sync_export_mode_controls()

        app.export_apply_fx_var.set.assert_called_once_with(False)
        app.chk_export_fx.configure.assert_called_with(state=studio.tk.DISABLED)
        for control in app.export_fx_value_controls:
            control.configure.assert_called_with(state=studio.tk.DISABLED)

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

    def test_export_project_session_round_trip_preserves_virtual_m4b_chapters(self):
        root_path = Path("audio") / "root.mp3"
        book_path = Path("audio") / "book.m4b"
        cover_path = Path("covers") / "book.jpg"
        root_items = ("root-file", "virtual-book")
        group_children = {
            "virtual-book": ("chapter-2", "chapter-1"),
        }
        export_groups = {
            "virtual-book": {
                "name": "Книга",
                "merge": False,
                "pause": 0,
                "source_kind": "m4b_chapters",
                "source_path": book_path,
                "cover": cover_path,
                "cover_source": str(book_path),
            },
        }
        export_files = {
            "root-file": {
                "path": root_path,
                "title": "Новое название",
                "duration": float("nan"),
            },
            "chapter-1": {
                "path": str(book_path),
                "title": "Глава 1",
                "chapter_index": 1,
                "clip_start": 0.0,
                "clip_end": 10.5,
                "duration": 10.5,
                "cover_source": str(book_path),
            },
            "chapter-2": {
                "path": str(book_path),
                "title": "Глава 2",
                "chapter_index": 2,
                "clip_start": 10.5,
                "clip_end": 21.0,
                "duration": 10.5,
                "cover_source": str(book_path),
            },
        }

        snapshot = studio.build_export_project_session(
            root_items,
            group_children,
            export_groups,
            export_files,
        )
        restored = studio.normalize_export_project_session(snapshot)

        self.assertEqual(restored, snapshot)
        self.assertEqual(
            [item["kind"] for item in snapshot["items"]],
            ["file", "group"],
        )
        self.assertEqual(snapshot["items"][0]["settings"]["duration"], 0.0)
        self.assertEqual(
            snapshot["items"][0]["settings"]["source_filename"],
            "root.mp3",
        )
        group = snapshot["items"][1]
        self.assertEqual(group["settings"]["source_path"], str(book_path))
        self.assertEqual(group["settings"]["cover"], str(cover_path))
        self.assertEqual(
            [chapter["chapter_index"] for chapter in group["files"]],
            [2, 1],
        )
        self.assertEqual(
            [chapter["clip_start"] for chapter in group["files"]],
            [10.5, 0.0],
        )
        self.assertEqual(
            [chapter["path"] for chapter in group["files"]],
            [str(book_path), str(book_path)],
        )
        # Стандартный json не должен встретить Path или специальные NaN/Infinity.
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)

    def test_export_project_session_drops_nonvisible_and_damaged_files(self):
        snapshot = studio.build_export_project_session(
            ("group", "valid-root", "broken-root", "unknown"),
            {"group": ("missing-child", "valid-child", "broken-child")},
            {"group": {"name": "Группа"}},
            {
                "valid-root": {"path": "/audio/root.mp3", "title": "Root"},
                "broken-root": {"path": "", "title": "Broken"},
                "valid-child": {"path": "/audio/child.mp3", "title": "Child"},
                "broken-child": {"title": "No path"},
                "orphan": {"path": "/audio/orphan.mp3", "title": "Orphan"},
            },
        )

        self.assertEqual(
            [item["kind"] for item in snapshot["items"]],
            ["group", "file"],
        )
        self.assertEqual(
            [item["settings"].get("name") for item in snapshot["items"]],
            ["Группа", None],
        )
        self.assertEqual(
            [item["title"] for item in snapshot["items"][0]["files"]],
            ["Child"],
        )

        normalized = studio.normalize_export_project_session(
            {
                "schema_version": studio.EXPORT_PROJECT_SCHEMA_VERSION,
                "items": [
                    None,
                    {"kind": "unknown", "settings": {}},
                    {"kind": "file", "settings": {"title": "No path"}},
                    {"kind": "group", "settings": "invalid", "files": []},
                    {
                        "kind": "group",
                        "settings": {"name": "Recovered"},
                        "files": [
                            {"path": "", "title": "Broken"},
                            {"path": "/audio/good.mp3", "title": ""},
                        ],
                    },
                ],
            }
        )
        self.assertEqual(len(normalized["items"]), 1)
        recovered = normalized["items"][0]
        self.assertEqual(recovered["settings"]["name"], "Recovered")
        self.assertEqual(len(recovered["files"]), 1)
        self.assertEqual(recovered["files"][0]["path"], "/audio/good.mp3")
        self.assertEqual(recovered["files"][0]["title"], "")
        self.assertEqual(recovered["files"][0]["source_title"], "good")

    def test_export_project_session_rejects_unknown_schema_and_non_array_items(self):
        invalid_payloads = (
            None,
            {},
            {
                "schema_version": studio.EXPORT_PROJECT_SCHEMA_VERSION + 1,
                "items": [],
            },
            {
                "schema_version": studio.EXPORT_PROJECT_SCHEMA_VERSION,
                "items": {},
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    studio.normalize_export_project_session(payload)

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

    def test_estimated_text_duration_is_explicit_and_bom_safe(self):
        self.assertEqual(
            studio.estimate_text_duration_seconds("\ufeff" + "a" * 900, 900),
            60.0,
        )
        self.assertEqual(studio.source_text_character_count("\ufeffa\ufeff"), 2)
        with self.assertRaises(ValueError):
            studio.estimate_text_duration_seconds("text", 0)

    def test_estimated_m4b_source_plan_respects_duration_and_file_limits(self):
        groups = studio.split_estimated_source_file_ids(
            ["a", "b", "c", "d"],
            {"a": 40, "b": 30, "c": 20, "d": 20},
            60,
            max_files=2,
        )
        self.assertEqual(groups, [["a"], ["b", "c"], ["d"]])

    def test_estimated_m4b_source_plan_allows_zero_duration_limit(self):
        groups = studio.split_estimated_source_file_ids(
            ["a", "b"], {"a": 900, "b": 900}, 0, max_files=1
        )
        self.assertEqual(groups, [["a"], ["b"]])

    def test_estimated_m4b_source_plan_supports_independent_character_limit(self):
        groups = studio.split_estimated_source_file_ids(
            ["a", "b", "c"],
            {"a": 0, "b": 0, "c": 0},
            0,
            char_counts={"a": 600, "b": 500, "c": 500},
            max_chars=1000,
        )
        self.assertEqual(groups, [["a"], ["b", "c"]])

    def test_estimated_m4b_source_plan_keeps_oversized_txt_whole(self):
        groups = studio.split_estimated_source_file_ids(
            ["a", "b"],
            {"a": 0, "b": 0},
            0,
            char_counts={"a": 1200, "b": 200},
            max_chars=1000,
        )
        self.assertEqual(groups, [["a"], ["b"]])

    def test_estimated_m4b_source_plan_requires_counts_for_character_limit(self):
        with self.assertRaisesRegex(ValueError, "размеры всех TXT"):
            studio.split_estimated_source_file_ids(
                ["a"], {"a": 0}, 0, max_chars=1000
            )
        with self.assertRaisesRegex(ValueError, "нет числа символов"):
            studio.split_estimated_source_file_ids(
                ["a", "b"],
                {"a": 0, "b": 0},
                0,
                char_counts={"a": 100},
                max_chars=1000,
            )

    def test_estimated_m4b_source_plan_rejects_invalid_character_limit(self):
        with self.assertRaises(ValueError):
            studio.split_estimated_source_file_ids(
                ["a"], {"a": 0}, 0, char_counts={"a": 1}, max_chars=-1
            )
        with self.assertRaises(ValueError):
            studio.split_estimated_source_file_ids(
                ["a"], {"a": 0}, 0, char_counts={"a": 1}, max_chars="oops"
            )

    def test_manual_source_grouping_splits_selected_contiguous_range(self):
        groups = studio.group_selected_source_file_ids(
            ["a", "b", "c", "d"],
            [("a", "b", "c", "d")],
            ["b", "c"],
        )
        self.assertEqual(groups, (("a",), ("b", "c"), ("d",)))

    def test_manual_source_grouping_merges_old_boundaries_inside_selection(self):
        groups = studio.group_selected_source_file_ids(
            ["a", "b", "c", "d"],
            [("a",), ("b", "c"), ("d",)],
            ["a", "b", "c"],
        )
        self.assertEqual(groups, (("a", "b", "c"), ("d",)))

    def test_manual_source_grouping_rejects_noncontiguous_selection(self):
        with self.assertRaisesRegex(ValueError, "соседние TXT"):
            studio.group_selected_source_file_ids(
                ["a", "b", "c"], [("a", "b", "c")], ["a", "c"]
            )

    def test_manual_source_group_merge_requires_adjacent_parts(self):
        groups = studio.merge_source_m4b_group_indexes(
            [("a",), ("b", "c"), ("d",)], [0, 1]
        )
        self.assertEqual(groups, (("a", "b", "c"), ("d",)))
        with self.assertRaisesRegex(ValueError, "соседние части"):
            studio.merge_source_m4b_group_indexes(
                [("a",), ("b",), ("c",)], [0, 2]
            )

    def test_estimated_m4b_source_plan_rejects_negative_duration_limit(self):
        with self.assertRaises(ValueError):
            studio.split_estimated_source_file_ids(["a"], {"a": 1}, -1)

    def test_estimated_m4b_source_plan_rejects_invalid_duration_limit(self):
        with self.assertRaises(ValueError):
            studio.split_estimated_source_file_ids(["a"], {"a": 1}, "oops")

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
            studio.next_sequence_name(
                "Том {num:10:03d}",
                ["Том 010", "Том 011"],
            ),
            "Том 012",
        )

    def test_adaptive_group_template_renames_existing_auto_groups(self):
        template = "Том {num:00d}"
        groups = [
            (
                f"g{number}",
                {
                    "name": f"Том {number}",
                    "_group_name_auto": True,
                    "_group_name_template": template,
                    "_group_name_number": number,
                },
            )
            for number in range(1, 11)
        ]
        renamed = studio.adaptive_group_sequence_renames(template, groups)
        self.assertEqual(renamed["g1"], "Том 01")
        self.assertEqual(renamed["g9"], "Том 09")
        self.assertEqual(renamed["g10"], "Том 10")

        groups.append(
            (
                "manual",
                {
                    "name": "Том вручную",
                    "_group_name_auto": False,
                    "_group_name_template": template,
                    "_group_name_number": 11,
                },
            )
        )
        renamed = studio.adaptive_group_sequence_renames(template, groups)
        self.assertNotIn("manual", renamed)

        hundred = [
            (
                str(number),
                {
                    "name": f"Том {number:02d}",
                    "_group_name_auto": True,
                    "_group_name_template": template,
                    "_group_name_number": number,
                },
            )
            for number in range(1, 101)
        ]
        renamed = studio.adaptive_group_sequence_renames(template, hundred)
        self.assertEqual(renamed["1"], "Том 001")
        self.assertEqual(renamed["99"], "Том 099")
        self.assertEqual(renamed["100"], "Том 100")
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

    def test_export_output_preflight_rejects_nfc_casefold_duplicates(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            composed = root / "Caf\u00e9.MP3"
            decomposed = root / "cafe\u0301.mp3"

            with self.assertRaisesRegex(ValueError, "один файл"):
                studio.validate_export_output_paths((composed, decomposed))

    def test_export_output_preflight_returns_paths_in_original_order(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            outputs = (root / "one.mp3", root / "two.opus")

            self.assertEqual(
                studio.validate_export_output_paths(outputs, ()),
                outputs,
            )

    def test_export_output_preflight_rejects_source_through_symlink(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            real_dir = root / "real"
            real_dir.mkdir()
            source = real_dir / "chapter.mp3"
            source.write_bytes(b"audio")
            alias_dir = root / "alias"
            try:
                alias_dir.symlink_to(real_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Символические ссылки недоступны: {exc}")

            with self.assertRaisesRegex(ValueError, "совпадает с исходным"):
                studio.validate_export_output_paths(
                    (alias_dir / source.name,),
                    (source,),
                )

    def test_export_output_preflight_rejects_existing_hardlink_to_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            output = root / "result.mp3"
            try:
                output.hardlink_to(source)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Жёсткие ссылки недоступны: {exc}")

            with self.assertRaisesRegex(ValueError, "совпадает с исходным"):
                studio.validate_export_output_paths((output,), (source,))

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

    def test_output_names_do_not_repeat_the_selected_extension(self):
        """Пользовательский суффикс не дублируется при автодобавлении."""
        self.assertEqual(
            studio.normalize_output_filename("chapter.m4a.m4a", "m4a"),
            "chapter.m4a",
        )
        self.assertEqual(
            studio._template_basename("Книга.m4b.m4b", "m4b"),
            "Книга",
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


class SourceOutputResumeTests(unittest.TestCase):
    """Повторная сборка M4B не изменяет независимые готовые выходы."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "chapter.txt"
        self.source.write_text("Текст главы.", encoding="utf-8")
        fragment = self.root / "fragment.ogg"
        fragment.write_bytes(b"canonical audio")
        self.mp3 = self.root / "chapter.mp3"
        self.m4b = self.root / "book.m4b"
        self.processor = mock.Mock()
        self.processor.cfg = {"use_cache": True}
        self.processor.is_stopped = False
        self.processor.active_threads = []
        self.processor.encode_semaphore = None
        self.processor.processing_statuses_ram = {}
        self.processor.process_text_file.return_value = {
            "status": "success", "audio_files": (fragment,),
        }

        def mark_status(path, status):
            key = str(Path(path).resolve())
            if status in {"error", "warning"}:
                self.processor.processing_statuses_ram[key] = status
            else:
                self.processor.processing_statuses_ram.pop(key, None)

        self.processor._mark_output_status.side_effect = mark_status
        self.app = object.__new__(studio.TTSApp)
        self.app._source_plan_dirty = True
        self.app._invalidate_source_runtime_m4b_plan()
        self.app._source_path_by_id = {"chapter": self.source}
        for name in ("finish_processing", "update_total_ui",
                     "update_progress_ui", "update_file_status"):
            setattr(self.app, name, mock.Mock())
        self.app._post_to_ui = lambda callback, *args: callback(*args)
        mp3_target = studio.normalize_output_target({"format": "mp3", "bitrate": "128k"})
        m4b_target = studio.normalize_output_target({"format": "m4b", "bitrate": "96k"})
        self.regular_record = {
            "kind": "file", "target_index": 0, "target": mp3_target,
            "item_id": "chapter", "file_ids": ("chapter",),
            "source_path": self.source, "source_paths": (self.source,),
            "path": self.mp3,
        }
        self.m4b_record = {
            "kind": "m4b", "target_index": 1, "target": m4b_target,
            "item_id": "group", "file_ids": ("chapter",),
            "source_paths": (self.source,), "path": self.m4b,
        }
        self.config = {
            "input_dir": str(self.root), "output_dir": str(self.root),
            "output_format": "mp3", "source_multi_output": True,
            "source_path_by_id": self.app._source_path_by_id,
            "synthesis_targets": [mp3_target, m4b_target],
            "source_target_records": (self.regular_record, self.m4b_record),
            "source_m4b_auto_split_long": False,
        }
        self.encoded = []

        def encode(_audio, output, **_kwargs):
            self.encoded.append(Path(output).suffix)
            Path(output).write_bytes(f"output {len(self.encoded)}".encode())

        for name in ("_export_merged_audio_ffmpeg", "_export_m4b_ffmpeg"):
            patcher = mock.patch.object(studio, name, side_effect=encode)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_queue(self, *, skip=True):
        config = copy.deepcopy(self.config)
        config["source_plan_revision"] = self.app._source_plan_revision
        config["source_targets_rebuild_paths"] = self.app._source_regular_rebuild_paths(
            config["source_target_records"]
        )
        self.app.process_queue(self.processor, ("chapter",), config, skip)

    def test_deleted_m4b_keeps_successful_mp3_from_partial_dirty_plan(self):
        untouched_record = dict(self.regular_record, path=self.root / "other.mp3")
        self.run_queue()
        before = (self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns)
        self.assertEqual(self.encoded.count(".mp3"), 1)
        self.assertTrue(self.m4b.is_file())
        self.assertTrue(self.app._source_plan_dirty)
        self.m4b.unlink()

        self.run_queue()

        self.assertEqual(self.encoded.count(".mp3"), 1)
        self.assertEqual(self.encoded.count(".m4b"), 2)
        self.assertEqual(before, (self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns))
        self.assertTrue(self.m4b.is_file())
        self.assertEqual(self.app._source_regular_rebuild_paths(
            (self.regular_record, untouched_record)
        ), (str(untouched_record["path"]),))

    def test_m4b_collection_failure_does_not_poison_existing_mp3(self):
        self.run_queue()
        before = (self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns)
        self.m4b.unlink()
        for failure in ("missing_text", "exception", "error"):
            with self.subTest(failure=failure):
                if failure == "missing_text":
                    self.source.unlink()
                elif failure == "exception":
                    self.processor.process_text_file.side_effect = OSError("Ошибка чтения")
                else:
                    self.processor.process_text_file.side_effect = None
                    self.processor.process_text_file.return_value = {"status": "error"}
                self.run_queue()
                self.assertNotIn(str(self.mp3), self.processor.processing_statuses_ram)
                self.assertEqual(self.encoded.count(".mp3"), 1)
                self.assertEqual(before, (self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns))
                self.assertTrue(self.app.finish_processing.call_args.args[-1])
                self.source.write_text("Текст главы.", encoding="utf-8")

    def test_failed_regular_output_is_rebuilt_even_after_successful_plan(self):
        self.run_queue()
        for state in ("missing", "warning", "error"):
            with self.subTest(state=state):
                if state == "missing":
                    self.mp3.unlink()
                else:
                    self.processor.processing_statuses_ram[str(self.mp3)] = state
                count = self.encoded.count(".mp3")
                self.run_queue()
                self.assertEqual(self.encoded.count(".mp3"), count + 1)
                self.assertNotIn(str(self.mp3), self.processor.processing_statuses_ram)
                self.assertFalse(self.app.finish_processing.call_args.args[-1])

    def test_new_metadata_or_disabled_skip_rebuilds_regular_output(self):
        self.run_queue()
        self.regular_record["metadata_overrides"] = {"album": "Другой том"}
        self.run_queue()
        self.assertEqual(self.encoded.count(".mp3"), 2)
        self.run_queue(skip=False)
        self.assertEqual(self.encoded.count(".mp3"), 3)

    def test_failed_other_format_does_not_repeat_successful_mp3(self):
        opus = self.root / "chapter.opus"
        opus_record = dict(
            self.regular_record, path=opus, target_index=2,
            target=studio.normalize_output_target({"format": "opus", "bitrate": "48k"}),
        )
        self.config["source_target_records"] += (opus_record,)
        self.config["synthesis_targets"].append(opus_record["target"])

        def fail_opus(_audio, output, **_kwargs):
            if Path(output).suffix == ".opus":
                raise OSError("Ошибка записи Opus")
            self.encoded.append(Path(output).suffix)
            Path(output).write_bytes(b"successful mp3")

        with mock.patch.object(studio, "_export_merged_audio_ffmpeg", side_effect=fail_opus):
            self.run_queue()
        before = self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns
        self.assertIn(str(opus), self.processor.processing_statuses_ram)
        self.run_queue()
        self.assertEqual(self.encoded.count(".mp3"), 1)
        self.assertEqual(before, (self.mp3.read_bytes(), self.mp3.stat().st_mtime_ns))
        self.assertTrue(opus.is_file())
        self.assertNotIn(str(opus), self.processor.processing_statuses_ram)

    def test_invalidated_plan_rejects_stale_completion(self):
        self.run_queue()
        revision = self.app._source_plan_revision
        self.app._invalidate_source_runtime_m4b_plan()
        self.app._remember_source_completed_regular_targets(
            revision, (self.regular_record,)
        )
        self.assertEqual(self.app._source_regular_rebuild_paths(
            (self.regular_record,)
        ), (str(self.mp3),))
        self.run_queue()
        self.assertEqual(self.encoded.count(".mp3"), 2)


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

    def test_batch_normalization_preserves_tree_and_matches_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            output = root / "normalized"
            nested = source / "том 2"
            nested.mkdir(parents=True)
            first = source / "Глава 10.txt"
            second = nested / "Глава 2.txt"
            first.write_text("\ufeffГлава 10. Текст.", encoding="utf-8")
            second.write_text("Цена 20 рублей.", encoding="utf-8")

            context = self.make_context(
                config={"glossary_enabled": False}
            )
            events = []
            summary = studio.normalize_text_folder(
                source,
                output,
                context,
                progress_callback=lambda *event: events.append(event),
            )

            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["written"], 2)
            self.assertFalse(summary["errors"])
            self.assertEqual(len(events), 2)
            for original in (first, second):
                target = output / original.relative_to(source)
                expected = context.preview_normalization(
                    original.read_text(encoding="utf-8-sig")
                )["normalized_text_for_file"]
                self.assertEqual(target.read_text(encoding="utf-8"), expected)
                self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))

            second_run = studio.normalize_text_folder(
                source,
                output,
                context,
                overwrite=False,
            )
            self.assertEqual(second_run["written"], 0)
            self.assertEqual(second_run["skipped_existing"], 2)

    def test_batch_normalization_rejects_same_source_and_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir)
            (source / "chapter.txt").write_text("Текст.", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "отличаться"):
                studio.normalize_text_folder(
                    source,
                    source,
                    self.make_context(config={"glossary_enabled": False}),
                )

    def test_normalizer_profiles_keep_commit_actions_in_fixed_footer(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def open_normalizer_profiles_dialog(")
        end = source.index("    # Переносимые аудиопрофили", start)
        dialog_source = source[start:end]

        # Расширяемая подсказка не должна вытеснять подтверждение из footer:
        # обе кнопки находятся в отдельном фиксированном контейнере.
        self.assertIn("footer_actions = ttk.Frame(footer)", dialog_source)
        self.assertIn("footer_actions.pack(side=tk.RIGHT", dialog_source)
        self.assertIn('text="Сохранить и закрыть"', dialog_source)
        self.assertIn("command=commit", dialog_source)

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

    def test_normalizer_profile_library_roundtrip_keeps_stable_ids(self):
        profile = studio.normalizer_profile_from_config(
            {"normalizer_mode": "safe"}, name="Мой Safe"
        )
        entry = studio.make_normalizer_profile_entry(
            profile, profile_id="custom:" + "1" * 32
        )

        bundle = studio.normalizer_profile_library_from_profiles([entry])
        restored = studio.normalize_normalizer_profile_library(bundle)

        self.assertEqual(bundle["schema"], studio.NORMALIZER_PROFILE_LIBRARY_SCHEMA)
        self.assertEqual(restored, [entry])

    def test_normalizer_library_cannot_impersonate_builtin_profile(self):
        profile = studio.normalizer_profile_from_config(
            {"normalizer_mode": "safe"}, name="Safe (бережный)"
        )
        forged = {"id": "builtin:safe", "profile": profile}

        with self.assertRaisesRegex(ValueError, "встроенный id"):
            studio.normalize_normalizer_profile_entry(forged)

    def test_normalizer_builtin_id_requires_exact_builtin_content(self):
        builtin = studio.builtin_normalizer_profile_entries()[0]
        forged = copy.deepcopy(builtin["profile"])
        forged["normalizer"]["options"]["remove_links"] = not forged[
            "normalizer"
        ]["options"]["remove_links"]

        with self.assertRaisesRegex(ValueError, "не совпадает"):
            studio.make_normalizer_profile_entry(
                forged,
                profile_id=builtin["id"],
            )

    def test_normalizer_library_excludes_raw_builtin_clones(self):
        builtins = studio.builtin_normalizer_profile_entries()

        bundle = studio.normalizer_profile_library_from_profiles(
            [entry["profile"] for entry in builtins]
        )

        self.assertEqual(bundle["profiles"], [])

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
    def test_audio_profile_target_compatibility_is_codec_specific(self):
        """Профиль можно прикрепить только к соответствующему потоку.

        M4A/M4B сейчас кодируются AAC на уровне цели и не должны предлагать
        старые MP3/Opus/Vorbis/WAV-профили. Проверяем вспомогательную функцию отдельно от Tk,
        чтобы это правило не зависело от конкретной раскладки диалога.
        """
        profiles = studio.builtin_audio_profiles()
        for profile in profiles:
            profile_format = profile["audio"]["format"]
            self.assertTrue(
                studio.audio_profile_target_compatible(profile, profile_format)
            )
            for container_format in ("m4a", "m4b"):
                expected = profile_format == "m4a"
                self.assertEqual(
                    studio.audio_profile_target_compatible(
                        profile, container_format
                    ),
                    expected,
                    msg=f"{profile_format} compatibility with {container_format} differs",
                )

        self.assertFalse(
            studio.audio_profile_target_compatible(
                {"name": "broken", "audio": {"format": "mp3"}}, "m4b"
            )
        )
        self.assertFalse(studio.audio_profile_target_compatible({}, "mp3"))

    def test_m4b_rejects_audio_profiles_with_enabled_effects(self):
        profile = studio.make_audio_profile(
            "AAC с эффектом",
            output_format="m4a",
            bitrate="64k",
            effects_enabled=True,
            speed=1.2,
        )
        self.assertTrue(studio.audio_profile_target_compatible(profile, "m4a"))
        self.assertFalse(studio.audio_profile_target_compatible(profile, "m4b"))

    def test_m4a_audio_profile_is_builtin_and_valid_for_aac_targets(self):
        entries = {
            entry["id"]: entry["profile"]
            for entry in studio.audio_profile_entries_from_config({})
        }
        for bitrate in ("64k", "96k"):
            profile_id = f"builtin:m4a-{bitrate}"
            with self.subTest(bitrate=bitrate):
                profile = entries[profile_id]
                self.assertEqual(studio.audio_profile_id(profile), profile_id)
                stored = dict(profile, id=profile_id)
                restored = studio.normalize_audio_profile(
                    json.loads(json.dumps(stored)), require_envelope=True
                )
                for target in ("m4a", "m4b"):
                    self.assertTrue(
                        studio.audio_profile_target_compatible(restored, target)
                    )
                for target, prefix in (("book", "output"), ("export", "export")):
                    values = studio.audio_profile_config_values(restored, target)
                    self.assertEqual(values[f"{prefix}_format"], "m4a")
                    self.assertEqual(values[f"{prefix}_bitrate"], bitrate)
                    self.assertEqual(values[f"{prefix}_sample_rate"], "48000")
                    self.assertEqual(values[f"{prefix}_channels"], "mono")
                config = studio.normalize_config(
                    {
                        "default_book_audio_profile_id": profile_id,
                        "default_export_audio_profile_id": profile_id,
                    }
                )
                self.assertEqual(config["default_book_audio_profile_id"], profile_id)
                self.assertEqual(config["default_export_audio_profile_id"], profile_id)
        self.assertEqual(
            studio._select_book_audio_profile(
                "m4a", sample_rate="auto", channels="auto", bitrate="auto"
            ),
            {"sample_rate": 48000, "channels": 1, "bitrate": "64k"},
        )
        self.assertEqual(
            studio.normalize_output_filename("chapter.mp3", "m4a"),
            "chapter.m4a",
        )

    def test_audio_profile_library_roundtrip_preserves_stable_id(self):
        profile = studio.make_audio_profile(
            "Речь", output_format="opus", bitrate="48k"
        )
        profile["id"] = "custom:" + "2" * 32

        bundle = studio.audio_profile_library_from_profiles([profile])
        restored = studio.normalize_audio_profile_library(bundle)

        self.assertEqual(bundle["schema"], studio.AUDIO_PROFILE_LIBRARY_SCHEMA)
        self.assertEqual(restored[0]["id"], profile["id"])
        self.assertEqual(restored[0]["profile"]["id"], profile["id"])

    def test_audio_profile_default_id_survives_renamed_profile(self):
        profile = studio.make_audio_profile("Старое имя")
        profile["id"] = "custom:" + "3" * 32
        config = studio.normalize_config(
            {
                "audio_profiles": [profile],
                "default_book_audio_profile_id": profile["id"],
            }
        )
        config["audio_profiles"][0]["name"] = "Новое имя"

        normalized = studio.normalize_config(config)

        self.assertEqual(
            normalized["default_book_audio_profile_id"], profile["id"]
        )

    def test_audio_library_rejects_wrapper_inner_id_mismatch(self):
        profile = studio.make_audio_profile("Несовпадение")
        profile["id"] = "custom:" + "4" * 32
        wrapper = {"id": "custom:" + "5" * 32, "profile": profile}

        with self.assertRaisesRegex(ValueError, "не совпадает"):
            studio.normalize_audio_profile_entry(wrapper)

    def test_normalizer_custom_name_does_not_collide_with_draft_sentinel(self):
        self.assertTrue(
            studio.normalizer_profile_name_conflict("Пользовательский", [])
        )
        self.assertEqual(
            studio.unique_normalizer_profile_name("Пользовательский", []),
            "Пользовательский профиль",
        )

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
        self.assertIn("Ogg (Opus)", description)
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

        # Текст подсказки намеренно находится под кнопкой раскрытия; точная
        # строка кнопок является внутренней деталью адаптивного footer.
        self.assertIn("footer_hint.grid(", dialog_source)
        self.assertIn("apply_buttons.grid(\n            row=1", dialog_source)
        self.assertIn("close_buttons.grid(", dialog_source)
        self.assertIn('footer.bind("<Configure>"', dialog_source)
        self.assertIn('text="➕ Добавить"', dialog_source)
        self.assertIn('text="🗑 Удалить"', dialog_source)
        self.assertIn('text="Сохранить и закрыть"', dialog_source)
        self.assertNotIn('text="ОК"', dialog_source)
        self.assertNotIn("нажмите ОК", dialog_source)
        self.assertIn('candidate_config["audio_profiles"] = copy.deepcopy(staged_profiles)', commit_block)
        self.assertIn("stage_current_editor(refresh=False)", commit_block)
        self.assertIn("editor_profile_with_stable_id", commit_block)
        self.assertIn("staged_default_values", commit_block)
        self.assertIn("profile_id=apply_id", commit_block)
        self.assertIn("refresh_staged_default_snapshot", dialog_source)
        self.assertIn("self._persist_settings_snapshot(candidate_config)", commit_block)
        self.assertNotIn("self.save_settings()", commit_block)
        self.assertNotIn("self.set_ui_from_config()", commit_block)
        self.assertIn(
            'resolve_editor_before_navigation("удалением профиля")',
            dialog_source,
        )
        self.assertIn("refresh_list(select_key=editor_state[\"selection_key\"])", dialog_source)

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

    def test_profile_rejects_control_or_excessively_long_name(self):
        for name in ("Строка\nвторая", "x" * 129):
            with self.subTest(name=name[:20]):
                with self.assertRaisesRegex(ValueError, "имя аудиопрофиля"):
                    studio.make_audio_profile(name)

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


class TemplateHelperTests(unittest.TestCase):
    """Единый помощник должен повторять правила рабочих шаблонов."""

    @staticmethod
    def _field_names(context_kind):
        return {
            field_name
            for field_name, _label, _description in studio.template_helper_context(
                context_kind
            )["fields"]
        }

    def test_contexts_expose_only_supported_fields_and_modifiers(self):
        self.assertEqual(
            self._field_names("book_import"),
            studio.TEMPLATE_FIELDS_IMPORT,
        )
        self.assertEqual(self._field_names("group_sequence"), {"num"})
        self.assertEqual(
            self._field_names("output_file"),
            {
                "name", "filename", "source_name", "source_title", "book",
                "group", "title", "index", "chapter", "chapter_count",
                "group_index", "file_index",
                "part", "volume", "parts", "first_index", "last_index",
                "first_name", "last_name", "num", "range", "format", "ext",
                "author", "profile",
            },
        )
        self.assertEqual(
            self._field_names("m4b_part"),
            studio.SOURCE_M4B_TEMPLATE_FIELDS,
        )
        self.assertEqual(
            self._field_names("m4b_album"),
            studio.SOURCE_M4B_TEMPLATE_FIELDS,
        )
        self.assertEqual(
            self._field_names("m4b_chapter"),
            studio.M4B_CHAPTER_TEMPLATE_FIELDS,
        )

        book = studio.template_helper_context("book_import")
        self.assertEqual(book["start_fields"], {"num", "book_index"})
        self.assertEqual(book["width_fields"], {"num", "book_index"})
        self.assertFalse(book["empty_allowed"])

        group = studio.template_helper_context("group_sequence")
        self.assertEqual(group["start_fields"], {"num"})
        self.assertEqual(group["width_fields"], {"num"})

        output_file = studio.template_helper_context("output_file")
        self.assertEqual(
            output_file["start_fields"],
            {
                "index", "chapter", "part", "volume", "num",
                "group_index", "file_index",
            },
        )
        self.assertIn("index", output_file["width_fields"])
        self.assertTrue(output_file["empty_allowed"])

        part = studio.template_helper_context("m4b_part")
        self.assertEqual(
            part["start_fields"],
            {
                "volume", "part", "num", "index",
                "group_index", "file_index",
            },
        )
        self.assertIn("parts", part["width_fields"])
        self.assertNotIn("parts", part["start_fields"])
        self.assertTrue(part["empty_allowed"])

        chapter = studio.template_helper_context("m4b_chapter")
        self.assertIn("global_index", chapter["start_fields"])
        self.assertIn("volume_index", chapter["start_fields"])
        self.assertIn("global_index", chapter["width_fields"])
        self.assertNotIn("name", chapter["width_fields"])
        self.assertTrue(chapter["empty_allowed"])

    def test_token_builder_applies_contextual_start_and_width_rules(self):
        expected = (
            ("book_import", "title", "plain", 1, "{title}"),
            ("book_import", "book_index", "start", 8, "{book_index:8}"),
            ("group_sequence", "num", "start", 0, "{num:0}"),
            ("m4b_part", "volume", "start", 10, "{volume:10}"),
            ("m4b_part", "volume", "width", 3, "{volume:03d}"),
            (
                "m4b_chapter",
                "global_index",
                "width",
                4,
                "{global_index:04d}",
            ),
        )
        for context, field, mode, value, token in expected:
            with self.subTest(context=context, field=field, mode=mode):
                self.assertEqual(
                    studio.build_template_helper_token(
                        context,
                        field,
                        mode,
                        value,
                    ),
                    token,
                )

        self.assertEqual(
            studio.build_template_helper_token(
                "book_import",
                "book_index",
                start=8,
                width=3,
            ),
            "{book_index:8:03d}",
        )
        self.assertEqual(
            studio.build_template_helper_token(
                "m4b_chapter",
                "volume_index",
                start=0,
                width=4,
            ),
            "{volume_index:0:04d}",
        )
        self.assertEqual(
            studio.build_template_helper_token(
                "m4b_part",
                "volume",
                adaptive=True,
            ),
            "{volume:00d}",
        )
        self.assertEqual(
            studio.build_template_helper_token(
                "m4b_chapter",
                "volume_index",
                start=0,
                adaptive=True,
            ),
            "{volume_index:0:00d}",
        )

        invalid = (
            ("book_import", "book", "start", 2),
            ("book_import", "book", "width", 3),
            ("m4b_part", "parts", "start", 2),
            ("m4b_chapter", "name", "width", 3),
            ("m4b_part", "volume", "width", 0),
            ("m4b_part", "volume", "width", 10),
            ("group_sequence", "num", "start", -1),
            ("book_import", "unknown", "plain", 1),
            ("book_import", "num", "unsupported", 1),
            ("book_import", "num", "start", "не число"),
        )
        for context, field, mode, value in invalid:
            with self.subTest(context=context, field=field, mode=mode, value=value):
                with self.assertRaises(studio.TemplateError):
                    studio.build_template_helper_token(
                        context,
                        field,
                        mode,
                        value,
                    )

        with self.assertRaisesRegex(studio.TemplateError, "разрядность"):
            studio.build_template_helper_token(
                "book_import", "num", start=1, width=10
            )

        with self.assertRaises(studio.TemplateError):
            studio.template_helper_context("unknown")

    def test_custom_presets_are_normalized_and_merged_per_context(self):
        raw = {
            "book_import": [
                {"name": "  Моя серия  ", "template": "Серия {book_index:8:03d}"},
                {"name": "моя серия", "template": "Дубликат {num}"},
                {"name": "Главы книги", "template": "{num}"},
                {"name": "Сломан", "template": "{unknown}"},
                "не объект",
            ],
            "m4b_chapter": [
                {
                    "name": "Локальные главы",
                    "template": "Том {part:2:02d}. Глава {volume_index:0:03d}",
                }
            ],
            "unknown": [{"name": "Лишний", "template": "{num}"}],
        }
        normalized = studio.normalize_template_helper_presets(raw)
        self.assertEqual(
            normalized,
            {
                "book_import": [
                    {
                        "name": "Моя серия",
                        "template": "Серия {book_index:8:03d}",
                    }
                ],
                "m4b_chapter": [
                    {
                        "name": "Локальные главы",
                        "template": (
                            "Том {part:2:02d}. Глава {volume_index:0:03d}"
                        ),
                    }
                ],
            },
        )

        merged = studio.template_helper_preset_entries("book_import", normalized)
        self.assertTrue(merged[0]["builtin"])
        self.assertEqual(merged[-1]["name"], "Моя серия")
        self.assertFalse(merged[-1]["builtin"])
        self.assertEqual(
            studio.normalize_config({"template_helper_presets": raw})[
                "template_helper_presets"
            ],
            normalized,
        )

    def test_custom_presets_can_be_replaced_and_deleted_but_builtins_cannot(self):
        saved = studio.save_template_helper_preset(
            {},
            "m4b_chapter",
            "Мои метки",
            "Том {part}. Глава {volume_index}",
        )
        replaced = studio.save_template_helper_preset(
            saved,
            "m4b_chapter",
            "мои метки",
            "Том {part:1:02d}. Глава {volume_index:0:03d}",
        )
        self.assertEqual(len(replaced["m4b_chapter"]), 1)
        self.assertEqual(
            replaced["m4b_chapter"][0]["template"],
            "Том {part:1:02d}. Глава {volume_index:0:03d}",
        )
        deleted = studio.delete_template_helper_preset(
            replaced,
            "m4b_chapter",
            "МОИ МЕТКИ",
        )
        self.assertNotIn("m4b_chapter", deleted)

        with self.assertRaisesRegex(studio.TemplateError, "встроенный"):
            studio.save_template_helper_preset(
                {},
                "m4b_chapter",
                "Том и глава",
                "{name}",
            )
        with self.assertRaisesRegex(studio.TemplateError, "встроенный"):
            studio.delete_template_helper_preset(
                {},
                "m4b_chapter",
                "Том и глава",
            )

    def test_m4b_chapter_presets_include_practical_volume_and_chapter_label(self):
        presets = dict(studio.template_helper_context("m4b_chapter")["presets"])
        self.assertEqual(
            presets["Том и глава"],
            "Том {part}. Глава {volume_index}",
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_chapter",
                presets["Том и глава"],
            ),
            (
                "Том 1. Глава 1",
                "Том 1. Глава 2",
                "Том 2. Глава 1",
            ),
        )

    def test_template_helper_exposes_physical_filename_separately_from_title(self):
        self.assertIn("filename", self._field_names("m4b_part"))
        self.assertIn("filename", self._field_names("m4b_chapter"))
        rendered = studio.render_filename_template(
            "{filename} - {title}",
            {
                "filename": "001-original",
                "title": "Отредактированная глава",
            },
            studio.TEMPLATE_FIELDS_OUTPUT,
        )
        self.assertEqual(rendered, "001-original - Отредактированная глава")

    def test_template_helper_preview_uses_supplied_m4b_source_snapshot(self):
        preview_data = {
            "contexts": [
                {
                    "book": "Реальная книга",
                    "name": "Том 01",
                    "group": "Том 01",
                    "title": "Том 01",
                    "source_name": "Том 01",
                    "filename": "001_глава",
                    "first_name": "001_глава",
                    "last_name": "010_глава",
                    "part": 1,
                    "volume": 1,
                    "num": 1,
                    "first_index": 1,
                    "last_index": 10,
                    "range": "001-010",
                    "parts": 2,
                },
            ],
            "chapter_groups": [
                {
                    "book": "Реальная книга",
                    "group": "Том 01",
                    "part": 1,
                    "parts": 1,
                    "chapters": [
                        {
                            "title": "Глава из Title",
                            "source_name": "001_глава",
                            "source_title": "Исходный Title",
                            "path": "/tmp/001_глава.txt",
                        }
                    ],
                }
            ],
        }
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part",
                "{book} - {filename} - {name}",
                preview_data=preview_data,
            ),
            ("Реальная книга - 001_глава - Том 01 Часть 1.m4b",),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_chapter",
                "{filename}: {name}",
                preview_data=preview_data,
            ),
            ("001_глава: Глава из Title",),
        )

    def test_output_file_helper_preview_keeps_title_and_physical_filename_distinct(self):
        preview = studio.render_template_helper_preview(
            "output_file",
            "{filename} - {index:03d} - {name}",
            preview_data={
                "file_contexts": [
                    {
                        "filename": "book",
                        "source_name": "book",
                        "source_title": "Исходная глава",
                        "name": "Переименованная глава",
                        "title": "Переименованная глава",
                        "book": "Книга",
                        "group": "Книга",
                        "index": 2,
                        "chapter": 2,
                        "chapter_count": 4,
                        "range": "2",
                        "format": "opus",
                        "ext": "opus",
                    }
                ]
            },
        )
        self.assertEqual(
            preview,
            ("book - 002 - Переименованная глава.opus",),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "output_file",
                "{index:10:03d} - {name}",
                preview_data={
                    "file_contexts": [
                        {"index": 1, "name": "Первая", "ext": "mp3"},
                        {"index": 2, "name": "Вторая", "ext": "mp3"},
                    ]
                },
            ),
            ("010 - Первая.mp3", "011 - Вторая.mp3"),
        )

        self.assertEqual(
            studio.render_template_helper_preview(
                "output_file",
                "",
                preview_data={
                    "file_contexts": [
                        {
                            "name": "Переименованная глава",
                            "format": "mp3",
                            "ext": "mp3",
                        }
                    ]
                },
            ),
            ("Переименованная глава.mp3",),
        )

    def test_template_helper_import_preview_can_mark_unparsed_metadata(self):
        preview = studio.render_template_helper_preview(
            "book_import",
            "{book} - {title} - {author}",
            book_names=("/texts/Реальная книга.epub",),
            preview_data={
                "book_names": ("/texts/Реальная книга.epub",),
                "actual_files": True,
            },
        )
        self.assertEqual(
            preview[0],
            "Реальная книга - Заголовок главы - Автор не извлечён.txt",
        )

    def test_template_helper_group_preview_accounts_for_existing_names(self):
        self.assertEqual(
            studio.render_template_helper_preview(
                "group_sequence",
                "Том {num}",
                preview_data={"existing_names": ("Том 1", "Том 2")},
            ),
            ("Том 3", "Том 4", "Том 5"),
        )

    def test_combined_start_and_padding_preview_uses_each_working_renderer(self):
        self.assertEqual(
            studio.render_template_helper_preview(
                "book_import",
                "Серия {book_index:8:03d}-{num:15:04d}",
            ),
            (
                "Серия 008-0015.txt",
                "Серия 008-0016.txt",
                "Серия 009-0015.txt",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "group_sequence",
                "Том {num:10:03d}",
            ),
            ("Том 010", "Том 011", "Том 012"),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part",
                "{book} Том {volume:10:03d}",
            ),
            (
                "Моя книга Том 010.m4b",
                "Моя книга Том 011.m4b",
                "Моя книга Том 012.m4b",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_album",
                "{book} Том {volume:10:03d}",
            ),
            (
                "Моя книга Том 010",
                "Моя книга Том 011",
                "Моя книга Том 012",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_chapter",
                "{global_index:10:04d}/{volume_index:20:03d}",
            ),
            ("0010/020", "0011/021", "0012/020"),
        )

    def test_import_preview_matches_renderer_and_real_save(self):
        template = "Серия {book_index:8}-{num:15} - {name} - {title}"
        book_names = ("Первый.epub", "Второй.fb2", "Третий.docx")
        preview = studio.render_template_helper_preview(
            "book_import",
            template,
            book_names=book_names,
        )
        expected = (
            studio.render_book_import_filename(
                template,
                book_name="Первый",
                chapter_title="Глава 1. Начало",
                author="Автор Книги",
                chapter_position=1,
                chapter_count=12,
                book_position=1,
                book_count=3,
            ),
            studio.render_book_import_filename(
                template,
                book_name="Первый",
                chapter_title="Глава 2. Продолжение",
                author="Автор Книги",
                chapter_position=2,
                chapter_count=12,
                book_position=1,
                book_count=3,
            ),
            studio.render_book_import_filename(
                template,
                book_name="Второй",
                chapter_title="Глава 1. Начало",
                author="Автор Книги",
                chapter_position=1,
                chapter_count=12,
                book_position=2,
                book_count=3,
            ),
        )
        self.assertEqual(preview, expected)

        first_chapters = [
            (
                "Глава 1. Начало" if index == 1 else
                "Глава 2. Продолжение" if index == 2 else f"Глава {index}",
                f"Текст {index}",
            )
            for index in range(1, 13)
        ]
        second_chapters = [
            ("Глава 1. Начало" if index == 1 else f"Глава {index}", f"Текст {index}")
            for index in range(1, 13)
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            first_saved = studio.BookExtractor.save_chapters(
                first_chapters,
                tempdir,
                "Первый.epub",
                template,
                author="Автор Книги",
                book_index=1,
                book_count=3,
            )
            second_saved = studio.BookExtractor.save_chapters(
                second_chapters,
                tempdir,
                "Второй.fb2",
                template,
                author="Автор Книги",
                book_index=2,
                book_count=3,
            )

        self.assertEqual(preview[:2], tuple(first_saved[:2]))
        self.assertEqual(preview[2], second_saved[0])

        filename_preview = studio.render_template_helper_preview(
            "book_import",
            "{filename} - {title}",
            book_names=("Первый.epub",),
            preview_data={"actual_files": True},
        )
        self.assertTrue(filename_preview[0].startswith("Первый - "))

    def test_import_single_file_preview_uses_one_chapter_per_book(self):
        template = "{book_index:8} - {name} - {num:15} - {title}"
        preview = studio.render_template_helper_preview(
            "book_import",
            template,
            book_names=("Первый.epub", "Второй.fb2", "Третий.docx"),
            single_file=True,
        )

        self.assertEqual(
            preview,
            (
                "08 - Первый - 15 - Книга.txt",
                "09 - Второй - 15 - Книга.txt",
            ),
        )

    def test_m4b_part_preview_applies_suffix_start_and_width(self):
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{book} Том {part:10}"
            ),
            (
                "Моя книга Том 10.m4b",
                "Моя книга Том 11.m4b",
                "Моя книга Том 12.m4b",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview(
                "m4b_part", "{book} Том {part:03d}"
            ),
            (
                "Моя книга Том 001.m4b",
                "Моя книга Том 002.m4b",
                "Моя книга Том 003.m4b",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", "{book}"),
            (
                "Моя книга Часть 1.m4b",
                "Моя книга Часть 2.m4b",
                "Моя книга Часть 3.m4b",
            ),
        )

    def test_source_group_helper_describes_groups_without_output_extension(self):
        fields = {
            name: (label, description)
            for name, label, description in studio.template_helper_context(
                "source_group"
            )["fields"]
        }
        self.assertEqual(fields["part"][0], "Номер группы")
        self.assertIn("не формат выхода", fields["format"][1])
        self.assertEqual(
            studio.render_template_helper_preview(
                "source_group", "{book} Группа {part}"
            ),
            (
                "Моя книга Группа 1",
                "Моя книга Группа 2",
                "Моя книга Группа 3",
            ),
        )

    def test_m4b_chapter_preview_resets_volume_and_continues_global_counter(self):
        preview = studio.render_template_helper_preview(
            "m4b_chapter",
            "{global_index:10}/{volume_index:20}/{global_index:03d} {name}",
        )

        self.assertEqual(
            preview,
            (
                "10/20/001 Пролог",
                "11/21/002 Первая встреча",
                "12/20/003 Продолжение",
            ),
        )

    def test_empty_template_semantics_are_contextual(self):
        for context in ("book_import", "group_sequence"):
            with self.subTest(context=context):
                with self.assertRaises(studio.TemplateError):
                    studio.render_template_helper_preview(context, "")

        self.assertEqual(
            studio.render_template_helper_preview("m4b_part", ""),
            (
                "Моя книга Часть 1.m4b",
                "Моя книга Часть 2.m4b",
                "Моя книга Часть 3.m4b",
            ),
        )
        self.assertEqual(
            studio.render_template_helper_preview("m4b_album", ""),
            ("(тег альбома из настроек)",) * 3,
        )
        self.assertEqual(
            studio.render_template_helper_preview("m4b_chapter", ""),
            ("Пролог", "Первая встреча", "Продолжение"),
        )

    def test_preview_rejects_unknown_unbalanced_and_conflicting_templates(self):
        invalid = (
            ("book_import", "{unknown}"),
            ("book_import", "{book"),
            ("book_import", "{num:3}-{num:8}"),
            ("group_sequence", "Том {unknown}"),
            ("group_sequence", "Том {num"),
            ("m4b_part", "{unknown}"),
            ("m4b_part", "{book"),
            ("m4b_chapter", "{unknown}"),
            ("m4b_chapter", "{name"),
        )
        for context, template in invalid:
            with self.subTest(context=context, template=template):
                with self.assertRaises(studio.TemplateError):
                    studio.render_template_helper_preview(context, template)


class BookImportTests(unittest.TestCase):
    def test_batch_queue_preserves_order_and_ignores_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            first = Path(tempdir) / "Первая.epub"
            second = Path(tempdir) / "Вторая.fb2"

            paths = studio.merge_book_import_paths(
                [str(first)],
                [str(second), str(first), "", str(second)],
            )

        self.assertEqual(paths, [str(first), str(second)])

    def test_batch_queue_uses_file_identity_for_existing_aliases(self):
        with tempfile.TemporaryDirectory() as tempdir:
            original = Path(tempdir) / "Книга.epub"
            alias = Path(tempdir) / "Другое имя.epub"
            original.write_bytes(b"book")
            alias.hardlink_to(original)

            paths = studio.merge_book_import_paths([], [original, alias])

        self.assertEqual(paths, [str(original)])

    def test_batch_queue_can_sort_move_and_replace_paths(self):
        paths = [
            "/books/Том 10.epub",
            "/books/Том 2.epub",
            "/books/Том 1.epub",
            "/other/Том 2.epub",
        ]

        sorted_paths = studio.sort_book_import_paths(paths)
        self.assertEqual(
            [Path(path).name for path in sorted_paths],
            ["Том 1.epub", "Том 2.epub", "Том 2.epub", "Том 10.epub"],
        )

        moved, selection = studio.move_book_import_paths(
            ["a.epub", "b.epub", "c.epub", "d.epub"],
            (1, 2),
            -1,
        )
        self.assertEqual(moved, ["b.epub", "c.epub", "a.epub", "d.epub"])
        self.assertEqual(selection, (0, 1))
        restored, selection = studio.move_book_import_paths(moved, selection, 1)
        self.assertEqual(restored, ["a.epub", "b.epub", "c.epub", "d.epub"])
        self.assertEqual(selection, (1, 2))
        self.assertFalse(
            studio.can_move_book_import_paths(restored, (0, 1), -1)
        )
        self.assertFalse(
            studio.can_move_book_import_paths(restored, (2, 3), 1)
        )
        self.assertTrue(
            studio.can_move_book_import_paths(restored, (0, 2), -1)
        )

        replaced = studio.replace_book_import_path(paths, 0, "~/Новый том.epub")
        self.assertEqual(replaced[0], str(Path("~/Новый том.epub").expanduser()))
        with self.assertRaisesRegex(ValueError, "уже находится"):
            studio.replace_book_import_path(paths, 0, paths[1])

    def test_batch_queue_naturally_sorts_standalone_roman_numbers(self):
        paths = [
            f"/books/Книга {number}.epub"
            for number in ("X", "IV", "IX", "I", "VIII", "V", "II", "VII", "III", "VI")
        ]

        sorted_paths = studio.sort_book_import_paths(paths)

        self.assertEqual(
            [Path(path).stem.rsplit(" ", 1)[-1] for path in sorted_paths],
            ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"],
        )

    def test_extract_file_single_mode_keeps_epub_author_and_joins_chapters(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "book.epub"
            source.touch()
            with mock.patch.object(
                studio.BookExtractor,
                "extract_epub",
                return_value=([("Первая", "Один"), ("Вторая", "Два")], "Автор"),
            ):
                chapters, author = studio.BookExtractor.extract_file(
                    source,
                    single_file=True,
                )

        self.assertEqual(chapters, [("Книга", "Один\n\nДва")])
        self.assertEqual(author, "Автор")

    def test_batch_import_resets_numbers_for_each_book_and_continues_after_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_dir = Path(tempdir) / "sources"
            output_dir = Path(tempdir) / "output"
            source_dir.mkdir()
            first = source_dir / "Том 1.txt"
            missing = source_dir / "Нет книги.txt"
            second = source_dir / "Том 2.txt"
            first.write_text(
                "Глава 1\nПервый текст.\nГлава 2\nВторой текст.",
                encoding="utf-8",
            )
            second.write_text(
                "Глава 1\nТретий текст.\nГлава 2\nЧетвёртый текст.",
                encoding="utf-8",
            )
            progress = []

            results = studio.BookExtractor.import_files(
                [first, missing, second],
                output_dir,
                "{name}-{num}",
                regex_pattern=r"^Глава \d+",
                progress_callback=lambda *values: progress.append(values),
            )

            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                [
                    "Том 1-1.txt",
                    "Том 1-2.txt",
                    "Том 2-1.txt",
                    "Том 2-2.txt",
                ],
            )

        self.assertIsNone(results[0]["error"])
        self.assertIn("Файл не найден", results[1]["error"])
        self.assertIsNone(results[2]["error"])
        self.assertEqual([result["book_index"] for result in results], [1, 2, 3])
        self.assertEqual([Path(item["path"]).name for item in results], [
            "Том 1.txt",
            "Нет книги.txt",
            "Том 2.txt",
        ])
        self.assertIn("error", [event[3] for event in progress])
        self.assertEqual(progress[-1][3], "complete")

    def test_batch_import_has_independent_book_and_chapter_indices(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_dir = Path(tempdir) / "sources"
            output_dir = Path(tempdir) / "output"
            source_dir.mkdir()
            first = source_dir / "Первый.txt"
            second = source_dir / "Второй.txt"
            content = "Глава 1\nТекст 1.\nГлава 2\nТекст 2."
            first.write_text(content, encoding="utf-8")
            second.write_text(content, encoding="utf-8")

            results = studio.BookExtractor.import_files(
                [first, second],
                output_dir,
                "{book_index}-{num}",
                regex_pattern=r"^Глава \d+",
            )

            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                ["1-1.txt", "1-2.txt", "2-1.txt", "2-2.txt"],
            )
            self.assertTrue(all(result["error"] is None for result in results))

    def test_batch_book_index_supports_custom_start_and_keeps_error_gap(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_dir = Path(tempdir) / "sources"
            output_dir = Path(tempdir) / "output"
            source_dir.mkdir()
            first = source_dir / "Первый.txt"
            missing = source_dir / "Пропущенный.txt"
            third = source_dir / "Третий.txt"
            first.write_text("Первый текст.", encoding="utf-8")
            third.write_text("Третий текст.", encoding="utf-8")

            results = studio.BookExtractor.import_files(
                [first, missing, third],
                output_dir,
                "Серия {book_index:8}",
                single_file=True,
            )

            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                ["Серия 08.txt", "Серия 10.txt"],
            )
            self.assertIsNotNone(results[1]["error"])
            self.assertEqual(results[2]["book_index"], 3)

    def test_batch_single_file_mode_creates_one_txt_per_source_book(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source_dir = Path(tempdir) / "sources"
            output_dir = Path(tempdir) / "output"
            source_dir.mkdir()
            first = source_dir / "Первая.txt"
            second = source_dir / "Вторая.txt"
            first.write_text("Глава 1\nОдин.\nГлава 2\nДва.", encoding="utf-8")
            second.write_text("Глава 1\nТри.", encoding="utf-8")

            results = studio.BookExtractor.import_files(
                [first, second],
                output_dir,
                "{name}",
                regex_pattern=r"^Глава \d+",
                single_file=True,
            )

            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                ["Вторая.txt", "Первая.txt"],
            )
            self.assertEqual(
                (output_dir / "Первая.txt").read_text(encoding="utf-8"),
                "Глава 1\nОдин.\nГлава 2\nДва.",
            )

        self.assertTrue(all(result["error"] is None for result in results))
        self.assertEqual([result["chapter_count"] for result in results], [1, 1])

    def test_save_chapters_logs_each_created_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertLogs(level="INFO") as captured:
                studio.BookExtractor.save_chapters(
                    [("Один", "Текст 1"), ("Два", "Текст 2")],
                    tempdir,
                    "book.epub",
                    "{num}-{title}",
                )

        messages = [
            message for message in captured.output
            if "Импорт книги: сохранена глава" in message
        ]
        self.assertEqual(len(messages), 2)
        self.assertIn("1/2", messages[0])
        self.assertIn("2/2", messages[1])

    def test_import_template_rejects_unknown_fields_and_invalid_parameters(self):
        for template, message in (
            ("{index}", "недоступно"),
            ("{title:10}", "не поддерживает числовой параметр"),
            ("{num:3}-{num:8}", "разные начальные номера"),
            ("{num:3:02d}-{num:3:04d}", "разная разрядность"),
        ):
            with self.subTest(template=template):
                with self.assertRaisesRegex(studio.TemplateError, message):
                    studio.BookExtractor.save_chapters(
                        [("Глава", "Текст")],
                        tempfile.gettempdir(),
                        "book.epub",
                        template,
                    )

        with self.assertRaisesRegex(
            studio.TemplateError,
            r"^поле \{index\} недоступно в этом контексте$",
        ):
            studio._parse_book_import_template("{index}")
        with self.assertRaisesRegex(
            studio.TemplateError,
            "начальный номер.*слишком длинный",
        ):
            studio._parse_book_import_template("{num:" + "9" * 5000 + "}")

    def test_import_template_combines_counter_start_and_zero_padding(self):
        rendered = studio.render_book_import_filename(
            "Серия {book_index:8:03d}-{num:15:04d} - {title}",
            book_name="Книга",
            chapter_title="Начало",
            chapter_position=2,
            chapter_count=12,
            book_position=2,
            book_count=3,
        )
        self.assertEqual(rendered, "Серия 009-0016 - Начало.txt")

        width_only = studio.render_book_import_filename(
            "{book_index:03d}-{num:04d}",
            book_name="Книга",
            chapter_title="Начало",
            chapter_position=2,
            chapter_count=12,
            book_position=2,
            book_count=3,
        )
        self.assertEqual(width_only, "002-0002.txt")

    def test_import_template_does_not_reprocess_placeholders_inside_values(self):
        with tempfile.TemporaryDirectory() as tempdir:
            saved = studio.BookExtractor.save_chapters(
                [("{author}", "Текст")],
                tempdir,
                "Том {title}.epub",
                "{name}--{title}--{author}--{{буквально}}",
                author="{name}",
            )

            self.assertEqual(
                saved,
                ["Том {title}--{author}--{name}--{буквально}.txt"],
            )

    def test_save_chapters_does_not_overwrite_unicode_equivalent_name(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output_dir = Path(tempdir)
            existing = output_dir / "И\u0306ога.txt"
            existing.write_text("Старый текст", encoding="utf-8")

            saved = studio.BookExtractor.save_chapters(
                [("Йога", "Новый текст")],
                output_dir,
                "book.epub",
                "{title}",
            )

            self.assertEqual(saved, ["Йога (2).txt"])
            self.assertEqual(existing.read_text(encoding="utf-8"), "Старый текст")
            self.assertEqual(
                (output_dir / saved[0]).read_text(encoding="utf-8"),
                "Новый текст",
            )

    def test_save_chapters_removes_partial_result_after_write_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output_dir = Path(tempdir)
            original_write = studio._write_text_atomic
            calls = 0

            def fail_second_write(path, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk full")
                return original_write(path, content)

            with mock.patch.object(
                studio,
                "_write_text_atomic",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    studio.BookExtractor.save_chapters(
                        [("Один", "Текст 1"), ("Два", "Текст 2")],
                        output_dir,
                        "book.epub",
                        "{num}-{title}",
                    )

            self.assertEqual(list(output_dir.glob("*.txt")), [])

    def test_epub_cover_page_is_not_exported_as_a_text_chapter(self):
        class Item:
            def __init__(self, item_id, file_name, html):
                self.id = item_id
                self.file_name = file_name
                self._html = html.encode("utf-8")

            def get_content(self):
                return self._html

        cover = Item(
            "cover",
            "cover.xhtml",
            "<html><body><img src='cover.jpg'/><h1>Книга</h1></body></html>",
        )
        description = Item(
            "description",
            "description.xhtml",
            "<html><body><h1>Описание</h1><p>Аннотация.</p></body></html>",
        )
        chapter = Item(
            "chapter1",
            "chapters/chapter1.xhtml",
            "<html><body><h1>Глава 1</h1><p>Текст.</p></body></html>",
        )
        book = mock.Mock()
        book.toc = [
            studio.epub.Link("cover.xhtml", "Обложка", "cover"),
            studio.epub.Link("description.xhtml", "Описание", "description"),
            studio.epub.Link(
                "chapters/chapter1.xhtml", "Глава 1", "chapter1"
            ),
        ]
        book.get_items.return_value = [cover, description, chapter]
        # Допустимое, но пустое ``dc:creator`` должно остаться пустой строкой,
        # а не передавать ``None`` в шаблоны имён и вызывающий код.
        book.get_metadata.return_value = [(None, {})]

        with mock.patch.object(studio.epub, "read_epub", return_value=book):
            chapters, author = studio.BookExtractor.extract_epub("book.epub")

        self.assertEqual(author, "")
        self.assertEqual(
            chapters,
            [
                ("Описание", "Описание\nАннотация."),
                ("Глава 1", "Глава 1\nТекст."),
            ],
        )

    def test_epub_chapter_named_cover_is_kept_without_cover_file_hint(self):
        link = studio.epub.Link(
            "chapters/chapter42.xhtml", "Обложка", "chapter42"
        )
        item = mock.Mock(id="chapter42")

        self.assertFalse(
            studio.BookExtractor._is_epub_cover_document(item, link)
        )

    def test_epub_spine_cover_requires_both_manifest_hints(self):
        explicit_cover = mock.Mock(id="cover", file_name="cover.xhtml")
        chapter_named_cover = mock.Mock(
            id="chapter42", file_name="cover.xhtml"
        )

        self.assertTrue(
            studio.BookExtractor._is_epub_cover_document(explicit_cover)
        )
        self.assertFalse(
            studio.BookExtractor._is_epub_cover_document(chapter_named_cover)
        )

    def test_epub_toc_decodes_hrefs_and_deduplicates_anchor_links(self):
        class Item:
            id = "chapter1"
            file_name = "chapters/Глава 1.xhtml"

            def get_content(self):
                return "<html><body><p>Текст.</p></body></html>".encode()

        item = Item()
        book = mock.Mock()
        book.toc = [
            studio.epub.Link(
                "chapters/%D0%93%D0%BB%D0%B0%D0%B2%D0%B0%201.xhtml#one",
                "Глава 1",
                "chapter1",
            ),
            studio.epub.Link(
                "./chapters/Глава 1.xhtml#two",
                "Глава 1 (продолжение)",
                "chapter1",
            ),
        ]
        book.get_items.return_value = [item]
        book.get_metadata.return_value = []

        with mock.patch.object(studio.epub, "read_epub", return_value=book):
            chapters, _author = studio.BookExtractor.extract_epub("book.epub")

        self.assertEqual(chapters, [("Глава 1", "Текст.")])

    def test_epub_toc_without_matching_documents_falls_back_to_spine(self):
        class Item:
            id = "chapter1"
            file_name = "chapter.xhtml"

            def get_content(self):
                return "<html><body><p>Текст из spine.</p></body></html>".encode()

            def get_type(self):
                return studio.ebooklib.ITEM_DOCUMENT

        item = Item()
        book = mock.Mock()
        book.toc = [studio.epub.Link("missing.xhtml", "Потеряно", "missing")]
        book.get_items.return_value = [item]
        book.spine = [("chapter1", "yes")]
        book.get_item_with_id.return_value = item
        book.get_metadata.return_value = []

        with mock.patch.object(studio.epub, "read_epub", return_value=book):
            chapters, _author = studio.BookExtractor.extract_epub("book.epub")

        self.assertEqual(chapters, [("Глава", "Текст из spine.")])

    def test_epub_toc_with_only_cover_falls_back_to_spine_chapters(self):
        class Item:
            def __init__(self, item_id, file_name, text, item_type):
                self.id = item_id
                self.file_name = file_name
                self._text = text
                self._type = item_type

            def get_content(self):
                return self._text.encode()

            def get_type(self):
                return self._type

        cover = Item(
            "cover",
            "cover.xhtml",
            "<html><body><img src='cover.jpg'/></body></html>",
            studio.ebooklib.ITEM_DOCUMENT,
        )
        chapter = Item(
            "chapter1",
            "chapter.xhtml",
            "<html><body><p>Текст главы.</p></body></html>",
            studio.ebooklib.ITEM_DOCUMENT,
        )
        book = mock.Mock()
        book.toc = [studio.epub.Link("cover.xhtml", "Обложка", "cover")]
        book.get_items.return_value = [cover, chapter]
        book.spine = [("cover", "yes"), ("chapter1", "yes")]
        book.get_item_with_id.side_effect = {"cover": cover, "chapter1": chapter}.get
        book.get_metadata.return_value = []

        with mock.patch.object(studio.epub, "read_epub", return_value=book):
            chapters, _author = studio.BookExtractor.extract_epub("book.epub")

        self.assertEqual(chapters, [("Глава", "Текст главы.")])

    def test_docx_localized_heading_styles_split_chapters(self):
        class Paragraph:
            def __init__(self, style_name, text, style_id=""):
                self.style = mock.Mock(name=style_name)
                self.style.name = style_name
                self.style.style_id = style_id
                self.text = text

        document = mock.Mock()
        document.paragraphs = [
            Paragraph("Заголовок 1", "Глава 1", "Heading1"),
            Paragraph("Обычный", "Текст один"),
            Paragraph("Заголовок 1", "Глава 2", "Heading1"),
            Paragraph("Обычный", "Текст два"),
        ]

        with mock.patch.object(studio.docx, "Document", return_value=document):
            chapters = studio.BookExtractor.extract_docx("book.docx")

        self.assertEqual(
            chapters,
            [
                ("Глава 1", "Глава 1\nТекст один"),
                ("Глава 2", "Глава 2\nТекст два"),
            ],
        )

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


class SynthesisStatusTests(unittest.TestCase):
    def test_m4b_activity_subject_identifies_group_sources_and_output(self):
        subject = studio.format_m4b_activity_subject(
            "Книга",
            "Книга 01.m4b",
            ("/tmp/Глава 01.txt", "/tmp/Глава 02.txt", "/tmp/Глава 03.txt"),
            chapter_start=1,
            chapter_end=3,
        )
        self.assertIn("Книга", subject)
        self.assertIn("Глава 01.txt", subject)
        self.assertIn("Глава 03.txt", subject)
        self.assertIn("главы 1–3", subject)
        self.assertIn("Книга 01.m4b", subject)

    def test_encoding_status_includes_output_format_when_available(self):
        self.assertEqual(
            studio.format_synthesis_encoding_status(),
            "⚙️ Сборка аудиофайла...",
        )
        self.assertEqual(
            studio.format_synthesis_encoding_status("opus"),
            "⚙️ Сборка аудиофайла (Opus)...",
        )
        self.assertEqual(
            studio.format_synthesis_encoding_status(".m4b"),
            "⚙️ Сборка аудиофайла (M4B)...",
        )
        self.assertEqual(
            studio.format_synthesis_encoding_status(["mp3", "opus", "mp3"]),
            "⚙️ Сборка аудиофайла (MP3, Opus)...",
        )

    def test_encoding_callback_contract_remains_one_argument(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "chapter.txt"
            source.write_text("Текст.", encoding="utf-8")
            processor = object.__new__(studio.TTSProcessor)
            processor.cfg = {
                "output_format": "opus",
                "output_dir": tempdir,
            }
            processor.process_raw_text = mock.Mock()
            calls = []

            processor.process_text_file(
                source,
                encoding_callback=lambda filename: calls.append(filename),
            )
            wrapped_callback = processor.process_raw_text.call_args.args[6]
            wrapped_callback("chapter.opus")

        self.assertEqual(calls, ["chapter.txt"])

    def test_tree_status_uses_format_for_background_encoding(self):
        app = object.__new__(studio.TTSApp)
        app.tree = mock.Mock()
        app.tree.exists.return_value = True
        app.tree.item.side_effect = [(), (), None]

        app.update_file_status("chapter.txt", "encoding", "opus")

        setter = app.tree.item.call_args_list[-1]
        self.assertEqual(
            setter.kwargs["values"],
            ("⚙️ Сборка аудиофайла (Opus)...", "chapter.txt"),
        )
        self.assertEqual(setter.kwargs["tags"], ("processing",))

    def test_virtual_m4b_group_status_preserves_estimated_duration_column(self):
        app = object.__new__(studio.TTSApp)
        app.tree = mock.Mock()
        app.tree.exists.return_value = True
        app.tree.item.side_effect = [
            ("📁 Группа", "≈ 01:20:00"),
            ("queued",),
            None,
        ]
        app._source_group_ids = {"m4b_plan:1"}

        app.update_file_status("m4b_plan:1", "encoding", "m4b")

        setter = app.tree.item.call_args_list[-1]
        self.assertEqual(
            setter.kwargs["values"],
            ("⚙️ Сборка аудиофайла (M4B)...", "≈ 01:20:00"),
        )
        self.assertEqual(setter.kwargs["tags"], ("processing",))

    def test_sequential_export_status_is_compact_and_normalizes_stages(self):
        self.assertEqual(
            studio.format_sequential_export_status(
                3,
                12,
                "Том 03.m4b",
                "M4B: кодирование аудио…",
            ),
            "Готово 3/12 · Том 03.m4b · кодирование",
        )
        self.assertEqual(
            studio.format_sequential_export_status(
                3,
                12,
                "Том 03.m4b",
                "FFmpeg: подготовка главы 2 / 8...",
            ),
            "Готово 3/12 · Том 03.m4b · глава 2/8",
        )
        self.assertEqual(
            studio.format_sequential_export_status(
                12,
                12,
                "Том 12.m4b",
                "M4B: готово…",
            ),
            "Готово 12/12 · Том 12.m4b",
        )
        self.assertEqual(
            studio.format_sequential_export_status(
                12,
                12,
                "Том 12.m4b",
            ),
            "Готово 12/12 · Том 12.m4b",
        )

    def test_completed_compact_m4b_status_keeps_build_prefix(self):
        app = object.__new__(studio.TTSApp)
        app.file_progress = mock.MagicMock()
        app.file_progress.__getitem__.return_value = 0
        app.lbl_file_pct = mock.Mock()
        app.lbl_file_pct.cget.return_value = "0%"
        app._set_source_activity_status = mock.Mock()

        app.update_progress_ui(100, "Готово 1/1 · Том 01.m4b")

        app._set_source_activity_status.assert_called_once_with(
            "Сборка",
            "Готово 1/1 · Том 01.m4b",
            "info",
        )

    def test_m4b_target_progress_commits_only_completed_outputs(self):
        """Внутренние обратные вызовы M4B не двигают индикатор дробными шагами."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book} {range}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "book"
        file_ids = ("chapter-1", "chapter-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 10,
            }
            for file_id in file_ids
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b", output_dir=tempdir, bitrate="64k"
            ).to_dict()
            events = []
            output_names = []

            def fake_m4b(_audio_files, output_path, **kwargs):
                output_names.append(Path(output_path).name)
                callback = kwargs.get("progress_callback")
                if callback is not None:
                    callback(1, 3, "подготовка")
                    callback(-1, 3, "кодирование")
                    callback(3, 3, "M4B: готово")
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                    progress_callback=lambda current, total, text: events.append(
                        (current, total, text)
                    ),
                )

        self.assertTrue(events)
        self.assertEqual(
            [event[0] for event in events if event[0] != studio.PROGRESS_STATUS_ONLY],
            [1],
        )
        self.assertTrue(
            all(
                event[0] == studio.PROGRESS_STATUS_ONLY
                or float(event[0]).is_integer()
                for event in events
            )
        )
        self.assertEqual(len(output_names), 1)
        output_name = output_names[0]
        status_events = [
            event
            for event in events
            if event[0] == studio.PROGRESS_STATUS_ONLY
        ]
        completed_events = [
            event
            for event in events
            if event[0] != studio.PROGRESS_STATUS_ONLY
        ]
        self.assertTrue(status_events)
        self.assertEqual(
            completed_events,
            [(1, 1, f"Готово 1/1 · {output_name}")],
        )
        for _current, total, text in status_events:
            self.assertEqual(total, 1)
            self.assertTrue(text.startswith(f"Готово 0/1 · {output_name}"))
            self.assertNotIn("chapter-1.mp3", text)
            self.assertNotIn("chapter-2.mp3", text)
            self.assertNotIn("Book:", text)
        self.assertNotIn(
            f"Готово 0/1 · {output_name}",
            [text for _current, _total, text in status_events],
        )


class MacAquaRestoreTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.root.winfo_exists.return_value = True
        app.root.state.return_value = "normal"
        app.root.focus_displayof.return_value = None
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
            (150, app._refresh_mac_after_restore),
        )
        self.assertTrue(app._mac_restore_progress_pending)

    def test_restore_map_marks_progress_for_late_native_refresh(self):
        app = self.make_app()
        app._refresh_mac_aqua_widget_states = mock.Mock()

        app._on_mac_restore_map(mock.Mock(widget=app.root))

        self.assertTrue(app._mac_restore_progress_pending)
        app._refresh_mac_aqua_widget_states.assert_not_called()

    def test_restore_map_finishes_idle_drawing_before_return(self):
        """Потомки дорисовываются сразу; сброс Aqua остаётся отложенным."""
        app = self.make_app()
        order = []

        def schedule(delay, callback):
            order.append("schedule")
            self.assertEqual(delay, 150)
            self.assertIs(callback.__self__, app)
            self.assertTrue(app._mac_restore_progress_pending)
            return "restore-after"

        def draw():
            order.append("draw")
            self.assertTrue(app._mac_restore_idle_flush_in_progress)

        app.root.after.side_effect = schedule
        app.root.update_idletasks.side_effect = draw

        app._on_mac_restore_map(mock.Mock(widget=app.root))

        self.assertEqual(order, ["schedule", "draw"])
        app.root.update_idletasks.assert_called_once_with()
        self.assertFalse(app._mac_restore_idle_flush_in_progress)
        app.root.focus_force.assert_not_called()
        app.root.update.assert_not_called()

    def test_restore_map_ignores_children_startup_closing_and_hidden_root(self):
        """Обработка восстановления ограничена уже работающим главным окном."""
        for case in ("child", "startup", "closing", "iconic", "destroyed"):
            with self.subTest(case=case):
                app = self.make_app()
                event = mock.Mock(widget=app.root)
                if case == "child":
                    event.widget = mock.Mock()
                elif case == "startup":
                    app._mac_startup_focus_done = False
                elif case == "closing":
                    app._is_closing = True
                elif case == "iconic":
                    app.root.state.return_value = "iconic"
                else:
                    app.root.winfo_exists.return_value = False

                app._on_mac_restore_map(event)

                app.root.after.assert_not_called()
                app.root.update_idletasks.assert_not_called()

    def test_restore_map_does_not_reenter_idle_drawing(self):
        """Повторный корневой Map из idle не запускает вложенную отрисовку."""
        app = self.make_app()
        event = mock.Mock(widget=app.root)
        app.root.update_idletasks.side_effect = lambda: app._on_mac_restore_map(event)

        app._on_mac_restore_map(event)

        app.root.after.assert_called_once_with(150, app._refresh_mac_after_restore)
        app.root.update_idletasks.assert_called_once_with()
        self.assertFalse(app._mac_restore_idle_flush_in_progress)

    def test_restore_map_recovers_after_tcl_error(self):
        """Ошибка Tk не оставляет защиту от повторного входа включённой."""
        for operation in ("winfo_exists", "update_idletasks"):
            with self.subTest(operation=operation):
                app = self.make_app()
                failing = getattr(app.root, operation)
                failing.side_effect = studio.tk.TclError("Окно пока недоступно")
                event = mock.Mock(widget=app.root)

                app._on_mac_restore_map(event)
                self.assertFalse(app._mac_restore_idle_flush_in_progress)

                failing.side_effect = None
                app.root.winfo_exists.return_value = True
                app.root.reset_mock()
                app._on_mac_restore_map(event)

                app.root.update_idletasks.assert_called_once_with()
                self.assertTrue(app._mac_restore_progress_pending)
                self.assertFalse(app._mac_restore_idle_flush_in_progress)

    @mock.patch.object(studio.ttk, "Style")
    def test_activate_without_map_refreshes_native_widget_states(self, style_class):
        app = self.make_app()
        app._mac_window_active = True
        app._mac_restore_progress_pending = False
        app._mac_restore_activation_refresh_pending = False
        app._refresh_mac_aqua_widget_states = mock.Mock()
        style_class.return_value.theme_use.side_effect = ("aqua", None)
        event = mock.Mock(widget=app.root)

        app._on_mac_restore_deactivate(event)
        app._on_mac_restore_activate(event)
        self.assertTrue(app._mac_restore_progress_pending)
        self.assertEqual(
            app.root.after.call_args.args,
            (150, app._refresh_mac_after_restore),
        )
        app._refresh_mac_after_restore()

        app._refresh_mac_aqua_widget_states.assert_called_once_with()
        self.assertFalse(app._mac_restore_progress_pending)

    @mock.patch.object(studio.ttk, "Style")
    def test_active_restore_refreshes_native_widget_states(self, style_class):
        app = self.make_app()
        app._mac_window_active = True
        app._mac_restore_progress_pending = True
        app._refresh_mac_aqua_widget_states = mock.Mock()
        style_class.return_value.theme_use.side_effect = ("aqua", None)

        app._refresh_mac_after_restore()

        app._refresh_mac_aqua_widget_states.assert_called_once_with()
        self.assertFalse(app._mac_restore_progress_pending)

    @mock.patch.object(studio.ttk, "Style")
    def test_map_only_restore_refreshes_states_without_tk_focus(
        self, style_class
    ):
        app = self.make_app()
        app._mac_window_active = False
        app._refresh_mac_aqua_widget_states = mock.Mock()
        style_class.return_value.theme_use.side_effect = ("aqua", None)

        app._on_mac_restore_map(mock.Mock(widget=app.root))
        app._refresh_mac_after_restore()

        app._refresh_mac_aqua_widget_states.assert_called_once_with()
        self.assertFalse(app._mac_restore_progress_pending)
        self.assertTrue(app._mac_restore_activation_refresh_pending)
        app.root.focus_displayof.assert_not_called()

    @mock.patch.object(studio.ttk, "Style")
    def test_later_activate_refreshes_after_inactive_map(self, style_class):
        app = self.make_app()
        app._mac_window_active = False
        app._refresh_mac_aqua_widget_states = mock.Mock()
        style_class.return_value.theme_use.side_effect = (
            "aqua", None, "aqua", None
        )

        app._on_mac_restore_map(mock.Mock(widget=app.root))
        app._refresh_mac_after_restore()
        self.assertEqual(app._refresh_mac_aqua_widget_states.call_count, 1)
        self.assertTrue(app._mac_restore_activation_refresh_pending)

        app._on_mac_restore_activate(mock.Mock(widget=app.root))
        app._refresh_mac_after_restore()

        self.assertEqual(app._refresh_mac_aqua_widget_states.call_count, 2)
        self.assertFalse(app._mac_restore_progress_pending)
        self.assertFalse(app._mac_restore_activation_refresh_pending)

    def test_native_refresh_redraws_only_widgets_with_stale_background(self):
        """Штатно активные и уничтоженные контролы не требуют перерисовки."""
        app = self.make_app()
        background = app.file_progress = mock.Mock()
        active = app.total_progress = mock.Mock()
        destroyed = app.export_progress = mock.Mock()
        selected = app.chk_auto_scroll = mock.Mock()
        background.instate.return_value = True
        active.instate.return_value = False
        destroyed.winfo_exists.return_value = False
        selected.instate.return_value = True

        app._refresh_mac_aqua_widget_states()

        background.state.assert_called_once_with(["!background"])
        selected.state.assert_called_once_with(["!background"])
        active.state.assert_not_called()
        destroyed.instate.assert_not_called()
        destroyed.state.assert_not_called()

        # Повторный Map/Activate не ставит ещё одну перерисовку, когда флаг снят.
        background.instate.return_value = False
        selected.instate.return_value = False
        app._refresh_mac_aqua_widget_states()

        background.state.assert_called_once_with(["!background"])
        selected.state.assert_called_once_with(["!background"])
        active.state.assert_not_called()
        destroyed.state.assert_not_called()

    def test_native_refresh_preserves_widgets_layout_values_and_animation(self):
        """Восстановление сбрасывает состояние Aqua у уже существующих виджетов."""
        try:
            root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        self.addCleanup(root.destroy)

        frame = studio.ttk.Frame(root)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        export_frame = studio.ttk.Frame(root)
        export_frame.grid(row=1, column=0, sticky="ew")
        app = object.__new__(studio.TTSApp)
        app.file_progress = studio.ttk.Progressbar(
            frame, mode="indeterminate", maximum=100, value=0
        )
        app.total_progress = studio.ttk.Progressbar(
            frame, mode="determinate", maximum=240, value=73
        )
        app.export_progress = studio.ttk.Progressbar(
            export_frame, mode="determinate", maximum=100, value=31,
            length=145, style="Horizontal.TProgressbar",
        )
        app.file_progress.grid(row=0, column=0, sticky="ew")
        app.total_progress.grid(row=1, column=0, sticky="ew")
        app.total_progress.state(["disabled"])
        app.export_progress.pack(side="right", fill="x", expand=True, padx=(8, 0))
        app.auto_scroll_var = studio.tk.BooleanVar(root, value=True)
        app.chk_auto_scroll = studio.ttk.Checkbutton(
            frame, text="Автопрокрутка", variable=app.auto_scroll_var
        )
        app.chk_auto_scroll.grid(row=3, column=0, sticky="e")
        app.chk_auto_scroll.state(["disabled"])
        for widget in (
            app.file_progress,
            app.total_progress,
            app.export_progress,
            app.chk_auto_scroll,
        ):
            widget.state(["background"])
        app.file_progress.start(12)
        self.addCleanup(app.file_progress.stop)
        root.update()

        widgets = (
            app.file_progress,
            app.total_progress,
            app.export_progress,
            app.chk_auto_scroll,
        )
        identities = tuple(str(widget) for widget in widgets)
        placements = tuple(
            widget.pack_info() if widget.winfo_manager() == "pack" else widget.grid_info()
            for widget in widgets
        )
        values = (
            float(app.total_progress["value"]),
            float(app.export_progress["value"]),
            float(app.total_progress["maximum"]),
        )
        initial_animation_value = float(app.file_progress["value"])

        app._refresh_mac_aqua_widget_states()
        root.update_idletasks()

        for name, original in zip(
            ("file_progress", "total_progress", "export_progress", "chk_auto_scroll"),
            widgets,
        ):
            self.assertIs(getattr(app, name), original)
            self.assertTrue(original.winfo_exists())
        self.assertEqual(tuple(str(widget) for widget in widgets), identities)
        self.assertEqual(
            tuple(
                widget.pack_info() if widget.winfo_manager() == "pack" else widget.grid_info()
                for widget in widgets
            ),
            placements,
        )
        self.assertEqual(
            (
                float(app.total_progress["value"]),
                float(app.export_progress["value"]),
                float(app.total_progress["maximum"]),
            ),
            values,
        )
        self.assertEqual(str(app.file_progress["mode"]), "indeterminate")
        self.assertEqual(float(app.export_progress["length"]), 145)
        self.assertEqual(str(app.export_progress["style"]), "Horizontal.TProgressbar")
        self.assertIn("disabled", app.total_progress.state())
        self.assertIn("selected", app.chk_auto_scroll.state())
        self.assertIn("disabled", app.chk_auto_scroll.state())
        for widget in widgets:
            self.assertNotIn("background", widget.state())

        root.after(80, root.quit)
        root.mainloop()
        self.assertGreater(float(app.file_progress["value"]), initial_animation_value)







    @mock.patch.object(studio.ttk, "Style")
    def test_restore_updates_only_native_widgets_without_full_theme_flash(
        self, style_class
    ):
        app = self.make_app()
        style = style_class.return_value
        style.theme_use.side_effect = ("aqua", None)
        app._refresh_mac_aqua_widget_states = mock.Mock()

        app._refresh_mac_after_restore()

        style_class.assert_not_called()
        app.root.tk.call.assert_not_called()
        app.root.event_generate.assert_not_called()
        app.root.after_idle.assert_not_called()
        app._refresh_mac_aqua_widget_states.assert_not_called()
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


class ModalCancelTransactionTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def fake_modal():
        buttons = {}
        dialog = mock.Mock()

        def make_button(_parent, **options):
            buttons[options["text"]] = options["command"]
            return mock.Mock()

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(studio.tk, "Toplevel", return_value=dialog))
            stack.enter_context(mock.patch.object(studio.tk, "BooleanVar", side_effect=lambda value=False: mock.Mock(get=lambda: value)))
            stack.enter_context(mock.patch.object(studio.tk, "StringVar", side_effect=lambda value="": mock.Mock(get=lambda: value)))
            for name in ("Label", "Checkbutton", "Radiobutton", "Separator", "Frame"):
                stack.enter_context(mock.patch.object(studio.ttk, name, return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(studio.ttk, "Button", side_effect=make_button))
            yield buttons, dialog

    @staticmethod
    def make_app():
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.config = {"last_config_dir": "old-config", "last_glossary_dir": "old-glossary"}
        app._source_plan_running = False
        app._config_dialog_initial_dir = mock.Mock(return_value="/tmp")
        app._glossary_dialog_initial_dir = mock.Mock(return_value="/tmp")
        app._center_popup = mock.Mock()
        app._remember_dialog_directory = mock.Mock()
        app.save_settings = mock.Mock()
        app._persist_settings_snapshot = mock.Mock()
        app.update_config_from_ui = mock.Mock()
        app._show_error = mock.Mock()
        app._show_warning = mock.Mock()
        return app

    def test_cancel_config_import_does_not_save_browse_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filepath = Path(tempdir) / "settings.json"
            filepath.write_text("{}", encoding="utf-8")
            app = self.make_app()
            with self.fake_modal() as (buttons, dialog), mock.patch.object(
                studio.filedialog, "askopenfilename", return_value=str(filepath)
            ):
                app.import_config()
                buttons["Отмена"]()

            dialog.destroy.assert_called_once()
            self.assertEqual(app.config["last_config_dir"], "old-config")
            app._remember_dialog_directory.assert_not_called()
            app.save_settings.assert_not_called()
            app._persist_settings_snapshot.assert_not_called()

    def test_config_import_saves_selected_directory_in_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filepath = Path(tempdir) / "settings.json"
            filepath.write_text("{}", encoding="utf-8")
            app = self.make_app()
            app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
            app.config["last_config_dir"] = "old-config"
            app.shared_rate_limiter = mock.Mock()
            app.full_ui_refresh = mock.Mock()
            app._sync_glossary_editor_cache = mock.Mock()
            app.load_files = mock.Mock()
            app._show_info = mock.Mock()
            with self.fake_modal() as (buttons, _dialog), mock.patch.object(
                studio.filedialog, "askopenfilename", return_value=str(filepath)
            ), mock.patch.object(studio, "ensure_config_directories"):
                app.import_config()
                buttons["Импортировать"]()

            saved_config = app._persist_settings_snapshot.call_args.args[0]
            self.assertEqual(saved_config["last_config_dir"], str(filepath.parent))
            self.assertEqual(app.config["last_config_dir"], str(filepath.parent))
            app._remember_dialog_directory.assert_not_called()

    def test_cancel_glossary_import_does_not_save_browse_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filepath = Path(tempdir) / "glossary.json"
            filepath.write_text(
                json.dumps(studio.empty_glossary_data()), encoding="utf-8"
            )
            app = self.make_app()
            with self.fake_modal() as (buttons, dialog), mock.patch.object(
                studio.filedialog, "askopenfilename", return_value=str(filepath)
            ):
                app.import_glossary()
                buttons["Отмена"]()

            dialog.destroy.assert_called_once()
            self.assertEqual(app.config["last_glossary_dir"], "old-glossary")
            app._remember_dialog_directory.assert_not_called()
            app.save_settings.assert_not_called()

    def test_glossary_import_remembers_directory_after_successful_save(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filepath = Path(tempdir) / "glossary.json"
            filepath.write_text(
                json.dumps(studio.empty_glossary_data()), encoding="utf-8"
            )
            app = self.make_app()
            app.txt_glossary = mock.Mock()
            app.txt_glossary.get.return_value = json.dumps(
                studio.empty_glossary_data()
            )
            app._replace_glossary_editor_data = mock.Mock()
            app.save_glossary_ui = mock.Mock(return_value=True)
            app._show_info = mock.Mock()
            with self.fake_modal() as (buttons, _dialog), mock.patch.object(
                studio.filedialog, "askopenfilename", return_value=str(filepath)
            ):
                app.import_glossary()
                app._remember_dialog_directory.assert_not_called()
                buttons["Импортировать (Добавить)"]()

            app.save_glossary_ui.assert_called_once_with(show_popup=False)
            app._remember_dialog_directory.assert_called_once_with(
                "last_glossary_dir", str(filepath)
            )

    def test_config_export_updates_live_values_only_after_file_choice(self):
        app = self.make_app()
        app.config["marker"] = "old"
        app.update_config_from_ui.side_effect = lambda: app.config.update(marker="new")
        app._write_json_atomic = mock.Mock()
        app._show_info = mock.Mock()
        with self.fake_modal() as (buttons, dialog), mock.patch.object(
            studio.filedialog, "asksaveasfilename", return_value="/tmp/profile.json"
        ), mock.patch.object(
            studio, "select_config_values", side_effect=lambda config, *_args, **_kwargs: dict(config)
        ):
            app.export_config()
            app.update_config_from_ui.assert_not_called()
            self.assertEqual(app.config["marker"], "old")
            buttons["Экспортировать"]()

        dialog.destroy.assert_called_once()
        app.update_config_from_ui.assert_called_once()
        app._write_json_atomic.assert_called_once_with(
            "/tmp/profile.json",
            {"last_config_dir": "old-config", "last_glossary_dir": "old-glossary", "marker": "new"},
        )
        app._remember_dialog_directory.assert_called_once_with(
            "last_config_dir", "/tmp/profile.json"
        )

    def test_cancel_config_export_does_not_read_unsaved_ui_values(self):
        app = self.make_app()
        with self.fake_modal() as (buttons, dialog):
            app.export_config()
            buttons["Отмена"]()

        dialog.destroy.assert_called_once()
        app.update_config_from_ui.assert_not_called()
        app.save_settings.assert_not_called()


class ExportLayoutContractTests(unittest.TestCase):
    def test_cancel_adding_audio_files_leaves_export_project_unchanged(self):
        app = object.__new__(studio.TTSApp)
        app._export_lock = False
        app._export_running = False
        app.config = {"last_browse_dir": "old", "export_dir": "old-export"}
        app.export_groups = {"group-1": {"name": "Existing"}}
        app.export_files = {"file-1": {"path": "existing.mp3"}}
        app.export_tree = mock.Mock()
        app.export_outdir_var = mock.Mock()
        app.save_settings = mock.Mock()
        app.add_export_group = mock.Mock()
        app._set_export_ui_state = mock.Mock()
        app._choose_export_destination = mock.Mock(return_value=None)

        with mock.patch.object(
            studio.filedialog,
            "askopenfilenames",
            return_value=("/tmp/new.mp3",),
        ):
            app.add_export_files()

        app._choose_export_destination.assert_called_once()
        self.assertEqual(app.config, {"last_browse_dir": "old", "export_dir": "old-export"})
        self.assertEqual(app.export_groups, {"group-1": {"name": "Existing"}})
        self.assertEqual(app.export_files, {"file-1": {"path": "existing.mp3"}})
        app.export_tree.assert_not_called()
        app.export_outdir_var.assert_not_called()
        app.save_settings.assert_not_called()
        app.add_export_group.assert_not_called()
        app._set_export_ui_state.assert_not_called()
        self.assertFalse(app._export_lock)

    def test_cancel_adding_audio_folder_leaves_export_project_unchanged(self):
        with tempfile.TemporaryDirectory() as tempdir:
            folder = Path(tempdir) / "audio"
            folder.mkdir()
            (folder / "chapter.mp3").touch()

            app = object.__new__(studio.TTSApp)
            app._export_lock = False
            app._export_running = False
            app.config = {"last_browse_dir": "old", "export_dir": "old-export"}
            app.export_groups = {"group-1": {"name": "Existing"}}
            app.export_files = {"file-1": {"path": "existing.mp3"}}
            app.export_tree = mock.Mock()
            app.save_settings = mock.Mock()
            app.add_export_group = mock.Mock()
            app.add_export_files = mock.Mock()
            app._choose_export_destination = mock.Mock(return_value=None)

            with mock.patch.object(studio.filedialog, "askdirectory", return_value=str(folder)):
                app.add_export_folder()

            app._choose_export_destination.assert_called_once()
            self.assertEqual(app.config, {"last_browse_dir": "old", "export_dir": "old-export"})
            self.assertEqual(app.export_groups, {"group-1": {"name": "Existing"}})
            self.assertEqual(app.export_files, {"file-1": {"path": "existing.mp3"}})
            app.export_tree.assert_not_called()
            app.save_settings.assert_not_called()
            app.add_export_group.assert_not_called()
            app.add_export_files.assert_not_called()
            self.assertFalse(app._export_lock)

    def test_export_tree_actions_follow_content_and_selection(self):
        class FakeTree:
            def __init__(self):
                self.roots = []
                self.children = {}
                self.selected = ()

            def get_children(self, parent=""):
                if parent == "":
                    return tuple(self.roots)
                return tuple(self.children.get(parent, ()))

            def selection(self):
                return self.selected

            def exists(self, item):
                return item in self.roots or any(
                    item in children for children in self.children.values()
                )

            def parent(self, item):
                for parent, children in self.children.items():
                    if item in children:
                        return parent
                return ""

        app = object.__new__(studio.TTSApp)
        app._export_lock = False
        app._export_running = False
        app.export_groups = {}
        app.export_files = {}
        app.export_tree = FakeTree()
        action_names = (
            "btn_export_remove",
            "btn_export_group_selected",
            "btn_export_ungroup",
            "btn_export_up",
            "btn_export_down",
            "btn_export_auto_split",
            "btn_export_clear",
        )
        for name in action_names:
            setattr(app, name, mock.Mock())
        app.lbl_export_empty = mock.Mock()

        app._refresh_export_tree_action_states()

        for name in action_names:
            getattr(app, name).configure.assert_called_once_with(
                state=studio.tk.DISABLED
            )
        app.lbl_export_empty.place.assert_called_once()
        app.lbl_export_empty.place_forget.assert_not_called()

        for name in action_names:
            getattr(app, name).reset_mock()
        app.lbl_export_empty.reset_mock()
        app.export_tree.roots = ["group-1"]
        app.export_tree.children = {"group-1": ["file-1", "file-2"]}
        app.export_groups = {"group-1": {"name": "Том 1"}}
        app.export_files = {"file-1": {}, "file-2": {}}

        app._refresh_export_tree_action_states()

        app.btn_export_auto_split.configure.assert_called_once_with(
            state=studio.tk.NORMAL
        )
        app.btn_export_clear.configure.assert_called_once_with(
            state=studio.tk.NORMAL
        )
        for name in (
            "btn_export_remove",
            "btn_export_group_selected",
            "btn_export_ungroup",
            "btn_export_up",
            "btn_export_down",
        ):
            getattr(app, name).configure.assert_called_once_with(
                state=studio.tk.DISABLED
            )
        app.lbl_export_empty.place_forget.assert_called_once_with()

        for name in action_names:
            getattr(app, name).reset_mock()
        app.export_tree.selected = ("file-1",)

        app._refresh_export_tree_action_states()

        app.btn_export_remove.configure.assert_called_once_with(
            state=studio.tk.NORMAL
        )
        app.btn_export_group_selected.configure.assert_called_once_with(
            state=studio.tk.NORMAL
        )
        app.btn_export_ungroup.configure.assert_called_once_with(
            state=studio.tk.DISABLED
        )
        app.btn_export_up.configure.assert_called_once_with(
            state=studio.tk.DISABLED
        )
        app.btn_export_down.configure.assert_called_once_with(
            state=studio.tk.NORMAL
        )

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
        self.assertIn(
            'self.grp_notebook.add(self.grp_tab_mass, text="Массово")',
            basic_block,
        )
        self.assertIn(
            "self.btn_mass_apply_basic = ttk.Button(\n"
            "            self.grp_tab_mass,",
            basic_block,
        )
        self.assertIn("self.btn_mass_apply_basic.pack(", basic_block)

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
        self.assertIn('values=["mp3", "wav", "ogg", "opus", "m4a"]', format_block)
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
        # Компактная сводка выводится общим адаптивным помощником подписей.
        # Даже в узком окне она остаётся в одну строку, а подробности доступны
        # в окне профиля.
        self.assertIn("self._bind_compact_label(", summary_block)
        self.assertNotIn("wraplength=max(320, event.width - 10)", summary_block)

        # Подробные числовые параметры, эффекты и поля M4B редактируются во
        # всплывающем окне; на основной вкладке остаются краткая сводка и
        # заметная кнопка открытия.
        self.assertIn('text="⚙ Параметры сборки…"', setup_source)
        self.assertIn("command=self.open_export_settings_dialog", setup_source)
        self.assertIn("def open_export_settings_dialog(self):", source)
        self.assertIn("staged = {", source)
        self.assertIn('text="Применить"', source)
        self.assertIn('text="Отмена"', source)

        # Расширенные параметры сборки используют вертикальные двухколоночные
        # формы вместо прежней таблицы с четырьмя колонками. В каждой секции
        # свои колонки подписи и управления, поэтому узкое окно не сжимает
        # несвязанные параметры в одну строку.
        settings_start = source.index("    def open_export_settings_dialog(self):")
        settings_end = source.index("    # --- Логика интерфейса Сборщика", settings_start)
        settings_dialog = source[settings_start:settings_end]
        self.assertIn('def make_section(title):', settings_dialog)
        self.assertIn('ttk.LabelFrame(body, text=title', settings_dialog)
        self.assertIn('output_section = make_section("Формат и параметры вывода")', settings_dialog)
        self.assertIn('effects_section = make_section("Эффекты постобработки")', settings_dialog)
        self.assertIn('m4b_section = make_section("M4B (AAC-LC, главы)")', settings_dialog)
        self.assertNotIn('column=2, width=', settings_dialog)
        self.assertIn('m4b_control_widgets', settings_dialog)

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

    def test_source_synthesis_advanced_settings_are_compact_and_transactional(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_main_tab(self):")
        end = source.index("    # --- Вкладка \"Прямой синтез\" ---", start)
        setup_source = source[start:end]
        self.assertIn('text="⚙ Настройки синтеза…"', setup_source)
        self.assertIn("command=self.open_source_synthesis_settings_dialog", setup_source)
        self.assertIn("self.lbl_source_settings_summary", setup_source)
        # Старые ссылки фонового обработчика сохраняются как неуправляемые элементы
        # совместимости; на видимой панели остаются только кнопка и сводка.
        visible_source_end = setup_source.index(
            "    def _create_source_synthesis_settings_dialog(self):"
        )
        visible_source = setup_source[:visible_source_end]
        self.assertNotIn('text="Включать подпапки (дерево)"', visible_source)
        self.assertNotIn('text="Текст уже подготовлен (только этот запуск)"', visible_source)

        dialog_start = source.index(
            "    def open_source_synthesis_settings_dialog(self):"
        )
        dialog_end = source.index("    # --- Вкладка \"Прямой синтез\" ---", dialog_start)
        dialog_source = source[dialog_start:dialog_end]
        self.assertIn('dialog.title("Продвинутые настройки синтеза")', dialog_source)
        self.assertIn('text="Применить"', dialog_source)
        self.assertIn('text="Отмена"', dialog_source)
        self.assertIn("candidate = normalize_config(copy.deepcopy(self.config))", dialog_source)
        self.assertIn("self._persist_settings_snapshot(candidate)", dialog_source)

        progress_start = setup_source.index("prog_frame = ttk.Frame(")
        progress_end = setup_source.index(
            "# Настройки очереди раньше занимали", progress_start
        )
        progress_source = setup_source[progress_start:progress_end]
        self.assertNotIn("width=110", progress_source)
        self.assertNotIn("length=600", progress_source)
        self.assertIn("prog_frame.columnconfigure(1, weight=1)", progress_source)
        self.assertIn("sticky=tk.EW", progress_source)
        self.assertIn("def _render_source_activity_status(self):", source)
        self.assertIn("middle_ellipsize_to_width(", source)

        auto_split_start = source.index("    def auto_split_export(self):")
        auto_split_end = source.index("    # --- Процесс Экспорта ---", auto_split_start)
        auto_split_source = source[auto_split_start:auto_split_end]
        self.assertIn("N — старт", auto_split_source)

    def test_source_tab_keeps_destination_in_settings_and_has_editable_groups(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_main_tab(self):")
        end = source.index('    # --- Вкладка "Прямой синтез" ---', start)
        setup_source = source[start:end]

        # Вкладка источников по-прежнему владеет рабочей ``StringVar`` из
        # «Настройки → Папки», но не должна показывать второе поле и кнопку
        # назначения на основной панели.
        self.assertIn("self.source_output_dir_var = tk.StringVar", setup_source)
        self.assertNotIn('text="Папка для аудио:"', setup_source)
        self.assertNotIn("choose_source_output_dir", setup_source)

        # Одна видимая колонка имени (#0) и статус; старое значение имени
        # файла остаётся внутренним для обратных вызовов и совместимых тестов.
        self.assertIn('displaycolumns=("status",)', setup_source)
        self.assertIn('self.tree.heading("#0", text="Имя файла")', setup_source)
        self.assertNotIn('self.tree.heading("filename", text="Имя файла")', setup_source)

        self.assertIn('text="✏ Переименовать часть"', setup_source)
        self.assertIn("command=self.rename_selected_source_group", setup_source)
        self.assertIn('self.tree.bind(\n            "<Double-1>"', setup_source)
        self.assertIn("def rename_selected_source_group(self):", source)
        self.assertIn('name_template"] = None', source)

    def test_source_group_tag_dialog_is_transactional_and_inheritable(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        setup_start = source.index("    def setup_main_tab(self):")
        setup_end = source.index(
            '    # --- Вкладка "Прямой синтез" ---', setup_start
        )
        setup_source = source[setup_start:setup_end]
        self.assertIn('text="🏷 Теги части…"', setup_source)
        self.assertIn(
            "command=self.edit_selected_source_group_tags", setup_source
        )

        dialog_start = source.index(
            "    def edit_selected_source_group_tags(self):"
        )
        dialog_end = source.index(
            "    def group_selected_source_items(self):", dialog_start
        )
        dialog_source = source[dialog_start:dialog_end]
        for key in (
            "title",
            "artist",
            "album_artist",
            "album",
            "genre",
            "composer",
            "year",
            "cover",
        ):
            self.assertIn(f'"{key}"', dialog_source)
        for label in (
            "Том {volume_number} из {volume_total}",
            "Переопределить",
            "Наследовать всё",
            "Отмена",
            "Применить",
        ):
            self.assertIn(label, dialog_source)
        self.assertIn("if override_vars[key].get()", dialog_source)
        self.assertIn(
            "self._commit_source_group_metadata_overrides(", dialog_source
        )
        self.assertIn("filedialog.askopenfilename(", dialog_source)
        self.assertIn('dialog.protocol("WM_DELETE_WINDOW", close_dialog)', dialog_source)
        # Кнопка блокируется и возвращается вместе с остальными
        # действиями плана при расчёте и синтезе.
        self.assertGreaterEqual(source.count("btn_source_group_tags"), 8)

    def test_direct_synthesis_keeps_its_destination_field(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index('    def setup_direct_tab(self):')
        end = source.index('    def setup_import_tab(self):', start)
        direct = source[start:end]
        self.assertIn("direct_path_frame = ttk.Frame(frame)", direct)
        self.assertIn('text="Папка:"', direct)
        self.assertIn("self.direct_output_dir_var = tk.StringVar", direct)
        self.assertIn("choose_direct_output_dir", direct)

    def test_source_plan_dialog_describes_template_fields_and_hints_are_collapsible(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def setup_main_tab(self):")
        end = source.index('    # --- Вкладка "Прямой синтез" ---', start)
        setup_source = source[start:end]
        self.assertIn("self._make_collapsible_hint(", setup_source)
        self.assertIn('title="О плане групп"', setup_source)
        self.assertIn('text="🧩 Подготовить план групп…"', setup_source)
        self.assertIn('title="О ручном разбиении"', setup_source)

        dialog_start = source.index("    def prepare_m4b_source_plan(self):")
        dialog_end = source.index("    def _finish_m4b_source_plan(", dialog_start)
        dialog_source = source[dialog_start:dialog_end]
        self.assertIn('title="Поля шаблона"', dialog_source)
        for field in ("{book}", "{name}", "{part}", "{range}", "{first_name}", "{last_name}"):
            self.assertIn(field, dialog_source)

        targets_start = source.index("    def open_output_targets_dialog(self):")
        targets_end = source.index("    def _choose_output_target_dir", targets_start)
        targets_dialog = source[targets_start:targets_end]
        self.assertIn('title="О наборе выходов"', targets_dialog)
        self.assertIn("target_hint_shell.pack(", targets_dialog)
        self.assertEqual(targets_dialog.count('title="О наборе выходов"'), 1)

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
        self.assertIn('text="📁 Нормализовать папку…"', setup_source)
        self.assertIn("command=self.open_batch_normalization_dialog", setup_source)
        self.assertIn("self.lbl_normalizer_scope_status", setup_source)
        self.assertIn("ttk.Frame(main_pane, width=440)", setup_source)
        self.assertIn("main_pane.add(text_pane, weight=2)", setup_source)
        self.assertIn("main_pane.add(settings_pane, weight=3)", setup_source)
        self.assertEqual(setup_source.count("\n            width=44,\n"), 2)
        self.assertIn("self.normalizer_font_combobox", setup_source)
        self.assertIn("textvariable=self.font_size_var", setup_source)
        self.assertIn("self.root.after(10, self.update_fonts)", setup_source)

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
            "self.btn_mass_apply_basic = ttk.Button(\n"
            "            self.grp_tab_mass,",
            basic_block,
        )
        self.assertIn("self.btn_mass_apply_basic.pack(", basic_block)

        disable_start = source.index("    def _disable_export_settings(self):")
        disable_end = source.index("    def on_export_tree_select(self, event):")
        state_block = source[disable_start:disable_end]
        self.assertGreaterEqual(
            state_block.count(
                "self.grp_tab_mass,"
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
            "    def setup_normalizer_tab(self):", manager_start
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

    def test_parallel_export_status_shows_progress_active_count_and_subject(self):
        status = studio.format_parallel_export_status(
            completed=3,
            total=12,
            active_count=2,
            subject="Том 03.opus",
        )

        self.assertEqual(
            status,
            "Готово 3/12 · активно: 2 · Том 03.opus",
        )
        self.assertEqual(
            studio.format_parallel_export_status(12, 12, 0),
            "Готово 12/12",
        )

    def test_parallel_export_status_clamps_counts_and_shortens_long_subject(self):
        long_subject = (
            "Очень длинное начало "
            + "промежуточный фрагмент " * 12
            + "Том 99.m4b"
        )

        status = studio.format_parallel_export_status(
            completed=50,
            total=8,
            active_count=-4,
            subject=long_subject,
            max_subject_chars=44,
        )

        prefix = "Готово 8/8 · "
        self.assertTrue(status.startswith(prefix))
        subject = status.removeprefix(prefix)
        self.assertEqual(len(subject), 44)
        self.assertTrue(subject.startswith("Очень длинное начало"))
        self.assertTrue(subject.endswith("Том 99.m4b"))
        self.assertIn("…", subject)
        self.assertNotIn("активно:", status)

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
        self.assertIn("settings_snapshot=export_runtime_config", worker)
        self.assertIn('"export_m4b_template": m4b_template', worker)
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


class SourceTreeScrollTests(unittest.TestCase):
    def test_auto_scroll_centers_current_file_after_tab_is_shown(self):
        try:
            root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.geometry("500x360+60+60")
        notebook = studio.ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        source = studio.ttk.Frame(notebook)
        other = studio.ttk.Frame(notebook)
        notebook.add(source, text="Файлы")
        notebook.add(other, text="Другая вкладка")
        tree = studio.ttk.Treeview(source)
        tree.pack(fill="both", expand=True)
        for index in range(100):
            tree.insert("", "end", iid=f"file-{index}", text=f"Файл {index}")
        app = object.__new__(studio.TTSApp)
        app.tree = tree
        app.auto_scroll_var = studio.tk.BooleanVar(value=True)
        app.current_processing_file = "file-50"
        tree.bind("<Map>", app._on_source_tree_map, add="+")
        root.update()

        notebook.select(other)
        root.update()
        app.scroll_to_current()
        notebook.select(source)
        root.update()

        bounds = tree.bbox("file-50")
        self.assertTrue(bounds)
        self.assertAlmostEqual(
            bounds[1] + bounds[3] / 2,
            tree.winfo_height() / 2,
            delta=bounds[3],
        )

    def test_current_file_is_centered_in_flat_and_nested_trees(self):
        try:
            root = studio.tk.Tk()
        except studio.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.geometry("500x360+60+60")
        tree = studio.ttk.Treeview(root, columns=("status",), show="tree headings")
        tree.pack(fill="both", expand=True)
        app = object.__new__(studio.TTSApp)
        app.tree = tree

        for nested in (False, True):
            with self.subTest(nested=nested):
                tree.delete(*tree.get_children())
                parent = ""
                if nested:
                    parent = tree.insert("", "end", iid="group", text="Группа", open=False)
                for index in range(100):
                    tree.insert(parent, "end", iid=f"file-{index}", text=f"Файл {index}")
                root.update()

                app.current_processing_file = "file-50"
                app.scroll_to_current()
                root.update_idletasks()

                if nested:
                    self.assertTrue(tree.item("group", "open"))
                bounds = tree.bbox("file-50")
                self.assertTrue(bounds)
                row_middle = bounds[1] + bounds[3] / 2
                self.assertAlmostEqual(
                    row_middle, tree.winfo_height() / 2, delta=bounds[3]
                )


class TkLayoutRegressionTests(unittest.TestCase):
    def test_hint_collapse_keeps_dialog_size_and_normalizer_actions_fit(self):
        script = textwrap.dedent(
            """
            import importlib.util
            import json
            import logging
            import os
            import shutil
            import sys
            import tempfile
            import tkinter as tk
            from contextlib import ExitStack
            from pathlib import Path

            original_directory = Path.cwd()
            with tempfile.TemporaryDirectory(prefix="stts_ui_test_") as temporary, ExitStack() as cleanup:
                cleanup.callback(os.chdir, original_directory)
                cleanup.callback(logging.shutdown)
                isolated = Path(temporary) / "SileroTTS_Studio.py"
                shutil.copy2(sys.argv[1], isolated)
                spec = importlib.util.spec_from_file_location("stts_ui_test", isolated)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                try:
                    root = tk.Tk()
                except tk.TclError as exc:
                    print("SKIP_TK_UI=" + str(exc))
                    raise SystemExit(0)
                root.withdraw()
                try:
                    app = module.TTSApp(root)
                    for geometry in ("1100x700+60+70", "900x600+60+70"):
                        root.geometry(geometry)
                        root.deiconify()
                        for tab in app.notebook.tabs():
                            app.notebook.select(tab)
                            root.update()
                    app.notebook.select(app.tab_normalizer)
                    root.update()
                    actions = app.btn_save_normalized_text.master
                    batch = app.btn_batch_normalize_text.master
                    rows = [
                        (actions.winfo_reqwidth(), actions.winfo_width()),
                        (batch.winfo_reqwidth(), batch.winfo_width()),
                    ]
                    app.open_source_synthesis_settings_dialog()
                    root.update()
                    dialog = app._source_settings_dialog

                    def descendants(widget):
                        for child in widget.winfo_children():
                            yield child
                            yield from descendants(child)

                    hint = next(
                        widget for widget in descendants(dialog)
                        if widget.winfo_class() == "TButton"
                        and "Как работают настройки" in widget.cget("text")
                    )
                    initial_height = dialog.winfo_height()
                    hint.invoke()
                    root.update()
                    hint.invoke()
                    root.update()
                    collapsed_height = dialog.winfo_height()
                    dialog.destroy()

                    root.geometry("900x520+60+70")
                    app.notebook.select(app.tab_settings)
                    root.update()
                    app.settings_vars["api_token"].set("test-token")
                    token_initially_masked = (
                        app.api_token_entry.cget("show") == "*"
                        and not app.api_token_visible_var.get()
                    )
                    app.api_token_show_check.invoke()
                    token_revealed = (
                        app.api_token_entry.cget("show") == ""
                        and app.settings_vars["api_token"].get() == "test-token"
                    )
                    app.update_config_from_ui()
                    token_saved = app.config["api_token"] == "test-token"
                    app.set_ui_from_config()
                    token_remasked_on_reload = (
                        app.api_token_entry.cget("show") == "*"
                        and not app.api_token_visible_var.get()
                        and app.settings_vars["api_token"].get() == "test-token"
                    )
                    limit_var = str(app.settings_vars["max_parallel_encodes"])
                    limit_entry = next(
                        widget for widget in descendants(app.tab_settings)
                        if widget.winfo_class() == "TEntry"
                        and str(widget.cget("textvariable")) == limit_var
                    )
                    api_canvas = limit_entry.master
                    while api_canvas.winfo_class() != "Canvas":
                        api_canvas = api_canvas.master
                    initial_scroll = api_canvas.yview()
                    api_canvas.yview_moveto(1)
                    root.update()
                    print("UI_LAYOUT_REPORT=" + json.dumps({
                        "rows": rows,
                        "initial_height": initial_height,
                        "collapsed_height": collapsed_height,
                        "token_initially_masked": token_initially_masked,
                        "token_revealed": token_revealed,
                        "token_saved": token_saved,
                        "token_remasked_on_reload": token_remasked_on_reload,
                        "initial_api_scroll": initial_scroll,
                        "last_api_entry_visible": (
                            limit_entry.winfo_rooty() >= api_canvas.winfo_rooty()
                            and limit_entry.winfo_rooty() + limit_entry.winfo_height()
                            <= api_canvas.winfo_rooty() + api_canvas.winfo_height()
                        ),
                    }))
                finally:
                    root.destroy()
            """
        )
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script, str(MODULE_PATH)],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "SKIP_TK_UI=" in result.stdout:
            self.skipTest("Tk display is unavailable")
        reports = [
            line.removeprefix("UI_LAYOUT_REPORT=")
            for line in result.stdout.splitlines()
            if line.startswith("UI_LAYOUT_REPORT=")
        ]
        self.assertEqual(len(reports), 1, result.stdout + result.stderr)
        report = json.loads(reports[0])
        self.assertEqual(report["collapsed_height"], report["initial_height"])
        self.assertTrue(report["token_initially_masked"])
        self.assertTrue(report["token_revealed"])
        self.assertTrue(report["token_saved"])
        self.assertTrue(report["token_remasked_on_reload"])
        # Если все настройки помещаются, прокрутка не нужна; нижнее поле доступно в обоих случаях.
        self.assertTrue(report["last_api_entry_visible"])
        for requested, available in report["rows"]:
            self.assertLessEqual(requested, available)


class BuildWorkflowContractTests(unittest.TestCase):
    def test_embedded_python_blocks_are_syntactically_valid(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        blocks = re.findall(
            r"(?ms)^[ \t]*[^\n]*<<'PY'\n(?P<body>.*?)^[ \t]*PY[ \t]*$",
            workflow,
        )

        self.assertGreaterEqual(len(blocks), 2)
        for index, block in enumerate(blocks, start=1):
            with self.subTest(block=index):
                compile(
                    textwrap.dedent(block),
                    f"build.yml:embedded-python-{index}",
                    "exec",
                )

    def test_release_is_published_once_after_all_build_jobs(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(workflow.count("softprops/action-gh-release@v2"), 1)
        self.assertIn("name: Publish release", workflow)
        self.assertIn("needs: build", workflow)
        self.assertIn(
            "if: success() && startsWith(github.ref, 'refs/tags/')",
            workflow,
        )
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("merge-multiple: true", workflow)
        self.assertIn("fail_on_unmatched_files: true", workflow)
        publish_release = workflow.index(
            "- name: Publish release only after every platform passed"
        )
        download_release = workflow.index(
            "- name: Download build artifacts"
        )
        self.assertLess(download_release, publish_release)

    def test_release_ffmpeg_is_checked_for_opus_support(self):
        workflow = (PROJECT_DIR / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Verify required FFmpeg codecs", workflow)
        self.assertIn("libopus", workflow)
        self.assertIn("libvorbis", workflow)

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

    def test_manual_path_edit_updates_live_config_before_focus_loss(self):
        """Кнопки должны видеть свежий текст ``Entry`` даже без события ``FocusOut``."""
        app = object.__new__(studio.TTSApp)
        app.config = {"input_dir": "old-input"}
        app.settings_vars = {}
        app._is_updating_ui = False
        app._is_closing = False
        variable = mock.Mock(get=mock.Mock(return_value=" new-input "))

        app._path_var_changed("input_dir", variable)

        self.assertEqual(app.config["input_dir"], "new-input")

    def test_manual_path_commit_persists_after_enter_or_focus_out(self):
        app = object.__new__(studio.TTSApp)
        app.config = {"export_dir": "old-export"}
        app.settings_vars = {}
        app._is_updating_ui = False
        app._is_closing = False
        app.save_settings = mock.Mock()
        variable = mock.Mock(get=mock.Mock(return_value="new-export"))

        app._commit_path_var("export_dir", variable)

        self.assertEqual(app.config["export_dir"], "new-export")
        app.save_settings.assert_called_once_with()

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


class DirectPreviewCleanupTests(unittest.TestCase):
    @staticmethod
    def make_app(path, *, saved=False):
        app = object.__new__(studio.TTSApp)
        app.last_direct_audio = str(path)
        app.last_direct_audio_has_effects = saved
        app.stop_audio_playback = mock.Mock()
        app.btn_direct_play = mock.Mock()
        return app

    def test_discard_removes_only_named_preview_from_session_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            session_dir = root / "session"
            session_dir.mkdir()
            owned_preview = session_dir / "out_preview.ogg"
            unrelated_temp = session_dir / "synth_fragment.ogg"
            wrong_extension = session_dir / "out_preview.wav"
            saved_preview = root / "saved" / "out_saved.ogg"
            saved_preview.parent.mkdir()
            for path in (
                owned_preview,
                unrelated_temp,
                wrong_extension,
                saved_preview,
            ):
                path.write_bytes(b"audio")

            with mock.patch.object(studio, "SESSION_TEMP_DIR", session_dir):
                owned_app = self.make_app(owned_preview)
                owned_app._discard_last_direct_preview()
                self.assertFalse(owned_preview.exists())
                owned_app.stop_audio_playback.assert_called_once_with()
                self.assertIsNone(owned_app.last_direct_audio)
                owned_app.btn_direct_play.config.assert_called_once_with(
                    state=studio.tk.DISABLED
                )

                unrelated_app = self.make_app(unrelated_temp)
                unrelated_app._discard_last_direct_preview()
                self.assertTrue(unrelated_temp.exists())
                unrelated_app.stop_audio_playback.assert_not_called()

                wrong_extension_app = self.make_app(wrong_extension)
                wrong_extension_app._discard_last_direct_preview()
                self.assertTrue(wrong_extension.exists())
                wrong_extension_app.stop_audio_playback.assert_not_called()

                external_app = self.make_app(saved_preview)
                external_app._discard_last_direct_preview()
                self.assertTrue(saved_preview.exists())
                external_app.stop_audio_playback.assert_not_called()

    def test_saved_direct_result_is_never_treated_as_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            session_dir = Path(tempdir)
            saved_file = session_dir / "out_saved.ogg"
            saved_file.write_bytes(b"saved audio")
            app = self.make_app(saved_file, saved=True)

            with mock.patch.object(studio, "SESSION_TEMP_DIR", session_dir):
                app._discard_last_direct_preview()

            self.assertTrue(saved_file.exists())
            app.stop_audio_playback.assert_not_called()
            app.btn_direct_play.config.assert_not_called()


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
        """Запускает планировщик без сети и FFmpeg и возвращает запросы пауз."""
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

    def test_unsupported_paragraph_breaks_previous_colon_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            processor = self.make_processor(Path(tempdir))
            self.set_pause_config(
                processor,
                pause_paragraph=300,
                pause_colon=900,
            )

            pauses = self.collect_silence_durations(
                processor,
                "Автор сказал:\n(王)\nПродолжение.",
            )

            self.assertEqual(pauses, [300])

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

    def test_return_audio_files_collects_once_without_legacy_concat(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            processor.cfg["pause_file_start"] = 0
            processor.cfg["pause_file_end"] = 0
            fragment = root / "fragment.ogg"
            fragment.write_bytes(b"canonical fragment")
            processor.synthesize_sentence = mock.Mock(
                return_value=(fragment, True)
            )
            processor._save_cache = mock.Mock()
            processor._run_ffmpeg_concat = mock.Mock()

            result = processor.process_raw_text(
                "Одна фраза.",
                "collected.mp3",
                save_to_disk=False,
                return_audio_files=True,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["audio_files"], (fragment,))
            processor._run_ffmpeg_concat.assert_not_called()

    def test_legacy_save_path_limits_created_encoder_threads(self):
        """Старый путь не создаёт очередь потоков сверх лимита FFmpeg."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            processor = self.make_processor(root)
            processor.cfg["pause_file_start"] = 0
            processor.cfg["pause_file_end"] = 0
            processor.active_threads = []
            processor.max_encode_workers = 2
            fragment = root / "fragment.ogg"
            fragment.write_bytes(b"canonical fragment")
            processor.synthesize_sentence = mock.Mock(
                return_value=(fragment, True)
            )
            processor._save_cache = mock.Mock()

            release_encoders = threading.Event()
            two_encoders_started = threading.Event()
            counter_lock = threading.Lock()
            counters = {
                "active": 0,
                "peak": 0,
                "calls": 0,
                "timeouts": 0,
            }

            def blocking_merge(*_args, **_kwargs):
                with counter_lock:
                    counters["active"] += 1
                    counters["calls"] += 1
                    counters["peak"] = max(
                        counters["peak"], counters["active"]
                    )
                    if counters["active"] == 2:
                        two_encoders_started.set()
                released = release_encoders.wait(timeout=5)
                with counter_lock:
                    if not released:
                        counters["timeouts"] += 1
                    counters["active"] -= 1

            processor._merge_save_and_notify = blocking_merge
            scheduling_errors = []

            def schedule_outputs():
                try:
                    for index in range(5):
                        processor.process_raw_text(
                            "Фраза.", f"chapter-{index}.mp3"
                        )
                except BaseException as exc:  # pragma: no cover - диагностика потока
                    scheduling_errors.append(exc)

            scheduler = threading.Thread(target=schedule_outputs)
            scheduler.start()
            try:
                self.assertTrue(two_encoders_started.wait(timeout=5))
                self.assertTrue(scheduler.is_alive())
                with counter_lock:
                    self.assertEqual(counters["active"], 2)
                    self.assertEqual(counters["peak"], 2)
                self.assertEqual(len(processor.active_threads), 2)
            finally:
                release_encoders.set()
                scheduler.join(timeout=5)
                for encoder in tuple(processor.active_threads):
                    encoder.join(timeout=5)

            self.assertFalse(scheduler.is_alive())
            for encoder in tuple(processor.active_threads):
                self.assertFalse(encoder.is_alive())

            self.assertEqual(scheduling_errors, [])
            self.assertEqual(counters["calls"], 5)
            self.assertEqual(counters["timeouts"], 0)
            self.assertLessEqual(counters["peak"], 2)

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

            self.assertEqual(progress, [
                (0, 1, ""), (1, 1, "[ПАУЗА РАЗДЕЛИТЕЛЯ]"),
            ])

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
            # Первая пауза относится к реплике, оформленной регулярным выражением, вторая —
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
            captured_metadata = []

            def fake_run(command, **_kwargs):
                captured.append(command)
                metadata_path = Path(command[command.index("ffmetadata") + 2])
                captured_metadata.append((metadata_path, metadata_path.read_text()))
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
            self.assertEqual(command.count("-i"), 2)
            self.assertNotIn("-c:v", command)
            self.assertNotIn("-disposition:v:0", command)
            self.assertIn("0:a:0", command)
            self.assertIn("METADATA_BLOCK_PICTURE=", captured_metadata[0][1])
            self.assertFalse(captured_metadata[0][0].exists())
            self.assertNotIn("METADATA_BLOCK_PICTURE=", " ".join(command))

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
                encoding="utf-8",
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
                        "language": "eng",
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
        self.assertEqual(metadata["language"], "eng")
        self.assertEqual(metadata["duration"], 12.5)


class M4BChapterProbeTests(unittest.TestCase):
    def test_normalizer_preserves_valid_chapters_and_format_metadata(self):
        probe_data = {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "12.500000",
                "tags": {"album": "Книга", "artist": "Автор"},
            },
            "chapters": [
                {
                    "time_base": "1/1000",
                    "start": 0,
                    "end": 4250,
                    "tags": {"title": "Вступление"},
                },
                {
                    "start_time": "4.250000",
                    "end_time": "12.500000",
                    "tags": {"title": "Глава 1"},
                },
            ],
        }

        result = studio.normalize_ffprobe_chapters(probe_data)

        self.assertEqual(result["format"], probe_data["format"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            result["chapters"],
            [
                {
                    "start": 0.0,
                    "end": 4.25,
                    "duration": 4.25,
                    "title": "Вступление",
                },
                {
                    "start": 4.25,
                    "end": 12.5,
                    "duration": 8.25,
                    "title": "Глава 1",
                },
            ],
        )

    def test_normalizer_repairs_missing_ends_and_overlaps(self):
        result = studio.normalize_ffprobe_chapters(
            {
                "format": {"duration": "30"},
                "chapters": [
                    {
                        "start_time": "0",
                        "end_time": "15",
                        "tags": {"title": "Первая"},
                    },
                    {"start_time": "10", "tags": {"title": "Вторая"}},
                    {
                        "start_time": "20",
                        "end_time": "20",
                        "tags": {"title": "Третья"},
                    },
                ],
            },
            source_name="book.m4b",
        )

        self.assertEqual(
            [
                (chapter["start"], chapter["end"], chapter["duration"])
                for chapter in result["chapters"]
            ],
            [(0.0, 10.0, 10.0), (10.0, 20.0, 10.0), (20.0, 30.0, 10.0)],
        )
        warnings_text = "\n".join(result["warnings"])
        self.assertIn("обрезан по началу следующей главы", warnings_text)
        self.assertEqual(warnings_text.count("восстановлен по соседней границе"), 2)
        self.assertIn("book.m4b", warnings_text)

    def test_normalizer_discards_invalid_and_duplicate_ranges_with_warnings(self):
        result = studio.normalize_ffprobe_chapters(
            {
                "format": {"duration": "10"},
                "chapters": [
                    {"start_time": "5", "end_time": "10"},
                    {"start_time": "bad", "end_time": "3"},
                    {"start_time": "0", "end_time": "5"},
                    {"start_time": "5", "end_time": "7"},
                    {"start_time": "10", "end_time": "10"},
                ],
            }
        )

        self.assertEqual(
            [(item["start"], item["end"], item["title"]) for item in result["chapters"]],
            [(0.0, 5.0, "Глава 3"), (5.0, 10.0, "Глава 1")],
        )
        warnings_text = "\n".join(result["warnings"])
        self.assertIn("переставлены по времени", warnings_text)
        self.assertIn("отсутствует корректное начало", warnings_text)
        self.assertIn("начало совпадает", warnings_text)
        self.assertIn("невозможно определить положительную длительность", warnings_text)

    def test_probe_reads_json_and_reports_subprocess_failure(self):
        source = Path(tempfile.gettempdir()).resolve() / "book.m4b"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {"duration": "2", "tags": {"album": "Book"}},
                    "chapters": [
                        {
                            "start_time": "0",
                            "end_time": "2",
                            "tags": {"title": "Chapter"},
                        }
                    ],
                }
            ),
            stderr="",
        )
        with mock.patch.object(
            studio.subprocess, "run", return_value=completed
        ) as run:
            result = studio.probe_m4b_chapters(source, timeout=7)

        command = run.call_args.args[0]
        self.assertIn("-show_chapters", command)
        self.assertIn("-show_format", command)
        self.assertIn("-show_streams", command)
        self.assertEqual(command[-1], str(source))
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(result["chapters"][0]["title"], "Chapter")
        self.assertEqual(result["format"]["tags"]["album"], "Book")

        failure = subprocess.CalledProcessError(
            1, command, stderr="invalid data"
        )
        with mock.patch.object(studio.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "invalid data"):
                studio.probe_m4b_chapters(source.with_name("broken.m4b"))

    def test_virtual_group_inherits_container_metadata_and_clip_ranges(self):
        source = str(Path(tempfile.gettempdir()).resolve() / "Книга.m4b")
        probe_result = {
            "format": {
                "tags": {
                    "title": "Название книги. Том 02",
                    "ALBUM": "Название книги",
                    "artist": "Чтец",
                    "albumartist": "Автор",
                    "GENRE": "Аудиокнига",
                    "composer": "Композитор",
                    "date": "2026",
                }
            },
            "streams": [
                {"codec_type": "audio", "tags": {"language": "rus"}},
                {
                    "codec_type": "video",
                    "disposition": {"attached_pic": 1},
                },
            ],
            "has_embedded_cover": True,
            "chapters": [
                {"start": 0.0, "end": 5.5, "duration": 5.5, "title": "Первая"},
                {"start": 5.5, "end": 12.0, "duration": 6.5, "title": "Вторая"},
            ],
            "warnings": ["Исправлена тестовая граница."],
        }

        result = studio.build_m4b_virtual_export_group(source, probe_result)

        self.assertEqual(result["group"]["name"], "Название книги. Том 02")
        self.assertFalse(result["group"]["merge"])
        self.assertEqual(result["group"]["source_kind"], "m4b_chapters")
        self.assertEqual(result["group"]["artist"], "Чтец")
        self.assertEqual(result["group"]["album_artist"], "Автор")
        self.assertEqual(result["group"]["genre"], "Аудиокнига")
        self.assertEqual(result["group"]["composer"], "Композитор")
        self.assertEqual(result["group"]["year"], "2026")
        self.assertEqual(result["group"]["language"], "rus")
        self.assertEqual(result["group"]["cover"], "")
        self.assertEqual(result["group"]["cover_source"], source)
        self.assertEqual(result["warnings"], ["Исправлена тестовая граница."])
        self.assertEqual(
            [
                (
                    chapter["path"],
                    chapter["clip_start"],
                    chapter["clip_end"],
                    chapter["duration"],
                    chapter["title"],
                    chapter["chapter_index"],
                )
                for chapter in result["chapters"]
            ],
            [
                (source, 0.0, 5.5, 5.5, "Первая", 1),
                (source, 5.5, 12.0, 6.5, "Вторая", 2),
            ],
        )
        for chapter in result["chapters"]:
            self.assertEqual(chapter["album"], "Название книги")
            self.assertEqual(chapter["artist"], "Чтец")
            self.assertEqual(chapter["cover"], "")
            self.assertEqual(chapter["cover_source"], source)

    def test_virtual_group_uses_filename_and_rejects_invalid_chapter(self):
        valid = studio.build_m4b_virtual_export_group(
            "/books/Без тегов.m4b",
            {
                "format": {},
                "chapters": [
                    {"start": 1, "end": 2.25, "duration": 1.25, "title": ""}
                ],
            },
        )
        self.assertEqual(valid["group"]["name"], "Без тегов")
        self.assertEqual(valid["chapters"][0]["title"], "Глава 1")
        self.assertEqual(valid["group"]["cover_source"], "")

        with self.assertRaisesRegex(ValueError, "глава 1"):
            studio.build_m4b_virtual_export_group(
                "/books/broken.m4b",
                {
                    "format": {},
                    "chapters": [
                        {"start": 4, "end": 4, "duration": 0, "title": "Broken"}
                    ],
                },
            )


class FfmpegSaveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.audio_file = self.root / "input.ogg"
        # Финальная сборка принимает только внутренние канонические фрагменты.
        # Для теста построения команды достаточно сигнатуры Ogg/Opus: сам
        # дочерний процесс ниже подменён, поэтому содержимое декодироваться не будет.
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

        self.assertEqual(command.count("-i"), 2)
        self.assertIn("ffmetadata", command)
        self.assertEqual(self.values_after(command, "-map"), ["0:a:0"])
        self.assertNotIn("-c:v", command)
        self.assertNotIn("-disposition:v", command)
        metadata = self.values_after(command, "-metadata")
        self.assertIn("album=Book", metadata)
        self.assertFalse(any(
            value.startswith("METADATA_BLOCK_PICTURE=")
            for value in metadata
        ))
        self.assertEqual(self.values_after(command, "-map_metadata"), ["1"])
        self.assertEqual(callbacks[-1][1], "success")

    def test_legacy_vorbis_and_m4a_exports_embed_cover(self):
        """Старый путь сборки одной книги не теряет обложку OGG/M4A."""
        self.cover_file.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        for fmt, filename in (("ogg", "chapter.ogg"), ("m4a", "chapter.m4a")):
            with self.subTest(fmt=fmt):
                processor = self.make_processor(output_format=fmt)
                _output, command, callbacks = self.run_save(
                    processor, filename
                )

                self.assertEqual(command.count("-i"), 2)
                maps = self.values_after(command, "-map")
                if fmt == "ogg":
                    self.assertIn("ffmetadata", command)
                    self.assertEqual(maps, ["0:a:0"])
                    self.assertEqual(
                        self.values_after(command, "-map_metadata"), ["1"]
                    )
                    self.assertEqual(
                        self.values_after(command, "-c:a"), ["libvorbis"]
                    )
                    self.assertNotIn("-c:v", command)
                    self.assertNotIn(
                        "METADATA_BLOCK_PICTURE", " ".join(command)
                    )
                else:
                    self.assertEqual(maps, ["0:a:0", "1:v:0"])
                    self.assertEqual(
                        self.values_after(command, "-map_metadata"), ["-1"]
                    )
                    self.assertEqual(self.values_after(command, "-c:a"), ["aac"])
                    self.assertEqual(self.values_after(command, "-c:v"), ["mjpeg"])
                    self.assertEqual(
                        self.values_after(command, "-disposition:v:0"),
                        ["attached_pic"],
                    )
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

    def test_book_opus_auto_stream_copies_uniform_cache_fragments(self):
        # Для быстрого пути сборки книги также проверяется канальность заголовка Ogg/Opus:
        # в идентификационном пакете поле каналов находится на смещении 9.
        canonical_header = b"OpusHead" + bytes([0, 1]) + b"\x00" * 9
        self.audio_file.write_bytes(fake_ogg_first_page(canonical_header))
        second_audio = self.root / "second.ogg"
        second_audio.write_bytes(fake_ogg_first_page(canonical_header))
        processor = self.make_processor(
            output_format="opus",
            output_bitrate="auto",
            output_sample_rate="auto",
            output_channels="auto",
            apply_output_tags=False,
            tag_cover="",
        )
        output = self.root / "book.opus"
        profile = {
            "codec": "opus",
            "sample_rate": 48000,
            "channels": 1,
            # Opus/VBR обычно не публикует номинальный ``bit_rate`` в ``ffprobe``.
            "bitrate": None,
        }

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"copied")
            return _FakeCompletedProcess()

        with mock.patch.object(
            studio, "_probe_audio_stream_profile", return_value=profile
        ) as probe, mock.patch.object(
            studio.subprocess, "run", side_effect=fake_run
        ):
            processor._merge_save_and_notify(
                [self.audio_file, second_audio],
                output,
                output.name,
                False,
                None,
            )

        probe.assert_not_called()
        command = list(processor._last_ffmpeg_save_command)
        self.assertEqual(self.values_after(command, "-c:a"), ["copy"])
        self.assertNotIn("-ar", command)
        self.assertNotIn("-ac", command)
        self.assertNotIn("-af", command)
        self.assertEqual(output.read_bytes(), b"copied")

    def test_book_opus_explicit_bitrate_keeps_encode_path(self):
        second_audio = self.root / "second.ogg"
        canonical_header = b"OpusHead" + bytes([0, 1]) + b"\x00" * 9
        self.audio_file.write_bytes(fake_ogg_first_page(canonical_header))
        second_audio.write_bytes(fake_ogg_first_page(canonical_header))
        processor = self.make_processor(
            output_format="opus",
            output_bitrate="48k",
            output_sample_rate="auto",
            output_channels="auto",
            apply_output_tags=False,
            tag_cover="",
        )
        output = self.root / "book-explicit.opus"
        profile = {
            "codec": "opus",
            "sample_rate": 48000,
            "channels": 1,
            "bitrate": None,
        }

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"encoded")
            return _FakeCompletedProcess()

        with mock.patch.object(
            studio, "_probe_audio_stream_profile", return_value=profile
        ) as probe, mock.patch.object(
            studio.subprocess, "run", side_effect=fake_run
        ):
            processor._merge_save_and_notify(
                [self.audio_file, second_audio],
                output,
                output.name,
                False,
                None,
            )

        probe.assert_not_called()
        command = list(processor._last_ffmpeg_save_command)
        self.assertEqual(self.values_after(command, "-c:a"), ["libopus"])
        self.assertEqual(self.values_after(command, "-b:a"), ["48k"])
        self.assertEqual(self.values_after(command, "-ar"), ["48000"])
        self.assertEqual(self.values_after(command, "-ac"), ["1"])

    def test_book_opus_retries_encoding_when_fast_copy_is_rejected(self):
        canonical_header = b"OpusHead" + bytes([0, 1]) + b"\x00" * 9
        self.audio_file.write_bytes(fake_ogg_first_page(canonical_header))
        second_audio = self.root / "second.ogg"
        second_audio.write_bytes(fake_ogg_first_page(canonical_header))
        processor = self.make_processor(
            output_format="opus",
            output_bitrate="auto",
            output_sample_rate="auto",
            output_channels="auto",
            apply_output_tags=False,
            tag_cover="",
        )
        output = self.root / "book-retry.opus"
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if len(commands) == 1:
                return mock.Mock(returncode=1, stderr=b"packet mismatch")
            Path(command[-1]).write_bytes(b"encoded")
            return _FakeCompletedProcess()

        with mock.patch.object(
            studio, "_probe_audio_stream_profile"
        ) as probe, mock.patch.object(
            studio.subprocess, "run", side_effect=fake_run
        ), self.assertLogs(level=logging.WARNING) as logs:
            processor._merge_save_and_notify(
                [self.audio_file, second_audio],
                output,
                output.name,
                False,
                None,
            )

        probe.assert_not_called()
        self.assertEqual(len(commands), 2)
        self.assertEqual(self.values_after(commands[0], "-c:a"), ["copy"])
        self.assertNotIn("-ar", commands[0])
        self.assertNotIn("-ac", commands[0])
        self.assertEqual(self.values_after(commands[1], "-c:a"), ["libopus"])
        self.assertEqual(self.values_after(commands[1], "-b:a"), ["48k"])
        self.assertEqual(self.values_after(commands[1], "-ar"), ["48000"])
        self.assertEqual(self.values_after(commands[1], "-ac"), ["1"])
        self.assertTrue(any(
            "повтор через libopus" in message for message in logs.output
        ))
        self.assertEqual(output.read_bytes(), b"encoded")


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

    def test_cache_hit_does_not_require_api_token(self):
        """Заполненный кэш можно использовать при пустом API-токене."""
        processor = self.make_processor(use_cache=True)
        processor.cfg["api_token"] = ""
        text = "Фраза."
        text_hash = processor.get_hash(text)
        cache_file = processor.cache_audio_dir / f"{text_hash}.ogg"
        cache_file.write_bytes(fake_ogg_first_page(b"OpusHead-cached"))
        processor.cache[text_hash] = {
            "file_name": cache_file.name,
            "speaker": processor.cfg["speaker"],
        }

        returned_file, success = processor.synthesize_sentence(text, text)

        self.assertTrue(success)
        self.assertEqual(returned_file, cache_file)
        self.assertEqual(processor.session.calls, [])

    def test_request_callback_only_reports_cache_miss(self):
        processor = self.make_processor(use_cache=True)
        text = "Фраза."
        text_hash = processor.get_hash(text)
        cache_file = processor.cache_audio_dir / f"{text_hash}.ogg"
        cache_file.write_bytes(fake_ogg_first_page(b"OpusHead-cached"))
        processor.cache[text_hash] = {
            "file_name": cache_file.name,
            "speaker": processor.cfg["speaker"],
        }
        requests_seen = []

        processor.synthesize_sentence(text, text, request_callback=requests_seen.append)
        self.assertEqual(requests_seen, [])

        with mock.patch.object(
            studio, "_prepare_api_audio_file",
            side_effect=lambda _source, destination, **_kwargs: Path(destination).write_bytes(
                fake_ogg_first_page(b"OpusHead-new audio")
            ),
        ), mock.patch.object(studio, "_require_opus_audio_file"):
            processor.synthesize_sentence(
                "Новая фраза.", "Новая фраза.", request_callback=requests_seen.append
            )
        self.assertEqual(requests_seen, ["Новая фраза."])
        self.assertEqual(len(processor.session.calls), 1)

    def test_request_callback_failure_does_not_retry_or_replace_audio(self):
        """Ошибка наблюдателя не должна выглядеть как ошибка API-запроса."""
        processor = self.make_processor(use_cache=True)

        def broken_callback(_text):
            raise RuntimeError("UI callback failed")

        with mock.patch.object(
            studio,
            "_prepare_api_audio_file",
            side_effect=lambda _source, destination, **_kwargs: Path(
                destination
            ).write_bytes(fake_ogg_first_page(b"OpusHead-callback-failure")),
        ), mock.patch.object(studio, "_require_opus_audio_file"):
            returned_file, success = processor.synthesize_sentence(
                "Фраза с ошибкой наблюдателя.",
                "Фраза с ошибкой наблюдателя.",
                request_callback=broken_callback,
            )

        self.assertTrue(success)
        self.assertTrue(returned_file.exists())
        self.assertEqual(len(processor.session.calls), 1)

    def test_stop_in_request_callback_prevents_http_request(self):
        """Остановка перед отправкой запроса не должна обращаться к API."""
        processor = self.make_processor(use_cache=True)

        def stop_before_request(_text):
            processor.is_stopped = True

        returned_file, success = processor.synthesize_sentence(
            "Фраза перед остановкой.",
            "Фраза перед остановкой.",
            request_callback=stop_before_request,
        )

        self.assertIsNone(returned_file)
        self.assertFalse(success)
        self.assertEqual(processor.session.calls, [])
        self.assertEqual(processor.cache, {})

    def synthesize_without_decoding(self, processor, *, force_new=False):
        def fake_prepare(source, destination, **_kwargs):
            destination = Path(destination)
            # Имитируем контракт реальной ``_prepare_api_audio_file``: наружу
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

    def test_disabled_cache_removes_only_owned_temporary_audio_on_cleanup(self):
        processor = self.make_processor(use_cache=False)

        returned_file, success = self.synthesize_without_decoding(processor)

        self.assertTrue(success)
        self.assertTrue(returned_file.is_file())
        self.assertIn(returned_file, processor._transient_audio_paths)

        processor.cleanup_transient_audio_files()

        self.assertFalse(returned_file.exists())
        self.assertEqual(processor._transient_audio_paths, set())

    def test_disabled_cache_does_not_apply_limits_to_existing_cache(self):
        processor = self.make_processor(use_cache=False)
        processor.cfg.update(
            {
                "enable_cache_lru": True,
                "cache_max_entries": 1,
                "enable_cache_ttl": True,
                "cache_ttl_hours": 0.000001,
            }
        )
        processor.cache.update(
            {
                "first": {"file_name": "first.ogg", "created_at": 1.0},
                "second": {"file_name": "second.ogg", "created_at": 2.0},
            }
        )

        processor.flush_cache()

        self.assertEqual(set(processor.cache), {"first", "second"})

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

    def test_shared_cache_miss_is_published_by_one_api_request(self):
        """Два процессора объединяют совпавший промах кэша в один запрос."""
        shared_cache = {}
        shared_lock = threading.RLock()
        shared_inflight = {}
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "cache_dir": str(self.root / "shared-cache"),
                "input_dir": str(self.root / "input"),
                "output_dir": str(self.root / "output"),
                "use_cache": True,
                "auto_trim_silence": False,
                "max_retries": 1,
                "api_max_requests": 100,
                "api_time_window": 0,
            }
        )

        def make_shared_processor():
            return studio.TTSProcessor(
                config.copy(),
                shared_cache=shared_cache,
                shared_cache_lock=shared_lock,
                shared_processing_statuses={},
                shared_synthesis_inflight=shared_inflight,
            )

        owner = make_shared_processor()
        waiter = make_shared_processor()
        owner_post_started = threading.Event()
        release_owner_post = threading.Event()
        waiter_is_waiting = threading.Event()
        real_event_type = threading.Event

        class ObservedInflightEvent:
            def __init__(self):
                self._event = real_event_type()

            def wait(self, timeout=None):
                waiter_is_waiting.set()
                return self._event.wait(timeout)

            def set(self):
                return self._event.set()

        class BlockingSession(self.FakeSession):
            def post(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                owner_post_started.set()
                if not release_owner_post.wait(timeout=5):
                    raise AssertionError("владелец cache miss не был освобождён")
                return self.response

        class ForbiddenSession(self.FakeSession):
            def post(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                raise AssertionError("ожидающий процессор повторил API-запрос")

        owner.session = BlockingSession()
        waiter.session = ForbiddenSession()
        results = {}
        thread_errors = []

        def run_processor(name, processor, force_new=False):
            try:
                results[name] = processor.synthesize_sentence(
                    "Фраза.", "Фраза.", force_new=force_new
                )
            except BaseException as exc:  # pragma: no cover - диагностика потока
                thread_errors.append(exc)

        def fake_prepare(_source, destination, **_kwargs):
            destination = Path(destination)
            destination.write_bytes(
                fake_ogg_first_page(b"OpusHead-shared audio")
            )
            return destination

        owner_thread = threading.Thread(
            target=run_processor, args=("owner", owner)
        )
        waiter_thread = threading.Thread(
            target=run_processor, args=("waiter", waiter)
        )
        with mock.patch.object(
            studio.threading,
            "Event",
            side_effect=ObservedInflightEvent,
        ), mock.patch.object(
            studio, "_prepare_api_audio_file", side_effect=fake_prepare
        ):
            owner_thread.start()
            self.assertTrue(owner_post_started.wait(timeout=5))

            waiter_thread.start()
            self.assertTrue(waiter_is_waiting.wait(timeout=5))
            release_owner_post.set()

            owner_thread.join(timeout=5)
            waiter_thread.join(timeout=5)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertEqual(len(owner.session.calls), 1)
        self.assertEqual(waiter.session.calls, [])
        self.assertTrue(results["owner"][1])
        self.assertTrue(results["waiter"][1])
        self.assertEqual(results["owner"][0], results["waiter"][0])
        self.assertTrue(results["waiter"][0].is_file())

    def test_force_new_waits_for_owner_then_performs_own_request(self):
        """Принудительный запрос не обгоняет публикацию того же хэша."""
        shared_cache = {}
        shared_lock = threading.RLock()
        shared_inflight = {}
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "cache_dir": str(self.root / "force-shared-cache"),
                "input_dir": str(self.root / "input"),
                "output_dir": str(self.root / "output"),
                "use_cache": True,
                "auto_trim_silence": False,
                "max_retries": 1,
                "api_max_requests": 100,
                "api_time_window": 0,
            }
        )

        def make_shared_processor():
            return studio.TTSProcessor(
                config.copy(),
                shared_cache=shared_cache,
                shared_cache_lock=shared_lock,
                shared_processing_statuses={},
                shared_synthesis_inflight=shared_inflight,
            )

        owner = make_shared_processor()
        forced = make_shared_processor()
        owner_post_started = threading.Event()
        release_owner_post = threading.Event()
        forced_waiting = threading.Event()
        forced_post_started = threading.Event()
        real_event_type = threading.Event

        class ObservedInflightEvent:
            def __init__(self):
                self._event = real_event_type()

            def wait(self, timeout=None):
                forced_waiting.set()
                return self._event.wait(timeout)

            def set(self):
                return self._event.set()

        class OwnerSession(self.FakeSession):
            def post(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                owner_post_started.set()
                if not release_owner_post.wait(timeout=5):
                    raise AssertionError("первый запрос не был освобождён")
                return self.response

        class ForcedSession(self.FakeSession):
            def post(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                forced_post_started.set()
                return self.response

        owner.session = OwnerSession()
        forced.session = ForcedSession()
        results = {}
        thread_errors = []

        def run_processor(name, processor, force_new=False):
            try:
                results[name] = processor.synthesize_sentence(
                    "Фраза.", "Фраза.", force_new=force_new
                )
            except BaseException as exc:  # pragma: no cover - диагностика потока
                thread_errors.append(exc)

        prepare_calls = []

        def fake_prepare(_source, destination, **_kwargs):
            destination = Path(destination)
            prepare_calls.append(destination)
            destination.write_bytes(
                fake_ogg_first_page(b"OpusHead-forced audio")
            )
            return destination

        owner_thread = threading.Thread(
            target=run_processor, args=("owner", owner)
        )
        forced_thread = threading.Thread(
            target=run_processor,
            args=("forced", forced, True),
        )
        with mock.patch.object(
            studio.threading,
            "Event",
            side_effect=ObservedInflightEvent,
        ), mock.patch.object(
            studio, "_prepare_api_audio_file", side_effect=fake_prepare
        ):
            owner_thread.start()
            self.assertTrue(owner_post_started.wait(timeout=5))

            forced_thread.start()
            self.assertTrue(forced_waiting.wait(timeout=5))
            self.assertFalse(forced_post_started.wait(timeout=0.05))
            release_owner_post.set()

            owner_thread.join(timeout=5)
            forced_thread.join(timeout=5)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(forced_thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertEqual(len(owner.session.calls), 1)
        self.assertEqual(len(forced.session.calls), 1)
        self.assertTrue(forced_post_started.is_set())
        self.assertEqual(len(prepare_calls), 2)
        self.assertTrue(results["owner"][1])
        self.assertTrue(results["forced"][1])

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

    def test_source_target_cache_eviction_is_deferred_until_final_flush(self):
        """LRU/TTL не удаляют главы до завершения отложенной сборки M4B."""
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "cache_dir": str(self.root / "guarded-cache"),
                "input_dir": str(self.root / "input"),
                "output_dir": str(self.root / "output"),
                "enable_cache_lru": True,
                "cache_max_entries": 1,
                "enable_cache_ttl": False,
            }
        )
        shared_cache = {
            "old": {
                "file_name": "old.ogg",
                "last_accessed": 1.0,
            },
            "new": {
                "file_name": "new.ogg",
                "last_accessed": 2.0,
            },
        }
        state = {"deferred": 0, "pending": False}
        processor = studio.TTSProcessor(
            config,
            shared_cache=shared_cache,
            shared_cache_lock=threading.RLock(),
            shared_cache_eviction_state=state,
        )
        processor.unsaved_cache_items = 1

        processor.defer_cache_eviction()
        processor._save_cache()
        self.assertEqual(set(processor.cache), {"old", "new"})
        self.assertTrue(state["pending"])

        processor.resume_cache_eviction()
        processor.flush_cache()
        self.assertEqual(set(processor.cache), {"new"})
        self.assertEqual(state["deferred"], 0)
        self.assertFalse(state["pending"])

    def test_source_target_ttl_is_deferred_until_final_flush(self):
        """Истёкшая запись TTL остаётся доступной до снятия защиты запуска."""
        config = studio.DEFAULT_CONFIG.copy()
        config.update(
            {
                "cache_dir": str(self.root / "guarded-ttl-cache"),
                "input_dir": str(self.root / "input"),
                "output_dir": str(self.root / "output"),
                "enable_cache_lru": False,
                "enable_cache_ttl": True,
                "cache_ttl_hours": 0.000001,
            }
        )
        shared_cache = {
            "expired": {
                "file_name": "expired.ogg",
                "last_accessed": 1.0,
            }
        }
        state = {"deferred": 0, "pending": False}
        processor = studio.TTSProcessor(
            config,
            shared_cache=shared_cache,
            shared_cache_lock=threading.RLock(),
            shared_cache_eviction_state=state,
        )
        processor.unsaved_cache_items = 1

        processor.defer_cache_eviction()
        processor._save_cache()
        self.assertIn("expired", processor.cache)
        self.assertTrue(state["pending"])

        processor.resume_cache_eviction()
        processor.flush_cache()
        self.assertNotIn("expired", processor.cache)
        self.assertEqual(state["deferred"], 0)
        self.assertFalse(state["pending"])


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
                encoding="utf-8",
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
                encoding="utf-8",
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "opus")
            self.assertEqual(stream["sample_rate"], "48000")
            self.assertEqual(stream["channels"], 1)
            self.assertEqual(source.read_bytes(), destination.read_bytes())

    def test_stereo_api_opus_is_downmixed_even_without_trimming(self):
        """Быстрый путь не должен помещать в кэш неканонический вариант Opus."""
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
                encoding="utf-8",
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
                encoding="utf-8",
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
                encoding="utf-8",
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
                encoding="utf-8",
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            self.assertGreater(duration, 0.45)
            self.assertGreater(output.stat().st_size, 1000)

    def test_m4b_jpeg_cover_round_trip_is_lossless(self):
        """M4B сохраняет исходные байты JPEG и корректный attached_pic."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            audio = root / "chapter.ogg"
            cover = root / "cover.jpg"
            output = root / "book.m4b"
            extracted = root / "extracted.jpg"

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
                    "-ar", "48000", "-ac", "1", "-c:a", "libopus",
                    str(audio),
                ],
                check=True,
            )
            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=32x32",
                    "-frames:v", "1", str(cover),
                ],
                check=True,
            )

            studio._export_m4b_ffmpeg(
                (audio,),
                output,
                chapters=({"title": "Глава", "duration": 0.1},),
                cover=cover,
            )

            probe = subprocess.run(
                [
                    studio.get_ffprobe_path(), "-v", "error", "-of", "json",
                    "-show_entries",
                    "stream=codec_name,codec_type:stream_disposition=attached_pic:chapter_tags=title",
                    str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            data = json.loads(probe.stdout)
            pictures = [
                stream for stream in data["streams"]
                if stream.get("codec_type") == "video"
            ]
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0]["codec_name"], "mjpeg")
            self.assertEqual(pictures[0]["disposition"]["attached_pic"], 1)
            self.assertEqual(data["chapters"][0]["tags"]["title"], "Глава")

            subprocess.run(
                [
                    studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                    "-i", str(output), "-map", "0:v:0", "-c:v", "copy",
                    str(extracted),
                ],
                check=True,
            )
            self.assertEqual(extracted.read_bytes(), cover.read_bytes())

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
                encoding="utf-8",
            )
            streams = json.loads(probe.stdout)["streams"]
            pictures = [
                stream for stream in streams
                if stream.get("codec_type") == "video"
            ]
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0]["codec_name"], "mjpeg")
            self.assertEqual(pictures[0]["disposition"]["attached_pic"], 1)


class OutputPlanningUnitTests(unittest.TestCase):
    """Поведение чистых планировщиков 1.x без запуска графического интерфейса."""

    def test_m4b_virtual_chapters_use_template_for_separate_output_names(self):
        """Разбор M4B сохраняет Title главы для массового переименования."""
        source = "/books/Оперативник с ИИ. Том 1.m4b"
        prepared = studio.build_m4b_virtual_export_group(
            source,
            {
                "format": {"tags": {"title": "Оперативник с ИИ. Том 1"}},
                "chapters": [
                    {
                        "start": 0.0,
                        "end": 3.0,
                        "duration": 3.0,
                        "title": "Том 1. Глава 1",
                    },
                    {
                        "start": 3.0,
                        "end": 7.0,
                        "duration": 4.0,
                        "title": "Том 1. Глава 2",
                    },
                ],
            },
        )
        groups = {"book": prepared["group"]}
        children = {"book": ("chapter-1", "chapter-2")}
        files = {
            file_id: dict(chapter)
            for file_id, chapter in zip(children["book"], prepared["chapters"])
        }
        target = studio.OutputTarget(
            format="mp3",
            output_dir="/tmp/chapters",
            assembly_mode="files",
            filename_template="{book} {index:03d} - {name}",
        ).to_dict()

        records = studio.plan_export_target_paths(
            ("book",),
            children,
            groups,
            files,
            (target,),
            "/tmp/chapters",
        )

        self.assertEqual(
            [record["path"].name for record in records],
            [
                "Оперативник с ИИ. Том 1 001 - Том 1. Глава 1.mp3",
                "Оперативник с ИИ. Том 1 002 - Том 1. Глава 2.mp3",
            ],
        )
        self.assertEqual(
            {chapter["source_name"] for chapter in prepared["chapters"]},
            {"Оперативник с ИИ. Том 1"},
        )

    def test_output_file_template_start_and_width_reaches_planner(self):
        groups = {"book": {"name": "Книга"}}
        children = {"book": ("one", "two")}
        files = {
            "one": {"path": "/books/one.wav", "title": "Первая"},
            "two": {"path": "/books/two.wav", "title": "Вторая"},
        }
        target = studio.OutputTarget(
            format="mp3",
            output_dir="/tmp/output-file-template",
            assembly_mode="files",
            filename_template="{index:10:03d} - {name}",
        ).to_dict()
        records = studio.plan_export_target_paths(
            ("book",), children, groups, files, (target,), "/tmp/output-file-template"
        )
        self.assertEqual(
            [record["path"].name for record in records],
            ["010 - Первая.mp3", "011 - Вторая.mp3"],
        )

    def test_output_template_exposes_group_and_file_counters(self):
        groups = {
            "one": {"name": "Том 1", "album": "Книга"},
            "two": {"name": "Том 2", "album": "Книга"},
        }
        children = {
            "one": ("one-a", "one-b"),
            "two": ("two-a",),
        }
        files = {
            "one-a": {"path": "/books/a.wav", "title": "A"},
            "one-b": {"path": "/books/b.wav", "title": "B"},
            "two-a": {"path": "/books/c.wav", "title": "C"},
        }
        target = studio.OutputTarget(
            format="mp3",
            output_dir="/tmp/group-file-counters",
            assembly_mode="files",
            filename_template="{group_index:1:02d}-{file_index:10:03d}-{name}",
        ).to_dict()
        records = studio.plan_export_target_paths(
            ("one", "two"),
            children,
            groups,
            files,
            (target,),
            "/tmp/group-file-counters",
        )
        self.assertEqual(
            [record["path"].name for record in records],
            ["01-010-A.mp3", "01-011-B.mp3", "02-010-C.mp3"],
        )

    def test_imported_m4b_keeps_source_volume_for_subset_and_parts(self):
        prepared = studio.build_m4b_virtual_export_group(
            "/books/Книга. Том 03.m4b",
            {
                "format": {
                    "tags": {
                        "album": "Книга",
                        "disc": "3/13",
                    }
                },
                "chapters": [
                    {"start": 0.0, "end": 1.0, "duration": 1.0, "title": "Глава"}
                ],
            },
        )
        groups = {"book": prepared["group"]}
        children = {"book": ("chapter",)}
        files = {"chapter": dict(prepared["chapters"][0])}
        target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/m4b-subset",
            filename_template="Книга {volume:1:02d}",
        ).to_dict()
        records = studio.plan_export_target_paths(
            ("book",), children, groups, files, (target,), "/tmp/m4b-subset"
        )
        self.assertEqual([record["path"].name for record in records], ["Книга 03.m4b"])

        children["book"] = ("chapter", "chapter-2")
        files["chapter-2"] = {
            **files["chapter"],
            "path": "/books/Книга. Том 03.m4b",
            "title": "Глава 2",
            "source_title": "Глава 2",
            "file_index": 2,
        }
        split_target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/m4b-subset-split",
            max_chapters=1,
            filename_template="Книга {volume:1:02d}",
        ).to_dict()
        split_records = studio.plan_export_target_paths(
            ("book",),
            children,
            groups,
            files,
            (split_target,),
            "/tmp/m4b-subset-split",
        )
        self.assertEqual(
            [record["path"].name for record in split_records],
            ["Книга 03.m4b", "Книга 04.m4b"],
        )

    def test_m4b_template_can_use_physical_source_filename(self):
        """Имя исходного контейнера доступно отдельно от тега группы."""
        source = "/books/Оперативник с ИИ. Том 1.m4b"
        groups = {
            "book": {
                "name": "Изменённое название",
                "source_path": source,
            }
        }
        children = {"book": ("chapter",)}
        files = {
            "chapter": {
                "path": source,
                "title": "Глава 1",
                "source_name": "Оперативник с ИИ. Том 1",
                "source_title": "Глава 1",
                "duration": 1,
            }
        }
        target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/m4b-filename",
            filename_template="{filename} - {name}",
        ).to_dict()
        records = studio.plan_export_target_paths(
            ("book",), children, groups, files, (target,), "/tmp/m4b-filename"
        )
        self.assertEqual(
            records[0]["path"].name,
            "Оперативник с ИИ. Том 1 - Изменённое название.m4b",
        )

    def test_m4b_duplicate_names_receive_non_destructive_suffix(self):
        groups = {
            "one": {"name": "Один том", "album": "Одна книга"},
            "two": {"name": "Один том", "album": "Одна книга"},
        }
        children = {"one": ("one-file",), "two": ("two-file",)}
        files = {
            "one-file": {"path": "/books/one.wav", "title": "Глава 1", "duration": 1},
            "two-file": {"path": "/books/two.wav", "title": "Глава 2", "duration": 1},
        }
        target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/m4b-duplicate",
            filename_template="{book}",
        ).to_dict()
        records = studio.plan_export_target_paths(
            ("one", "two"),
            children,
            groups,
            files,
            (target,),
            "/tmp/m4b-duplicate",
        )
        self.assertEqual(
            [record["path"].name for record in records],
            ["Один том.m4b", "Один том Часть 2.m4b"],
        )

    def test_output_target_worker_parallelizes_items_but_not_targets(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        first_started = threading.Event()
        second_started = threading.Event()
        release_first_pair = threading.Event()
        calls = {}
        progress = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            sources = []
            files = {}
            for item_id in ("one", "two"):
                source = root / f"{item_id}.wav"
                source.write_bytes(b"audio")
                sources.append(source)
                files[item_id] = {"path": str(source), "title": item_id}
            targets = (
                studio.OutputTarget(
                    format="mp3", output_dir=str(root / "mp3")
                ).to_dict(),
                studio.OutputTarget(
                    format="opus", output_dir=str(root / "opus")
                ).to_dict(),
            )

            def fake_export(source, _output, **kwargs):
                item_id = Path(source).stem
                output_format = kwargs["output_format"]
                with lock:
                    calls.setdefault(item_id, []).append(output_format)
                if output_format == "mp3":
                    (first_started if item_id == "one" else second_started).set()
                    self.assertTrue(
                        (second_started if item_id == "one" else first_started).wait(1)
                    )
                    release_first_pair.set()
                else:
                    self.assertTrue(release_first_pair.is_set())

            with mock.patch.object(
                studio, "_export_single_audio_ffmpeg", side_effect=fake_export
            ):
                app._run_output_target_set(
                    ("one", "two"),
                    {},
                    {},
                    files,
                    targets,
                    root,
                    max_workers=2,
                    progress_callback=lambda current, total, subject: progress.append(
                        (current, total, subject)
                    ),
                )

        self.assertEqual(calls, {"one": ["mp3", "opus"], "two": ["mp3", "opus"]})
        completed = [
            item for item in progress
            if item[0] != studio.PROGRESS_STATUS_ONLY
        ]
        self.assertEqual([item[0] for item in completed], [1, 2, 3, 4])
        self.assertTrue(all(item[1] == 4 for item in progress))
        self.assertTrue(
            any(
                "активно: 2" in item[2] and "Готово" in item[2]
                for item in progress
                if item[0] == studio.PROGRESS_STATUS_ONLY
            )
        )

    def test_output_target_worker_is_sequential_by_default(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        active = 0
        peak = 0
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for item_id in ("one", "two"):
                source = root / f"{item_id}.wav"
                source.write_bytes(b"audio")
                files[item_id] = {"path": str(source), "title": item_id}
            target = studio.OutputTarget(
                format="mp3", output_dir=str(root / "mp3")
            ).to_dict()

            def fake_export(_source, _output, **_kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                threading.Event().wait(0.01)
                with lock:
                    active -= 1

            with mock.patch.object(
                studio, "_export_single_audio_ffmpeg", side_effect=fake_export
            ):
                app._run_output_target_set(
                    ("one", "two"), {}, {}, files, (target,), root
                )

        self.assertEqual(peak, 1)

    def test_sequential_file_export_reports_activity_before_ffmpeg_and_completion_after(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        trace = []

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for item_id, title in (("one", "First"), ("two", "Second")):
                source = root / f"{item_id}.wav"
                source.write_bytes(b"audio")
                files[item_id] = {"path": str(source), "title": title}
            target = studio.OutputTarget(
                format="mp3",
                output_dir=str(root / "mp3"),
            ).to_dict()

            def fake_export(_source, output_path, **_kwargs):
                trace.append(("export", Path(output_path).name))

            with mock.patch.object(
                studio,
                "_export_single_audio_ffmpeg",
                side_effect=fake_export,
            ):
                app._run_output_target_set(
                    ("one", "two"),
                    {},
                    {},
                    files,
                    (target,),
                    root,
                    progress_callback=lambda current, total, text: trace.append(
                        ("progress", current, total, text)
                    ),
                )

        self.assertEqual(
            trace,
            [
                (
                    "progress",
                    studio.PROGRESS_STATUS_ONLY,
                    2,
                    "Готово 0/2 · First.mp3 · сборка",
                ),
                ("export", "First.mp3"),
                ("progress", 1, 2, "Готово 1/2 · First.mp3"),
                (
                    "progress",
                    studio.PROGRESS_STATUS_ONLY,
                    2,
                    "Готово 1/2 · Second.mp3 · сборка",
                ),
                ("export", "Second.mp3"),
                ("progress", 2, 2, "Готово 2/2 · Second.mp3"),
            ],
        )

    def test_sequential_merged_export_uses_output_name_in_compact_status(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        trace = []
        group_id = "book"
        children = {group_id: ("one", "two")}
        groups = {
            group_id: {"name": "Book", "merge": True, "pause": 0}
        }

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for item_id in children[group_id]:
                source = root / f"{item_id}.wav"
                source.write_bytes(b"audio")
                files[item_id] = {"path": str(source), "title": item_id}
            target = studio.OutputTarget(
                format="mp3",
                output_dir=str(root / "mp3"),
            ).to_dict()
            planned = studio.plan_export_target_paths(
                (group_id,),
                children,
                groups,
                files,
                (target,),
                root,
            )
            self.assertEqual([record["path"].name for record in planned], ["Book.mp3"])

            def fake_export(_sources, output_path, **_kwargs):
                trace.append(("export", Path(output_path).name))

            with mock.patch.object(
                studio,
                "_export_merged_audio_ffmpeg",
                side_effect=fake_export,
            ):
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    root,
                    planned_records=planned,
                    progress_callback=lambda current, total, text: trace.append(
                        ("progress", current, total, text)
                    ),
                )

        self.assertEqual(
            trace,
            [
                (
                    "progress",
                    studio.PROGRESS_STATUS_ONLY,
                    1,
                    "Готово 0/1 · Book.mp3 · сборка",
                ),
                ("export", "Book.mp3"),
                ("progress", 1, 1, "Готово 1/1 · Book.mp3"),
            ],
        )

    def test_m4b_preflight_probes_only_selected_group_or_file_sources(self):
        """Предварительный ffprobe не должен обходить всё дерево экспорта."""
        for selected_kind in ("group", "file"):
            with self.subTest(selected_kind=selected_kind), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                app = object.__new__(studio.TTSApp)
                app.config = {
                    "export_m4b_template": "{book}",
                    "export_m4b_max_duration_hours": 0,
                    "export_m4b_max_chapters": 0,
                    "export_m4b_bitrate": "64k",
                }
                selected_group = "selected-group"
                skipped_group = "skipped-group"
                selected_group_file = "selected-group-file"
                skipped_group_file = "skipped-group-file"
                selected_root_file = "selected-root-file"
                skipped_root_file = "skipped-root-file"
                groups = {
                    selected_group: {
                        "name": "Selected group",
                        "merge": True,
                        "pause": 0,
                    },
                    skipped_group: {
                        "name": "Skipped group",
                        "merge": True,
                        "pause": 0,
                    },
                }
                children = {
                    selected_group: (selected_group_file,),
                    skipped_group: (skipped_group_file,),
                }
                files = {}
                for file_id in (
                    selected_group_file,
                    skipped_group_file,
                    selected_root_file,
                    skipped_root_file,
                ):
                    source = root / f"{file_id}.mp3"
                    source.write_bytes(b"audio")
                    files[file_id] = {
                        "path": str(source),
                        "title": file_id,
                        "duration": 0,
                    }
                target = studio.OutputTarget(
                    format="m4b",
                    output_dir=str(root / "m4b"),
                    filename_template="{book}",
                ).to_dict()
                selected_item = (
                    selected_group
                    if selected_kind == "group"
                    else selected_root_file
                )
                expected_source = Path(
                    files[
                        selected_group_file
                        if selected_kind == "group"
                        else selected_root_file
                    ]["path"]
                )

                with mock.patch.object(
                    studio, "_probe_audio_duration", return_value=12.5
                ) as probe, mock.patch.object(studio, "_export_m4b_ffmpeg"):
                    app._run_output_target_set(
                        (selected_item,),
                        children,
                        groups,
                        files,
                        (target,),
                        root,
                    )

                self.assertEqual(
                    [Path(call.args[0]) for call in probe.call_args_list],
                    [expected_source],
                )

    def test_m4b_preflight_cancel_between_probes_stops_remaining_probes(self):
        """Отмена после одного ffprobe должна остановить подготовительный проход."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "book"
        file_ids = ("chapter-1", "chapter-2", "chapter-3")
        groups = {
            group_id: {"name": "Book", "merge": True, "pause": 0}
        }
        children = {group_id: file_ids}
        cancel_event = threading.Event()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for file_id in file_ids:
                source = root / f"{file_id}.mp3"
                source.write_bytes(b"audio")
                files[file_id] = {
                    "path": str(source),
                    "title": file_id,
                    "duration": 0,
                }
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(root / "m4b"),
                filename_template="{book}",
            ).to_dict()

            def probe_then_cancel(_source):
                cancel_event.set()
                return 10.0

            with mock.patch.object(
                studio,
                "_probe_audio_duration",
                side_effect=probe_then_cancel,
            ) as probe, mock.patch.object(
                studio, "plan_export_target_paths"
            ) as planner, mock.patch.object(
                studio, "_export_m4b_ffmpeg"
            ) as exporter:
                with self.assertRaises(InterruptedError):
                    app._run_output_target_set(
                        (group_id,),
                        children,
                        groups,
                        files,
                        (target,),
                        root,
                        cancelled=cancel_event.is_set,
                    )

        self.assertEqual(probe.call_count, 1)
        planner.assert_not_called()
        exporter.assert_not_called()

    def test_output_target_worker_parallelizes_unmerged_group_files_from_plan(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        group_id = "group"
        file_ids = ("file-1", "file-2")
        groups = {
            group_id: {
                "name": "Book",
                "merge": False,
                "artist": "Group Artist",
                "album": "Group Album",
                "genre": "Audiobook",
                "year": "2026",
            }
        }
        children = {group_id: file_ids}

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for file_id, title in zip(file_ids, ("Alpha", "Beta")):
                source = root / f"{file_id}.wav"
                source.write_bytes(b"audio")
                files[file_id] = {"path": str(source), "title": title}
            target = studio.OutputTarget(
                format="mp3",
                output_dir=str(root / "exports"),
                filename_template="{index:02d}-{title}",
            ).to_dict()
            planned = studio.plan_export_target_paths(
                (group_id,),
                children,
                groups,
                files,
                (target,),
                root,
            )
            expected_paths = {
                "file-1": root / "exports" / "01-Alpha.mp3",
                "file-2": root / "exports" / "02-Beta.mp3",
            }
            self.assertEqual(
                {record["item_id"]: record["path"] for record in planned},
                expected_paths,
            )

            def run(worker_count):
                active = 0
                peak = 0
                calls = {}
                lock = threading.Lock()
                rendezvous = (
                    threading.Barrier(2) if worker_count > 1 else None
                )

                def fake_export(source, output_path, **kwargs):
                    nonlocal active, peak
                    file_id = Path(source).stem
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        if rendezvous is not None:
                            rendezvous.wait(timeout=2)
                        else:
                            threading.Event().wait(0.01)
                        with lock:
                            calls[file_id] = {
                                "path": Path(output_path),
                                "tags": kwargs["tags"],
                            }
                    finally:
                        with lock:
                            active -= 1

                with mock.patch.object(
                    studio,
                    "_export_single_audio_ffmpeg",
                    side_effect=fake_export,
                ):
                    app._run_output_target_set(
                        (group_id,),
                        children,
                        groups,
                        files,
                        (target,),
                        root,
                        planned_records=planned,
                        max_workers=worker_count,
                    )
                return peak, calls

            parallel_peak, parallel_calls = run(2)
            sequential_peak, sequential_calls = run(1)

        expected_tags = {
            "file-1": {
                "title": "Alpha",
                "artist": "Group Artist",
                "album": "Group Album",
                "genre": "Audiobook",
                "date": "2026",
            },
            "file-2": {
                "title": "Beta",
                "artist": "Group Artist",
                "album": "Group Album",
                "genre": "Audiobook",
                "date": "2026",
            },
        }
        self.assertEqual(parallel_peak, 2)
        self.assertEqual(sequential_peak, 1)
        for calls in (parallel_calls, sequential_calls):
            self.assertEqual(
                {file_id: call["path"] for file_id, call in calls.items()},
                expected_paths,
            )
            self.assertEqual(
                {file_id: call["tags"] for file_id, call in calls.items()},
                expected_tags,
            )

    def test_parallel_m4b_groups_keep_shared_volume_numbering(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "volume-1": {"name": "Том 1", "merge": True, "album": "Книга"},
            "volume-2": {"name": "Том 2", "merge": True, "album": "Книга"},
        }
        children = {"volume-1": ("one",), "volume-2": ("two",)}
        started = threading.Barrier(2)
        calls = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for item_id in ("one", "two"):
                source = root / f"{item_id}.mp3"
                source.write_bytes(b"audio")
                files[item_id] = {
                    "path": str(source),
                    "title": item_id,
                    "duration": 10,
                }
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(root / "m4b"),
                filename_template="{book}",
                chapter_title_template="Том {part}/{parts}. Глава {volume_index}",
            ).to_dict()

            def fake_m4b(_sources, output_path, **kwargs):
                started.wait(timeout=1)
                with lock:
                    calls.append(
                        (
                            Path(output_path).name,
                            kwargs["disk_number"],
                            kwargs["disk_total"],
                            kwargs["chapters"][0]["title"],
                        )
                    )

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    tuple(groups),
                    children,
                    groups,
                    files,
                    (target,),
                    root,
                    max_workers=2,
                )

        self.assertEqual(
            sorted((number, total) for _name, number, total, _title in calls),
            [(1, 2), (2, 2)],
        )
        self.assertEqual(
            sorted(title for _name, _number, _total, title in calls),
            ["Том 1/2. Глава 1", "Том 2/2. Глава 1"],
        )

    def test_auto_split_m4b_parts_share_pool_numbering_and_progress(self):
        """Части одной группы становятся отдельными заданиями общего пула."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "book"
        file_ids = tuple(f"chapter-{index}" for index in range(1, 4))
        groups = {
            group_id: {
                "name": "Book",
                "merge": True,
                "album": "Album",
                "pause": 0,
            }
        }
        children = {group_id: file_ids}
        first_pair = threading.Barrier(2)
        lock = threading.Lock()
        calls = []
        progress = []
        active = 0
        peak = 0
        started = 0

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for file_id in file_ids:
                source = root / f"{file_id}.mp3"
                source.write_bytes(b"audio")
                files[file_id] = {
                    "path": str(source),
                    "title": file_id,
                    "duration": 10,
                }
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(root / "m4b"),
                filename_template="{book}",
                chapter_title_template=(
                    "{part}/{parts}|{global_index}/{chapter_count}|"
                    "{volume_index}"
                ),
                max_chapters=1,
            ).to_dict()

            def fake_m4b(audio_files, output_path, **kwargs):
                nonlocal active, peak, started
                with lock:
                    active += 1
                    peak = max(peak, active)
                    started += 1
                    ordinal = started
                try:
                    if ordinal <= 2:
                        first_pair.wait(timeout=2)
                    threading.Event().wait(0.02)
                    with lock:
                        calls.append(
                            (
                                tuple(Path(path).name for path in audio_files),
                                Path(output_path).name,
                                kwargs["disk_number"],
                                kwargs["disk_total"],
                                kwargs["track_number"],
                                kwargs["track_total"],
                                kwargs["chapters"][0]["title"],
                            )
                        )
                    Path(output_path).write_bytes(b"m4b")
                finally:
                    with lock:
                        active -= 1

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ), mock.patch.object(
                studio,
                "_probe_audio_duration",
                side_effect=AssertionError("known durations must not be probed"),
            ), mock.patch.object(
                studio,
                "plan_export_target_paths",
                wraps=studio.plan_export_target_paths,
            ) as planner:
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    root,
                    max_workers=2,
                    progress_callback=lambda current, total, subject: progress.append(
                        (current, total, subject)
                    ),
                )

        planner.assert_called_once()
        self.assertEqual(peak, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            sorted(
                (disk_number, disk_total, track_number, track_total)
                for (
                    _sources,
                    _name,
                    disk_number,
                    disk_total,
                    track_number,
                    track_total,
                    _title,
                ) in calls
            ),
            [(1, 3, 1, 3), (2, 3, 2, 3), (3, 3, 3, 3)],
        )
        self.assertEqual(
            sorted(title for *_prefix, title in calls),
            ["1/3|1/3|1", "2/3|2/3|1", "3/3|3/3|1"],
        )
        completed = [
            event for event in progress
            if event[0] != studio.PROGRESS_STATUS_ONLY
        ]
        self.assertEqual([event[0] for event in completed], [1, 2, 3])
        self.assertTrue(all(event[1] == 3 for event in progress))

    def test_parallel_m4b_part_failure_cancels_siblings_and_next_target(self):
        """Первая ошибка гасит соседние части и не запускает следующую цель."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "book"
        file_ids = tuple(f"chapter-{index}" for index in range(1, 4))
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        first_pair = threading.Barrier(2)
        cancellation_seen = threading.Event()
        calls = []
        progress = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for file_id in file_ids:
                source = root / f"{file_id}.mp3"
                source.write_bytes(b"audio")
                files[file_id] = {
                    "path": str(source),
                    "title": file_id,
                    "duration": 10,
                }
            targets = (
                studio.OutputTarget(
                    format="m4b",
                    output_dir=str(root / "m4b"),
                    filename_template="{book}",
                    max_chapters=1,
                ).to_dict(),
                studio.OutputTarget(
                    format="mp3", output_dir=str(root / "mp3")
                ).to_dict(),
            )

            def fake_m4b(_audio_files, _output_path, **kwargs):
                part_number = kwargs["disk_number"]
                with lock:
                    calls.append(part_number)
                if part_number in {1, 2}:
                    first_pair.wait(timeout=2)
                if part_number == 1:
                    raise RuntimeError("part 1 failed")
                for _attempt in range(400):
                    if kwargs["cancelled"]():
                        cancellation_seen.set()
                        raise InterruptedError("cancelled sibling")
                    threading.Event().wait(0.005)
                raise TimeoutError("parallel cancellation was not propagated")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ), mock.patch.object(studio, "_export_merged_audio_ffmpeg") as mp3_export:
                with self.assertRaisesRegex(RuntimeError, "part 1 failed"):
                    app._run_output_target_set(
                        (group_id,),
                        children,
                        groups,
                        files,
                        targets,
                        root,
                        max_workers=2,
                        progress_callback=lambda current, total, subject: progress.append(
                            (current, total, subject)
                        ),
                    )

        self.assertTrue(cancellation_seen.is_set())
        self.assertTrue({1, 2}.issubset(calls))
        self.assertEqual(len(calls), len(set(calls)))
        mp3_export.assert_not_called()
        self.assertEqual(
            [
                event[0] for event in progress
                if event[0] != studio.PROGRESS_STATUS_ONLY
            ],
            [],
        )

    def test_external_cancel_interrupts_parallel_m4b_parts_without_completion(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "book"
        file_ids = ("chapter-1", "chapter-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        cancel_event = threading.Event()
        cancel_announced = threading.Event()
        both_started = threading.Barrier(2)
        calls = []
        progress = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            files = {}
            for file_id in file_ids:
                source = root / f"{file_id}.mp3"
                source.write_bytes(b"audio")
                files[file_id] = {
                    "path": str(source),
                    "title": file_id,
                    "duration": 10,
                }
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(root / "m4b"),
                filename_template="{book}",
                max_chapters=1,
            ).to_dict()

            def fake_m4b(_audio_files, _output_path, **kwargs):
                part_number = kwargs["disk_number"]
                with lock:
                    calls.append(part_number)
                both_started.wait(timeout=2)
                if part_number == 1:
                    cancel_event.set()
                    cancel_announced.set()
                else:
                    self.assertTrue(cancel_announced.wait(timeout=2))
                self.assertTrue(kwargs["cancelled"]())
                raise InterruptedError("user cancelled")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                with self.assertRaises(InterruptedError):
                    app._run_output_target_set(
                        (group_id,),
                        children,
                        groups,
                        files,
                        (target,),
                        root,
                        max_workers=2,
                        cancelled=cancel_event.is_set,
                        progress_callback=lambda current, total, subject: progress.append(
                            (current, total, subject)
                        ),
                    )

        self.assertEqual(sorted(calls), [1, 2])
        self.assertEqual(
            [
                event[0] for event in progress
                if event[0] != studio.PROGRESS_STATUS_ONLY
            ],
            [],
        )

    def test_export_file_record_preserves_source_names_and_empty_editable_title(self):
        settings = {
            "path": "/library/physical-name.mp3",
            "source_name": "physical-name",
            "source_title": "OriginalTitle",
            "title": "",
        }

        normalized = studio.normalize_export_file_record(settings)

        self.assertEqual(normalized["source_name"], "physical-name")
        self.assertEqual(normalized["source_title"], "OriginalTitle")
        self.assertEqual(normalized["title"], "")
        self.assertEqual(normalized["source_filename"], "physical-name.mp3")
        self.assertEqual(settings["title"], "")

    def test_export_path_template_distinguishes_source_and_editable_titles(self):
        planned = studio.plan_export_target_paths(
            ("file-1",),
            {},
            {},
            {
                "file-1": {
                    "path": "/library/physical-name.wav",
                    "source_name": "physical-name",
                    "source_title": "OriginalTitle",
                    "title": "EditedTitle",
                }
            },
            (
                studio.OutputTarget(
                    format="mp3",
                    output_dir="/exports",
                    filename_template=(
                        "{source_name}__{source_title}__{title}"
                    ),
                ),
            ),
            "/fallback",
        )

        self.assertEqual(len(planned), 1)
        self.assertEqual(
            planned[0]["path"].name,
            "physical-name__OriginalTitle__EditedTitle.mp3",
        )

    def test_m4b_chapter_template_distinguishes_source_and_editable_titles(self):
        chapters = [
            {
                "path": "/library/physical-name.wav",
                "source_name": "physical-name",
                "source_title": "OriginalTitle",
                "title": "EditedTitle",
                "duration": 1,
            }
        ]

        rendered = studio.render_m4b_chapter_titles(
            chapters,
            "{source_name}|{source_title}|{title}",
            book="Book",
        )

        self.assertEqual(
            rendered[0]["title"],
            "physical-name|OriginalTitle|EditedTitle",
        )
        self.assertEqual(chapters[0]["title"], "EditedTitle")

    def test_output_template_renders_padding_and_literal_braces(self):
        rendered = studio.render_output_template(
            "{book} — Главы {first_index:03d}-{last_index:03d} {{preview}}.{ext}",
            {
                "book": "Моя книга",
                "first_index": 1,
                "last_index": 10,
                "ext": "m4b",
            },
            studio.TEMPLATE_FIELDS_OUTPUT,
        )
        self.assertEqual(rendered, "Моя книга — Главы 001-010 {preview}.m4b")

    def test_output_template_alias_supports_combined_start_and_width(self):
        rendered = studio.render_output_template(
            "{volume:1:02d}-{file_index:1:03d}",
            {"volume": 2, "file_index": 3},
            studio.TEMPLATE_FIELDS_OUTPUT,
        )
        self.assertEqual(rendered, "02-003")

    def test_output_template_supports_adaptive_padding(self):
        template = "{file_index:00d}"
        self.assertEqual(
            studio.render_output_template(
                template,
                {"file_index": 1, "chapter_count": 9},
                studio.TEMPLATE_FIELDS_OUTPUT,
            ),
            "1",
        )
        self.assertEqual(
            studio.render_output_template(
                template,
                {"file_index": 9, "chapter_count": 9},
                studio.TEMPLATE_FIELDS_OUTPUT,
            ),
            "9",
        )
        self.assertEqual(
            studio.render_output_template(
                template,
                {"file_index": 1, "chapter_count": 10},
                studio.TEMPLATE_FIELDS_OUTPUT,
            ),
            "01",
        )
        self.assertEqual(
            studio.render_output_template(
                "{file_index:10:00d}",
                {"file_index": 1, "chapter_count": 10},
                studio.TEMPLATE_FIELDS_OUTPUT,
            ),
            "10",
        )
        self.assertEqual(
            studio.render_output_template(
                "{file_index:1:auto}",
                {"file_index": 10, "chapter_count": 10},
                studio.TEMPLATE_FIELDS_OUTPUT,
            ),
            "10",
        )

    def test_m4b_chapter_template_supports_adaptive_padding(self):
        chapters = [
            {"title": f"Глава {index}", "duration": 1}
            for index in range(1, 10)
        ]
        rendered = studio.render_m4b_chapter_titles(
            chapters,
            "Глава {volume_index:00d}",
            book="Книга",
            chapter_count=9,
        )
        self.assertEqual(rendered[0]["title"], "Глава 1")
        self.assertEqual(rendered[-1]["title"], "Глава 9")

        chapters.append({"title": "Глава 10", "duration": 1})
        rendered = studio.render_m4b_chapter_titles(
            chapters,
            "Глава {volume_index:00d}",
            book="Книга",
            chapter_count=10,
        )
        self.assertEqual(rendered[0]["title"], "Глава 01")
        self.assertEqual(rendered[-1]["title"], "Глава 10")

    def test_output_template_rejects_unknown_or_unsafe_fields(self):
        invalid_templates = (
            "{unknown}",
            "{book.__class__}",
            "{book[0]}",
            "{first_index:03x}",
            "{book!r}",
            "{book",
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                with self.assertRaises(studio.TemplateError):
                    studio.validate_output_template(
                        template, studio.TEMPLATE_FIELDS_OUTPUT
                    )

    def test_output_template_reports_missing_context_value(self):
        with self.assertRaisesRegex(studio.TemplateError, "нет значения.*last_index"):
            studio.render_output_template(
                "{first_index:03d}-{last_index:03d}",
                {"first_index": 1},
                studio.TEMPLATE_FIELDS_OUTPUT,
            )

    def test_output_template_supports_short_part_aliases_in_common_context(self):
        rendered = studio.render_output_template(
            "{book} {num:02d} {first:03d}-{last:03d}",
            {
                "book": "Книга",
                "num": 2,
                "first": 11,
                "last": 20,
            },
            studio.TEMPLATE_FIELDS_OUTPUT,
        )
        self.assertEqual(rendered, "Книга 02 011-020")

    def test_m4b_chapter_title_helper_preserves_empty_template_and_global_numbering(self):
        chapters = [
            {"title": "Первая глава", "duration": 1},
            {"title": "Вторая глава", "duration": 2},
        ]

        self.assertEqual(
            [
                item["title"]
                for item in studio.render_m4b_chapter_titles(
                    chapters,
                    "",
                    book="Книга",
                    chapter_start=7,
                    chapter_count=20,
                )
            ],
            ["Первая глава", "Вторая глава"],
        )
        rendered = studio.render_m4b_chapter_titles(
            chapters,
            "{num:03d}. {chapter_title}",
            book="Книга",
            part=2,
            parts=3,
            chapter_start=7,
            chapter_count=20,
        )
        self.assertEqual(
            [item["title"] for item in rendered],
            ["007. Первая глава", "008. Вторая глава"],
        )
        # Рендеринг не должен менять записи глав, переданные вызывающим кодом.
        self.assertEqual(chapters[0]["title"], "Первая глава")

    def test_m4b_chapter_title_template_supports_global_and_volume_counters(self):
        """Сквозная нумерация глав продолжается, а в томах начинается заново.

        Разделённый M4B обрабатывается по одной части. Поэтому поле
        ``global_index`` сохраняет нумерацию исходника/книги, а
        ``volume_index`` относится к текущей части и снова начинается с
        единицы. ``volume_chapter_count`` описывает только эту часть, поэтому
        метка может использовать любую политику без разбора имени файла.
        """
        chapters = [
            {"index": 1, "title": "Глава 1", "duration": 1},
            {"index": 2, "title": "Глава 2", "duration": 1},
            {"index": 3, "title": "Глава 3", "duration": 1},
            {"index": 4, "title": "Глава 4", "duration": 1},
        ]
        template = "{global_index:02d}/{volume_index:02d}/{volume_chapter_count}"
        first_volume = studio.render_m4b_chapter_titles(
            chapters[:2],
            template,
            book="Книга",
            part=1,
            parts=2,
            chapter_start=1,
            chapter_count=4,
        )
        second_volume = studio.render_m4b_chapter_titles(
            chapters[2:],
            template,
            book="Книга",
            part=2,
            parts=2,
            chapter_start=3,
            chapter_count=4,
        )

        self.assertEqual(
            [item["title"] for item in first_volume],
            ["01/01/2", "02/02/2"],
        )
        self.assertEqual(
            [item["title"] for item in second_volume],
            ["03/01/2", "04/02/2"],
        )

    def test_m4b_counter_fields_are_valid_in_profiles_and_not_leaked_to_other_formats(self):
        target = studio.normalize_output_target(
            {
                "format": "m4b",
                "chapter_title_template": (
                    "Том {part}/{parts}, глава {volume_index:02d}/"
                    "{volume_chapter_count}, книга {global_index:03d}"
                ),
            }
        )
        self.assertEqual(
            target["chapter_title_template"],
            "Том {part}/{parts}, глава {volume_index:02d}/"
            "{volume_chapter_count}, книга {global_index:03d}",
        )

        # Метки глав имеют смысл только для M4B. При смене сохранённой цели на
        # другой формат их нужно очистить, а не сохранять неиспользуемое поле
        # в аудиопрофиле.
        non_m4b = studio.normalize_output_target(
            {
                "format": "opus",
                "chapter_title_template": "{volume_index}",
            }
        )
        self.assertEqual(non_m4b["chapter_title_template"], "")

    def test_m4b_counter_start_after_colon_is_applied_per_counter(self):
        """Простое число задаёт старт, а ``:03d`` остаётся форматом ширины."""
        chapters = [
            {"index": 4, "title": "A", "duration": 1},
            {"index": 5, "title": "B", "duration": 1},
        ]
        rendered = studio.render_m4b_chapter_titles(
            chapters,
            "{global_index:10}/{volume_index:20}/{global_index:03d}",
            book="Книга",
            part=2,
            parts=3,
            chapter_start=4,
            chapter_count=6,
            global_start=4,
        )
        self.assertEqual(
            [item["title"] for item in rendered],
            ["10/20/004", "11/21/005"],
        )

        # Второй том получает то же локальное начальное значение, а сквозной
        # счётчик продолжается от переданного ``chapter_start``.
        second = studio.render_m4b_chapter_titles(
            [{"index": 6, "title": "C", "duration": 1}],
            "{global_index:10}/{volume_index:20}",
            book="Книга",
            part=3,
            parts=3,
            chapter_start=6,
            chapter_count=6,
            global_start=4,
        )
        self.assertEqual(second[0]["title"], "12/20")

    def test_m4b_volume_counter_supports_custom_start_and_padding(self):
        first = {
            "book": "Книга",
            "volume": 1,
            "part": 1,
            "num": 1,
            "index": 1,
            "parts": 2,
        }
        second = dict(first, volume=2, part=2, num=2, index=2)
        self.assertEqual(
            studio.render_m4b_part_template(
                "{book} Том {volume:10}",
                first,
                studio.SOURCE_M4B_TEMPLATE_FIELDS,
            ),
            "Книга Том 10",
        )
        self.assertEqual(
            studio.render_m4b_part_template(
                "{book} Том {volume:10}",
                second,
                studio.SOURCE_M4B_TEMPLATE_FIELDS,
            ),
            "Книга Том 11",
        )
        self.assertEqual(
            studio.render_m4b_part_template(
                "{book} Том {volume:03d}",
                first,
                studio.SOURCE_M4B_TEMPLATE_FIELDS,
            ),
            "Книга Том 001",
        )
        self.assertEqual(
            studio.render_m4b_part_template(
                "{book} Том {volume:10:03d}",
                second,
                studio.SOURCE_M4B_TEMPLATE_FIELDS,
            ),
            "Книга Том 011",
        )

    def test_m4b_chapter_counters_combine_custom_start_and_padding(self):
        rendered = studio.render_m4b_chapter_titles(
            [
                {"index": 4, "title": "A", "duration": 1},
                {"index": 5, "title": "B", "duration": 1},
            ],
            "{global_index:10:04d}/{volume_index:20:03d}",
            book="Книга",
            chapter_start=4,
            chapter_count=6,
            global_start=4,
        )
        self.assertEqual(
            [item["title"] for item in rendered],
            ["0010/020", "0011/021"],
        )

    def test_m4b_chapter_labels_can_start_volume_number_separately(self):
        rendered = studio.render_m4b_chapter_titles(
            [{"title": "Глава", "index": 7}],
            "Том {volume:10}, глава {volume_index:20}",
            book="Книга",
            part=3,
            parts=4,
            chapter_start=7,
            chapter_count=20,
        )
        self.assertEqual(rendered[0]["title"], "Том 12, глава 20")

    def test_source_group_rebuild_overrides_preserve_exact_manual_group(self):
        overrides = studio.source_group_rebuild_overrides(
            {
                "one": {
                    "name": "Мой том",
                    "name_template": None,
                    "file_ids": ("a", "b"),
                    "metadata_overrides": {
                        "album": "Книга",
                        "artist": "Автор",
                    },
                },
                "two": {
                    "name": "Автоматически",
                    "name_template": "{book} {part}",
                    "file_ids": ("c",),
                },
            }
        )
        self.assertEqual(overrides[("a", "b")]["name"], "Мой том")
        self.assertIsNone(overrides[("a", "b")]["name_template"])
        self.assertEqual(
            overrides[("a", "b")]["metadata_overrides"],
            {"album": "Книга", "artist": "Автор"},
        )
        self.assertNotIn("name", overrides[("c",)])

    def test_source_group_tag_overrides_allow_explicit_clear(self):
        tags = studio.apply_source_metadata_overrides(
            {"title": "Старое", "album": "Книга", "artist": "Автор"},
            {"title": "Том 2", "album": "", "year": "2026"},
        )
        self.assertEqual(
            tags,
            {"title": "Том 2", "artist": "Автор", "date": "2026"},
        )

    def test_saved_audio_log_includes_resolved_path_and_warning(self):
        with tempfile.TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "book.opus"
            with self.assertLogs(level="INFO") as captured:
                studio.log_saved_audio(target, has_warnings=True)
            message = "\n".join(captured.output)
            self.assertIn("Аудиофайл сохранён (с предупреждениями)", message)
            self.assertIn(str(target.resolve()), message)

    def test_m4b_counter_start_preserves_sparse_global_positions(self):
        """Начало сквозного счётчика привязано к позиции в книге.

        План источников может начинаться с главы 7 или содержать намеренный
        пропуск (например, главы 7 и 9). Обычное число после двоеточия задаёт
        смещение от позиции в книге, а счётчик тома остаётся последовательным
        и запускается заново для каждого тома.
        """
        rendered = studio.render_m4b_chapter_titles(
            [
                {"index": 7, "title": "Седьмая", "duration": 1},
                {"index": 9, "title": "Девятая", "duration": 1},
            ],
            "{global_index:10}/{volume_index:20}",
            book="Книга",
            chapter_start=7,
            chapter_count=20,
            global_start=1,
        )
        self.assertEqual(
            [item["title"] for item in rendered],
            ["16/20", "18/21"],
        )

        # Если общее число не передано явно, разреженный план всё равно должен
        # показывать значение не меньше последней сквозной позиции.
        with_total = studio.render_m4b_chapter_titles(
            [
                {"index": 7, "title": "Седьмая", "duration": 1},
                {"index": 9, "title": "Девятая", "duration": 1},
            ],
            "{chapter_count}",
            book="Книга",
            chapter_start=7,
            global_start=1,
        )
        self.assertEqual(
            [item["title"] for item in with_total],
            ["9", "9"],
        )

    def test_m4b_global_index_field_overrides_legacy_index_when_supplied(self):
        rendered = studio.render_m4b_chapter_titles(
            [
                {
                    "index": 2,
                    "global_index": 17,
                    "title": "Глава",
                    "duration": 1,
                },
                {"index": 3, "title": "Следующая", "duration": 1},
            ],
            "{global_index}/{index}/{volume_index}",
            book="Книга",
            chapter_start=10,
            chapter_count=20,
        )
        self.assertEqual(
            [item["title"] for item in rendered],
            ["17/17/1", "3/3/2"],
        )

    def test_m4b_chapter_title_template_normalizes_and_serializes(self):
        target = studio.normalize_output_target(
            {
                "format": "m4b",
                "chapter_title_template": "  {num:03d} — {title}  ",
            }
        )
        self.assertEqual(
            target["chapter_title_template"], "{num:03d} — {title}"
        )

        bundle = studio.output_target_library_from_targets(
            [studio.OutputTarget(**target)]
        )
        restored = studio.normalize_output_target_library(bundle)["targets"]
        self.assertEqual(
            restored[0]["chapter_title_template"], "{num:03d} — {title}"
        )

    def test_m4b_chapter_title_template_rejects_unknown_field(self):
        with self.assertRaises(studio.TemplateError):
            studio.normalize_output_target(
                {
                    "format": "m4b",
                    "chapter_title_template": "{unknown}",
                }
            )

    def test_non_m4b_target_clears_chapter_title_template(self):
        normalized = studio.normalize_output_target(
            {
                "format": "opus",
                "chapter_title_template": "{num:03d} — {title}",
            }
        )
        self.assertEqual(normalized["chapter_title_template"], "")

    def test_adaptive_padding_uses_global_book_extent(self):
        self.assertEqual(studio.adaptive_number_width(9), 1)
        self.assertEqual(studio.adaptive_number_width(10), 2)
        self.assertEqual(studio.adaptive_number_width(100), 3)
        # Для части с главами 1..10 из книги на 100 глав сохраняется ширина 3.
        self.assertEqual(
            studio.format_chapter_range(1, 10, total_count=100), "001-010"
        )
        self.assertEqual(studio.format_chapter_range(1, 10, total_count=10), "01-10")
        self.assertEqual(studio.format_chapter_range(1, 9, total_count=9), "1-9")
        self.assertEqual(
            studio.format_adaptive_index(7, 100),
            "007",
        )

    def test_adaptive_padding_can_be_overridden_explicitly(self):
        self.assertEqual(
            studio.format_adaptive_index(7, 100, width=4), "0007"
        )
        self.assertEqual(
            studio.format_chapter_range(1, 10, total_count=100, width=2),
            "01-10",
        )

    def test_m4b_planner_splits_only_between_chapters(self):
        chapters = [
            {"index": 1, "title": "Вступление", "duration": 10},
            {"index": 2, "title": "Глава 1", "duration": 20},
            {"index": 3, "title": "Глава 2", "duration": 30},
            {"index": 4, "title": "Глава 3", "duration": 40},
        ]
        parts = studio.plan_m4b_parts(chapters, max_duration_seconds=50)

        self.assertEqual(len(parts), 3)
        self.assertEqual(
            [(part.number, part.total, part.chapter_start, part.chapter_end)
             for part in parts],
            [(1, 3, 1, 2), (2, 3, 3, 3), (3, 3, 4, 4)],
        )
        self.assertEqual([part.duration_seconds for part in parts], [30.0, 30.0, 40.0])
        # Отдельная глава длиннее лимита остаётся целой.
        long_part = studio.plan_m4b_parts(
            [("Очень длинная глава", 120)], max_duration_seconds=60
        )

        # Явный ноль оставляет обычные книги без ограничения.
        safe_parts = studio.plan_m4b_parts(
            [("Глава 1", 12 * 3600), ("Глава 2", 12 * 3600)],
            max_duration_seconds=0,
        )
        self.assertEqual(len(safe_parts), 1)
        self.assertEqual(len(long_part), 1)
        self.assertEqual(long_part[0].chapters[0]["duration"], 120.0)

    def test_m4b_planner_supports_chapter_count_limit_and_empty_input(self):
        chapters = [(f"Глава {index}", 1) for index in range(1, 6)]
        parts = studio.plan_m4b_parts(chapters, max_chapters=2)
        self.assertEqual(
            [(part.chapter_start, part.chapter_end, part.duration_seconds)
             for part in parts],
            [(1, 2, 2.0), (3, 4, 2.0), (5, 5, 1.0)],
        )
        self.assertEqual(studio.plan_m4b_parts([]), ())

    def test_m4b_planner_counts_interchapter_pause_in_duration_limit(self):
        parts = studio.plan_m4b_parts(
            [("A", 5), ("B", 5), ("C", 5)],
            max_duration_seconds=11,
            pause_seconds=1,
        )
        self.assertEqual(
            [(part.chapter_start, part.chapter_end, part.duration_seconds)
             for part in parts],
            [(1, 2, 11.0), (3, 3, 5.0)],
        )

    def test_m4b_automatic_long_book_limit_is_conservative_and_explicit(self):
        """Автополитика включается только после суток и не превышает 23:50."""
        self.assertEqual(
            studio.resolve_m4b_auto_duration_limit(24 * 3600, 0),
            0.0,
        )
        self.assertEqual(
            studio.resolve_m4b_auto_duration_limit(24 * 3600 + 1, 0),
            float(studio.M4B_SAFE_VOLUME_SECONDS),
        )
        # Более строгий лимит пользователя имеет приоритет.
        self.assertEqual(
            studio.resolve_m4b_auto_duration_limit(40 * 3600, 2 * 3600),
            2 * 3600,
        )
        # Слишком большой явный лимит ограничивается защитным пределом.
        self.assertEqual(
            studio.resolve_m4b_auto_duration_limit(40 * 3600, 30 * 3600),
            float(studio.M4B_SAFE_VOLUME_SECONDS),
        )

    def test_m4b_automatic_planner_keeps_chapters_atomic(self):
        chapters = [
            {"title": "A", "duration": 12 * 3600},
            {"title": "B", "duration": 11 * 3600},
            {"title": "C", "duration": 12 * 3600},
        ]
        parts = studio.plan_m4b_parts(
            chapters,
            max_duration_seconds=0,
            auto_split_long=True,
        )
        self.assertEqual(
            [(part.chapter_start, part.chapter_end, part.total) for part in parts],
            [(1, 2, 2), (3, 3, 2)],
        )
        self.assertLessEqual(
            parts[0].duration_seconds,
            float(studio.M4B_SAFE_VOLUME_SECONDS),
        )

    def test_m4b_reflow_uses_actual_durations_and_preserves_chapter_boundaries(self):
        # При включённом переразбиении существующие виртуальные границы
        # намеренно снимаются: плоский порядок собирается заново по измеренной
        # длительности, но глава никогда не разрезается пополам.
        result = studio.reflow_m4b_groups_by_duration(
            [("a", "b"), ("c", "d")],
            {"a": 40, "b": 20, "c": 35, "d": 25},
            60,
        )
        self.assertEqual(result.groups, (("a", "b"), ("c", "d")))
        self.assertEqual(result.oversized, ())
        self.assertEqual(result.total_duration_seconds, 120.0)
        self.assertEqual(result.limit_seconds, 60.0)

    def test_m4b_reflow_accounts_for_pause_between_whole_chapters(self):
        result = studio.reflow_m4b_parts_by_duration(
            [["a", "b", "c"]],
            {"a": 5, "b": 5, "c": 5},
            max_duration_seconds=11,
            pause_seconds=1,
        )
        self.assertEqual(result.groups, (("a", "b"), ("c",)))
        self.assertEqual(result.total_duration_seconds, 17.0)

    def test_m4b_reflow_reports_oversized_chapter_without_splitting_it(self):
        result = studio.reflow_m4b_groups_by_duration(
            ["intro", "long", "tail"],
            {"intro": 10, "long": 125, "tail": 10},
            60,
        )
        self.assertEqual(result.groups, (("intro",), ("long",), ("tail",)))
        self.assertEqual(len(result.oversized), 1)
        diagnostic = result.oversized[0]
        self.assertIsInstance(diagnostic, studio.M4BReflowDiagnostic)
        self.assertEqual(diagnostic.chapter_id, "long")
        self.assertEqual(diagnostic.chapter_index, 2)
        self.assertEqual(diagnostic.duration_seconds, 125.0)
        self.assertEqual(diagnostic.limit_seconds, 60.0)
        self.assertEqual(diagnostic.over_by_seconds, 65.0)
        self.assertEqual(diagnostic.reason, "chapter_exceeds_limit")
        self.assertEqual(result.diagnostics, result.oversized)
        self.assertEqual(result.oversized_chapters, result.oversized)

    def test_m4b_reflow_zero_limit_is_a_non_destructive_noop(self):
        result = studio.reflow_m4b_groups_by_duration(
            [("a",), ("b",)], {"a": 2, "b": 3}, 0
        )
        self.assertEqual(result.groups, (("a",), ("b",)))
        self.assertEqual(result.oversized, ())

    def test_m4b_reflow_rejects_missing_invalid_or_duplicate_durations(self):
        with self.assertRaisesRegex(ValueError, "нет фактической длительности"):
            studio.reflow_m4b_groups_by_duration([["a", "b"]], {"a": 1}, 10)
        with self.assertRaises(ValueError):
            studio.reflow_m4b_groups_by_duration([["a"]], {"a": float("nan")}, 10)
        with self.assertRaisesRegex(ValueError, "несколько M4B-групп"):
            studio.reflow_m4b_groups_by_duration(
                [("a",), ("a",)], {"a": 1}, 10
            )

    def test_m4b_reflow_result_is_serializable_without_gui_objects(self):
        result = studio.reflow_m4b_groups_by_duration(
            [["a", "b"]], {"a": 70, "b": 1}, 60
        )
        payload = result.to_dict()
        self.assertEqual(payload["groups"], [["a"], ["b"]])
        self.assertEqual(payload["oversized"][0]["chapter_id"], "a")

    def test_m4b_target_dict_inherits_global_limits_only_when_omitted(self):
        group_id = "group"
        file_ids = tuple(f"file-{index}" for index in range(1, 4))
        groups = {group_id: {"name": "Book", "merge": True}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 3600,
            }
            for file_id in file_ids
        }
        inherited = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [{"format": "m4b"}],
            "/tmp/out",
            m4b_max_duration_seconds=2 * 3600,
        )
        self.assertEqual(len(inherited), 2)

        unlimited = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [{"format": "m4b", "max_duration_seconds": 0}],
            "/tmp/out",
            m4b_max_duration_seconds=2 * 3600,
        )
        self.assertEqual(len(unlimited), 1)

    def test_output_targets_can_choose_group_assembly_independently(self):
        group_id = "group"
        file_ids = ("file-1", "file-2")
        groups = {group_id: {"name": "Book", "merge": False}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 10,
            }
            for file_id in file_ids
        }
        planned = studio.plan_export_target_paths(
            (group_id,),
            children,
            groups,
            files,
            (
                studio.OutputTarget(
                    format="mp3", assembly_mode="merge"
                ),
                studio.OutputTarget(
                    format="opus", assembly_mode="files"
                ),
            ),
            "/tmp/out",
        )
        self.assertEqual(
            [(item["target"]["format"], item["kind"]) for item in planned],
            [("mp3", "group"), ("opus", "file"), ("opus", "file")],
        )

    def test_output_target_files_mode_overrides_merged_group(self):
        planned = studio.plan_export_target_paths(
            ("group",),
            {"group": ("file-1", "file-2")},
            {"group": {"name": "Book", "merge": True}},
            {
                "file-1": {"path": "/tmp/1.mp3", "title": "One"},
                "file-2": {"path": "/tmp/2.mp3", "title": "Two"},
            },
            (studio.OutputTarget(format="mp3", assembly_mode="files"),),
            "/tmp/out",
        )
        self.assertEqual([item["kind"] for item in planned], ["file", "file"])

    def test_m4b_always_normalizes_to_merged_assembly(self):
        target = studio.normalize_output_target(
            studio.OutputTarget(format="m4b", assembly_mode="files")
        )
        self.assertEqual(target["assembly_mode"], "merge")
        self.assertTrue(
            studio.output_target_merges_group(target, {"merge": False})
        )

    def test_compact_export_target_uses_the_common_group_contract(self):
        target = studio.compact_export_output_target(
            output_format="opus",
            bitrate="48k",
            sample_rate="48000",
            channels="mono",
            apply_effects=True,
            effects={"speed": 1.1, "pitch": 1.0, "echo": False},
        )

        self.assertEqual(target["format"], "opus")
        self.assertEqual(target["assembly_mode"], "inherit")
        self.assertTrue(target["apply_effects"])
        self.assertTrue(
            studio.output_target_merges_group(target, {"merge": True})
        )
        self.assertFalse(
            studio.output_target_merges_group(target, {"merge": False})
        )

    def test_compact_m4b_target_keeps_limits_and_disables_effects(self):
        target = studio.compact_export_output_target(
            output_format="m4b",
            bitrate="128k",
            sample_rate="48000",
            channels="mono",
            apply_effects=True,
            effects={"speed": 1.2, "pitch": 1.1, "echo": True},
            m4b_bitrate="96k",
            m4b_max_duration_seconds=3600,
            m4b_max_chapters=20,
        )

        self.assertEqual(target["bitrate"], "96k")
        self.assertEqual(target["assembly_mode"], "merge")
        self.assertFalse(target["apply_effects"])
        self.assertEqual(target["max_duration_seconds"], 3600)
        self.assertEqual(target["max_chapters"], 20)

    def test_source_targets_expose_their_fixed_assembly_contract(self):
        targets = studio.normalize_source_synthesis_targets(
            [
                {"format": "mp3", "assembly_mode": "inherit"},
                {"format": "m4b", "assembly_mode": "files"},
            ]
        )
        self.assertEqual(
            [target["assembly_mode"] for target in targets],
            ["files", "merge"],
        )

    def test_export_m4b_static_template_gets_deterministic_part_suffix(self):
        """Статическое имя M4B не должно перезаписывать соседние тома."""
        group_id = "group"
        file_ids = tuple(f"file-{index}" for index in range(1, 4))
        groups = {group_id: {"name": "Book", "merge": True}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 3600,
            }
            for file_id in file_ids
        }
        planned = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [
                studio.OutputTarget(
                    format="m4b",
                    output_dir="/tmp/out",
                    filename_template="{book}",
                    max_duration_seconds=2 * 3600,
                )
            ],
            "/tmp/out",
            auto_split_long_m4b=False,
        )
        self.assertEqual(
            [item["path"].name for item in planned],
            ["Book Часть 1.m4b", "Book Часть 2.m4b"],
        )

    def test_export_m4b_worker_keeps_explicit_empty_template(self):
        """Пустой шаблон экспорта не должен возвращать диапазон глав."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "group"
        children = {group_id: ("chapter-1",)}
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        files = {
            "chapter-1": {
                "path": "/tmp/chapter-1.mp3",
                "title": "Chapter 1",
                "duration": 10,
            }
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b", output_dir=tempdir, filename_template=""
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(Path(output_path))
                Path(output_path).write_bytes(b"m4b")

            # Принудительно используем резервное именование фонового обработчика. Обычная
            # предварительная проверка передаёт готовый путь, поэтому здесь
            # проверяется второй рендерер для старого/неполного снимка.
            with mock.patch.object(
                studio, "plan_export_target_paths", return_value=()
            ), mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    (group_id,), children, groups, files, (target,), tempdir
                )

        self.assertEqual([path.name for path in calls], ["Book.m4b"])

    def test_export_m4b_empty_template_uses_group_name_and_parts_suffix(self):
        """Пустой шаблон совпадает с политикой имени группы для исходников."""
        group_id = "group"
        file_ids = ("chapter-1", "chapter-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 3600,
            }
            for file_id in file_ids
        }
        target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/out",
            filename_template="",
            max_duration_seconds=3600,
        )
        planned = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [target],
            "/tmp/out",
            m4b_template="",
            auto_split_long_m4b=False,
        )
        self.assertEqual(
            [item["path"].name for item in planned],
            ["Book Часть 1.m4b", "Book Часть 2.m4b"],
        )

    def test_export_m4b_default_template_uses_group_name_without_range(self):
        """Новая быстрая M4B-цель не добавляет диапазон без выбора шаблона."""
        group_id = "group"
        chapter_id = "chapter-1"
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: (chapter_id,)}
        files = {
            chapter_id: {
                "path": "/tmp/chapter-1.mp3",
                "title": "Chapter 1",
                "duration": 10,
            }
        }
        target = studio.OutputTarget(
            format="m4b", output_dir="/tmp/out", filename_template=""
        )

        planned = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [target],
            "/tmp/out",
            auto_split_long_m4b=False,
        )

        self.assertEqual([item["path"].name for item in planned], ["Book.m4b"])

    def test_export_m4b_current_template_overrides_stale_legacy_target_snapshot(self):
        """Старый диапазон из снимка цели не должен побеждать новое поле."""
        group_id = "group"
        file_ids = ("chapter-1", "chapter-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 10,
            }
            for file_id in file_ids
        }
        stale_target = studio.OutputTarget(
            format="m4b",
            output_dir="/tmp/out",
            filename_template=studio.DEFAULT_SOURCE_M4B_TEMPLATE,
        )
        planned = studio.plan_export_target_paths(
            [group_id],
            children,
            groups,
            files,
            [stale_target],
            "/tmp/out",
            m4b_template="{book}",
            auto_split_long_m4b=False,
        )
        self.assertEqual([item["path"].name for item in planned], ["Book.m4b"])
        self.assertTrue(all("Главы" not in item["path"].name for item in planned))

    def test_m4b_worker_uses_same_normalized_pause_as_planner(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book} {range}",
            "export_m4b_max_duration_hours": 23.0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "group"
        file_ids = ("file-1", "file-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": "1000.5"}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 5,
            }
            for file_id in file_ids
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                bitrate="64k",
                max_duration_seconds=11,
            ).to_dict()
            with mock.patch.object(studio, "_export_m4b_ffmpeg") as export:
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

            export.assert_called_once()
            self.assertEqual(export.call_args.kwargs["pause_ms"], 1000)
            self.assertEqual(len(export.call_args.kwargs["chapters"]), 2)

    def test_m4b_worker_reuses_automatic_long_book_split_and_disc_metadata(self):
        """Фоновый обработчик кодирует каждый том длиннее суток из предварительного плана."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book} {range}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        group_id = "long-book"
        file_ids = ("chapter-1", "chapter-2", "chapter-3")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        # 12 ч + 11 ч + 12 ч = 35 ч. При явном нулевом лимите автоматическая
        # политика должна создать два тома с пределом 23:50, не разрезая главу.
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": duration,
            }
            for file_id, duration in zip(
                file_ids, (12 * 3600, 11 * 3600, 12 * 3600)
            )
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                bitrate="64k",
                max_duration_seconds=0,
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append((tuple(audio_files), Path(output_path), kwargs))
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [(call[2]["disk_number"], call[2]["disk_total"])
             for call in calls],
            [(1, 2), (2, 2)],
        )
        self.assertEqual(
            [(call[2]["track_number"], call[2]["track_total"])
             for call in calls],
            [(1, 2), (2, 2)],
        )
        self.assertEqual(
            [len(call[2]["chapters"]) for call in calls],
            [2, 1],
        )
        self.assertEqual(
            [path.name for _sources, path, _kwargs in calls],
            ["Book 1-2.m4b", "Book 3-3.m4b"],
        )

    def test_m4b_worker_numbers_manual_groups_as_one_album_sequence(self):
        """Группы с общим альбомом получают общие N/T, а не по 1/1 каждая."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "volume-1": {"name": "Том 1", "merge": True, "album": "Книга"},
            "volume-2": {"name": "Том 2", "merge": True, "album": "книга"},
        }
        children = {"volume-1": ("chapter-1",), "volume-2": ("chapter-2",)}
        files = {
            "chapter-1": {
                "path": "/tmp/chapter-1.mp3",
                "title": "Глава 1",
                "duration": 10,
            },
            "chapter-2": {
                "path": "/tmp/chapter-2.mp3",
                "title": "Глава 2",
                "duration": 10,
            },
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b", output_dir=tempdir, filename_template="{book}"
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    ("volume-1", "volume-2"),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )
        self.assertEqual(
            [
                (
                    item["disk_number"],
                    item["disk_total"],
                    item["track_number"],
                    item["track_total"],
                )
                for item in calls
            ],
            [(1, 2, 1, 2), (2, 2, 2, 2)],
        )

    def test_m4b_worker_chapter_counters_continue_across_same_album_groups(self):
        """``{global_index}`` проходит через тома альбома, а ``{volume_index}`` сбрасывается."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "volume-1": {"name": "Том 1", "merge": True, "album": "Книга"},
            "volume-2": {"name": "Том 2", "merge": True, "album": "книга"},
        }
        children = {
            "volume-1": ("chapter-1", "chapter-2"),
            "volume-2": ("chapter-3",),
        }
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 10,
            }
            for file_id in ("chapter-1", "chapter-2", "chapter-3")
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                filename_template="{book}",
                chapter_title_template="{global_index:10}/{volume_index:20}",
                max_chapters=1,
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    ("volume-1", "volume-2"),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

        self.assertEqual(
            [
                [chapter["title"] for chapter in call["chapters"]]
                for call in calls
            ],
            [["10/20"], ["11/20"], ["12/20"]],
        )

    def test_m4b_worker_global_counter_does_not_skip_after_part_split(self):
        """Разделение части сохраняет непрерывную сквозную нумерацию меток."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "volume-1": {"name": "Том 1", "merge": True, "album": "Книга"},
            "volume-2": {"name": "Том 2", "merge": True, "album": "книга"},
        }
        children = {
            "volume-1": ("chapter-1", "chapter-2"),
            "volume-2": ("chapter-3",),
        }
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": file_id,
                "duration": 10,
            }
            for file_id in ("chapter-1", "chapter-2", "chapter-3")
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                filename_template="{book}",
                chapter_title_template="{global_index}/{volume_index}",
                max_chapters=1,
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    ("volume-1", "volume-2"),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

        self.assertEqual(
            [
                [chapter["title"] for chapter in call["chapters"]]
                for call in calls
            ],
            [["1/1"], ["2/1"], ["3/1"]],
        )

    def test_m4b_worker_restarts_volume_sequence_for_different_albums(self):
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "book-a": {"name": "A", "merge": True, "album": "Книга A"},
            "book-b": {"name": "B", "merge": True, "album": "Книга B"},
        }
        children = {"book-a": ("a",), "book-b": ("b",)}
        files = {
            key: {"path": f"/tmp/{key}.mp3", "title": key, "duration": 10}
            for key in ("a", "b")
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(format="m4b", output_dir=tempdir).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    tuple(groups), children, groups, files, (target,), tempdir
                )
        self.assertEqual(
            [(item["disk_number"], item["disk_total"]) for item in calls],
            [(1, 1), (1, 1)],
        )

    def test_m4b_worker_keeps_same_album_separate_for_different_artists(self):
        """Одинаковое название альбома разных авторов не смешивает тома."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "book-a": {
                "name": "A",
                "merge": True,
                "album": "Общий альбом",
                "artist": "Автор A",
            },
            "book-b": {
                "name": "B",
                "merge": True,
                "album": "общий альбом",
                "artist": "Автор B",
            },
        }
        children = {"book-a": ("a",), "book-b": ("b",)}
        files = {
            key: {"path": f"/tmp/{key}.mp3", "title": key, "duration": 10}
            for key in ("a", "b")
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b", output_dir=tempdir,
                filename_template="{first_name}",
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    tuple(groups), children, groups, files, (target,), tempdir
                )
        self.assertEqual(
            [(item["disk_number"], item["disk_total"]) for item in calls],
            [(1, 1), (1, 1)],
        )

    def test_m4b_worker_does_not_join_blank_album_fallback_names(self):
        """Пустой альбом не объединяет независимые группы с одинаковым именем."""
        app = object.__new__(studio.TTSApp)
        app.config = {
            "export_m4b_template": "{book}",
            "export_m4b_max_duration_hours": 0,
            "export_m4b_max_chapters": 0,
            "export_m4b_bitrate": "64k",
        }
        groups = {
            "book-a": {"name": "Книга", "merge": True},
            "book-b": {"name": "Книга", "merge": True},
        }
        children = {"book-a": ("a",), "book-b": ("b",)}
        files = {
            key: {"path": f"/tmp/{key}.mp3", "title": key, "duration": 10}
            for key in ("a", "b")
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b", output_dir=tempdir,
                filename_template="{first_name}",
            ).to_dict()
            calls = []

            def fake_m4b(audio_files, output_path, **kwargs):
                calls.append(kwargs)
                Path(output_path).write_bytes(b"m4b")

            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_m4b
            ):
                app._run_output_target_set(
                    tuple(groups), children, groups, files, (target,), tempdir
                )
        self.assertEqual(
            [(item["disk_number"], item["disk_total"]) for item in calls],
            [(1, 1), (1, 1)],
        )

    def test_output_target_group_inherits_first_file_tags(self):
        app = object.__new__(studio.TTSApp)
        app.config = {}
        group_id = "group"
        file_ids = ("file-1", "file-2")
        groups = {
            group_id: {"name": "Book", "merge": True, "pause": 0}
        }
        children = {group_id: file_ids}
        files = {
            "file-1": {
                "path": "/tmp/one.mp3",
                "title": "Chapter 1",
                "album": "Book album",
                "artist": "Author",
                "duration": 1,
            },
            "file-2": {
                "path": "/tmp/two.mp3",
                "title": "Chapter 2",
                "album": "Book album",
                "artist": "Author",
                "duration": 1,
            },
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="opus",
                output_dir=tempdir,
                bitrate="48k",
                sample_rate="48000",
                channels="mono",
            ).to_dict()
            with mock.patch.object(studio, "_export_merged_audio_ffmpeg") as export:
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

            self.assertEqual(export.call_args.kwargs["tags"]["album"], "Book album")
            self.assertEqual(export.call_args.kwargs["tags"]["artist"], "Author")

    def test_output_target_m4b_uses_group_name_when_album_is_blank(self):
        """Сгруппированные части M4B получают стабильный резервный альбом."""
        app = object.__new__(studio.TTSApp)
        app.config = {}
        group_id = "group"
        file_ids = ("file-1", "file-2")
        groups = {group_id: {"name": "Book", "merge": True, "pause": 0}}
        children = {group_id: file_ids}
        files = {
            file_id: {
                "path": f"/tmp/{file_id}.mp3",
                "title": f"Chapter {index}",
                "duration": 1,
                "album": "",
            }
            for index, file_id in enumerate(file_ids, 1)
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                bitrate="64k",
                max_duration_seconds=0,
            ).to_dict()
            with mock.patch.object(studio, "_export_m4b_ffmpeg") as export:
                app._run_output_target_set(
                    (group_id,),
                    children,
                    groups,
                    files,
                    (target,),
                    tempdir,
                )

        self.assertEqual(export.call_count, 1)
        self.assertEqual(export.call_args.kwargs["tags"]["album"], "Book")
        self.assertEqual(export.call_args.kwargs["disk_number"], 1)
        self.assertEqual(export.call_args.kwargs["disk_total"], 1)
        self.assertEqual(export.call_args.kwargs["track_number"], 1)
        self.assertEqual(export.call_args.kwargs["track_total"], 1)

    def test_output_target_standalone_m4b_uses_title_when_album_is_blank(self):
        """Корневой M4B получает альбом для группировки в медиатеке."""
        app = object.__new__(studio.TTSApp)
        app.config = {}
        file_id = "file-1"
        files = {
            file_id: {
                "path": "/tmp/standalone-source.mp3",
                "title": "Standalone audiobook",
                "duration": 1,
                "album": "",
            }
        }
        with tempfile.TemporaryDirectory() as tempdir:
            target = studio.OutputTarget(
                format="m4b",
                output_dir=tempdir,
                bitrate="64k",
                max_duration_seconds=0,
            ).to_dict()
            with mock.patch.object(studio, "_export_m4b_ffmpeg") as export:
                app._run_output_target_set(
                    (file_id,),
                    {},
                    {},
                    files,
                    (target,),
                    tempdir,
                )

        self.assertEqual(export.call_count, 1)
        self.assertEqual(
            export.call_args.kwargs["tags"]["album"],
            "Standalone audiobook",
        )

    def test_ffmetadata_escapes_special_values_and_rounds_only_at_output(self):
        escaped = studio.escape_ffmetadata_value("a\\b=c;d#e\nnext")
        self.assertEqual(escaped, "a\\\\b\\=c\\;d\\#e\\nnext")
        metadata = studio.build_ffmetadata_chapters(
            [
                {"title": "A\\B = C; #1", "start": 0.0, "end": 1.2344},
                ("Вторая", 2.005),
            ],
            metadata={"album": "A=B", "disk": "2/3"},
        )
        self.assertTrue(metadata.startswith(";FFMETADATA1\n"))
        self.assertIn("album=A\\=B\ndisk=2/3\n", metadata)
        self.assertIn("START=0\nEND=1234\n", metadata)
        self.assertIn("title=A\\\\B \\= C\\; \\#1\n", metadata)
        self.assertIn("START=1234\nEND=3239\n", metadata)

    def test_m4b_command_contains_aac_low_chapters_and_cover_mapping(self):
        self.assertEqual(studio.normalize_m4b_audio_language("und"), "rus")
        self.assertEqual(studio.normalize_m4b_audio_language("ru"), "rus")
        self.assertEqual(studio.normalize_m4b_audio_language("en"), "eng")
        command = studio.build_m4b_ffmpeg_command(
            "/tmp/chapters.txt",
            "/tmp/metadata.txt",
            "/tmp/book.m4b",
            bitrate="64k",
            sample_rate=48000,
            channels=1,
            cover="/tmp/cover.jpg",
            disk_number=2,
            disk_total=4,
            ffmpeg_path="ffmpeg-test",
        )
        self.assertEqual(command[0], "ffmpeg-test")
        self.assertIn("-profile:a", command)
        self.assertEqual(command[command.index("-profile:a") + 1], "aac_low")
        self.assertEqual(command[command.index("-f", 1) + 1], "concat")
        self.assertIn(("-f", "ipod"), tuple(zip(command, command[1:])))
        self.assertIn("M4B ", command)
        self.assertIn("media_type=2", command)
        self.assertEqual(
            command[command.index("-metadata:s:a:0") + 1],
            "language=rus",
        )
        self.assertIn("disc=2/4", command)
        self.assertIn("track=2/4", command)
        self.assertNotIn("disk=2/4", command)
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertIn("2:v:0", command)
        self.assertEqual(command[-1], "/tmp/book.m4b")
        no_track = studio.build_m4b_ffmpeg_command(
            "/tmp/chapters.txt",
            "/tmp/metadata.txt",
            "/tmp/book-no-track.m4b",
            disk_number=2,
            disk_total=4,
            include_track_metadata=False,
            ffmpeg_path="ffmpeg-test",
        )
        self.assertIn("disc=2/4", no_track)
        self.assertNotIn("track=2/4", no_track)

        english = studio.build_m4b_ffmpeg_command(
            "/tmp/chapters.txt",
            "/tmp/metadata.txt",
            "/tmp/book-en.m4b",
            language="en",
            ffmpeg_path="ffmpeg-test",
        )
        self.assertEqual(
            english[english.index("-metadata:s:a:0") + 1],
            "language=eng",
        )

    def test_m4b_command_copies_jpeg_but_converts_png_cover(self):
        """Чистый построитель команды применяет ту же политику обложек."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            jpeg = root / "cover.bin"
            png = root / "cover.jpg"
            jpeg.write_bytes(b"\xff\xd8\xff\xe0jpeg")
            png.write_bytes(b"\x89PNG\r\n\x1a\npng")

            unknown = root / "cover.data"
            unknown.write_bytes(b"unknown")

            for cover, expected_codec in (
                (jpeg, "copy"),
                (png, "mjpeg"),
                (unknown, "mjpeg"),
            ):
                with self.subTest(cover=cover.name):
                    command = studio.build_m4b_ffmpeg_command(
                        "/tmp/chapters.txt",
                        "/tmp/metadata.txt",
                        "/tmp/book.m4b",
                        cover=cover,
                    )
                    self.assertEqual(
                        command[command.index("-c:v") + 1], expected_codec
                    )

    def test_m4b_effect_validation_rejects_non_neutral_effects(self):
        """M4B не должен принимать эффекты, меняющие длительность."""
        for kwargs in (
            {"speed": 1.1},
            {"pitch": 0.9},
            {"echo": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "Эффекты для M4B отключены"):
                    studio._validate_m4b_effects_disabled(**kwargs)

        # Нейтральные значения явно разрешены: этот помощник также используют
        # низкоуровневые экспортёры перед запуском FFmpeg.
        studio._validate_m4b_effects_disabled(
            speed=1.0, pitch=1.0, echo=False
        )

    def test_m4b_target_canonicalizes_neutral_effect_flag(self):
        """Старые нейтральные флаги не должны объявлять эффекты M4B."""
        target = studio.normalize_output_target(
            {
                "format": "m4b",
                "bitrate": "64k",
                "apply_effects": True,
                "effects": {"speed": 1.0, "pitch": 1.0, "echo": False},
            }
        )
        self.assertFalse(target["apply_effects"])

    def test_generic_m4b_merge_rejects_effects_before_ffprobe(self):
        """Старый компактный путь M4B не обходит защиту от эффектов."""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "chapter.mp3"
            source.write_bytes(b"not-a-real-audio-file")
            with self.assertRaisesRegex(ValueError, "Эффекты для M4B отключены"):
                studio._export_merged_audio_ffmpeg(
                    [source],
                    Path(tempdir) / "book.m4b",
                    output_format="m4b",
                    speed=1.1,
                )

    def test_chapter_aware_m4b_export_rejects_effects_before_stage(self):
        """Прямые вызовы помощника M4B получают ту же однозначную ошибку."""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "chapter.mp3"
            source.write_bytes(b"not-a-real-audio-file")
            with self.assertRaisesRegex(ValueError, "Эффекты для M4B отключены"):
                studio._export_m4b_ffmpeg(
                    [source],
                    Path(tempdir) / "book.m4b",
                    chapters=({"title": "Глава", "duration": 1},),
                    pitch=1.1,
                )

    def test_m4b_disk_tag_uses_itunes_numeric_pair(self):
        self.assertEqual(studio.format_m4b_disk_tag(1, 3), "1/3")
        self.assertEqual(studio.format_m4b_disk_tag(2), "2")
        self.assertEqual(studio.format_m4b_disk_tag(0, 3), "")
        self.assertEqual(studio.format_m4b_disk_tag("bad", 3), "")
        # FFmpeg называет поле метаданных ``disc``, хотя итоговый атом QuickTime
        # называется ``disk``. Мультиплексор MOV игнорирует второй вариант,
        # поэтому его нельзя передавать как дополнительный алиас.
        self.assertEqual(studio.m4b_disk_metadata(2, 3), {"disc": "2/3"})

    def test_m4b_volume_metadata_contains_disc_and_track_pairs(self):
        self.assertEqual(
            studio.m4b_volume_metadata(2, 3),
            {"disc": "2/3", "track": "2/3"},
        )
        self.assertEqual(
            studio.m4b_volume_metadata(track_number=4, track_total=5),
            {"track": "4/5"},
        )
        self.assertEqual(
            studio.m4b_volume_metadata(2, 3, include_track=False),
            {"disc": "2/3"},
        )

    def test_m4b_target_round_trips_optional_track_metadata_flag(self):
        target = studio.normalize_output_target(
            {"format": "m4b", "include_track_metadata": "false"}
        )
        self.assertFalse(target["include_track_metadata"])
        self.assertFalse(
            studio.OutputTarget(**target).to_dict()["include_track_metadata"]
        )

    def test_source_tree_recurses_naturally_and_ignores_non_text_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "Том 10").mkdir()
            (root / "Том 2").mkdir()
            (root / ".hidden").mkdir()
            (root / "Том 10" / "001.TXT").write_text("10", encoding="utf-8")
            (root / "Том 2" / "Глава 2.txt").write_text("2", encoding="utf-8")
            (root / "Том 2" / "Глава 10.txt").write_text("10", encoding="utf-8")
            (root / "Том 2" / "cover.jpg").write_bytes(b"not text")
            (root / ".hidden" / "secret.txt").write_text("secret", encoding="utf-8")

            tree = studio.build_source_tree(root)
            self.assertTrue(tree.is_group)
            self.assertEqual([child.name for child in tree.children], ["Том 2", "Том 10"])
            self.assertEqual(
                [leaf.relative_path for leaf in studio.flatten_source_files(tree)],
                ["Том 2/Глава 2.txt", "Том 2/Глава 10.txt", "Том 10/001.TXT"],
            )
            serialized = studio.source_tree_to_dict(tree)
            self.assertEqual(serialized["children"][0]["id"], "dir:Том 2")
            self.assertEqual(serialized["children"][0]["children"][0]["kind"], "text_file")

    def test_source_tree_serialization_does_not_alias_metadata(self):
        node = studio.SourceNode(
            id="dir:x",
            kind="directory",
            name="X",
            metadata_overrides={"artist": "Автор", "nested": {"x": 1}},
        )
        serialized = studio.source_tree_to_dict(node)
        serialized["metadata_overrides"]["nested"]["x"] = 99
        self.assertEqual(node.metadata_overrides["nested"]["x"], 1)

    def test_effective_source_tags_inherit_and_allow_explicit_clear(self):
        tags = studio.effective_source_tags(
            {"artist": "Автор", "album": "Книга", "genre": "Речь"},
            ancestors=({"album": "Том 1", "composer": "Редактор"},),
            file_overrides={"album": "", "genre": "", "unknown": "ignored"},
        )
        self.assertEqual(
            tags,
            {
                "artist": "Автор",
                "album": "",
                "genre": "",
                "composer": "Редактор",
            },
        )

    def test_source_output_paths_mirror_tree_and_detect_casefold_collisions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "output"
            (root / "Tom 1").mkdir(parents=True)
            first = root / "Tom 1" / "001.txt"
            first.write_text("one", encoding="utf-8")
            node = studio.build_source_tree(root).children[0].children[0]
            planned = studio.plan_source_output_paths(root, output, [node], extension="opus")
            self.assertEqual(planned[0][0], str(first.resolve()))
            self.assertEqual(planned[0][1], output / "Tom 1" / "001.opus")

            # Без этого выравнивание двух каталогов привело бы к перезаписи
            # в целевой файловой системе без учёта регистра.
            (root / "A").mkdir()
            (root / "B").mkdir()
            a = root / "A" / "same.txt"
            b = root / "B" / "same.txt"
            a.write_text("a", encoding="utf-8")
            b.write_text("b", encoding="utf-8")
            nodes = studio.build_source_tree(root)
            with self.assertRaisesRegex(ValueError, "совпадающие выходные имена"):
                studio.plan_source_output_paths(
                    root, output, studio.flatten_source_files(nodes), mirror_relative=False
                )

    def test_source_output_paths_reject_sources_outside_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            root.mkdir()
            outside = Path(tempdir) / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            foreign = studio.SourceNode(
                id="file:outside.txt", kind="text_file", name=outside.name, path=str(outside)
            )
            with self.assertRaisesRegex(ValueError, "вне корневой папки"):
                studio.plan_source_output_paths(root, Path(tempdir) / "out", [foreign])

    def test_source_synthesis_targets_default_to_legacy_config(self):
        config = studio.normalize_config({})
        self.assertEqual(config[studio.SYNTHESIS_TARGETS_CONFIG_KEY], [])
        self.assertEqual(config["synthesis_m4b_bitrate"], "64k")
        targets = studio.source_synthesis_targets_from_config(config)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["format"], "mp3")
        self.assertEqual(targets[0]["output_dir"], config["output_dir"])

    def test_source_m4b_bitrate_is_normalized_independently_from_mp3(self):
        config = studio.normalize_config(
            {"output_bitrate": "192k", "synthesis_m4b_bitrate": "48K"}
        )
        self.assertEqual(config["output_bitrate"], "192k")
        self.assertEqual(config["synthesis_m4b_bitrate"], "48k")
        self.assertEqual(
            studio.normalize_config(
                {"output_bitrate": "192k", "synthesis_m4b_bitrate": "bad"}
            )["synthesis_m4b_bitrate"],
            "64k",
        )

    def test_source_synthesis_targets_are_normalized_and_invalid_entries_dropped(self):
        with self.assertLogs(level=logging.WARNING):
            config = studio.normalize_config(
                {
                    "synthesis_targets": [
                        {"format": " OpUs ", "bitrate": "48K"},
                        {"format": "flac"},
                    ]
                }
            )
        self.assertEqual(
            [item["format"] for item in config["synthesis_targets"]],
            ["opus"],
        )
        self.assertEqual(config["synthesis_targets"][0]["bitrate"], "48k")
        self.assertEqual(
            config[studio.SOURCE_OUTPUT_TARGETS_CONFIG_KEY],
            config[studio.SYNTHESIS_TARGETS_CONFIG_KEY],
        )

    def test_source_synthesis_target_alias_is_migrated(self):
        config = studio.normalize_config(
            {
                studio.SOURCE_OUTPUT_TARGETS_CONFIG_KEY: [
                    {"format": "opus", "bitrate": "48k"}
                ]
            }
        )
        self.assertEqual(
            config[studio.SYNTHESIS_TARGETS_CONFIG_KEY][0]["format"], "opus"
        )
        self.assertEqual(
            studio.source_synthesis_targets_from_config(
                {studio.SOURCE_OUTPUT_TARGETS_CONFIG_KEY: config[studio.SOURCE_OUTPUT_TARGETS_CONFIG_KEY]}
            )[0]["format"],
            "opus",
        )

    def test_source_synthesis_planner_preserves_legacy_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            (root / "Tom 1").mkdir(parents=True)
            first = root / "Tom 1" / "001.txt"
            first.write_text("one", encoding="utf-8")
            node = studio.build_source_tree(root).children[0].children[0]
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [node],
                targets=[],
                mirror_relative=True,
                legacy_output_dir=output,
            )
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["kind"], "file")
            self.assertEqual(planned[0]["target"]["format"], "mp3")
            self.assertEqual(planned[0]["path"], output / "Tom 1" / "001.mp3")

            id_plan = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [node.id],
                targets=[],
                mirror_relative=True,
                legacy_output_dir=output,
                path_by_id={node.id: str(first)},
            )
            self.assertEqual(id_plan[0]["item_id"], node.id)

            opus_plan = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [node],
                targets=[],
                mirror_relative=True,
                legacy_output_dir=output,
                legacy_format="opus",
                legacy_bitrate="48k",
            )
            self.assertEqual(opus_plan[0]["target"]["format"], "opus")
            self.assertEqual(opus_plan[0]["target"]["bitrate"], "48k")
            self.assertEqual(opus_plan[0]["path"], output / "Tom 1" / "001.opus")

    def test_source_synthesis_planner_assigns_format_subdirectories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            first = root / "001.txt"
            second = root / "002.txt"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            targets = [
                studio.OutputTarget(format="mp3", bitrate="128k"),
                studio.OutputTarget(format="opus", bitrate="48k"),
            ]
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [first, second],
                targets=targets,
                legacy_output_dir=output,
            )
            self.assertEqual(len(planned), 4)
            self.assertEqual(
                sorted({Path(item["path"]).parent.name for item in planned}),
                ["mp3", "opus"],
            )
            self.assertEqual(
                [item["path"].suffix for item in planned],
                [".mp3", ".mp3", ".opus", ".opus"],
            )

    def test_source_synthesis_planner_emits_m4b_post_stage_groups(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {"id": "plan-1", "name": "Том 1", "file_ids": [str(files[0]), str(files[1])]},
                {"id": "plan-2", "name": "Том 2", "file_ids": [str(files[2])]},
            ]
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b",
                        output_dir=str(output),
                        filename_template="{book} {range}",
                    )
                ],
                source_m4b_groups=groups,
                book_name="Книга",
            )
            self.assertEqual(len(planned), 2)
            self.assertTrue(all(item["kind"] == "m4b" for item in planned))
            self.assertEqual(
                [item["file_ids"] for item in planned],
                [(str(files[0]), str(files[1])), (str(files[2]),)],
            )
            self.assertEqual(
                [item["path"].name for item in planned],
                ["Книга 1-2.m4b", "Книга 3-3.m4b"],
            )

            split_target = studio.OutputTarget(
                format="m4b", output_dir=str(output), max_chapters=1
            )
            split = studio.plan_source_synthesis_target_paths(
                root, output, files, targets=[split_target], source_m4b_groups=groups
            )
            self.assertEqual(len({item["item_id"] for item in split}), len(split))

    def test_source_synthesis_m4b_without_estimate_plan_is_one_book(self):
        """Без виртуального плана M4B все выбранные TXT образуют одну книгу."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[studio.OutputTarget(format="m4b")],
                # Пользователь ещё не подготовил план групп.
                source_m4b_groups=(),
                book_name="Книга",
            )

            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["kind"], "m4b")
            self.assertEqual(
                planned[0]["file_ids"], tuple(str(path) for path in files)
            )
            self.assertEqual(planned[0]["path"].name, "Книга Главы 1-3.m4b")

    def test_source_synthesis_m4b_custom_template_can_omit_range(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            first = root / "001.txt"
            first.write_text("one", encoding="utf-8")
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [first],
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=(),
                book_name="Книга",
                m4b_template="{book}",
            )
            self.assertEqual(planned[0]["path"].name, "Книга.m4b")

    def test_source_m4b_template_extension_is_not_duplicated_in_tree_or_path(self):
        """Шаблон с ``.m4b`` не превращается в ``.m4b.m4b``."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            first = root / "001.txt"
            first.write_text("one", encoding="utf-8")

            # Чистый планировщик получает ту же готовую метку группы, которую
            # интерфейс сохраняет после подготовки плана групп из TXT.
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [first],
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=[
                    {
                        "id": "group-1",
                        "name": "Книга.m4b",
                        "name_template": "{book}.m4b",
                        "file_ids": [str(first)],
                    }
                ],
                book_name="Книга",
                m4b_template="{book}.m4b",
            )
            self.assertEqual([item["path"].name for item in planned], ["Книга.m4b"])

            # Рендерер интерфейса должен нормализовать запись тем же способом перед
            # добавлением метки в виртуальное дерево. Проверяем помощник
            # напрямую, чтобы покрыть контракт без Tk.
            self.assertEqual(
                studio._template_basename("Книга.m4b", "m4b"),
                "Книга",
            )

    def test_source_synthesis_m4b_name_alias_is_available_in_plan_context(self):
        """Шаблоны M4B источников принимают общий короткий псевдоним имени."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            chapter = root / "001.txt"
            chapter.write_text("one", encoding="utf-8")
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                [chapter],
                # Явный шаблон цели проверяет алиас напрямую; пустой шаблон
                # намеренно использует метку виртуальной группы как запасное
                # значение и не стал бы обрабатывать ``{name}``.
                targets=[
                    studio.OutputTarget(
                        format="m4b", filename_template="{name}"
                    )
                ],
                source_m4b_groups=[
                    {
                        "id": "group-1",
                        "name": "Том 1",
                        "file_ids": [str(chapter)],
                    }
                ],
                book_name="Книга",
                m4b_template="{name}",
            )
            self.assertEqual([item["path"].name for item in planned], ["Том 1.m4b"])

    def test_source_m4b_name_template_preserves_distinct_virtual_names(self):
        """Уникальные имена групп уже различают M4B без лишнего суффикса."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {
                    "id": f"group-{number}",
                    "name": f"Том {number:02d}",
                    "file_ids": (str(path),),
                }
                for number, path in enumerate(files, 1)
            ]

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b", filename_template="{name}"
                    )
                ],
                source_m4b_groups=groups,
                book_name="Книга",
            )

            self.assertEqual(
                [item["path"].name for item in planned],
                ["Том 01.m4b", "Том 02.m4b", "Том 03.m4b"],
            )

    def test_source_m4b_name_template_disambiguates_repeated_virtual_names(self):
        """Повторы в ``{name}`` получают стабильный глобальный номер части."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {
                    "id": f"group-{number}",
                    "name": "Том",
                    "file_ids": (str(path),),
                }
                for number, path in enumerate(files, 1)
            ]

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b", filename_template="{name}"
                    )
                ],
                source_m4b_groups=groups,
                book_name="Книга",
            )

            self.assertEqual(
                [item["path"].name for item in planned],
                [
                    "Том Часть 1.m4b",
                    "Том Часть 2.m4b",
                    "Том Часть 3.m4b",
                ],
            )

    def test_source_m4b_name_template_compares_names_without_output_extension(self):
        """Расширение в имени группы не должно обходить проверку коллизий."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {
                    "id": "group-1",
                    "name": "Том",
                    "file_ids": (str(files[0]),),
                },
                {
                    "id": "group-2",
                    "name": "Том.m4b",
                    "file_ids": (str(files[1]),),
                },
            ]

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b", filename_template="{name}"
                    )
                ],
                source_m4b_groups=groups,
                book_name="Книга",
            )

            self.assertEqual(
                [item["path"].name for item in planned],
                ["Том Часть 1.m4b", "Том Часть 2.m4b"],
            )

    def test_source_m4b_part_suffix_survives_filename_length_limit(self):
        """Ограничение длины сохраняет различающий номер части."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            long_name = "Очень длинный том " + ("а" * 220)
            groups = [
                {
                    "id": f"group-{number}",
                    "name": long_name,
                    "file_ids": (str(path),),
                }
                for number, path in enumerate(files, 1)
            ]

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b", filename_template="{name}"
                    )
                ],
                source_m4b_groups=groups,
                book_name="Книга",
            )
            names = [item["path"].name for item in planned]

            self.assertEqual(len(set(names)), 2)
            self.assertTrue(names[0].endswith("Часть 1.m4b"))
            self.assertTrue(names[1].endswith("Часть 2.m4b"))
            self.assertTrue(all(len(Path(name).stem) <= 180 for name in names))

    def test_source_synthesis_m4b_short_template_aliases_are_rendered(self):
        """Все короткие поля, разрешённые интерфейсом, есть и в контексте фонового обработчика."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(f"chapter {number}", encoding="utf-8")
                files.append(path)
            for template, expected in (
                ("{num}", "1.m4b"),
                ("{first}-{last}", "1-2.m4b"),
            ):
                with self.subTest(template=template):
                    planned = studio.plan_source_synthesis_target_paths(
                        root,
                        output,
                        files,
                        targets=[studio.OutputTarget(format="m4b")],
                        book_name="Книга",
                        m4b_template=template,
                    )
                    self.assertEqual([item["path"].name for item in planned], [expected])

    def test_source_synthesis_m4b_volume_template_uses_custom_start(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {"id": "one", "file_ids": (str(files[0]),)},
                {"id": "two", "file_ids": (str(files[1]),)},
            ]
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=groups,
                book_name="Книга",
                m4b_template="{book} Том {volume:10}",
            )
            self.assertEqual(
                [item["path"].name for item in planned],
                ["Книга Том 10.m4b", "Книга Том 11.m4b"],
            )

    def test_source_m4b_static_template_gets_part_suffix_when_split(self):
        """Шаблон ``{book}`` безопасен и при ручном делении на части."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[
                    studio.OutputTarget(
                        format="m4b",
                        output_dir=str(output),
                        filename_template="{book}",
                        max_chapters=1,
                    )
                ],
                book_name="Книга",
            )
            self.assertEqual(
                [item["path"].name for item in planned],
                [
                    "Книга Часть 1.m4b",
                    "Книга Часть 2.m4b",
                    "Книга Часть 3.m4b",
                ],
            )

    def test_source_m4b_static_template_disambiguates_repeated_virtual_names(self):
        """Повторяющиеся имена виртуальных групп не должны перезаписывать M4B."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)
            groups = [
                {
                    "id": f"group-{index}",
                    "name": "Книга",
                    "file_ids": (str(path),),
                }
                for index, path in enumerate(files, 1)
            ]
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=groups,
                book_name="Книга",
                m4b_template="{book}",
            )
            self.assertEqual(
                [item["path"].name for item in planned],
                [
                    "Книга Часть 1.m4b",
                    "Книга Часть 2.m4b",
                    "Книга Часть 3.m4b",
                ],
            )

    def test_source_m4b_reflow_uses_cache_durations_and_sets_volume_numbers(self):
        """Финальный этап M4B делит целые TXT по измеренной длительности кэша."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            source_paths = []
            audio_paths = {}
            for number in range(1, 4):
                source = root / f"{number:03d}.txt"
                source.write_text(f"chapter {number}", encoding="utf-8")
                source_paths.append(source)
                audio = root / f"{number:03d}.ogg"
                audio.write_bytes(b"cached")
                audio_paths[str(source)] = (audio,)
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(output),
                filename_template="",
                max_duration_seconds=0,
            ).to_dict()
            record = {
                "target_index": 0,
                "target": target,
                "item_id": "book",
                "path": output / "Book.m4b",
                "kind": "m4b",
                "group_index": 1,
                "parts": 1,
                "group_name": "Book",
                "book": "Book",
                "file_ids": tuple(str(path) for path in source_paths),
                "source_paths": tuple(source_paths),
                "chapter_count": 3,
                "metadata_overrides": {
                    "album": "Тестовая книга",
                    "artist": "Автор",
                },
            }
            durations = iter((12 * 3600, 11 * 3600, 12 * 3600))
            with mock.patch.object(
                studio, "_probe_audio_duration", side_effect=lambda _path: next(durations)
            ):
                snapshot = studio.reflow_source_m4b_target_records(
                    (record,),
                    audio_paths,
                    {
                        "source_m4b_auto_split_long": True,
                        "source_m4b_template": "{book}",
                    },
                    source_root=root,
                    output_root=output,
                    source_path_by_id={
                        str(path): path for path in source_paths
                    },
                )
            rebuilt = tuple(snapshot["records"])
            self.assertTrue(snapshot["changed"])
            self.assertEqual(len(rebuilt), 2)
            self.assertEqual(
                [tuple(item["file_ids"]) for item in rebuilt],
                [
                    (str(source_paths[0]), str(source_paths[1])),
                    (str(source_paths[2]),),
                ],
            )
            self.assertEqual(
                [(item["group_index"], item["parts"]) for item in rebuilt],
                [(1, 2), (2, 2)],
            )
            self.assertEqual(
                [item["metadata_overrides"] for item in rebuilt],
                [
                    {"album": "Тестовая книга", "artist": "Автор"},
                    {"album": "Тестовая книга", "artist": "Автор"},
                ],
            )
            self.assertEqual(
                [item["path"].name for item in rebuilt],
                ["Book Часть 1.m4b", "Book Часть 2.m4b"],
            )

    def test_source_m4b_reflow_rejects_internal_duplicates_without_leaking_paths(self):
        """Отклонённая цель не должна резервировать возможные пути."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            target = studio.OutputTarget(format="m4b").to_dict()

            def old_record(target_index, prefix):
                paths = tuple(root / f"{prefix}-{number}.txt" for number in (1, 2))
                return {
                    "target_index": target_index,
                    "target": target,
                    "path": output / f"old-{prefix}.m4b",
                    "kind": "m4b",
                    "file_ids": tuple(str(path) for path in paths),
                    "source_paths": paths,
                }

            records = (old_record(0, "first"), old_record(1, "second"))
            durations = {
                str(path): 40.0
                for record in records
                for path in record["source_paths"]
            }

            def fake_plan(_root, _output, source_nodes, **kwargs):
                prefix = Path(source_nodes[0]["path"]).stem.split("-", 1)[0]
                groups = kwargs["source_m4b_groups"]
                if prefix == "first":
                    names = ("Candidate.m4b", "candidate.m4b")
                else:
                    names = ("candidate.m4b", "accepted.m4b")
                return tuple(
                    {
                        "kind": "m4b",
                        "path": output / name,
                        "file_ids": tuple(group["file_ids"]),
                    }
                    for name, group in zip(names, groups)
                )

            with mock.patch.object(
                studio,
                "_measure_source_m4b_chapter_durations",
                return_value=durations,
            ), mock.patch.object(
                studio,
                "plan_source_synthesis_target_paths",
                side_effect=fake_plan,
            ):
                snapshot = studio.reflow_source_m4b_target_records(
                    records,
                    {},
                    {
                        "source_m4b_reflow_actual_duration": True,
                        "source_m4b_reflow_limit_seconds": 60,
                    },
                    source_root=root,
                    output_root=output,
                    auto_split_long=False,
                )

            self.assertTrue(snapshot["changed"])
            self.assertEqual(
                [Path(record["path"]).name for record in snapshot["records"]],
                ["old-first.m4b", "candidate.m4b", "accepted.m4b"],
            )

    def test_source_m4b_reflow_rollback_reserves_original_paths(self):
        """Следующая цель не занимает исходный путь отменённой цели."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            target = studio.OutputTarget(format="m4b").to_dict()

            def old_record(target_index, prefix, chapter_count=2, path=None):
                paths = tuple(
                    root / f"{prefix}-{number}.txt"
                    for number in range(1, chapter_count + 1)
                )
                return {
                    "target_index": target_index,
                    "target": target,
                    "path": path or output / f"old-{prefix}.m4b",
                    "kind": "m4b",
                    "file_ids": tuple(str(item) for item in paths),
                    "source_paths": paths,
                }

            records = (
                old_record(0, "first"),
                old_record(1, "second"),
                # Одна глава сохраняет прежнюю границу, поэтому этот путь
                # входит в исходный занятый набор и отклоняет нулевую цель.
                old_record(2, "blocker", 1, output / "blocker.m4b"),
            )
            durations = {
                str(path): 40.0
                for record in records
                for path in record["source_paths"]
            }

            def fake_plan(_root, _output, source_nodes, **kwargs):
                prefix = Path(source_nodes[0]["path"]).stem.split("-", 1)[0]
                groups = kwargs["source_m4b_groups"]
                names = (
                    ("unused-first.m4b", "blocker.m4b")
                    if prefix == "first"
                    else ("old-first.m4b", "unused-second.m4b")
                )
                return tuple(
                    {
                        "kind": "m4b",
                        "path": output / name,
                        "file_ids": tuple(group["file_ids"]),
                    }
                    for name, group in zip(names, groups)
                )

            with mock.patch.object(
                studio,
                "_measure_source_m4b_chapter_durations",
                return_value=durations,
            ), mock.patch.object(
                studio,
                "plan_source_synthesis_target_paths",
                side_effect=fake_plan,
            ):
                snapshot = studio.reflow_source_m4b_target_records(
                    records,
                    {},
                    {
                        "source_m4b_reflow_actual_duration": True,
                        "source_m4b_reflow_limit_seconds": 60,
                    },
                    source_root=root,
                    output_root=output,
                    auto_split_long=False,
                )

            self.assertFalse(snapshot["changed"])
            self.assertEqual(
                [Path(record["path"]).name for record in snapshot["records"]],
                ["old-first.m4b", "old-second.m4b", "blocker.m4b"],
            )

    def test_source_m4b_reflow_reserves_future_target_paths_until_commit(self):
        """Предыдущая цель не может занять путь, освобождённый откатившейся следующей целью."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            target = studio.OutputTarget(format="m4b").to_dict()

            def old_record(target_index, prefix, chapter_count=2, path=None):
                paths = tuple(
                    root / f"{prefix}-{number}.txt"
                    for number in range(1, chapter_count + 1)
                )
                return {
                    "target_index": target_index,
                    "target": target,
                    "path": path or output / f"old-{prefix}.m4b",
                    "kind": "m4b",
                    "file_ids": tuple(str(item) for item in paths),
                    "source_paths": paths,
                }

            records = (
                old_record(0, "first"),
                old_record(1, "second"),
                old_record(2, "blocker", 1, output / "blocker.m4b"),
            )
            durations = {
                str(path): 40.0
                for record in records
                for path in record["source_paths"]
            }

            def fake_plan(_root, _output, source_nodes, **kwargs):
                prefix = Path(source_nodes[0]["path"]).stem.split("-", 1)[0]
                groups = kwargs["source_m4b_groups"]
                names = (
                    ("old-second.m4b", "first-new.m4b")
                    if prefix == "first"
                    else ("second-new.m4b", "blocker.m4b")
                )
                return tuple(
                    {
                        "kind": "m4b",
                        "path": output / name,
                        "file_ids": tuple(group["file_ids"]),
                    }
                    for name, group in zip(names, groups)
                )

            with mock.patch.object(
                studio,
                "_measure_source_m4b_chapter_durations",
                return_value=durations,
            ), mock.patch.object(
                studio,
                "plan_source_synthesis_target_paths",
                side_effect=fake_plan,
            ):
                snapshot = studio.reflow_source_m4b_target_records(
                    records,
                    {},
                    {
                        "source_m4b_reflow_actual_duration": True,
                        "source_m4b_reflow_limit_seconds": 60,
                    },
                    source_root=root,
                    output_root=output,
                    auto_split_long=False,
                )

            paths = [Path(record["path"]).name for record in snapshot["records"]]
            self.assertFalse(snapshot["changed"])
            self.assertEqual(
                paths,
                ["old-first.m4b", "old-second.m4b", "blocker.m4b"],
            )
            self.assertEqual(len(paths), len(set(paths)))

    def test_source_m4b_template_runtime_alias_prefers_explicit_alias_and_falls_back_to_canonical(self):
        """Снимок запуска не должен возвращать исторический диапазон из-за пропущенного поля."""
        # ``source_m4b_template`` — имя поля в снимке запуска. Оно имеет
        # приоритет даже при явной пустой строке: пустота означает
        # сохранённый компактный вариант ``{book}``, а не отсутствие поля.
        self.assertEqual(
            studio._source_m4b_template_from_config(
                {
                    "source_m4b_template": "",
                    "synthesis_m4b_template": "{book} {range}",
                }
            ),
            "{book}",
        )
        # Прямой или старый вызывающий код может передать только основной ключ
        # настроек. В этом случае помощник должен сохранить пользовательское
        # или пустое значение: вызов ``normalize_source_m4b_template(None)``
        # вернул бы ``{book} Главы {range}``.
        self.assertEqual(
            studio._source_m4b_template_from_config(
                {"synthesis_m4b_template": "{book}"}
            ),
            "{book}",
        )
        self.assertEqual(
            studio._source_m4b_template_from_config(
                {"synthesis_m4b_template": ""}
            ),
            "{book}",
        )
        self.assertEqual(
            studio._source_m4b_template_from_config({}),
            studio.DEFAULT_SOURCE_M4B_TEMPLATE,
        )
        self.assertEqual(
            studio._source_m4b_template_from_config(
                {
                    "source_m4b_template": None,
                    "synthesis_m4b_template": "{book}",
                }
            ),
            "{book}",
        )

        # Вспомогательная функция интерфейса следует тому же правилу, даже если у облегчённого или
        # старого экземпляра ещё нет атрибута сессии.
        app = object.__new__(studio.TTSApp)
        app.config = {"synthesis_m4b_template": "{book}"}
        self.assertEqual(app._current_source_m4b_template(), "{book}")

    def test_source_m4b_reflow_uses_canonical_template_without_runtime_alias(self):
        """Переразбиение по длительности сохраняет канонический шаблон без диапазона."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            source_paths = []
            audio_paths = {}
            records = []
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(output),
                filename_template="",
                max_duration_seconds=0,
            ).to_dict()
            for number in (1, 2):
                source = root / f"{number:03d}.txt"
                source.write_text(f"chapter {number}", encoding="utf-8")
                source_paths.append(source)
                audio = root / f"{number:03d}.ogg"
                audio.write_bytes(b"cached")
                audio_paths[str(source)] = (audio,)
                records.append(
                    {
                        "target_index": 0,
                        "target": target,
                        "item_id": f"book:{number}",
                        "path": output / f"old-{number}.m4b",
                        "kind": "m4b",
                        "group_index": number,
                        "parts": 2,
                        "group_name": f"source Часть {number}",
                        "book": "source",
                        "file_ids": (str(source),),
                        "source_paths": (source,),
                        "chapter_count": 2,
                        "source_template_owned": True,
                    }
                )

            config = {
                # Намеренно не передаём временный псевдоним фонового обработчика. Это форма,
                # которую создаёт прямой или старый вызывающий код с основными
                # настройками, но ещё без снимка запуска.
                "synthesis_m4b_template": "{book}",
                "source_m4b_reflow_actual_duration": True,
                "source_m4b_reflow_limit_seconds": 120,
            }
            durations = iter((50.0, 50.0))
            with mock.patch.object(
                studio,
                "_probe_audio_duration",
                side_effect=lambda _path: next(durations),
            ):
                snapshot = studio.reflow_source_m4b_target_records(
                    tuple(records),
                    audio_paths,
                    config,
                    source_root=root,
                    output_root=output,
                    source_path_by_id={
                        str(path): path for path in source_paths
                    },
                    auto_split_long=False,
                )

            self.assertTrue(snapshot["changed"])
            self.assertEqual(
                [item["path"].name for item in snapshot["records"]],
                ["source.m4b"],
            )
            self.assertTrue(
                all("Главы" not in item["path"].name for item in snapshot["records"])
            )

    def test_process_queue_fallback_uses_canonical_source_template(self):
        """Резервный планировщик фонового обработчика должен использовать ``synthesis_m4b_template``."""
        app = object.__new__(studio.TTSApp)
        app._post_to_ui = lambda *_args, **_kwargs: None

        class Processor:
            is_stopped = False
            active_threads = []

            def defer_cache_eviction(self):
                pass

            def resume_cache_eviction(self):
                pass

            def flush_cache(self):
                pass

            def _save_processing_statuses(self):
                pass

        captured = {}

        def capture_and_abort(*_args, **kwargs):
            captured["template"] = kwargs.get("m4b_template")
            raise RuntimeError("stop after planner capture")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            chapter = root / "001.txt"
            chapter.write_text("chapter", encoding="utf-8")
            processing_config = {
                "input_dir": str(root),
                "output_dir": str(output),
                "output_format": "m4b",
                "synthesis_targets": [
                    {"format": "m4b", "output_dir": str(output)}
                ],
                "synthesis_m4b_template": "{book}",
                "source_path_by_id": {"001.txt": chapter},
                "include_subdirs": False,
            }
            with mock.patch.object(
                studio,
                "plan_source_synthesis_target_paths",
                side_effect=capture_and_abort,
            ):
                studio.TTSApp.process_queue(
                    app,
                    Processor(),
                    ("001.txt",),
                    processing_config,
                    False,
                )

        self.assertEqual(captured.get("template"), "{book}")

    def test_source_m4b_auto_safety_preserves_manual_volume_boundaries(self):
        """Защита свыше 24 часов делит том, но не объединяет созданные вручную тома."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            output.mkdir()
            source_paths = []
            audio_paths = {}
            records = []
            target = studio.OutputTarget(
                format="m4b",
                output_dir=str(output),
                filename_template="",
                max_duration_seconds=0,
            ).to_dict()
            for number in range(1, 4):
                source = root / f"{number:03d}.txt"
                source.write_text(f"chapter {number}", encoding="utf-8")
                source_paths.append(source)
                audio = root / f"{number:03d}.ogg"
                audio.write_bytes(b"cached")
                file_id = str(source)
                audio_paths[file_id] = (audio,)
                records.append(
                    {
                        "target_index": 0,
                        "target": target,
                        "item_id": f"book:{number}",
                        "path": output / f"Book Часть {number}.m4b",
                        "kind": "m4b",
                        "group_index": number,
                        "parts": 3,
                        "group_name": f"Book Часть {number}",
                        "book": "Book",
                        # Каждая запись — отдельный том, созданный вручную. Защитный проход
                        # не должен объединять такие десятичасовые тома в 20 ч + 10 ч.
                        "file_ids": (file_id,),
                        "source_paths": (source,),
                        "chapter_count": 3,
                    }
                )

            def run(config):
                durations = iter((10 * 3600, 10 * 3600, 10 * 3600))
                with mock.patch.object(
                    studio,
                    "_probe_audio_duration",
                    side_effect=lambda _path: next(durations),
                ):
                    return studio.reflow_source_m4b_target_records(
                        tuple(records),
                        audio_paths,
                        config,
                        source_root=root,
                        output_root=output,
                        source_path_by_id={
                            str(path): path for path in source_paths
                        },
                    )

            automatic = run(
                {
                    "source_m4b_auto_split_long": True,
                    "source_m4b_template": "{book}",
                }
            )
            self.assertFalse(automatic["changed"])
            self.assertEqual(
                [tuple(item["file_ids"]) for item in automatic["records"]],
                [(str(path),) for path in source_paths],
            )
            self.assertEqual(
                [(item["group_index"], item["parts"]) for item in automatic["records"]],
                [(1, 3), (2, 3), (3, 3)],
            )

            # Явный флажок включает общее переразбиение. При подходящем лимите он
            # может объединить соседние тома, созданные вручную; это подтверждает,
            # что две политики не смешиваются.
            explicit = run(
                {
                    "source_m4b_auto_split_long": True,
                    "source_m4b_reflow_actual_duration": True,
                    "source_m4b_reflow_limit_seconds": 23 * 3600 + 50 * 60,
                    "source_m4b_template": "{book}",
                }
            )
            self.assertTrue(explicit["changed"])
            self.assertEqual(
                [tuple(item["file_ids"]) for item in explicit["records"]],
                [
                    (str(source_paths[0]), str(source_paths[1])),
                    (str(source_paths[2]),),
                ],
            )
            self.assertEqual(
                [(item["group_index"], item["parts"]) for item in explicit["records"]],
                [(1, 2), (2, 2)],
            )

    def test_source_m4b_album_template_is_rendered_for_every_part(self):
        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = root / "chapter.ogg"
            fragment.write_bytes(b"canonical audio")
            output = root / "book.m4b"
            record = {
                "target": studio.normalize_output_target(
                    {"format": "m4b", "bitrate": "64k"}
                ),
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_index": 1,
                "parts": 2,
                "chapter_start": 1,
                "chapter_end": 1,
                "chapter_count": 2,
                "book": "Моя книга",
            }
            captured = {}

            def fake_export(_audio_files, output_path, **kwargs):
                captured.update(kwargs)
                Path(output_path).write_bytes(b"m4b")

            config = {
                "input_dir": str(root),
                "synthesis_m4b_album_template": "Серия: {book}",
                "source_m4b_album_template": "Серия: {book}",
                "apply_output_tags": True,
                "tag_album": "старый альбом",
                "synthesis_m4b_bitrate": "64k",
            }
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_export
            ):
                result = studio.run_source_synthesis_m4b_target(
                    Processor(), record, {"chapter-id": (fragment,)}, config
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(captured["tags"]["album"], "Серия: Моя книга")
            self.assertEqual(captured["disk_number"], 1)
            self.assertEqual(captured["disk_total"], 2)
            self.assertEqual(captured["track_number"], 1)
            self.assertEqual(captured["track_total"], 2)

    def test_source_m4b_default_album_template_keeps_settings_album(self):
        """Стандартный ``{book}`` не заменяет тег альбома именем папки."""

        class Processor:
            is_stopped = False
            encode_semaphore = None
            processing_statuses_ram = {}

            def _mark_output_status(self, path, status):
                self.processing_statuses_ram[str(Path(path).resolve())] = status

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "chapter.txt"
            source.write_text("Текст", encoding="utf-8")
            fragment = root / "chapter.ogg"
            fragment.write_bytes(b"canonical audio")
            output = root / "book.m4b"
            record = {
                "target": studio.normalize_output_target(
                    {"format": "m4b", "bitrate": "64k"}
                ),
                "path": output,
                "file_ids": ("chapter-id",),
                "source_paths": (source,),
                "group_index": 1,
                "parts": 1,
                "book": root.name,
            }
            captured = {}

            def fake_export(_audio_files, output_path, **kwargs):
                captured.update(kwargs)
                Path(output_path).write_bytes(b"m4b")

            config = {
                "input_dir": str(root),
                "source_m4b_album_template": studio.DEFAULT_SOURCE_M4B_ALBUM_TEMPLATE,
                "apply_output_tags": True,
                "tag_album": "Альбом из настроек",
                "synthesis_m4b_bitrate": "64k",
            }
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg", side_effect=fake_export
            ):
                result = studio.run_source_synthesis_m4b_target(
                    Processor(), record, {"chapter-id": (fragment,)}, config
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(captured["tags"]["album"], "Альбом из настроек")

    def test_source_synthesis_m4b_uses_estimated_group_name_without_target_template(self):
        """Имя части из оценочного дерева не расходится с именем файла."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=[
                    {
                        "id": "estimate-1",
                        "name": "Книга Главы 01-02",
                        "file_ids": [str(path) for path in files],
                    }
                ],
                book_name="Книга",
            )

            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["path"].name, "Книга Главы 01-02.m4b")

            split_target = studio.OutputTarget(
                format="m4b", output_dir=str(output), max_chapters=1
            )
            split = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[split_target],
                source_m4b_groups=[
                    {
                        "id": "estimate-1",
                        "name": "Книга Главы 01-02",
                        "file_ids": [str(path) for path in files],
                    }
                ],
                book_name="Книга",
            )
            self.assertEqual(
                [item["path"].name for item in split],
                ["Книга Главы 1-1.m4b", "Книга Главы 2-2.m4b"],
            )

    def test_source_synthesis_m4b_drops_stale_generated_name_after_template_change(self):
        """Изменение шаблона не возвращает старый диапазон в имя M4B."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)

            # Эти метки созданы историческим шаблоном по умолчанию и содержат
            # сведения об их происхождении. Поэтому текущий компактный шаблон
            # ``{book}`` должен построить новые имена, а не считать старый
            # текст ``Главы 01-01`` пользовательской меткой группы.
            groups = [
                {
                    "id": f"estimate-{number}",
                    "name": f"Книга Главы {number:02d}-{number:02d}",
                    "name_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE,
                    "file_ids": [str(files[number - 1])],
                }
                for number in range(1, 3)
            ]
            target = studio.OutputTarget(
                format="m4b", output_dir=str(output)
            )

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[target],
                source_m4b_groups=groups,
                book_name="Книга",
                m4b_template="{book}",
            )

            self.assertEqual(
                [item["path"].name for item in planned],
                ["Книга Часть 1.m4b", "Книга Часть 2.m4b"],
            )
            self.assertTrue(
                all("Главы" not in item["path"].name for item in planned)
            )

            # Очистка поля приводится к тому же явному варианту без диапазона.
            # План из одного тома также не должен возвращать диапазон.
            compact = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[target],
                source_m4b_groups=[
                    {
                        "id": "estimate-all",
                        "name": "Книга Главы 01-02",
                        "name_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE,
                        "file_ids": [str(path) for path in files],
                    }
                ],
                book_name="Книга",
                m4b_template="",
            )
            self.assertEqual([item["path"].name for item in compact], ["Книга.m4b"])

    def test_source_synthesis_m4b_current_template_overrides_stale_target_snapshot(self):
        """Старый снимок цели не должен возвращать диапазон после смены шаблона."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 3):
                path = root / f"{number:03d}.txt"
                path.write_text(f"chapter {number}", encoding="utf-8")
                files.append(path)

            # Это снимок старого окна целей источника, созданный до того, как
            # пользователь сменил поле шаблона плана на ``{book}``.
            stale_target = studio.OutputTarget(
                format="m4b",
                output_dir=str(output),
                filename_template=studio.DEFAULT_SOURCE_M4B_TEMPLATE,
            )
            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[stale_target],
                source_m4b_groups=(),
                book_name="Книга",
                m4b_template="{book}",
            )

            self.assertEqual([item["path"].name for item in planned], ["Книга.m4b"])
            self.assertNotIn("Главы", planned[0]["path"].name)

            # Подготовленный многотомный план получает такое же временное
            # переопределение рендера и сохраняет детерминированные суффиксы
            # частей для статического шаблона.
            groups = [
                {
                    "id": f"group-{number}",
                    "name": f"Книга Главы {number:02d}-{number:02d}",
                    "name_template": studio.DEFAULT_SOURCE_M4B_TEMPLATE,
                    "file_ids": [str(files[number - 1])],
                }
                for number in range(1, 3)
            ]
            split = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files,
                targets=[stale_target],
                source_m4b_groups=groups,
                book_name="Книга",
                m4b_template="{book}",
            )
            self.assertEqual(
                [item["path"].name for item in split],
                ["Книга Часть 1.m4b", "Книга Часть 2.m4b"],
            )
            self.assertTrue(all(item.get("source_template_owned") for item in split))

            # Короткие алиасы также являются полями шаблона источника. Если
            # виртуальные метки унаследованы от старого шаблона, их нужно
            # начинать с текущего имени книги; иначе новый шаблон ``{name}``
            # случайно воспроизведёт устаревший диапазон.
            for template, expected in (
                ("{name}", ["Книга Часть 1.m4b", "Книга Часть 2.m4b"]),
                ("{group} {range}", ["Книга 1-1.m4b", "Книга 2-2.m4b"]),
            ):
                with self.subTest(template=template):
                    alias_plan = studio.plan_source_synthesis_target_paths(
                        root,
                        output,
                        files,
                        targets=[stale_target],
                        source_m4b_groups=groups,
                        book_name="Книга",
                        m4b_template=template,
                    )
                    self.assertEqual(
                        [item["path"].name for item in alias_plan],
                        expected,
                    )

    def test_source_synthesis_m4b_selected_subset_does_not_keep_stale_group_name(self):
        """Выбор части подготовленной группы получает новый диапазон имени."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            output = Path(tempdir) / "audio"
            root.mkdir()
            files = []
            for number in range(1, 4):
                path = root / f"{number:03d}.txt"
                path.write_text(str(number), encoding="utf-8")
                files.append(path)

            planned = studio.plan_source_synthesis_target_paths(
                root,
                output,
                files[1:],
                targets=[studio.OutputTarget(format="m4b")],
                source_m4b_groups=[
                    {
                        "id": "estimate-1",
                        "name": "Книга Главы 01-03",
                        "file_ids": [str(path) for path in files],
                    }
                ],
                book_name="Книга",
            )

            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["path"].name, "Книга Главы 01-02.m4b")
            self.assertEqual(
                planned[0]["file_ids"], tuple(str(path) for path in files[1:])
            )

    def test_source_synthesis_planner_rejects_output_collisions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            root.mkdir()
            first = root / "001.txt"
            first.write_text("one", encoding="utf-8")
            targets = [
                studio.OutputTarget(format="mp3", output_dir=str(Path(tempdir) / "same")),
                studio.OutputTarget(format="mp3", output_dir=str(Path(tempdir) / "same")),
            ]
            with self.assertRaisesRegex(ValueError, "совпадающие выходные имена"):
                studio.plan_source_synthesis_target_paths(
                    root, Path(tempdir) / "out", [first], targets=targets
                )

    def test_output_target_planner_keeps_legacy_single_directory(self):
        root = Path(tempfile.gettempdir()).resolve()
        target = studio.OutputTarget(format="mp3", enabled=True)
        planned = studio.plan_output_targets(
            [target], root / "base", legacy_single_dir=root / "legacy"
        )
        self.assertEqual(len(planned), 1)
        self.assertEqual(Path(planned[0]["output_dir"]), root / "legacy")
        self.assertEqual(planned[0]["format"], "mp3")

    def test_output_target_planner_assigns_separate_dirs_and_skips_disabled(self):
        base = Path(tempfile.gettempdir()).resolve() / "base"
        planned = studio.plan_output_targets(
            [
                studio.OutputTarget(format="mp3"),
                studio.OutputTarget(format="opus"),
                studio.OutputTarget(format="m4b", enabled=False),
            ],
            base,
        )
        self.assertEqual(len(planned), 2)
        self.assertEqual([Path(item["output_dir"]).parent for item in planned], [base, base])
        self.assertEqual([Path(item["output_dir"]).name for item in planned], ["mp3", "opus"])

    def test_output_target_planner_disambiguates_duplicate_default_formats(self):
        planned = studio.plan_output_targets(
            [studio.OutputTarget(format="mp3"), studio.OutputTarget(format="mp3")],
            "/base",
        )
        self.assertEqual(
            [Path(item["output_dir"]).name for item in planned], ["mp3", "mp3_2"]
        )

    def test_output_target_planner_is_idempotent_for_relative_directories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir) / "results"
            first = studio.plan_output_targets(
                [studio.OutputTarget(format="m4b", output_dir="m4b")],
                base,
            )
            second = studio.plan_output_targets(first, base)
        self.assertEqual(second[0]["output_dir"], first[0]["output_dir"])
        self.assertEqual(
            Path(first[0]["output_dir"]),
            (base / "m4b").resolve(strict=False),
        )

    def test_output_target_library_roundtrip_keeps_duplicate_formats(self):
        """Две цели MP3 с разными профилями/каталогами не схлопываются."""
        targets = [
            studio.OutputTarget(
                format="mp3", output_dir="/tmp/mp3-128", bitrate="128k"
            ),
            studio.OutputTarget(
                format="mp3", output_dir="/tmp/mp3-320", bitrate="320k"
            ),
        ]

        bundle = studio.output_target_library_from_targets(targets)
        restored = studio.normalize_output_target_library(bundle)["targets"]

        self.assertEqual([item["format"] for item in restored], ["mp3", "mp3"])
        self.assertEqual(
            [item["bitrate"] for item in restored], ["128k", "320k"]
        )
        self.assertEqual(
            [item["output_dir"] for item in restored],
            ["/tmp/mp3-128", "/tmp/mp3-320"],
        )

    def test_duplicate_output_target_formats_are_reported_without_losing_order(self):
        duplicates = studio.duplicate_output_target_formats(
            [
                {"format": "Opus", "enabled": True},
                studio.OutputTarget(format="mp3"),
                {"format": "opus", "enabled": False},
                {"format": "MP3"},
                {"format": "wav"},
            ]
        )
        self.assertEqual(duplicates, ("opus", "mp3"))

    def test_output_target_editor_keeps_disabled_rows_and_supports_duplicate_rows(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        dialog_start = source.index("    def open_output_targets_dialog(self):")
        dialog_end = source.index("    def _choose_output_target_dir", dialog_start)
        dialog = source[dialog_start:dialog_end]
        self.assertIn("existing_items =", dialog)
        self.assertIn("def add_target_row(", dialog)
        self.assertIn("＋ Добавить цель", dialog)
        self.assertIn("↺ Общая папка для всех", dialog)
        self.assertIn("def use_common_directory_for_targets(", dialog)
        self.assertIn("def _sync_target_directory_controls(", dialog)
        self.assertIn('active_count >= 2', dialog)
        self.assertIn('"Папка (2+ цели)"', dialog)
        self.assertIn("При одной активной цели", dialog)
        self.assertIn('enabled.trace_add("write", _sync_target_directory_controls)', dialog)
        self.assertIn('text="🗑 Удалить"', dialog)
        self.assertIn('if len(controls) <= 1:', dialog)
        self.assertIn('"Нельзя удалить цель"', dialog)
        self.assertIn('if profile_id and fmt == "m4b":', dialog)
        self.assertIn('audio_profile_target_compatible(', dialog)
        self.assertIn("_destroy_target_rows()", dialog)
        self.assertNotIn("Текущее компактное окно содержит одну строку", dialog)
        self.assertIn('enabled=bool(enabled.get()),', dialog)
        self.assertIn('if not enabled.get() and not old:', dialog)
        self.assertIn('"Выберите хотя бы один активный формат."', dialog)
        self.assertIn('narrow_viewport = viewport_width < 857', dialog)
        self.assertIn('table_width = max(viewport_width, 857 if narrow_viewport else 0)', dialog)
        self.assertIn('width=table_width', dialog)
        self.assertIn('text="🗑 Удалить",\n                width=11,', dialog)

    def test_output_target_editor_resolves_profiles_lazily(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        dialog_start = source.index("    def open_output_targets_dialog(self):")
        dialog_end = source.index("    def _choose_output_target_dir", dialog_start)
        dialog = source[dialog_start:dialog_end]
        # Выбранный именованный профиль не должен читать посторонние временно
        # неверные элементы ``DoubleVar``/``IntVar`` из Tk. Пустой идентификатор означает,
        # что используются текущие компактные элементы, а не старый снимок
        # строки.
        self.assertIn("def _selected_export_value(", dialog)
        self.assertIn("if profile_id and key in profile_values:", dialog)
        self.assertIn("if not profile_id:", dialog)
        self.assertIn("saved_key=None", dialog)
        self.assertIn('"bitrate",', dialog)
        self.assertIn('"export_m4b_bitrate_var"', dialog)
        self.assertNotIn("float(self.exp_speed_var.get())", dialog)
        self.assertNotIn("int(self.exp_delay_var.get())", dialog)

    def test_output_target_editor_preserves_empty_m4b_template(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        dialog_start = source.index("    def open_output_targets_dialog(self):")
        dialog_end = source.index("    def _choose_output_target_dir", dialog_start)
        dialog = source[dialog_start:dialog_end]
        # Пустое значение — осознанный компактный вариант шаблона. Существующие
        # строки сохраняют явный пустой снимок, а новые читают текущее поле без
        # общего резервного значения для необязательных параметров.
        self.assertIn('def _current_target_template():', dialog)
        self.assertIn('"filename_template" in old', dialog)
        self.assertIn('filename_template = _current_target_template()', dialog)

    def test_source_launch_clears_legacy_m4b_target_filename_snapshot(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def start_processing(self, only_selected=False):")
        end = source.find("\n    def ", start + 1)
        if end < 0:
            end = len(source)
        launch = source[start:end]
        self.assertIn("Имена M4B источника принадлежат шаблону виртуального плана источника", launch)
        self.assertIn('dict(target, filename_template="")', launch)
        self.assertIn('str(target.get("format", "")).strip().lower() == "m4b"', launch)

    def test_export_preflight_rejects_desynchronized_tree_items(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("    def start_export_process(self):")
        end = source.index("    def add_separator_row", start)
        export_start = source[start:end]
        self.assertIn("unknown_children = [", export_start)
        self.assertIn("unknown_roots = [", export_start)
        self.assertIn('"Структура списка повреждена"', export_start)
        self.assertIn("if unknown_children or unknown_roots:", export_start)

    def test_output_target_custom_directory_is_preserved_and_normalized(self):
        target = studio.OutputTarget(
            format=" OpUs ",
            output_dir=" /chosen ",
            enabled="false",
            max_duration_seconds="-2",
            max_chapters="bad",
        )
        normalized = target.normalized()
        self.assertEqual(normalized["format"], "opus")
        self.assertEqual(normalized["output_dir"], "/chosen")
        self.assertFalse(normalized["enabled"])
        self.assertEqual(normalized["max_duration_seconds"], 0.0)
        self.assertEqual(normalized["max_chapters"], 0)

    def test_direct_planner_preserves_explicit_target_directory_until_cleared(self):
        """Низкоуровневый API сохраняет явный путь для совместимости."""
        root = Path(tempfile.gettempdir()).resolve()
        old_dir = str(root / "Старая папка" / "opus")
        new_dir = str(root / "Новая папка" / "opus")
        target = studio.OutputTarget(
            format="opus",
            output_dir=old_dir,
            bitrate="48k",
            profile="speech-48",
            effects={"speed": 1.1},
        )

        unchanged = studio.plan_output_targets(
            [target], new_dir, legacy_single_dir=new_dir
        )
        self.assertEqual(unchanged[0]["output_dir"], old_dir)

        cleared = studio.clear_output_target_directories([target])
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["output_dir"], "")
        self.assertEqual(cleared[0]["profile"], "speech-48")
        self.assertEqual(cleared[0]["effects"]["speed"], 1.1)
        moved = studio.plan_output_targets(
            cleared, new_dir, legacy_single_dir=new_dir
        )
        self.assertEqual(moved[0]["output_dir"], new_dir)

    def test_saved_single_target_delegates_its_directory_to_common_folder(self):
        """Старая папка одной цели не переживает смену общей папки."""
        root = Path(tempfile.gettempdir()).resolve()
        stale_dir = str(root / "Старый проект" / "m4b")
        common_dir = str(root / "Новый проект" / "m4b")
        targets = studio.normalize_output_target_directories(
            [
                studio.OutputTarget(
                    format="m4b",
                    output_dir=stale_dir,
                    bitrate="64k",
                )
            ]
        )

        self.assertEqual(targets[0]["output_dir"], "")
        planned = studio.plan_output_targets(
            targets,
            common_dir,
            legacy_single_dir=common_dir,
        )
        self.assertEqual(planned[0]["output_dir"], common_dir)

        portable = studio.output_target_library_from_targets(
            [
                studio.OutputTarget(
                    format="m4b",
                    output_dir=stale_dir,
                    bitrate="64k",
                )
            ]
        )
        self.assertEqual(portable["targets"][0]["output_dir"], "")

    def test_saved_multi_target_set_preserves_explicit_directories(self):
        """Индивидуальные каталоги остаются осознанной опцией набора из нескольких целей."""
        targets = studio.normalize_output_target_directories(
            [
                studio.OutputTarget(format="mp3", output_dir="/tmp/mp3"),
                studio.OutputTarget(format="opus", output_dir="/tmp/opus"),
            ]
        )

        self.assertEqual(
            [target["output_dir"] for target in targets],
            ["/tmp/mp3", "/tmp/opus"],
        )

    def test_singleton_directory_is_cleared_only_when_staged_targets_are_applied(self):
        """Отключение цели в редакторе не стирает её путь до применения изменений."""
        staged = [
            studio.OutputTarget(
                format="mp3", output_dir="/tmp/custom-mp3", enabled=True
            ).to_dict(),
            studio.OutputTarget(
                format="opus", output_dir="/tmp/custom-opus", enabled=True
            ).to_dict(),
        ]

        # Пока редактор открыт, отключение второй строки не должно стирать
        # отложенное значение первой: после повторного включения набор целей
        # должен восстановиться.
        staged[1]["enabled"] = False
        self.assertEqual(staged[0]["output_dir"], "/tmp/custom-mp3")

        # Правило единственной цели применяется при сборе/подтверждении, а не
        # в живом обработчике изменения флажка. После подтверждения общей становится папка
        # экспорта, а индивидуальное переопределение удаляется.
        applied = studio.normalize_output_target_directories(staged)
        self.assertEqual(applied[0]["output_dir"], "")
        self.assertEqual(applied[1]["output_dir"], "/tmp/custom-opus")

    def test_output_target_editor_does_not_clear_staged_directory_on_toggle(self):
        """Проверяем, что отслеживание изменений в интерфейсе не меняет подготовленное поле ``Entry``."""
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("        def _sync_target_directory_controls(")
        end = source.index("        def add_target_row(", start)
        sync_block = source[start:end]
        self.assertNotIn("directory.set(\"\")", sync_block)
        self.assertIn("Политика каталога для", sync_block)

    def test_legacy_output_target_import_rolls_back_when_settings_save_fails(self):
        """Неудачный вызов ``save_settings`` не публикует импортированный набор."""
        app = object.__new__(studio.TTSApp)
        old_config = {
            "export_targets": [
                studio.OutputTarget(
                    format="mp3", output_dir="/tmp/old"
                ).to_dict()
            ],
            "unrelated": "keep-me",
        }
        app.config = old_config
        app.export_targets = copy.deepcopy(old_config["export_targets"])
        imported = [
            studio.OutputTarget(
                format="opus", output_dir="/tmp/new", bitrate="48k"
            ).to_dict()
        ]
        app._select_output_targets_profile = mock.Mock(return_value=imported)

        def fail_save():
            # Имитируем изменение другого поля в ``update_config_from_ui`` до того,
            # как сохранение обнаружит недоступный путь настроек.
            app.config["unrelated"] = "half-applied"
            return False

        app.save_settings = mock.Mock(side_effect=fail_save)
        app._refresh_output_targets_summary = mock.Mock()
        app._show_info = mock.Mock()
        app._show_error = mock.Mock()

        app.import_output_targets_profile()

        self.assertIs(app.config, old_config)
        self.assertEqual(app.config, {
            "export_targets": old_config["export_targets"],
            "unrelated": "keep-me",
        })
        self.assertEqual(app.export_targets, old_config["export_targets"])
        app.save_settings.assert_called_once_with()
        app._refresh_output_targets_summary.assert_not_called()
        app._show_info.assert_not_called()
        app._show_error.assert_called_once()

    def test_config_migrates_single_target_directory_but_not_multi_target_set(self):
        single = studio.normalize_config(
            {
                "export_dir": "/tmp/new",
                "export_targets": [
                    {"format": "m4b", "output_dir": "/tmp/old"}
                ],
            }
        )
        multiple = studio.normalize_config(
            {
                "export_dir": "/tmp/common",
                "export_targets": [
                    {"format": "mp3", "output_dir": "/tmp/mp3"},
                    {"format": "opus", "output_dir": "/tmp/opus"},
                ],
            }
        )

        self.assertEqual(single["export_targets"][0]["output_dir"], "")
        self.assertEqual(
            [target["output_dir"] for target in multiple["export_targets"]],
            ["/tmp/mp3", "/tmp/opus"],
        )

    def test_output_target_directory_diagnostics_flags_single_stale_override(self):
        old_dir = "/tmp/MediaGet Downloads/Пожиратель душ/m4b"
        new_dir = "/tmp/Downloads/Пожиратель душ/m4b"
        target = studio.normalize_output_target(
            {"format": "m4b", "output_dir": old_dir}
        )
        diagnostic = studio.output_target_directory_diagnostics([target], new_dir)
        self.assertEqual(diagnostic["active_count"], 1)
        self.assertEqual(diagnostic["explicit"], (old_dir,))
        self.assertTrue(diagnostic["single_mismatch"])

        # Для нескольких целей разные папки допустимы. Несовпадение с общей
        # папкой экспорта отмечается только при единственной активной цели.
        multi = studio.output_target_directory_diagnostics(
            [
                target,
                studio.normalize_output_target(
                    {"format": "mp3", "output_dir": new_dir}
                ),
            ],
            new_dir,
        )
        self.assertFalse(multi["single_mismatch"])

    def test_output_target_directory_diagnostics_resolves_relative_override_like_planner(self):
        """Переносимые относительные пути сравниваются с общей папкой, а не с текущим рабочим каталогом."""
        with tempfile.TemporaryDirectory() as tempdir:
            common = Path(tempdir) / "outputs"
            target = studio.normalize_output_target(
                {"format": "opus", "output_dir": "."}
            )

            diagnostic = studio.output_target_directory_diagnostics(
                [target], common, base_dir=common
            )

            self.assertFalse(diagnostic["single_mismatch"])

            nested = studio.normalize_output_target(
                {"format": "opus", "output_dir": "opus"}
            )
            nested_diagnostic = studio.output_target_directory_diagnostics(
                [nested], common / "opus", base_dir=common
            )
            self.assertFalse(nested_diagnostic["single_mismatch"])

    def test_m4b_temp_files_are_removed_after_stage_failure(self):
        """Ошибка промежуточной AAC-стадии не оставляет скрытый мусор."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "Глава 01.mp3"
            source.write_bytes(b"audio")
            output = root / "Пожиратель душ.m4b"

            def fail_merge(_sources, destination, **_kwargs):
                Path(destination).write_bytes(b"partial stage")
                raise RuntimeError("simulated stage failure")

            with mock.patch.object(
                studio, "_export_merged_audio_ffmpeg", side_effect=fail_merge
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated stage failure"):
                    studio._export_m4b_ffmpeg(
                        (source,),
                        output,
                        chapters=({"title": "Глава 01", "duration": 1},),
                    )

            self.assertFalse(output.exists())
            leftovers = tuple(root.glob(".*.stage.m4a")) + tuple(
                root.glob(".*.ffmeta")
            ) + tuple(root.glob(".*.tmp.m4b"))
            self.assertEqual(leftovers, ())


if __name__ == "__main__":
    unittest.main()
