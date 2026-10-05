import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class ExportTagCancellationTests(unittest.TestCase):
    @staticmethod
    def make_app():
        app = object.__new__(studio.TTSApp)
        app.lbl_export_status = object()
        app._post_status_label = mock.Mock()
        return app

    def test_stop_before_ffmpeg_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"original")
            stopped = threading.Event()
            stopped.set()
            app = self.make_app()
            with mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=InterruptedError
            ) as run:
                with self.assertRaises(InterruptedError):
                    app._update_file_tags_inplace(
                        source, {"title": "New"}, None, "Chapter",
                        cancelled=stopped.is_set,
                    )
            self.assertEqual(run.call_args.kwargs["cancelled"](), True)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_stop_after_ffmpeg_keeps_source_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"original")
            stopped = threading.Event()

            def finish_encoding(command, *, out_path, **_kwargs):
                Path(out_path).write_bytes(b"updated")
                stopped.set()

            app = self.make_app()
            with mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=finish_encoding
            ):
                with self.assertRaises(InterruptedError):
                    app._update_file_tags_inplace(
                        source, {"title": "New"}, None, "Chapter",
                        cancelled=stopped.is_set,
                    )
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_completed_ffmpeg_publishes_new_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"original")

            def finish_encoding(command, *, out_path, **_kwargs):
                Path(out_path).write_bytes(b"updated")

            app = self.make_app()
            with mock.patch.object(
                studio, "_run_ffmpeg_checked", side_effect=finish_encoding
            ) as run:
                self.assertTrue(app._update_file_tags_inplace(
                    source, {"title": "New"}, None, "Chapter",
                    cancelled=lambda: False,
                ))
            self.assertEqual(run.call_args.kwargs["out_path"].suffix, ".mp3")
            self.assertEqual(source.read_bytes(), b"updated")
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_tags_only_resets_progress_scale_after_target_set(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"source")
            app = self.make_app()
            app.export_tree = mock.Mock()
            app.export_tree.get_children.return_value = ("file",)
            app.export_files = {"file": {"path": str(source), "title": "Chapter"}}
            app.export_groups = {}
            app.export_targets = []
            app.export_tags_only_var = mock.Mock(get=lambda: True)
            app.export_outdir_var = mock.Mock(get=lambda: "")
            app.export_apply_fx_var = mock.Mock(get=lambda: False)
            app.export_fmt_var = mock.Mock(get=lambda: "mp3")
            app.export_bitrate_var = mock.Mock(get=lambda: "auto")
            app.export_sample_rate_var = mock.Mock(get=lambda: "auto")
            app.export_channels_var = mock.Mock(get=lambda: "auto")
            app.export_progress = mock.Mock()
            app.config = {}
            app.save_settings = mock.Mock()
            app._set_export_running_state = mock.Mock()
            app._set_export_progress_indeterminate = mock.Mock()
            app._show_error = mock.Mock()
            app._show_warning = mock.Mock()

            with mock.patch.object(threading.Thread, "start"):
                app.start_export_process()

            app.export_progress.configure.assert_called_once_with(
                maximum=100, value=0
            )
            app._show_error.assert_not_called()
            app._show_warning.assert_not_called()

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "FFmpeg and FFprobe are required",
    )
    def test_real_ffmpeg_updates_tags_with_cancellation_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=duration=0.1",
                "-c:a", "libmp3lame", str(source),
            ], check=True)
            app = self.make_app()
            self.assertTrue(app._update_file_tags_inplace(
                source, {"title": "Updated chapter"}, None, "Chapter",
                cancelled=lambda: False,
            ))
            data = json.loads(subprocess.check_output([
                studio.get_ffprobe_path(), "-v", "error",
                "-show_format", "-of", "json", str(source),
            ]))
            self.assertEqual(data["format"]["tags"]["title"], "Updated chapter")
            self.assertEqual(list(Path(directory).iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
