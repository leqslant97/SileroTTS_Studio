"""SQLite synthesis keeps batch, hit, force-new and disabled-cache contracts."""

import base64
import copy
import contextlib
import json
import queue
import sqlite3
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest import mock

try:
    import test_regressions as regression
except ModuleNotFoundError:
    from tests import test_regressions as regression

studio = regression.studio


class SQLiteSynthesisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.cfg.update(cache_dir=str(self.root / "cache"), input_dir=str(self.root / "input"),
                        output_dir=str(self.root / "output"), auto_trim_silence=False,
                        api_max_requests=100, api_time_window=0, max_retries=1,
                        cache_save_frequency=3)
        self.audio = regression.CacheBehaviorTests.complete_cached_opus()
        self.data_patch = mock.patch.object(studio, "APP_DATA_DIR", self.root / "data")
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.http_guard = mock.patch.object(studio.requests.Session, "post",
                                           side_effect=AssertionError("real HTTP forbidden"))
        self.http_guard.start()
        self.addCleanup(self.http_guard.stop)
        self.prepare_active()

    def prepare_active(self):
        cache_dir = Path(self.cfg["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "sentence_cache.sqlite"
        with studio.SQLiteCacheIndex(path) as index:
            index._request("set_meta", "activation", "active")
        (cache_dir / "sentence_cache.backend.json").write_text(json.dumps({
            "schema": 1, "backend": "sqlite", "database": "sentence_cache.sqlite",
            "storage_policy": "confirmed_local", "synchronous": "FULL",
        }), encoding="utf-8")

    def processor(self, **options):
        cfg = dict(self.cfg)
        cfg.update(options)
        processor = studio.TTSProcessor(cfg, shared_processing_statuses={})
        self.addCleanup(processor.close_cache)
        self.addCleanup(processor.cleanup_transient_audio_files)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [{"audio": base64.b64encode(self.audio).decode()}]}
        processor.session.post = mock.Mock(return_value=response)
        fallback = self.root / "fallback.ogg"
        fallback.write_bytes(self.audio)
        processor._get_silence_file = mock.Mock(return_value=fallback)
        return processor

    def persisted(self):
        with contextlib.closing(sqlite3.connect(Path(self.cfg["cache_dir"]) / "sentence_cache.sqlite")) as connection:
            return {key: json.loads(record) for key, record in connection.execute(
                "SELECT key, record_json FROM entries")}

    def seed(self, processor, text, **extra):
        key = processor.get_hash(text)
        path = processor.cache_audio_dir / (key + ".ogg")
        path.write_bytes(self.audio)
        metadata = {"file_name": path.name, "speaker": processor.cfg["speaker"],
                    "usage_count": 2, "last_accessed": 2, "audio_codec": "opus"}
        metadata.update(extra)
        processor.cache.upsert(key, metadata)
        processor.cache.flush()
        return key, path, metadata

    def test_new_audio_is_visible_before_batch_n_and_full_flush_preserves_hits(self):
        processor = self.processor()
        first, success = processor.synthesize_sentence("Один.", "Один.")
        self.assertTrue(success)
        self.assertIsInstance(processor.cache, studio.SQLiteCacheIndex)
        self.assertEqual(self.persisted(), {})
        same, success = processor.synthesize_sentence("Один.", "Один.")
        self.assertTrue(success)
        self.assertEqual(same, first)
        self.assertEqual(processor.session.post.call_count, 1)
        self.assertEqual(processor.unsaved_cache_items, 1)
        processor.synthesize_sentence("Два.", "Два.")
        self.assertEqual(self.persisted(), {})
        processor.synthesize_sentence("Три.", "Три.")
        self.assertEqual(len(self.persisted()), 3)
        self.assertEqual(self.persisted()[processor.get_hash("Один.")]["usage_count"], 2)
        self.assertEqual(processor.unsaved_cache_items, 0)

    def test_refresh_after_each_fragment_keeps_configured_commit_threshold(self):
        for frequency in (1, 7, 37, 100, 200, 257):
            with self.subTest(frequency=frequency):
                processor = self.processor(
                    cache_dir=str(self.root / ("batch_" + str(frequency))),
                    cache_save_frequency=frequency,
                )
                database = Path(processor.cfg["cache_dir"]) / studio._CACHE_DATABASE_NAME
                with mock.patch.object(processor.cache, "_commit_transaction",
                                       wraps=processor.cache._commit_transaction) as commit:
                    for number in range(1, 2 * frequency + 2):
                        text = f"Фрагмент номер {number}."
                        _, success = processor.synthesize_sentence(text, text)
                        self.assertTrue(success)
                        # The cache tab acquires this same owner, reads its
                        # pending overlay, then releases its extra reference.
                        reader = studio.acquire_sqlite_cache_index(database.parent)
                        try:
                            self.assertEqual(len(reader.query_page(limit=1000)["rows"]), number)
                            self.assertEqual(reader.count(), number)
                        finally:
                            studio.release_sqlite_cache_index(reader)
                        with contextlib.closing(sqlite3.connect(database)) as connection:
                            persisted = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
                        self.assertEqual(persisted, number // frequency * frequency)
                        self.assertEqual(commit.call_count, number // frequency)
                processor.close_cache()
                with contextlib.closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM entries").fetchone()[0],
                                     2 * frequency + 1)

    def test_warm_hits_aggregate_without_committing_on_each_file(self):
        processor = self.processor(api_token="")
        key, path, _metadata = self.seed(processor, "Попадание.")
        for _ in range(7):
            self.assertEqual(processor.synthesize_sentence("Попадание.", "Попадание."), (path, True))
            self.assertTrue(processor._save_cache())
        processor.session.post.assert_not_called()
        self.assertEqual(self.persisted()[key]["usage_count"], 2)
        self.assertEqual(processor.cache.get(key)["usage_count"], 9)
        self.assertTrue(processor.flush_cache())
        self.assertEqual(self.persisted()[key]["usage_count"], 9)

    def test_hit_avoids_repeated_key_and_actor_lookup_without_losing_statistics(self):
        processor = self.processor(api_token="")
        key, path, _metadata = self.seed(processor, "Попадание.", nested={"extra": [1, "ё"]})
        with mock.patch.object(processor.cache, "_request", wraps=processor.cache._request) as request, \
                mock.patch.object(processor, "get_hash", wraps=processor.get_hash) as get_hash:
            self.assertEqual(processor.synthesize_sentence("Попадание.", "Попадание."), (path, True))
        self.assertEqual([call.args[0] for call in request.call_args_list], ["get", "touch"])
        self.assertEqual(get_hash.call_count, 1)
        metadata = processor.cache.get(key)
        self.assertEqual(metadata["usage_count"], 3)
        self.assertEqual(metadata["nested"], {"extra": [1, "ё"]})
        processor.session.post.assert_not_called()
        processor.flush_cache()
        self.assertEqual(self.persisted()[key], metadata)

    def test_hit_uses_exact_metadata_filename_and_unknown_fields(self):
        processor = self.processor()
        key, original, metadata = self.seed(processor, "Старый формат.", nested={"extra": [1, "ё"]})
        moved = original.with_name("историческая запись.ogg")
        original.rename(moved)
        metadata["file_name"] = moved.name
        processor.cache.upsert(key, metadata)
        processor.cache.flush()
        self.assertEqual(processor.synthesize_sentence("Старый формат.", "Старый формат."), (moved, True))
        processor.flush_cache()
        self.assertEqual(self.persisted()[key]["nested"], {"extra": [1, "ё"]})
        processor.session.post.assert_not_called()

    def test_disabled_cache_does_not_read_backend_or_index_or_expire_it(self):
        index_path = Path(self.cfg["cache_dir"]) / "sentence_cache.sqlite"
        before = index_path.read_bytes()
        with mock.patch.object(studio, "get_active_cache_backend", side_effect=AssertionError("backend read")), \
             mock.patch.object(studio, "read_cache_index_with_backup", side_effect=AssertionError("JSON read")):
            processor = self.processor(use_cache=False, enable_cache_ttl=True, enable_cache_lru=True)
            result, success = processor.synthesize_sentence("Временная речь.", "Временная речь.")
            self.assertTrue(success)
            self.assertNotEqual(result.parent, processor.cache_audio_dir)
            self.assertEqual(processor.cache, {})
            self.assertTrue(processor.flush_cache())
        self.assertEqual(index_path.read_bytes(), before)

    def test_two_independent_processors_share_inflight_and_pending_overlay(self):
        owner, waiter = self.processor(), self.processor()
        self.assertIs(owner.cache, waiter.cache)
        self.assertIs(owner.cache_lock, waiter.cache_lock)
        self.assertIs(owner.synthesis_inflight, waiter.synthesis_inflight)
        started, release = threading.Event(), threading.Event()
        response = owner.session.post.return_value

        def post(*_args, **_kwargs):
            started.set()
            if not release.wait(3):
                raise AssertionError("owner was not released")
            return response

        owner.session.post.side_effect = post
        waiter.session.post.side_effect = AssertionError("duplicate HTTP")
        results = queue.Queue()
        threads = [threading.Thread(target=lambda p=p: results.put(
            p.synthesize_sentence("Общий запрос.", "Общий запрос."))) for p in (owner, waiter)]
        threads[0].start()
        self.assertTrue(started.wait(3))
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.get(), results.get())
        self.assertEqual(owner.session.post.call_count, 1)
        waiter.session.post.assert_not_called()

    def test_force_new_retains_previous_generation_until_reader_barrier(self):
        processor = self.processor()
        key, old, metadata = self.seed(processor, "Новая версия.")
        processor.defer_cache_eviction()
        result, success = processor.synthesize_sentence("Новая версия.", "Новая версия.", force_new=True)
        self.assertTrue(success)
        self.assertNotEqual(result, old)
        self.assertEqual(old.read_bytes(), self.audio)
        self.assertEqual(self.persisted()[key], metadata)
        processor.flush_cache()
        self.assertTrue(old.exists())
        processor.resume_cache_eviction()
        processor.flush_cache()
        self.assertFalse(old.exists())
        self.assertEqual(self.persisted()[key]["file_name"], result.name)
        self.assertEqual(result.read_bytes(), self.audio)

    def test_failed_force_new_and_bad_recovery_never_trigger_extra_http(self):
        processor = self.processor(max_retries=3)
        key, old, metadata = self.seed(processor, "Старый результат.")
        processor.session.post.return_value.json.return_value = {
            "results": [{"audio": base64.b64encode(self.audio[:27]).decode()}]}
        _result, success = processor.synthesize_sentence("Старый результат.", "Старый результат.", force_new=True)
        self.assertFalse(success)
        self.assertEqual(old.read_bytes(), self.audio)
        self.assertEqual(processor.cache.get(key), metadata)
        self.assertEqual(processor.session.post.call_count, 1)
        processor.cache._blocked_keys[key] = "ambiguous local publication"
        with self.assertRaises(studio.CacheRecoveryError):
            processor.synthesize_sentence("Старый результат.", "Старый результат.")
        with self.assertRaises(studio.CacheRecoveryError):
            processor.synthesize_sentence("Старый результат.", "Старый результат.", force_new=True)
        self.assertEqual(processor.session.post.call_count, 1)
        processor.cache._blocked_keys.pop(key)

    def test_failed_flush_keeps_pending_and_reference_until_successful_close(self):
        processor = self.processor()
        result, success = processor.synthesize_sentence("Сбой диска.", "Сбой диска.")
        self.assertTrue(success)
        with mock.patch.object(processor.cache, "flush", side_effect=OSError("disk full")):
            self.assertFalse(processor.flush_cache())
            self.assertFalse(processor.close_cache())
            self.assertEqual(processor.unsaved_cache_items, 1)
            self.assertTrue(processor._sqlite_cache_reference)
            self.assertEqual(processor.cache.get(processor.get_hash("Сбой диска."))["file_name"], result.name)
        self.assertTrue(processor.close_cache())
        self.assertFalse(processor._sqlite_cache_reference)
        self.assertTrue(processor.close_cache())

    def test_lru_deferred_shared_references_commit_before_audio_gc(self):
        processor = self.processor(enable_cache_lru=True, cache_max_entries=1)
        old_key, old, _info = self.seed(processor, "Старая речь.", last_accessed=1)
        new_key, new, _info = self.seed(processor, "Новая речь.", last_accessed=2)
        processor.defer_cache_eviction()
        processor.flush_cache()
        self.assertIsNotNone(processor.cache.get(old_key))
        self.assertTrue(old.exists())
        processor.resume_cache_eviction()
        processor.flush_cache()
        self.assertIsNone(processor.cache.get(old_key))
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertIn(new_key, self.persisted())

    def test_steps_key_and_mismatch_semantics_remain_unchanged(self):
        processor = self.processor(api_steps_enabled=True, api_steps=16)
        key, path, _info = self.seed(processor, "Шаги.", steps=16)
        self.assertEqual(key, studio.hashlib.md5(f"Шаги._{processor.cfg['speaker']}_steps=16".encode()).hexdigest())
        self.assertEqual(processor.synthesize_sentence("Шаги.", "Шаги."), (path, True))
        processor.session.post.assert_not_called()

    def test_lazy_vorbis_hit_publishes_new_generation_without_touching_old_audio(self):
        processor = self.processor()
        key, path, metadata = self.seed(processor, "Старый Vorbis.", audio_codec="opus")
        subprocess.run([
            studio.get_ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.15:sample_rate=48000", "-ac", "1", "-c:a",
            "libvorbis", str(path),
        ], capture_output=True, check=True)
        old_audio = path.read_bytes()
        processor.defer_cache_eviction()
        result, success = processor.synthesize_sentence("Старый Vorbis.", "Старый Vorbis.")
        self.assertTrue(success)
        self.assertNotEqual(result, path)
        self.assertEqual(path.read_bytes(), old_audio)
        self.assertEqual(studio._detect_ogg_audio_codec(result), "opus")
        studio._validate_complete_ogg_audio_file(result)
        processor.session.post.assert_not_called()
        processor.flush_cache()
        self.assertTrue(path.exists())
        processor.resume_cache_eviction()
        processor.flush_cache()
        self.assertFalse(path.exists())
        self.assertEqual(self.persisted()[key]["file_name"], result.name)
        self.assertEqual(self.persisted()[key]["usage_count"], 3)

    def test_uncertain_local_publish_is_confirmed_from_overlay_without_network_retry(self):
        processor = self.processor(max_retries=3)
        real_publish = processor.cache.publish_audio

        def publish_then_raise(key, prepared, metadata):
            published = real_publish(key, prepared, metadata)
            raise studio.CachePublicationError("commit reply lost", key=key, published_path=published)

        with mock.patch.object(processor.cache, "publish_audio", side_effect=publish_then_raise):
            result, success = processor.synthesize_sentence("Ответ потерян локально.", "Ответ потерян локально.")
        self.assertTrue(success)
        self.assertEqual(processor.session.post.call_count, 1)
        self.assertEqual(processor.cache.get(processor.get_hash("Ответ потерян локально."))["file_name"], result.name)
        self.assertTrue(result.is_file())

    def test_shared_filename_survives_lru_until_last_key_is_removed(self):
        processor = self.processor(enable_cache_lru=True, cache_max_entries=1)
        key, path, metadata = self.seed(processor, "Общая ссылка.", last_accessed=1)
        shared = dict(metadata, last_accessed=2)
        processor.cache.upsert("manual:alias", shared)
        processor.cache.flush()
        processor.flush_cache()
        self.assertIsNone(processor.cache.get(key))
        self.assertTrue(path.exists())
        self.assertEqual(processor.cache.get("manual:alias")["file_name"], path.name)

    def test_store_failure_after_audio_rename_recovers_durable_intent_without_http_retry(self):
        processor = self.processor(max_retries=3)
        original_store = processor.cache._store
        calls = 0

        def fail_first_store(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("writer queue temporarily unavailable")
            return original_store(*args, **kwargs)

        with mock.patch.object(processor.cache, "_store", side_effect=fail_first_store):
            result, success = processor.synthesize_sentence("Устойчивый intent.", "Устойчивый intent.")
        self.assertTrue(success)
        self.assertEqual(processor.session.post.call_count, 1)
        self.assertEqual(result.read_bytes(), self.audio)
        self.assertEqual(self.persisted()[processor.get_hash("Устойчивый intent.")]["file_name"], result.name)
        self.assertEqual(list((processor.cache_dir / "publication_intents").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
