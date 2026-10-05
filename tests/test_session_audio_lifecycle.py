"""Временная речь сохраняется для продолжения и освобождается при смене книги."""

import copy
import unittest

import test_close_confirmation as closing
import test_regressions as regressions
import test_source_session as source_session


studio = closing.studio


class SessionAudioLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case = source_session.SourceSessionTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = self.case.app({"use_cache": False})
        self.store = studio.SessionAudioStore(self.case.root / "speech")
        self.app._session_audio_store = self.store
        self.app._session_audio_scopes = {}

    def publish(self, *, kind="source", text=None):
        store, scope = self.app._prepare_session_audio(self.app.config, kind, text)
        prepared = self.case.root / "prepared.ogg"
        prepared.write_bytes(regressions.fake_ogg_first_page(b"OpusHead-session"))
        key = (text or "Проверочная речь.", *studio.SessionAudioStore.config_key(self.app.config))
        return store.publish(key, prepared, scope), key, scope

    def test_refresh_and_repartition_keep_the_same_speech(self):
        path, key, scope = self.publish()
        self.case.group_all_files(self.app, reverse=True)
        self.app.load_files()
        store, current = self.app._prepare_session_audio(self.app.config, "source")
        self.assertEqual(scope, current)
        self.assertEqual(store.get(key, current), path)
        self.assertTrue(path.exists())

    def test_output_formats_tags_and_effects_do_not_invalidate_speech(self):
        path, key, scope = self.publish()
        self.app.config.update(output_format="opus", tag_title="Другая часть", fx_speed=1.25)
        store, current = self.app._prepare_session_audio(self.app.config, "source")
        self.assertEqual(scope, current)
        self.assertEqual(store.get(key, current), path)

    def test_refresh_preserves_prepared_text_until_the_next_run_selects_its_mode(self):
        self.app.config["text_is_prepared"] = True
        path, key, scope = self.publish()
        self.app.config.pop("text_is_prepared")
        self.app.load_files()
        self.assertTrue(path.exists())
        store, current = self.app._prepare_session_audio(
            dict(self.app.config, text_is_prepared=True), "source",
        )
        self.assertEqual(current, scope)
        self.assertEqual(store.get(key, current), path)
        self.app._prepare_session_audio(self.app.config, "source")
        self.assertFalse(path.exists())

    def test_changing_input_directory_removes_old_speech(self):
        path, _key, _scope = self.publish()
        other = self.case.root / "other"
        other.mkdir()
        (other / "01.txt").write_text("Другая книга.", encoding="utf-8")
        self.app.config["input_dir"] = str(other)
        self.app.load_files()
        self.assertFalse(path.exists())
        self.assertNotIn("source", self.app._session_audio_scopes)

    def test_changed_or_removed_txt_releases_the_previous_context(self):
        for action in ("change", "remove"):
            with self.subTest(action=action):
                path, _key, _scope = self.publish()
                source = self.case.source / "01.txt"
                if action == "change":
                    source.write_text("Изменённая глава, другой размер.", encoding="utf-8")
                else:
                    source.unlink()
                self.app.load_files()
                self.assertFalse(path.exists())

    def test_voice_steps_and_normalization_changes_release_previous_speech(self):
        for updates in (
            {"speaker": "другой голос"},
            {"api_steps_enabled": True, "api_steps": 16},
            {"normalizer_enabled": not self.app.config["normalizer_enabled"]},
        ):
            with self.subTest(updates=updates):
                path, _key, scope = self.publish()
                self.app.config.update(updates)
                _store, current = self.app._prepare_session_audio(self.app.config, "source")
                self.assertNotEqual(current, scope)
                self.assertFalse(path.exists())

    def test_enabling_persistent_cache_releases_session_speech(self):
        path, _key, _scope = self.publish()
        self.app.config["use_cache"] = True
        self.assertEqual(self.app._prepare_session_audio(self.app.config, "source"), (None, None))
        self.assertFalse(path.exists())

    def test_changing_direct_text_keeps_source_speech_and_releases_old_direct_text(self):
        source_path, _key, source_scope = self.publish()
        direct_path, _key, direct_scope = self.publish(kind="direct", text="Первый текст.")
        self.app._prepare_session_audio(self.app.config, "direct", "Второй текст.")
        self.assertFalse(direct_path.exists())
        self.assertTrue(source_path.exists())
        self.assertEqual(self.app._session_audio_scopes["source"], source_scope)
        self.assertNotEqual(self.app._session_audio_scopes["direct"], direct_scope)

    def test_successful_context_cleanup_preserves_the_other_owner(self):
        path, key, scope = self.publish()
        _store, direct_scope = self.app._prepare_session_audio(self.app.config, "direct", "Текст.")
        self.assertEqual(self.store.get(key, direct_scope), path)
        processor = type("Processor", (), {"session_audio_store": self.store, "session_audio_scope": scope})()
        self.app._discard_processor_session_audio(processor)
        self.assertTrue(path.exists())
        self.app._discard_session_audio("direct")
        self.assertFalse(path.exists())

    def test_source_change_defers_deletion_until_its_readers_finish(self):
        path, _key, scope = self.publish()
        self.store.acquire(scope)
        self.app.config["speaker"] = "другой голос"
        self.app._prepare_session_audio(self.app.config, "source")
        self.assertTrue(path.exists())
        self.store.release(scope)
        self.assertFalse(path.exists())

    def test_close_failure_preserves_session_until_close_succeeds(self):
        path, _key, _scope = self.publish()
        app = closing.CloseConfirmationTests("runTest").make_app()
        app._session_audio_store = self.store
        app._session_audio_scopes = copy.copy(self.app._session_audio_scopes)
        app.save_settings.return_value = False
        app._complete_application_close()
        self.assertTrue(path.exists())
        app.root.destroy.assert_not_called()
        app.save_settings.return_value = True
        app._complete_application_close()
        self.assertFalse(path.exists())
        app.root.destroy.assert_called_once()
        self.assertEqual(app._session_audio_scopes, {})


if __name__ == "__main__":
    unittest.main()
