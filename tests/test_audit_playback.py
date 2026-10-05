"""Поздняя подготовка звука не отменяет последующую остановку или новое воспроизведение."""

import importlib.util
import sys
import tempfile
import threading
import time
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


class PlaybackCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_dir = Path(temporary.name)
        self.app = object.__new__(studio.TTSApp)
        self.app._playback_lock = threading.Lock()
        self.app._playback_generation = 0
        self.app.current_playback_process = None

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("Playback worker did not reach the expected state")

    def make_closing_app(self):
        app = self.app
        app.root = mock.Mock()
        app._is_closing = False
        app.batch_processor = None
        app.direct_processor = None
        app.is_cache_operation_running = mock.Mock(return_value=False)
        app._import_running = False
        app._export_lock = False
        app._export_running = False
        app._normalizer_batch_running = False
        app._glossary_dirty = False
        app.save_settings = mock.Mock(return_value=True)
        app._save_export_project_session = mock.Mock(return_value=True)
        app._save_source_session = mock.Mock(return_value=True)
        app._discard_last_direct_preview = mock.Mock()
        app._show_error = mock.Mock()
        app.stop_audio_playback = mock.Mock()
        return app

    def test_stop_during_wav_export_prevents_player_start(self):
        exporting = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def export(path, *, format):
            self.assertEqual(format, "wav")
            Path(path).write_bytes(b"wav")
            exporting.set()
            release.wait(3)

        segment = mock.Mock()
        segment.export.side_effect = export
        with (
            mock.patch.object(studio, "SESSION_TEMP_DIR", self.temp_dir),
            mock.patch.object(studio.platform, "system", return_value="Darwin"),
            mock.patch.object(studio.subprocess, "Popen") as popen,
        ):
            self.app.play_audio_segment(segment)
            self.assertTrue(exporting.wait(3))
            self.app.stop_audio_playback()
            release.set()
            self.wait_for(lambda: not list(self.temp_dir.glob("playback_*.wav")))
            popen.assert_not_called()

    def test_old_export_cannot_override_new_playback(self):
        first_exporting = threading.Event()
        release_first = threading.Event()
        self.addCleanup(release_first.set)

        def slow_export(path, *, format):
            Path(path).write_bytes(b"old")
            first_exporting.set()
            release_first.wait(3)

        def fast_export(path, *, format):
            Path(path).write_bytes(b"new")

        first = mock.Mock()
        first.export.side_effect = slow_export
        second = mock.Mock()
        second.export.side_effect = fast_export
        with (
            mock.patch.object(studio, "SESSION_TEMP_DIR", self.temp_dir),
            mock.patch.object(studio.platform, "system", return_value="Darwin"),
            mock.patch.object(studio.subprocess, "Popen") as popen,
        ):
            self.app.play_audio_segment(first)
            self.assertTrue(first_exporting.wait(3))
            self.app.play_audio_segment(second)
            self.wait_for(lambda: popen.call_count == 1)
            release_first.set()
            self.wait_for(lambda: not list(self.temp_dir.glob("playback_*.wav")))
            self.assertEqual(popen.call_count, 1)

    def test_stop_terminates_active_player(self):
        release = threading.Event()
        self.addCleanup(release.set)
        segment = mock.Mock()
        segment.export.side_effect = lambda path, **_kwargs: Path(path).write_bytes(
            b"wav"
        )
        player = mock.Mock()
        player.wait.side_effect = lambda: release.wait(3)
        player.terminate.side_effect = release.set
        with (
            mock.patch.object(studio, "SESSION_TEMP_DIR", self.temp_dir),
            mock.patch.object(studio.platform, "system", return_value="Darwin"),
            mock.patch.object(studio.subprocess, "Popen", return_value=player),
        ):
            self.app.play_audio_segment(segment)
            self.wait_for(lambda: self.app.current_playback_process is player)
            self.app.stop_audio_playback()
            self.wait_for(lambda: not list(self.temp_dir.glob("playback_*.wav")))
            player.terminate.assert_called_once_with()
            self.assertIsNone(self.app.current_playback_process)

    def test_windows_stop_purges_sound(self):
        with (
            mock.patch.object(studio.platform, "system", return_value="Windows"),
            mock.patch.object(studio, "winsound", create=True) as sound,
        ):
            generation = self.app.stop_audio_playback()
        self.assertEqual(generation, 1)
        sound.PlaySound.assert_called_once_with(None, sound.SND_PURGE)

    def test_close_stops_audio_only_after_successful_save(self):
        app = self.make_closing_app()
        app.save_settings.return_value = False
        app.on_closing()
        app.stop_audio_playback.assert_not_called()
        app.root.destroy.assert_not_called()

        app.save_settings.return_value = True
        app.on_closing()
        app.stop_audio_playback.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_stop_during_direct_audio_decode_prevents_wav_export(self):
        source = self.temp_dir / "direct.ogg"
        source.write_bytes(b"audio")
        decoding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        segment = mock.Mock()

        def decode(_path):
            decoding.set()
            release.wait(3)
            return segment

        self.app.last_direct_audio = str(source)
        self.app.last_direct_audio_has_effects = True
        for name in (
            "dir_speed_var", "dir_pitch_var", "dir_echo_var",
            "dir_echo_delay_var", "dir_echo_decay_var",
        ):
            setattr(self.app, name, mock.Mock())
        handed_off = threading.Event()
        original_play = self.app.play_audio_segment

        def play_after_decode(audio_segment, **kwargs):
            try:
                return original_play(audio_segment, **kwargs)
            finally:
                handed_off.set()

        with (
            mock.patch.object(studio, "_load_audio_segment", side_effect=decode),
            mock.patch.object(studio.platform, "system", return_value="Darwin"),
            mock.patch.object(studio, "SESSION_TEMP_DIR", self.temp_dir),
            mock.patch.object(studio.subprocess, "Popen") as popen,
            mock.patch.object(self.app, "play_audio_segment", side_effect=play_after_decode),
        ):
            self.app.play_last_audio()
            self.assertTrue(decoding.wait(3))
            self.app.stop_audio_playback()
            release.set()
            self.assertTrue(handed_off.wait(3))
            segment.export.assert_not_called()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
