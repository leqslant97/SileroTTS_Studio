"""Real Ogg generation crash boundaries and idempotent local recovery."""
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests import test_sqlite_cache_runtime_core as core
studio = core.studio


class SQLiteRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(core.SQLiteRuntimeCoreTests.setUpClass.__func__)
    setUp = core.SQLiteRuntimeCoreTests.setUp
    close_indices = core.SQLiteRuntimeCoreTests.close_indices
    migrate = core.SQLiteRuntimeCoreTests.migrate
    acquire = core.SQLiteRuntimeCoreTests.acquire
    def crash_copy(self, index):
        """Copy committed SQLite and durable external files, dropping RAM overlay."""
        destination = self.root / 'after_crash'
        shutil.copytree(self.cache, destination, ignore=shutil.ignore_patterns('*.writer.lock', '*-wal', '*-shm'))
        copied = destination / 'sentence_cache.sqlite'
        copied.unlink(missing_ok=True)
        source = sqlite3.connect(index.path)
        target = sqlite3.connect(copied)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def open_crash(self, destination):
        index = studio.acquire_sqlite_cache_index(destination)
        self.addCleanup(studio.release_sqlite_cache_index, index)
        return index

    def test_crash_after_audio_rename_before_commit_recovers_exact_metadata_once(self):
        self.migrate({})
        index = self.acquire()
        path = index.publish_audio('key', self.prepared, {'usage_count': 7, 'unknown': ['ёж']})
        restored_dir = self.crash_copy(index)
        restored = self.open_crash(restored_dir)
        self.assertEqual(restored.get('key')['usage_count'], 7)
        self.assertEqual(restored.get('key')['unknown'], ['ёж'])
        self.assertEqual((restored_dir / 'audio' / path.name).read_bytes(), self.audio)
        restored.recover_publications()
        self.assertEqual(restored.get('key')['usage_count'], 7)
        self.assertFalse(list((restored_dir / 'publication_intents').glob('*.json')))

    def test_crash_after_intent_before_rename_recovers_prepared_generation(self):
        self.migrate({})
        index = self.acquire()
        original_replace = os.replace
        def fail_audio_rename(source, target):
            if Path(source).name.startswith('.prepared_'):
                raise OSError('simulated rename failure')
            return original_replace(source, target)
        with mock.patch.object(studio.os, 'replace', side_effect=fail_audio_rename):
            with self.assertRaises(studio.CachePublicationError):
                index.publish_audio('key', self.prepared, {'usage_count': 4})
        restored = self.open_crash(self.crash_copy(index))
        self.assertEqual(restored.get('key')['usage_count'], 4)

    def test_multiple_uncommitted_generations_recover_in_predecessor_order(self):
        self.migrate({})
        index = self.acquire()
        index.publish_audio('key', self.prepared, {'version': 1, 'usage_count': 1})
        final = index.publish_audio('key', self.prepared, {'version': 2, 'usage_count': 1})
        restored = self.open_crash(self.crash_copy(index))
        self.assertEqual(restored.get('key')['version'], 2)
        self.assertEqual(restored.get('key')['file_name'], final.name)

    def test_clock_rollback_cannot_reverse_publication_predecessor_order(self):
        self.migrate({})
        index = self.acquire()
        with mock.patch.object(studio.time, 'time_ns', side_effect=[9000, 9000, 1000, 1000]):
            index.publish_audio('key', self.prepared, {'version': 1})
            final = index.publish_audio('key', self.prepared, {'version': 2})
        restored = self.open_crash(self.crash_copy(index))
        self.assertEqual(restored.get('key')['file_name'], final.name)
        self.assertEqual(restored.get('key')['version'], 2)

    def test_committed_generation_with_remaining_intent_does_not_reapply_usage(self):
        self.migrate({})
        index = self.acquire()
        index.publish_audio('key', self.prepared, {'usage_count': 2})
        with mock.patch.object(index, '_cleanup_committed_intents'):
            index.flush()
        restored = self.open_crash(self.crash_copy(index))
        self.assertEqual(restored.get('key')['usage_count'], 2)
        restored.touch('key', 100, increment=3)
        restored.flush()
        restored.recover_publications()
        self.assertEqual(restored.get('key')['usage_count'], 5)

    def test_corrupt_generation_is_diagnostic_and_never_a_miss(self):
        self.migrate({})
        index = self.acquire()
        path = index.publish_audio('key', self.prepared, {'usage_count': 1})
        destination = self.crash_copy(index)
        (destination / 'audio' / path.name).write_bytes(b'corrupt')
        restored = self.open_crash(destination)
        with self.assertRaises(studio.CacheRecoveryError):
            restored.get('key')
        self.assertTrue(restored.recovery_diagnostics)
        self.assertTrue(list((destination / 'publication_intents').glob('*.json')))

    def test_malformed_intent_blocks_unknown_key_to_prevent_paid_retry(self):
        self.migrate({})
        index = self.acquire()
        intents = self.cache / 'publication_intents'
        intents.mkdir()
        (intents / ('1_' + 'a' * 32 + '.json')).write_text('{broken', encoding='utf-8')
        restored = self.open_crash(self.crash_copy(index))
        with self.assertRaises(studio.CacheRecoveryError):
            restored.get('arbitrary')

    def test_force_new_preparation_failure_preserves_prior_pointer_and_audio(self):
        self.migrate({})
        index = self.acquire()
        original = index.publish_audio('key', self.prepared, {'version': 1})
        index.flush()
        damaged = self.root / 'bad.ogg'
        damaged.write_bytes(self.audio[:-5])
        with self.assertRaises(ValueError):
            index.publish_audio('key', damaged, {'version': 2})
        self.assertEqual(index.get('key')['file_name'], original.name)
        self.assertEqual(original.read_bytes(), self.audio)

    def test_deleted_key_tombstone_supersedes_old_receipt_after_crash(self):
        self.migrate({})
        index = self.acquire()
        index.publish_audio('key', self.prepared, {'version': 1})
        with mock.patch.object(index, '_cleanup_committed_intents'):
            index.flush()
            index.delete('key')
            index.flush()
        source = sqlite3.connect(index.path)
        try:
            version = source.execute('SELECT value FROM meta WHERE key=?', (index._version_key('key'),)).fetchone()
        finally:
            source.close()
        self.assertIsNotNone(version, 'retained publication receipt still needs the deletion supersession marker')
        restored = self.open_crash(self.crash_copy(index))
        self.assertIsNone(restored.get('key'))
        self.assertFalse(list((restored.cache_dir / 'publication_intents').glob('*.json')))

    def test_same_process_recovery_commits_pending_delete_before_discarding_receipt(self):
        self.migrate({})
        index = self.acquire()
        index.publish_audio('key', self.prepared, {'version': 1})
        index.delete('key')
        index.recover_publications()
        restored = self.open_crash(self.crash_copy(index))
        self.assertIsNone(restored.get('key'))

    def test_postcommit_corrupt_audio_blocks_key_in_same_process(self):
        self.migrate({})
        index = self.acquire()
        path = index.publish_audio('key', self.prepared, {'version': 1})
        path.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            index.flush()
        with self.assertRaises(studio.CacheRecoveryError):
            index.get('key')
        self.assertTrue(list((self.cache / 'publication_intents').glob('*.json')))


if __name__ == '__main__':
    unittest.main()
