"""Предупреждения о повторных запросах учитывают готовность и снимок запуска."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_close_confirmation as closing
import test_source_session as source_session


studio = closing.studio


class CachePolicyWarningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        studio.APP_DATA_DIR.mkdir()
        self.app = object.__new__(studio.TTSApp)
        self.app._ask_cache_policy_action = mock.Mock(return_value="continue")
        self.config = {
            "use_cache": False, "cache_dir": str(self.root / "cache"),
            "source_target_records": [self.record()],
        }

    def record(self, kind="m4b", name="book.m4b", file_ids=("chapter",)):
        return {"kind": kind, "path": self.root / name, "file_ids": file_ids}

    def confirm(self, config=None, skip=True):
        return self.app._confirm_source_cache_policy(config or self.config, skip)

    def test_unrestricted_cache_never_reads_metadata_or_warns(self):
        self.config.update(use_cache="true", enable_cache_lru="false", enable_cache_ttl="0")
        with mock.patch.object(Path, "open", side_effect=AssertionError("Лишнее чтение")):
            self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_not_called()

    def test_pending_groups_warn_for_each_supported_cache_policy(self):
        for policy in (
            {"use_cache": False},
            {"use_cache": True, "enable_cache_lru": True},
            {"use_cache": True, "enable_cache_ttl": True},
        ):
            for kind, name in (("m4b", "book.m4b"), ("group", "book.mp3"), ("group", "book.opus")):
                with self.subTest(policy=policy, output=name):
                    app = object.__new__(studio.TTSApp)
                    app._ask_cache_policy_action = mock.Mock(return_value="continue")
                    config = dict(self.config, **policy, source_target_records=[self.record(kind, name)])
                    self.assertTrue(app._confirm_source_cache_policy(config, True))
                    app._ask_cache_policy_action.assert_called_once()
                    self.assertIn("лимит", app._ask_cache_policy_action.call_args.args[0])

    def test_single_chapters_and_local_exports_do_not_warn(self):
        for records in ((), (self.record("file", "chapter.mp3"),)):
            self.config["source_target_records"] = records
            self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_not_called()

    def test_ready_outputs_skip_without_acknowledging_risk(self):
        self.config["source_target_records"][0]["path"].write_bytes(b"ready")
        self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_not_called()
        self.assertIsNone(self.app._cache_warning_accepted_policy)
        self.config["source_target_records"][0]["path"].unlink()
        self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_called_once()

    def test_existing_error_and_warning_outputs_require_confirmation(self):
        path = self.config["source_target_records"][0]["path"]
        path.write_bytes(b"incomplete")
        for status in ("error", "warning"):
            with self.subTest(status=status):
                self.app._cache_warning_accepted_policy = None
                (studio.APP_DATA_DIR / "processing_statuses.json").write_text(json.dumps({str(path): status}))
                self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 2)

    def test_matching_shared_error_journal_takes_precedence_over_disk(self):
        path = self.config["source_target_records"][0]["path"]
        path.write_bytes(b"incomplete")
        self.app._shared_cache_dir = Path(self.config["cache_dir"])
        self.app._shared_processing_statuses = {str(path): "error"}
        with mock.patch.object(Path, "open", side_effect=AssertionError("Лишнее чтение")):
            self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_called_once()

    def test_changed_outputs_and_disabled_skipping_require_confirmation(self):
        path = self.config["source_target_records"][0]["path"]
        path.write_bytes(b"ready")
        for key, value in (
            ("source_m4b_force_rebuild", True),
            ("source_m4b_rebuild_paths", [str(path)]),
        ):
            with self.subTest(key=key):
                config = dict(self.config, **{key: value})
                self.assertTrue(studio.source_groups_need_synthesis(config, True, {}))
        self.assertTrue(self.confirm(skip=False))
        self.app._ask_cache_policy_action.assert_called_once()

    def test_failed_or_missing_member_invalidates_ready_group(self):
        group = self.config["source_target_records"][0]
        group["path"].write_bytes(b"ready book")
        single = self.record("file", "chapter.mp3")
        self.config["source_target_records"].append(single)
        self.assertTrue(studio.source_groups_need_synthesis(self.config, True, {}))
        single["path"].write_bytes(b"ready chapter")
        self.assertFalse(studio.source_groups_need_synthesis(self.config, True, {}))
        self.assertTrue(studio.source_groups_need_synthesis(
            self.config, True, {str(single["path"]): "warning"}
        ))

    def test_new_independent_format_does_not_warn_for_ready_group(self):
        group = self.config["source_target_records"][0]
        group["path"].write_bytes(b"ready book")
        single = self.record("file", "chapter.opus")
        self.config["source_target_records"].append(single)
        self.config["source_unrelated_target_paths"] = [str(single["path"])]
        self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_not_called()

    def test_acceptance_is_session_only_and_changes_reset_it(self):
        self.assertTrue(self.confirm())
        self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 1)
        self.app._observe_cache_warning_policy({"use_cache": True})
        self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 2)
        self.assertNotIn("_cache_warning_accepted_policy", self.config)
        other = object.__new__(studio.TTSApp)
        other._ask_cache_policy_action = mock.Mock(return_value="continue")
        self.assertTrue(other._confirm_source_cache_policy(self.config, True))
        other._ask_cache_policy_action.assert_called_once()

    def test_changing_active_limit_requires_new_acceptance(self):
        self.config.update(use_cache=True, enable_cache_lru=True, cache_max_entries=10)
        self.assertTrue(self.confirm())
        self.config["cache_max_entries"] = 5
        self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 2)

    def test_hint_follows_unsaved_changes_and_hides_for_unrestricted_cache(self):
        self.app.config = dict(self.config, use_cache=True)
        values = dict(use_cache=False, enable_cache_lru=False, enable_cache_ttl=False,
                      cache_max_entries=10, cache_ttl_hours=1)
        self.app.settings_vars = {key: mock.Mock(get=mock.Mock(return_value=value))
                                  for key, value in values.items()}
        self.app.lbl_cache_policy_hint = mock.Mock()
        self.app._refresh_cache_policy_hint()
        self.assertIn("Кэширование выключено", self.app.lbl_cache_policy_hint.configure.call_args.kwargs["text"])
        self.app.lbl_cache_policy_hint.grid.assert_called_once()
        self.app.settings_vars["use_cache"].get.return_value = True
        self.app._refresh_cache_policy_hint()
        self.assertEqual(self.app.lbl_cache_policy_hint.configure.call_args.kwargs["text"], "")
        self.app.lbl_cache_policy_hint.grid_remove.assert_called_once()
        self.app.settings_vars["enable_cache_ttl"].get.return_value = True
        self.app.settings_vars["cache_ttl_hours"].get.side_effect = studio.tk.TclError("Пустое поле")
        self.app._refresh_cache_policy_hint()
        self.assertIn("Ограничения кэша", self.app.lbl_cache_policy_hint.configure.call_args.kwargs["text"])

    def test_decline_does_not_acknowledge_risk(self):
        self.app._ask_cache_policy_action.return_value = "cancel"
        self.assertFalse(self.confirm())
        self.assertIsNone(self.app._cache_warning_accepted_policy)
        self.app._ask_cache_policy_action.return_value = "continue"
        self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 2)

    def test_settings_action_cancels_start_without_acceptance_or_policy_changes(self):
        before = copy.deepcopy(self.config)
        self.app._ask_cache_policy_action.return_value = "settings"
        self.app._open_cache_settings = mock.Mock()
        self.assertFalse(self.confirm())
        self.app._open_cache_settings.assert_called_once_with()
        self.assertIsNone(self.app._cache_warning_accepted_policy)
        self.assertEqual(self.config, before)
        self.app._ask_cache_policy_action.return_value = "continue"
        self.assertTrue(self.confirm())
        self.assertEqual(self.app._ask_cache_policy_action.call_count, 2)

    def test_settings_action_selects_the_cache_page(self):
        self.app.notebook = mock.Mock()
        self.app.tab_settings = object()
        self.app.settings_notebook = mock.Mock()
        self.app._cache_settings_page = "cache-page"
        self.app._schedule_focus_after_messagebox = mock.Mock()
        self.app._open_cache_settings()
        self.app.notebook.select.assert_called_once_with(self.app.tab_settings)
        self.app.settings_notebook.select.assert_called_once_with("cache-page")
        self.app._schedule_focus_after_messagebox.assert_called_once_with(self.app.settings_notebook)

    def test_broken_status_journal_does_not_hide_risk(self):
        self.config["source_target_records"][0]["path"].write_bytes(b"ready")
        (studio.APP_DATA_DIR / "processing_statuses.json").write_text("{broken")
        self.assertTrue(self.confirm())
        self.app._ask_cache_policy_action.assert_called_once()

    def test_declining_start_creates_no_processor_or_worker(self):
        case = source_session.SourceSessionTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        app = case.app({"use_cache": False, "synthesis_targets": [{"format": "m4b"}]})
        app.batch_processor = app.direct_processor = None
        app._warn_if_cache_busy_for_synthesis = mock.Mock(return_value=False)
        app._validate_api_steps_ui = mock.Mock(return_value=True)
        app._validate_book_output_profile = mock.Mock(return_value=True)
        app.settings_vars = {"skip_existing": mock.Mock(get=mock.Mock(return_value=True))}
        app._ask_cache_policy_action = mock.Mock(return_value="cancel")
        app._create_synthesis_processor = mock.Mock()
        with mock.patch.object(studio.threading, "Thread") as thread:
            app.start_processing()
        app._ask_cache_policy_action.assert_called_once()
        app._show_error.assert_not_called()
        app._create_synthesis_processor.assert_not_called()
        thread.assert_not_called()
        self.assertIsNone(app.batch_processor)

    def test_close_uses_active_configuration_and_cancel_preserves_work(self):
        for active_off in (True, False):
            with self.subTest(active_off=active_off):
                case = closing.CloseConfirmationTests("runTest")
                app = case.make_app()
                worker, processor = case.add_worker(app, "batch")
                app.config = {"use_cache": active_off}
                processor.cfg = dict(self.config, use_cache=not active_off)
                processor.processing_statuses_ram = {}
                app._ask_yes_no.return_value = False
                app.on_closing()
                text = app._ask_yes_no.call_args.args[1]
                self.assertEqual("повторный синтез" in text, active_off)
                self.assertFalse(processor.is_stopped)
                processor.flush_cache.assert_not_called()
                worker.join.assert_not_called()
                app.root.destroy.assert_not_called()

    def test_export_close_does_not_warn_about_speech_cache(self):
        case = closing.CloseConfirmationTests("runTest")
        app = case.make_app()
        case.add_worker(app, "export")
        app.config = copy.deepcopy(self.config)
        app._ask_yes_no.return_value = False
        app.on_closing()
        self.assertNotIn("повторный синтез", app._ask_yes_no.call_args.args[1])

    def test_close_warns_for_active_limits_even_when_global_limits_are_disabled(self):
        case = closing.CloseConfirmationTests("runTest")
        app = case.make_app()
        _worker, processor = case.add_worker(app, "batch")
        app.config = {"use_cache": True, "enable_cache_ttl": False}
        processor.cfg = dict(self.config, use_cache=True, enable_cache_ttl=True)
        processor.processing_statuses_ram = {}
        app._ask_yes_no.return_value = False
        app.on_closing()
        self.assertIn("Ограничения кэша LRU/TTL", app._ask_yes_no.call_args.args[1])
        self.assertFalse(processor.is_stopped)


if __name__ == "__main__":
    unittest.main()
