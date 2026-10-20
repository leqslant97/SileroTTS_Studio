"""Real Ogg publication durability boundaries and shared-file verification."""

import json
from pathlib import Path
from unittest import mock
import unittest

from tests import test_sqlite_cache_runtime_core as core

studio = core.studio


class SQLitePublicationEdgeTests(unittest.TestCase):
    setUpClass = classmethod(core.SQLiteRuntimeCoreTests.setUpClass.__func__)
    setUp = core.SQLiteRuntimeCoreTests.setUp
    close_indices = core.SQLiteRuntimeCoreTests.close_indices
    migrate = core.SQLiteRuntimeCoreTests.migrate
    acquire = core.SQLiteRuntimeCoreTests.acquire

    def test_intent_directory_fsync_failure_retains_prepared_audio_for_local_recovery(self):
        self.migrate({})
        index = self.acquire()
        real_fsync = studio._fsync_cache_directory
        failed = [False]

        def fail_once(path):
            if Path(path).resolve() == (self.cache / "publication_intents").resolve() and not failed[0]:
                failed[0] = True
                raise OSError("intent directory durability failed")
            return real_fsync(path)

        with mock.patch.object(studio, "_fsync_cache_directory", side_effect=fail_once):
            with self.assertRaises(studio.CachePublicationError) as error:
                index.publish_audio("key", self.prepared, {"usage_count": 7, "unknown": "Ёж"})
        self.assertEqual(error.exception.key, "key")
        receipts = list((self.cache / "publication_intents").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        intent = json.loads(receipts[0].read_text())
        prepared = self.cache / "audio" / intent["prepared_name"]
        self.assertEqual(prepared.read_bytes(), self.audio)
        self.assertEqual(index.recover_publications(), [])
        self.assertEqual(index.get("key")["usage_count"], 7)
        self.assertEqual(index.get("key")["unknown"], "Ёж")
        result = self.cache / "audio" / index.get("key")["file_name"]
        self.assertEqual(result.read_bytes(), self.audio)
        self.assertFalse(prepared.exists())
        self.assertFalse(list((self.cache / "publication_intents").glob("*.json")))

    def test_failure_before_intent_publication_does_not_keep_prepared_or_modify_previous_audio(self):
        self.migrate({})
        index = self.acquire()
        previous = index.publish_audio("key", self.prepared, {"version": 1})
        index.flush()
        with mock.patch.object(studio, "_write_cache_json_durable", side_effect=OSError("receipt write failed")):
            with self.assertRaisesRegex(OSError, "receipt write failed") as error:
                index.publish_audio("key", self.prepared, {"version": 2})
        self.assertNotIsInstance(error.exception, studio.CachePublicationError)
        self.assertEqual(index.get("key")["file_name"], previous.name)
        self.assertEqual(previous.read_bytes(), self.audio)
        self.assertFalse(list((self.cache / "audio").glob(".prepared_*.ogg")))

    def test_pending_predecessor_is_read_once_per_publication(self):
        self.migrate({})
        index = self.acquire()
        first = index.publish_audio("key", self.prepared, {"version": 1})
        with mock.patch.object(index, "_get_record", wraps=index._get_record) as read:
            second = index.publish_audio("key", self.prepared, {"version": 2})
        self.assertEqual(read.call_count, 1)
        receipt = next(json.loads(path.read_text()) for path in
                       (self.cache / "publication_intents").glob("*.json")
                       if json.loads(path.read_text())["file_name"] == second.name)
        self.assertEqual(receipt["previous_file_name"], first.name)
        index.flush()

    def test_shared_file_replacement_hashes_generation_once_before_bounded_cleanup(self):
        name = "a" * 32 + ".ogg"
        (self.cache / "audio" / name).write_bytes(self.audio)
        self.migrate({f"key{number}": {"file_name": name, "unknown": number}
                      for number in range(8)})
        index = self.acquire()
        with mock.patch.object(studio, "_cache_file_sha256", wraps=studio._cache_file_sha256) as digest:
            published = index.replace_audio_file(name, self.prepared, "opus")
        generation_reads = sum(Path(call.args[0]) == published for call in digest.call_args_list)
        self.assertLessEqual(generation_reads, 2)
        for number in range(8):
            metadata = index.get(f"key{number}")
            self.assertEqual(metadata["file_name"], published.name)
            self.assertEqual(metadata["unknown"], number)
            self.assertEqual(metadata["audio_codec"], "opus")
        self.assertEqual(published.read_bytes(), self.audio)
        self.assertFalse(list((self.cache / "publication_intents").glob("*.json")))

    def test_cleanup_rechecks_shared_generation_after_file_mutation(self):
        self._assert_cleanup_rechecks_shared_generation_after_file_mutation(reverse=False)

    def test_cleanup_rechecks_shared_generation_after_file_mutation_reverse_receipt_order(self):
        self._assert_cleanup_rechecks_shared_generation_after_file_mutation(reverse=True)

    def _assert_cleanup_rechecks_shared_generation_after_file_mutation(self, *, reverse):
        name = "a" * 32 + ".ogg"
        (self.cache / "audio" / name).write_bytes(self.audio)
        self.migrate({"a": {"file_name": name}, "b": {"file_name": name}})
        index = self.acquire()
        real_checked = index._checked_intent_audio
        calls = [0]
        checked_keys = []

        def checked_then_changed(intent):
            checked_keys.append(intent["key"])
            path = real_checked(intent)
            calls[0] += 1
            if calls[0] == 1:
                # A changed inode/statistic must not reuse the checked identity.
                path.write_bytes(b"corrupt")
            return path

        def ordered_intents():
            return iter(sorted((self.cache / "publication_intents").glob("*.json"), reverse=reverse))

        with mock.patch.object(index, "_checked_intent_audio", side_effect=checked_then_changed), \
                mock.patch.object(index, "_intent_paths", side_effect=ordered_intents):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                index.replace_audio_file(name, self.prepared, "opus")
        self.assertEqual(calls[0], 1)  # The second check rejects the changed file.
        self.assertEqual(len(checked_keys), 2)
        self.assertEqual(checked_keys, ["b", "a"] if reverse else ["a", "b"])
        self.assertTrue(list((self.cache / "publication_intents").glob("*.json")))
        with self.assertRaises(studio.CacheRecoveryError):
            # scandir does not guarantee receipt order: the rejected receipt's
            # key is blocked, whichever shared reference was checked first.
            index.get(checked_keys[-1])


if __name__ == "__main__":
    unittest.main()
