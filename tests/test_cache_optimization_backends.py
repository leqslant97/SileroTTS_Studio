"""Real cache optimization preserves live audio and fails safely in both backends."""

import copy
import importlib.util
import json
import subprocess
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


class CacheOptimizationBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(fixture.cleanup)
        sample = Path(fixture.name) / "sample.ogg"
        subprocess.run([
            studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.15:sample_rate=48000", "-ac", "1",
            "-c:a", "libopus", str(sample),
        ], check=True, capture_output=True)
        cls.audio = sample.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sequence = 0
        patcher = mock.patch.object(studio, "APP_DATA_DIR", self.root / "app-data")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            studio.requests.Session, "post", side_effect=AssertionError("real HTTP forbidden")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, backend, use_cache=True):
        self.sequence += 1
        root = self.root / str(self.sequence)
        source, cache = root / "input", root / "cache"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "book.txt").write_text("Нужная фраза.", encoding="utf-8")
        (cache / "audio").mkdir(parents=True)
        cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        cfg.update(input_dir=str(source), output_dir=str(root / "output"), cache_dir=str(cache),
                   use_cache=use_cache, include_subdirs=True, use_ru_normalizer=False,
                   api_steps_enabled=True, api_steps=16)
        base = {"speaker": cfg["speaker"], "normalized_text": "Нужная фраза.",
                "original_text": "Нужная фраза.", "file_name": "shared.ogg",
                "created_at": 1000.0, "last_accessed": 1001.0, "usage_count": 3,
                "unknown": {"preserve": [None, "Ёж", 1]}}
        records = {
            "legacy": dict(base),
            "current": dict(base, steps=16, file_name="current.ogg"),
            "string-current": dict(base, steps="16"),
            "other-steps": dict(base, steps=24, file_name="other.ogg"),
            "stale": dict(base, normalized_text="Удалённая фраза.", file_name="stale.ogg"),
            "shared-stale": dict(base, normalized_text="Удалённая фраза."),
            "speaker": dict(base, speaker="Другой голос", file_name="speaker.ogg"),
        }
        for record in records.values():
            (cache / "audio" / record["file_name"]).write_bytes(self.audio)
        (cache / "sentence_cache.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        (cache / "sentence_cache.json.bak").write_text('{"preexisting_backup":{}}')
        if backend == "sqlite":
            receipt = studio.prepare_sqlite_cache_migration(cache, confirmed_local_storage=True)
            studio.activate_sqlite_cache_migration(cache, receipt)
        return cfg, cache, source, records

    def rows(self, cache, backend):
        if backend == "json":
            return json.loads((cache / "sentence_cache.json").read_text())
        index = studio.acquire_sqlite_cache_index(cache)
        try:
            return {key: record for page in index.iter_pages() for key, record in page["rows"]}
        finally:
            studio.release_sqlite_cache_index(index)

    def app(self, cfg, policy):
        app = object.__new__(studio.TTSApp)
        app.config = cfg
        app.is_cache_operation_running = lambda: False
        app.is_synthesis_running = lambda: False
        app._validate_api_steps_ui = lambda **_kwargs: True
        app.save_settings = lambda: None
        app._begin_cache_operation = mock.Mock(return_value=mock.Mock())
        app._end_cache_operation = mock.Mock()
        app._choose_cache_variant_policy = lambda _cfg: policy
        app._confirm_cache_mutation = mock.Mock(return_value=False)
        app._create_wait_popup = mock.Mock(return_value=mock.Mock())
        app.is_cache_optimization_running = lambda: True
        app._post_to_ui = mock.Mock()
        return app

    def optimize(self, app):
        original_thread = studio.threading.Thread

        def choose_thread(*args, **kwargs):
            if getattr(kwargs.get("target"), "__name__", "") == "run_opt":
                return mock.Mock(start=kwargs["target"])
            return original_thread(*args, **kwargs)

        with mock.patch.object(studio.threading, "Thread", choose_thread):
            app.optimize_cache()
        self.assertEqual(app._post_to_ui.call_count, 1)
        return app._post_to_ui.call_args.args

    def assert_audio(self, cache, records):
        for record in records.values():
            self.assertEqual((cache / "audio" / record["file_name"]).read_bytes(), self.audio)

    def test_policies_work_in_both_backends_even_with_synthesis_cache_disabled(self):
        expected_keys = {
            studio.CACHE_VARIANT_KEEP_ALL: {"legacy", "current", "string-current", "other-steps"},
            studio.CACHE_VARIANT_KEEP_LEGACY: {"legacy"},
            studio.CACHE_VARIANT_KEEP_LEGACY_CURRENT: {"legacy", "current", "string-current"},
            studio.CACHE_VARIANT_KEEP_CURRENT: {"current", "string-current"},
        }
        for backend in ("json", "sqlite"):
            for use_cache in (True, False):
                for policy, retained in expected_keys.items():
                    with self.subTest(backend=backend, use_cache=use_cache, policy=policy):
                        cfg, cache, _source, records = self.fixture(backend, use_cache)
                        cfg_before = copy.deepcopy(cfg)
                        original_json = (cache / "sentence_cache.json").read_bytes()
                        mirror = cache / "sentence_cache.json.bak"
                        app = self.app(cfg, policy)
                        app._confirm_cache_mutation.return_value = cache.parent / "backup.zip"
                        actual_archive = studio.create_cache_archive

                        def checked_archive(*args, **kwargs):
                            self.assertEqual(self.rows(cache, backend), records)
                            self.assert_audio(cache, records)
                            return actual_archive(*args, **kwargs)

                        with mock.patch.object(studio, "create_cache_archive", side_effect=checked_archive) as backup:
                            outcome = self.optimize(app)
                        actual = self.rows(cache, backend)
                        self.assertEqual(actual, {key: records[key] for key in retained})
                        self.assert_audio(cache, actual)
                        self.assertEqual(cfg, cfg_before)
                        backup.assert_called_once()
                        self.assertIsNone(outcome[3])
                        self.assertEqual(outcome[4][0], "info")
                        if backend == "sqlite":
                            self.assertEqual((cache / "sentence_cache.json").read_bytes(), original_json)
                            self.assertFalse(mirror.exists())

    def test_no_text_or_unreadable_text_never_removes_records_or_audio(self):
        for backend in ("json", "sqlite"):
            for failure in ("missing", "invalid_utf8"):
                with self.subTest(backend=backend, failure=failure):
                    cfg, cache, source, records = self.fixture(backend)
                    text = source / "nested" / "book.txt"
                    text.unlink() if failure == "missing" else text.write_bytes(b"\xff invalid")
                    app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
                    with mock.patch.object(studio, "create_cache_index_backup") as backup:
                        outcome = self.optimize(app)
                    self.assertEqual(self.rows(cache, backend), records)
                    self.assert_audio(cache, records)
                    backup.assert_not_called()
                    self.assertEqual(bool(outcome[3]), failure == "invalid_utf8")

    def test_no_deletions_does_not_create_selected_zip(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend):
                cfg, cache, _source, records = self.fixture(backend)
                app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
                app._confirm_cache_mutation.return_value = cache.parent / "unused.zip"
                with (
                    mock.patch.object(studio, "cache_entry_matches_required_text", return_value=True),
                    mock.patch.object(studio, "create_cache_archive") as archive,
                    mock.patch.object(studio, "create_cache_index_backup") as backup,
                ):
                    outcome = self.optimize(app)
                self.assertIsNone(outcome[3])
                self.assertEqual(self.rows(cache, backend), records)
                self.assert_audio(cache, records)
                archive.assert_not_called()
                backup.assert_not_called()

    def test_backup_failure_cannot_remove_metadata_or_audio(self):
        for backend in ("json", "sqlite"):
            with self.subTest(backend=backend):
                cfg, cache, _source, records = self.fixture(backend)
                app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
                app._confirm_cache_mutation.return_value = cache.parent / "backup.zip"
                with mock.patch.object(studio, "create_cache_archive", side_effect=OSError("disk full")):
                    outcome = self.optimize(app)
                self.assertTrue(outcome[3])
                self.assertEqual(self.rows(cache, backend), records)
                self.assert_audio(cache, records)

    def test_json_write_failure_preserves_metadata_and_audio(self):
        cfg, cache, _source, records = self.fixture("json")
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        with mock.patch.object(studio, "write_cache_index_atomic", side_effect=OSError("disk full")):
            outcome = self.optimize(app)
        self.assertTrue(outcome[3])
        self.assertEqual(self.rows(cache, "json"), records)
        self.assert_audio(cache, records)

    def test_json_corruption_after_preview_is_reported_without_replacing_index(self):
        cfg, cache, _source, records = self.fixture("json", use_cache=False)
        main, backup = cache / "sentence_cache.json", cache / "sentence_cache.json.bak"
        main.write_bytes(b"{broken main")
        backup.write_bytes(b"{broken backup")
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        with mock.patch.object(studio, "create_cache_index_backup") as saved:
            outcome = self.optimize(app)
        self.assertTrue(outcome[3])
        self.assertIsNone(outcome[2])
        self.assertEqual(main.read_bytes(), b"{broken main")
        self.assertEqual(backup.read_bytes(), b"{broken backup")
        self.assert_audio(cache, records)
        saved.assert_not_called()

    def test_json_management_does_not_apply_synthesis_ttl_or_lru(self):
        cfg, cache, _source, records = self.fixture("json", use_cache=False)
        cfg.update(enable_cache_lru=True, cache_max_entries=1, enable_cache_ttl=True, cache_ttl_hours=1)
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        outcome = self.optimize(app)
        self.assertIsNone(outcome[3])
        self.assertEqual(set(self.rows(cache, "json")), {"legacy", "current", "string-current", "other-steps"})

    def test_backend_corruption_after_preview_finishes_operation_without_fallback(self):
        cfg, cache, _source, records = self.fixture("sqlite")
        original_json = (cache / "sentence_cache.json").read_bytes()
        mirror = cache / "sentence_cache.json.bak"
        self.assertFalse(mirror.exists())
        marker = cache / "sentence_cache.backend.json"
        marker.write_bytes(b"{broken backend marker")
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        with mock.patch.object(studio, "create_cache_index_backup") as backup:
            outcome = self.optimize(app)
        self.assertEqual(outcome[0], app._finish_cache_optimization)
        self.assertIsNone(outcome[2])
        self.assertTrue(outcome[3])
        app._cache_generation_changed = mock.Mock()
        app._close_popup_safely = mock.Mock()
        app._cache_ui_loaded = False
        app._show_task_notification = mock.Mock()
        outcome[0](*outcome[1:], cache_dir=cache)
        app._end_cache_operation.assert_called_with("optimize")
        app._close_popup_safely.assert_called_once()
        self.assertEqual(app._show_task_notification.call_args.args[0], "error")
        self.assertEqual(marker.read_bytes(), b"{broken backend marker")
        self.assertEqual((cache / "sentence_cache.json").read_bytes(), original_json)
        self.assertFalse(mirror.exists())
        self.assert_audio(cache, records)
        backup.assert_not_called()

    def test_sqlite_optimization_preserves_existing_inactive_json_mirror(self):
        cfg, cache, _source, records = self.fixture("sqlite")
        mirror = cache / "sentence_cache.json.bak"
        previous = b'{"preexisting_backup":{}}'
        mirror.write_bytes(previous)
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        outcome = self.optimize(app)
        self.assertIsNone(outcome[3])
        self.assertEqual(mirror.read_bytes(), previous)
        retained = self.rows(cache, "sqlite")
        self.assertEqual(set(retained), {"legacy", "current", "string-current", "other-steps"})
        self.assert_audio(cache, retained)

    def test_sqlite_close_retry_invalidates_table_after_committed_deletion(self):
        cfg, cache, _source, records = self.fixture("sqlite")
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        real_flush = studio.SQLiteCacheIndex.flush
        did_fail = []

        def fail_once(index):
            if not did_fail:
                did_fail.append(True)
                raise OSError("reply lost")
            return real_flush(index)

        with mock.patch.object(studio.SQLiteCacheIndex, "flush", fail_once):
            outcome = self.optimize(app)
        self.assertTrue(outcome[2])
        self.assertTrue(outcome[3])
        self.assertEqual(set(self.rows(cache, "sqlite")), {"legacy", "current", "string-current", "other-steps"})
        self.assert_audio(cache, records)

    def test_json_completion_discards_shared_snapshot_for_next_synthesis(self):
        cfg, cache, _source, _records = self.fixture("json")
        app = self.app(cfg, studio.CACHE_VARIANT_KEEP_ALL)
        app._shared_cache_dir = cache
        app._shared_cache = {"removed": {}}
        app._shared_processing_statuses = {}
        app._cache_generation_changed = mock.Mock()
        app._close_popup_safely = mock.Mock()
        app._cache_ui_loaded = False
        app._finish_cache_optimization(mock.Mock(), {}, cache_dir=cache)
        self.assertIsNone(app._shared_cache_dir)
        self.assertIsNone(app._shared_cache)
        self.assertIsNone(app._shared_processing_statuses)
        app._cache_generation_changed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
