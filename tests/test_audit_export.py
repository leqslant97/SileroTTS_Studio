import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_regressions import studio


class ExportCancellationTests(unittest.TestCase):
    def test_cancelled_remux_does_not_start_ffmpeg(self):
        process = mock.Mock(returncode=0, stderr=io.BytesIO())
        process.poll.return_value = 0
        with mock.patch.object(studio.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(InterruptedError):
                studio._run_ffmpeg_checked(["ffmpeg"], cancelled=lambda: True)
        popen.assert_not_called()

    def test_remux_detects_stop_at_process_completion(self):
        stopped = threading.Event()
        process = mock.Mock(returncode=0, stderr=io.BytesIO())

        def completed():
            stopped.set()
            return 0

        process.poll.side_effect = completed
        with mock.patch.object(studio.subprocess, "Popen", return_value=process):
            with self.assertRaises(InterruptedError):
                studio._run_ffmpeg_checked(
                    ["ffmpeg"], cancelled=stopped.is_set
                )

    def test_stopped_encoders_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sources = [folder / "chapter1.mp3", folder / "chapter2.mp3"]
            for source in sources:
                source.write_bytes(b"source")
            output = folder / "book.mp3"
            profile = {
                "sample_rate": 48000,
                "channels": 1,
                "channel_layout": "mono",
                "bitrate": "128k",
            }

            for copy_stream in (False, True):
                with self.subTest(copy_stream=copy_stream):
                    stopped = threading.Event()
                    output.write_bytes(b"previous complete output")
                    process = mock.Mock(returncode=0)

                    def completed():
                        stopped.set()
                        return 0

                    def start(command, **_kwargs):
                        Path(command[-1]).write_bytes(b"new output")
                        return process

                    process.poll.side_effect = completed
                    with (
                        mock.patch.object(studio.subprocess, "Popen", side_effect=start),
                        mock.patch.object(studio, "SESSION_TEMP_DIR", folder),
                        mock.patch.object(studio, "_select_merge_audio_profile", return_value=profile),
                    ):
                        with self.assertRaises(InterruptedError):
                            if copy_stream:
                                studio._run_ffmpeg_stream_copy_concat(
                                    sources, output, cancelled=stopped.is_set
                                )
                            else:
                                studio._export_merged_audio_ffmpeg(
                                    sources,
                                    output,
                                    output_format="mp3",
                                    cancelled=stopped.is_set,
                                    _probed_profiles=[None, None],
                                )
                    self.assertEqual(output.read_bytes(), b"previous complete output")
                    self.assertEqual(set(folder.iterdir()), {*sources, output})

    def test_m4b_stop_before_publication_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.mp3"
            source.write_bytes(b"source")
            encoded = folder / "prepared.m4a"
            encoded.write_bytes(b"prepared AAC")
            output = folder / ("a" * 180 + ".m4b")
            output.write_bytes(b"previous complete M4B")
            stopped = threading.Event()

            def remux(_command, *, out_path, **_kwargs):
                Path(out_path).write_bytes(b"new M4B")
                stopped.set()

            with mock.patch.object(studio, "_run_ffmpeg_checked", side_effect=remux):
                with self.assertRaises(InterruptedError):
                    studio._export_m4b_ffmpeg(
                        [source],
                        output,
                        chapters=[{"title": "Глава", "duration": 1}],
                        preencoded_audio=encoded,
                        cancelled=stopped.is_set,
                    )
            self.assertEqual(output.read_bytes(), b"previous complete M4B")
            self.assertEqual(set(folder.iterdir()), {source, encoded, output})


class InplaceMetadataAuditTests(unittest.TestCase):
    @staticmethod
    def make_app():
        app = object.__new__(studio.TTSApp)
        app.lbl_export_status = object()
        app._post_status_label = mock.Mock()
        return app

    @unittest.skipUnless(
        Path(studio.get_ffmpeg_path()).is_file()
        and Path(studio.get_ffprobe_path()).is_file(),
        "Для проверки необходимы FFmpeg и FFprobe",
    )
    def test_m4b_cover_and_tags_keep_chapters_and_volume_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.wav"
            cover = folder / "cover.png"
            output = folder / ("a" * 180 + ".m4b")
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=duration=0.05", str(source),
            ], check=True)
            subprocess.run([
                studio.get_ffmpeg_path(), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:s=32x32",
                "-frames:v", "1", str(cover),
            ], check=True)
            studio._export_m4b_ffmpeg(
                [source, source], output,
                chapters=[{"title": "Начало", "duration": .05},
                          {"title": "Конец", "duration": .05}],
                tags={"title": "Книга", "artist": "Старое имя"},
                disk_number=1, disk_total=3,
            )
            app = self.make_app()
            self.assertTrue(app._update_file_tags_inplace(
                output, {"title": "Новое название", "album": "Серия"},
                cover, "Книга",
            ))
            data = json.loads(subprocess.check_output([
                studio.get_ffprobe_path(), "-v", "error", "-show_format",
                "-show_streams", "-show_chapters", "-of", "json", str(output),
            ]))
            tags = data["format"]["tags"]
            self.assertEqual(tags["title"], "Новое название")
            self.assertEqual(tags["album"], "Серия")
            self.assertEqual(tags["disc"], "1/3")
            self.assertEqual(tags["track"], "1/3")
            self.assertNotIn("artist", tags)
            self.assertEqual([chapter["tags"]["title"] for chapter in data["chapters"]], ["Начало", "Конец"])
            covers = [stream for stream in data["streams"] if stream.get("disposition", {}).get("attached_pic")]
            self.assertEqual(len(covers), 1)
            self.assertEqual(covers[0]["width"], 32)

    def test_failed_xiph_cover_update_removes_temporary_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "chapter.opus"
            cover = folder / "cover.png"
            source.write_bytes(b"original audio")
            cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 524288)

            def fail(command, **_kwargs):
                self.assertLess(sum(len(str(arg)) for arg in command), 32768)
                metadata_path = Path(command[command.index("ffmetadata") + 2])
                self.assertGreater(metadata_path.stat().st_size, 524288)
                Path(command[-1]).write_bytes(b"partial output")
                return subprocess.CompletedProcess(command, 1, stderr=b"encoder error")

            app = self.make_app()
            with (
                mock.patch.object(studio, "SESSION_TEMP_DIR", folder),
                mock.patch.object(studio.subprocess, "run", side_effect=fail),
                self.assertLogs(level="ERROR"),
            ):
                self.assertFalse(app._update_file_tags_inplace(
                    source, {"title": "Глава"}, cover, "Глава"
                ))
            self.assertEqual(source.read_bytes(), b"original audio")
            self.assertEqual(set(folder.iterdir()), {source, cover})


class M4BTemplateConsistencyTests(unittest.TestCase):
    def test_source_metadata_filename_uses_physical_text_name(self):
        context = studio.source_m4b_metadata_context(
            {"input_dir": "/books"},
            {"group_name": "Том", "path": "/exports/Сборник.m4b",
             "source_paths": ("/books/001 Начало.txt", "/books/002 Продолжение.txt")},
        )
        self.assertEqual(context["filename"], "001 Начало")

    def test_export_fallback_uses_first_file_of_each_part(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            groups = {"book": {"name": "Книга", "merge": True}}
            children = {"book": ("a", "b")}
            files = {
                "a": {"path": str(folder / "Первый.mp3"), "title": "Метка 1", "duration": 1},
                "b": {"path": str(folder / "Второй.mp3"), "title": "Метка 2", "duration": 1},
            }
            target = studio.OutputTarget(
                format="m4b", filename_template="{filename}", max_chapters=1,
            ).to_dict()
            planned = studio.plan_export_target_paths(
                ("book",), children, groups, files, (target,), folder / "out"
            )
            paths = []
            app = object.__new__(studio.TTSApp)
            app.config = {}
            with mock.patch.object(
                studio, "_export_m4b_ffmpeg",
                side_effect=lambda _sources, output, **_kwargs: paths.append(output),
            ):
                app._run_output_target_set(
                    ("book",), children, groups, files, (target,), folder / "out",
                    _reuse_planned_records=True,
                )
            self.assertEqual(paths, [record["path"] for record in planned])
            self.assertTrue(paths[1].name.startswith("Второй"))


if __name__ == "__main__":
    unittest.main()
