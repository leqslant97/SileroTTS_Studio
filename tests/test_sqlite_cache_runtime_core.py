"""Active SQLite migration, generations, backup and archive without HTTP."""
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

MODULE_NAME = 'silero_tts_studio_under_test'
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, Path(__file__).resolve().parents[1] / 'SileroTTS_Studio.py')
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class SQLiteRuntimeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.sample_dir.cleanup)
        sample = Path(cls.sample_dir.name) / 'sample.ogg'
        subprocess.run([studio.get_ffmpeg_path(), '-y', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=0.15:sample_rate=48000', '-ac', '1',
                        '-c:a', 'libopus', str(sample)], check=True, capture_output=True)
        cls.audio = sample.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / 'cache'
        (self.cache / 'audio').mkdir(parents=True)
        self.prepared = self.root / 'prepared.ogg'
        self.prepared.write_bytes(self.audio)
        self.indices = []
        self.addCleanup(self.close_indices)

    def close_indices(self):
        for index in self.indices:
            if not index._closed:
                studio.release_sqlite_cache_index(index)

    def migrate(self, records=None):
        if records is not None:
            (self.cache / 'sentence_cache.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), 'json')
        studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), 'sqlite')
        return receipt

    def acquire(self):
        index = studio.acquire_sqlite_cache_index(self.cache)
        self.indices.append(index)
        return index

    def test_explicit_migration_preserves_unknown_fields_and_classifies_audio(self):
        name = 'a' * 32 + '.ogg'
        (self.cache / 'audio' / name).write_bytes(self.audio)
        records = {'historical:key': {'file_name': name, 'speaker': 'Нина', 'normalized_text': 'x',
                                    'unknown': {'a': [True, None, 'ёж']}},
                   'unsafe': {'file_name': '../escape.ogg'}, 'missing': {'file_name': 'gone.ogg'}}
        receipt = self.migrate(records)
        self.assertEqual(receipt['audio_inventory']['opus'], 1)
        self.assertEqual(receipt['audio_inventory']['unsafe'], 1)
        self.assertEqual(receipt['audio_inventory']['missing'], 1)
        self.assertEqual(receipt['audio_inventory']['key_mismatch'], 1)
        self.assertEqual(self.acquire().get('historical:key'), records['historical:key'])
        self.assertTrue((self.cache / 'sentence_cache.json').is_file())
        self.assertNotIn('source_snapshot', receipt)
        self.assertFalse((self.cache / 'index_backups').exists())

    def test_empty_migration_and_corrupt_marker_never_fall_back_to_json(self):
        self.migrate()
        index = self.acquire()
        self.assertEqual(index.count(), 0)
        studio.release_sqlite_cache_index(index)
        (self.cache / 'sentence_cache.backend.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            studio.get_active_cache_backend(self.cache)

    def test_pool_reference_count_and_close_failure_are_retryable(self):
        self.migrate({})
        first, second = self.acquire(), self.acquire()
        self.assertIs(first, second)
        first.upsert('key', {'x': 1})
        studio.release_sqlite_cache_index(first)
        with mock.patch.object(first, '_commit_transaction', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                studio.release_sqlite_cache_index(second)
            self.assertEqual(first.get('key'), {'x': 1})
        studio.release_sqlite_cache_index(second)
        self.assertTrue(first._closed)

    def test_single_writer_lease_and_runtime_full_only(self):
        self.migrate({})
        index = self.acquire()
        with self.assertRaisesRegex(RuntimeError, 'другим экземпляром'):
            studio.SQLiteCacheIndex(index.path)
        with self.assertRaises(ValueError):
            studio.SQLiteCacheIndex(self.root / 'bad.sqlite', runtime=True, synchronous='NORMAL')

    def test_publish_is_visible_pending_and_old_generation_is_kept_until_reader_barrier(self):
        self.migrate({})
        index = self.acquire()
        first = index.publish_audio('key', self.prepared, {'usage_count': 1, 'unknown': 1})
        self.assertTrue(first.is_file())
        self.assertEqual(index.get('key')['file_name'], first.name)
        self.assertEqual(len(list((self.cache / 'publication_intents').glob('*.json'))), 1)
        index.flush()
        self.assertEqual(len(list((self.cache / 'publication_intents').glob('*.json'))), 0)
        second = index.publish_audio('key', self.prepared, {'usage_count': 1})
        index.flush()
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        retired = [path for batch in index.collect_retired_audio() for path in batch]
        self.assertEqual(retired, [first])
        for path in retired:
            path.unlink()
            index.acknowledge_retired_audio(path)
        self.assertEqual(second.read_bytes(), self.audio)
        self.assertEqual(list(index.collect_retired_audio()), [])

    def test_whitespace_filename_projection_matches_legacy_path_resolution(self):
        self.migrate({'a': {'file_name': ' shared.ogg '}, 'b': {'file_name': 'shared.ogg'}})
        index = self.acquire()
        self.assertTrue(index.file_referenced('shared.ogg'))
        index.delete('b')
        index.flush()
        self.assertTrue(index.file_referenced('shared.ogg'))
        self.assertEqual(index.get('a')['file_name'], ' shared.ogg ')

    def test_inactive_candidate_can_be_reviewed_and_explicitly_discarded(self):
        (self.cache / 'sentence_cache.json').write_text('{"a":{}}', encoding='utf-8')
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(studio.read_prepared_sqlite_cache_migration(self.cache), receipt)
        studio.discard_prepared_sqlite_cache_migration(self.cache)
        self.assertFalse((self.cache / 'sentence_cache.sqlite').exists())
        self.assertEqual(json.loads((self.cache / 'sentence_cache.json').read_text()), {'a': {}})

    def test_clear_rejects_live_readers_before_backup_or_deleting_audio(self):
        self.migrate({})
        index = self.acquire()
        path = index.publish_audio('key', self.prepared, {'usage_count': 1})
        index.flush()
        before = list((self.cache / 'index_backups').glob('index_*'))
        with self.assertRaisesRegex(RuntimeError, 'активных читателей'):
            studio.clear_cache_storage(self.cache)
        self.assertEqual(index.get('key')['file_name'], path.name)
        self.assertEqual(path.read_bytes(), self.audio)
        self.assertEqual(list((self.cache / 'index_backups').glob('index_*')), before)
        studio.release_sqlite_cache_index(index)
        studio.clear_cache_storage(self.cache)
        self.assertFalse(path.exists())
        empty = self.acquire()
        self.assertEqual(empty.count(), 0)

    def test_empty_migration_cannot_ignore_json_created_before_activation(self):
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        (self.cache / 'sentence_cache.json').write_text('{"new":{}}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'изменился'):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), 'json')

    def test_backup_fallback_cannot_ignore_new_valid_main_before_activation(self):
        (self.cache / 'sentence_cache.json').write_text('{broken', encoding='utf-8')
        (self.cache / 'sentence_cache.json.bak').write_text('{"backup":{}}', encoding='utf-8')
        receipt = studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True)
        self.assertEqual(receipt['source_kind'], 'backup')
        (self.cache / 'sentence_cache.json').write_text('{"new":{}}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'изменился'):
            studio.activate_sqlite_cache_migration(self.cache, receipt)
        self.assertEqual(studio.get_active_cache_backend(self.cache), 'json')

    def test_repeated_lru_of_unique_keys_does_not_accumulate_version_tombstones(self):
        self.migrate({})
        index = self.acquire()
        for batch in range(12):
            for number in range(25):
                key = f'{batch:03}:{number:03}'
                index.upsert(key, {'last_accessed': batch * 25 + number})
            index.flush()
            list(index.expire_cache(max_entries=10, batch_size=7))
        index.flush()
        source = sqlite3.connect(index.path)
        try:
            markers = source.execute("SELECT count(*) FROM meta WHERE key LIKE 'record_version:%'").fetchone()[0]
        finally:
            source.close()
        self.assertEqual(index.count(), 10)
        self.assertEqual(markers, 10)

    def test_manual_backups_are_not_rotated_and_zip_restores_existing_audio(self):
        self.migrate({})
        index = self.acquire()
        path = index.publish_audio('key', self.prepared, {'speaker': 'Нина'})
        index.flush()
        (self.cache / 'glossary.json').write_text('{"a":"b"}', encoding='utf-8')
        (self.cache / '.DS_Store').write_bytes(b'junk')
        for number in range(5):
            studio.create_cache_index_backup(self.cache, destination=self.root / f'manual-{number}.sqlite')
        self.assertEqual(len(list(self.root.glob('manual-*.sqlite'))), 5)
        self.assertFalse(list((self.cache / 'index_backups').glob('index_*.sqlite')))
        archive = studio.create_cache_archive(self.cache, self.root / 'cache.zip')
        with zipfile.ZipFile(archive) as file:
            self.assertFalse(any(name.endswith(('-wal', '-shm', '.DS_Store')) for name in file.namelist()))
            file.extractall(self.root / 'restored')
        restored_dir = self.root / 'restored' / self.cache.name
        restored = studio.acquire_sqlite_cache_index(restored_dir)
        try:
            metadata = restored.get('key')
            audio = studio.resolve_cache_audio_path(restored_dir, metadata['file_name'])
            studio._validate_complete_ogg_audio_file(audio)
            self.assertEqual(audio.read_bytes(), self.audio)
        finally:
            studio.release_sqlite_cache_index(restored)

    def test_cancelled_prepare_keeps_json_and_allows_new_attempt(self):
        (self.cache / 'sentence_cache.json').write_text('{"x":{}}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'отменена'):
            studio.prepare_sqlite_cache_migration(self.cache, confirmed_local_storage=True, cancelled=lambda: True)
        self.assertEqual(studio.get_active_cache_backend(self.cache), 'json')
        self.assertFalse((self.cache / 'sentence_cache.sqlite').exists())
        self.migrate({'x': {}})

    def test_inventory_is_stable_during_generation_replacements_and_bounded_eviction(self):
        self.migrate({})
        index = self.acquire()
        for number in range(8):
            index.upsert(str(number), {'file_name': str(number)+'.ogg', 'last_accessed': number})
        inventory = index.begin_file_inventory()
        self.assertEqual(inventory['total'], 8)
        first = index.file_inventory_page(inventory['token'], limit=3)
        index.upsert('later', {'file_name': 'z.ogg'})
        cursor, names = first['cursor'], list(first['file_names'])
        while first['has_more']:
            first = index.file_inventory_page(inventory['token'], cursor=cursor, limit=3)
            names.extend(first['file_names'])
            cursor = first['cursor']
        self.assertNotIn('z.ogg', names)
        index.end_file_inventory(inventory['token'])
        batches = list(index.expire_cache(ttl_hours=1, now=4000, batch_size=3))
        self.assertTrue(all(len(batch) <= 3 for batch in batches))
        self.assertEqual(sum(map(len, batches)), 8)
        self.assertEqual(index.count(), 1)


if __name__ == '__main__':
    unittest.main()
