"""Метки помощника совпадают с действительным контекстом экспортёра M4B."""
import copy
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    _module_directory = tempfile.TemporaryDirectory()
    _module_file = Path(_module_directory.name) / "SileroTTS_Studio.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py", _module_file)
    _previous_directory = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, _module_file)
        studio = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = studio
        spec.loader.exec_module(studio)
    finally:
        os.chdir(_previous_directory)
else:
    studio = sys.modules[MODULE_NAME]


class PreviewTree:
    def __init__(self, roots, children):
        self.roots = tuple(roots)
        self.children = children

    def get_children(self, item=""):
        return self.roots if not item else self.children.get(item, ())

    def selection(self):
        return ()


class ExportChapterPreviewContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.app = studio.TTSApp.__new__(studio.TTSApp)
        self.app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.app.config["export_dir"] = str(self.base / "output")
        self.app.export_groups = {}
        self.app.export_files = {}
        self.children = {}
        self.template = "{book}|{group}|{name}|{source_name}|{filename}|{author}|{global_index}/{chapter_count}"

    def add_group(self, name, *, artist="", album="Серия", file_artist="Первый исполнитель"):
        group_id = f"g{len(self.app.export_groups) + 1}"
        self.app.export_groups[group_id] = {
            "name": name, "merge": True, "pause": 2000, "artist": artist, "album": album,
        }
        members = []
        for number in range(1, 3):
            file_id = f"{group_id}-file{number}"
            members.append(file_id)
            self.app.export_files[file_id] = {
                "path": str(self.base / f"Физическое имя {group_id}-{number}.wav"),
                "title": f"Изменённый Title {number}", "source_title": f"Исходный Title {number}",
                "source_name": f"Исходное имя {number}", "artist": file_artist, "album": album,
                "duration": 1.25,
            }
        self.children[group_id] = tuple(members)
        return group_id

    def assert_preview_matches_runtime(self, target=None):
        target = dict(target or {"format": "m4b", "max_chapters": 1})
        target["chapter_title_template"] = self.template
        roots = tuple(self.app.export_groups) + tuple(
            key for key in self.app.export_files if all(key not in members for members in self.children.values())
        )
        self.app.export_tree = PreviewTree(roots, self.children)
        before = copy.deepcopy((self.app.export_groups, self.app.export_files, target))
        snapshot = self.app._template_helper_export_preview_data("m4b", target=target)
        quick = studio.render_template_helper_preview("m4b_chapter", self.template, preview_data=snapshot)
        full = studio.template_helper_full_preview_rows("m4b_chapter", self.template, snapshot)
        captured = []
        with mock.patch.object(studio, "_export_m4b_ffmpeg", side_effect=lambda _sources, _path, **options: captured.extend(copy.deepcopy(options["chapters"]))):
            self.app._run_output_target_set(
                roots, self.children, self.app.export_groups, self.app.export_files,
                [target], str(self.base / "output"),
            )
        actual = tuple(chapter["title"] for chapter in captured)
        self.assertEqual(quick, actual[:3])
        self.assertEqual(tuple(row["result"] for row in full), actual)
        self.assertEqual((self.app.export_groups, self.app.export_files, target), before)
        self.assertEqual(list((self.base / "output").glob("*.m4b")), [])
        return snapshot, actual

    def test_group_suffix_is_removed_with_case_and_repeated_extensions(self):
        for name in ("Название.m4b", "Название.M4B.m4b", "Название.mp3"):
            with self.subTest(name=name):
                self.app.export_groups.clear()
                self.app.export_files.clear()
                self.children.clear()
                self.add_group(name, artist="Автор")
                _data, actual = self.assert_preview_matches_runtime()
                expected = "Название.mp3" if name.endswith(".mp3") else "Название"
                self.assertTrue(all(title.startswith(expected + "|" + expected + "|") for title in actual))

    def test_unusual_group_names_follow_runtime_sanitization_and_length(self):
        for name in ("  :<>/\\  ", "CON", "Том|Один.m4b", "Т" * 260 + ".m4b", "", ".m4b"):
            with self.subTest(name=name):
                self.app.export_groups.clear()
                self.app.export_files.clear()
                self.children.clear()
                self.add_group(name)
                self.assert_preview_matches_runtime()

    def test_album_and_author_inheritance_keep_runtime_sequence_and_template_values(self):
        self.add_group("Том 1.m4b", artist="", album="")
        self.add_group("Том 2.m4b", artist="Автор группы", album="Серия")
        self.add_group("Другая книга.m4b", artist="Другой автор", album="Другая серия")
        for file_id in self.children["g1"]:
            self.app.export_files[file_id]["album"] = "Серия"
            self.app.export_files[file_id]["album_artist"] = "Автор альбома"
        self.assert_preview_matches_runtime()

    def test_standalone_title_filename_and_source_aliases_follow_runtime(self):
        self.app.export_files["solo"] = {
            "path": str(self.base / "Физический файл.wav"), "title": "  Новое: название.m4b  ",
            "source_title": "Старый заголовок", "source_name": "Старое имя",
            "artist": "Автор файла", "duration": 2.5,
        }
        _data, actual = self.assert_preview_matches_runtime({"format": "m4b"})
        self.assertEqual(len(actual), 1)
        self.assertIn("Автор файла", actual[0])
        self.assertIn("Физический файл", actual[0])
        self.assertIn("Старое имя", actual[0])


if __name__ == "__main__":
    unittest.main()
