"""Проверки больших книг и смены настроек долгого предпросмотра."""

import copy
import importlib.util
import json
import queue
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


class LargeBookImportTests(unittest.TestCase):
    def test_nested_blocks_keep_inline_text_and_explicit_line_breaks(self):
        html = (
            "<body><div>Перед <em>связным</em> текстом<br>После переноса"
            "<p>Вложенный <strong>абзац</strong>.</p>Хвост</div>"
            "<hr><p>Следующий\nабзац.</p><!-- скрытый комментарий --></body>"
        )
        text = studio.BookExtractor._clean_html_text(
            studio.BeautifulSoup(html, "html.parser"), ["div", "p"]
        )
        self.assertEqual(text, (
            "Перед связным текстом\nПосле переноса Вложенный абзац .\n"
            "Хвост\nСледующий абзац."
        ))

    def test_many_fb2_paragraphs_keep_order_without_losing_empty_line_breaks(self):
        xml = "<section>" + "".join(
            f"<p>Абзац {index}.</p><empty-line/>" for index in range(2000)
        ) + "</section>"
        text = studio.BookExtractor._clean_html_text(
            studio.BeautifulSoup(xml, "xml"), ["p", "empty-line"]
        )
        self.assertEqual(text.splitlines(), [f"Абзац {i}." for i in range(2000)])

    def test_large_epub_uses_manifest_once_and_preserves_spine_and_toc(self):
        book = studio.epub.EpubBook()
        items = []
        for index in range(300):
            item = studio.epub.EpubHtml(
                uid=f"chapter-{index}", file_name=f"chapter-{index}.xhtml"
            )
            item.content = f"<p>Текст {index}.</p>"
            book.add_item(item)
            items.append(item)
        book.toc = tuple(
            studio.epub.Link(item.file_name, f"Глава {i}", item.id)
            for i, item in enumerate(items)
        )
        book.spine = [(item.id, "yes") for item in reversed(items)]
        with mock.patch.object(studio.epub, "read_epub", return_value=book), \
                mock.patch.object(book, "get_items", wraps=book.get_items) as manifest:
            chapters, author = studio.BookExtractor.extract_epub("fixture.epub")
        self.assertEqual(author, "")
        self.assertEqual(chapters, [
            (f"Глава {i}", f"Текст {i}.") for i in reversed(range(300))
        ])
        self.assertLessEqual(manifest.call_count, 2)

    def test_repeated_chapter_titles_use_linear_number_of_name_checks(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(studio, "_write_text_atomic"), \
                mock.patch.object(studio.logging, "info"), \
                mock.patch.object(
                    studio, "_book_import_name_identity",
                    wraps=studio._book_import_name_identity,
                ) as identity:
            names = studio.BookExtractor.save_chapters(
                [("Глава", "Текст")] * 2000,
                directory, "Книга.epub", "{title}",
            )
        self.assertEqual(names, ["Глава.txt"] + [
            f"Глава ({i}).txt" for i in range(2, 2001)
        ])
        self.assertLessEqual(identity.call_count, 10000)

    def test_collisions_fill_holes_and_preserve_case_and_long_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("Глава.txt", "Глава (3).txt", "Глава (5).txt"):
                (output / name).write_text("Старый текст", encoding="utf-8")
            names = studio.BookExtractor.save_chapters(
                [("Глава", "Новый текст"), ("глава", "Далее"), ("Глава", "Конец")],
                directory, "Книга.epub", "{title}",
            )
            self.assertEqual(names, ["Глава (2).txt", "глава (4).txt", "Глава (6).txt"])
            self.assertEqual((output / "Глава.txt").read_text(), "Старый текст")

            title = "Очень длинная глава " * 20
            names = studio.BookExtractor.save_chapters(
                [(title, "Текст")] * 12, directory, "Книга.epub", "{title}",
            )
            self.assertEqual(len(set(names)), 12)
            self.assertTrue(names[-1].endswith(" (12).txt"))
            self.assertTrue(all(len(Path(name).stem) <= 180 for name in names))

    def test_long_unicode_equivalent_bases_keep_independent_suffix_candidates(self):
        prefix = "x" * 175
        with tempfile.TemporaryDirectory() as directory:
            names = studio.BookExtractor.save_chapters(
                [(title, "Текст") for title in ("ß", "ß", "ss", "ß", "ss")],
                directory, "Книга.epub", prefix + "{title}",
            )
        self.assertEqual(names, [
            prefix + "ß.txt", prefix + "ß (2).txt", prefix + "s (2).txt",
            prefix + "ß (3).txt", prefix + "s (3).txt",
        ])


class NormalizerLatestPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(studio.TTSApp)
        self.app.root = mock.Mock()
        self.app.normalizer_source_text = mock.Mock()
        self.app.normalizer_result_text = mock.Mock()
        self.app.btn_save_normalized_text = mock.Mock()
        self.app.lbl_normalizer_preview_status = mock.Mock()
        self.app._set_status_label = mock.Mock()
        self.app._normalizer_preview_generation = 0
        self.app._normalizer_preview_after_id = None
        self.app._normalizer_last_preview_result = None
        self.app._collect_normalizer_preview_config = mock.Mock()
        self.app._normalizer_glossary_snapshot = mock.Mock()
        self.callbacks = queue.Queue()
        self.app._post_to_ui = lambda callback, *args: self.callbacks.put((callback, args))
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def request(self, source, setting="A", glossary="Словарь A"):
        self.app.normalizer_source_text.get.return_value = source
        self.app._collect_normalizer_preview_config.return_value = {
            "setting": setting, "glossary_enabled": True,
        }
        self.app._normalizer_glossary_snapshot.return_value = {"term": glossary}
        return self.app.run_normalizer_preview()

    def factory(self, config, glossary):
        with self.lock:
            self.calls.append((copy.deepcopy(config), copy.deepcopy(glossary)))
            first = len(self.calls) == 1
        context = mock.Mock()

        def preview(source):
            with self.lock:
                self.active += 1
                self.max_active = max(self.active, self.max_active)
            try:
                if first:
                    self.started.set()
                    if not self.release.wait(3):
                        raise RuntimeError("Test preview was not released")
                return {
                    "normalized_text": f"{source}:{config['setting']}:{glossary['term']}",
                    "normalized_text_for_file": source,
                    "sentence_count": 1, "skipped": [],
                }
            finally:
                with self.lock:
                    self.active -= 1

        context.preview_normalization.side_effect = preview
        return context

    def pump_until_idle(self):
        deadline = time.monotonic() + 3
        while getattr(self.app, "_normalizer_preview_worker_running", False):
            callback, args = self.callbacks.get(timeout=max(0.01, deadline - time.monotonic()))
            callback(*args)
            if time.monotonic() > deadline:
                self.fail("Preview completion did not reach the UI")

    def test_many_changes_run_only_first_and_latest_fresh_snapshot(self):
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Первый")
            self.assertTrue(self.started.wait(2))
            for index in range(1, 100):
                self.request(f"Текст {index}", f"Режим {index}", f"Словарь {index}")
            self.assertEqual(len(self.calls), 1)
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(self.max_active, 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1], (
            {"setting": "Режим 99", "glossary_enabled": True}, {"term": "Словарь 99"}
        ))
        self.assertEqual(self.app._normalizer_last_preview_result["normalized_text"],
                         "Текст 99:Режим 99:Словарь 99")
        self.app.btn_save_normalized_text.configure.assert_called_with(state=studio.tk.NORMAL)
        self.assertIsNone(self.app._normalizer_preview_thread)

    def test_edit_invalidation_discards_a_waiting_obsolete_request(self):
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Первый")
            self.assertTrue(self.started.wait(2))
            self.request("Уже устаревший")
            self.app._schedule_normalizer_preview()
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.app._normalizer_last_preview_result)
        self.app.btn_save_normalized_text.configure.assert_called_with(state=studio.tk.DISABLED)

    def test_empty_source_discards_waiting_work_and_stale_completion(self):
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Первый")
            self.assertTrue(self.started.wait(2))
            self.request("Ожидающий")
            self.request("")
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app._normalizer_last_preview_result["normalized_text"], "")
        self.app.btn_save_normalized_text.configure.assert_called_with(state=studio.tk.DISABLED)

    def test_invalid_settings_discard_waiting_request(self):
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Первый")
            self.assertTrue(self.started.wait(2))
            self.request("Ожидающий")
            self.app._collect_normalizer_preview_config.side_effect = ValueError("Плохой путь")
            self.app.run_normalizer_preview()
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.app._normalizer_last_preview_result)
        self.assertIn("Плохой путь", self.app._set_status_label.call_args.args[1])

    def test_worker_creation_failure_unlocks_retry_and_reports_error(self):
        with mock.patch.object(studio.threading.Thread, "start", side_effect=RuntimeError("Thread unavailable")):
            with self.assertLogs(level="ERROR"):
                self.request("Первый")
        self.assertFalse(self.app._normalizer_preview_worker_running)
        self.assertIsNone(self.app._normalizer_preview_thread)
        self.assertIn("Thread unavailable", self.app._set_status_label.call_args.args[1])
        self.release.set()
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Повтор")
            self.pump_until_idle()
        self.assertEqual(self.app._normalizer_last_preview_result["normalized_text_for_file"], "Повтор")

    def test_thread_constructor_failure_unlocks_preview(self):
        with mock.patch.object(studio.threading, "Thread", side_effect=RuntimeError("Thread creation unavailable")):
            with self.assertLogs(level="ERROR"):
                self.request("Первый")
        self.assertFalse(self.app._normalizer_preview_worker_running)
        self.assertIsNone(self.app._normalizer_preview_thread)
        self.assertIn("Thread creation unavailable", self.app._set_status_label.call_args.args[1])

    def test_failed_stale_context_does_not_skip_latest_request(self):
        def context(config, glossary):
            if config["setting"] == "bad":
                self.started.set()
                if not self.release.wait(3):
                    raise RuntimeError("Test context was not released")
                raise ValueError("Old dictionary unavailable")
            return self.factory(config, glossary)

        with mock.patch.object(studio.TTSProcessor, "normalization_context", context):
            self.request("Первый", "bad")
            self.assertTrue(self.started.wait(2))
            self.request("Последний", "good", "Новый словарь")
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(self.app._normalizer_last_preview_result["normalized_text"],
                         "Последний:good:Новый словарь")
        self.assertFalse(self.app._normalizer_preview_worker_running)

    def test_closing_discards_waiting_work_and_skips_destroyed_widgets(self):
        with mock.patch.object(studio.TTSProcessor, "normalization_context", self.factory):
            self.request("Первый")
            self.assertTrue(self.started.wait(2))
            self.request("Ожидающий")
            self.app.normalizer_result_text.reset_mock()
            self.app._is_closing = True
            self.release.set()
            self.pump_until_idle()
        self.assertEqual(len(self.calls), 1)
        self.app.normalizer_result_text.configure.assert_not_called()
        self.assertIsNone(self.app._normalizer_preview_pending_request)


class SourceOutputCallbackIsolationTests(unittest.TestCase):
    def test_status_callback_error_does_not_drop_other_rows_or_completion(self):
        delivered = []
        fail_first = True

        def post(*args):
            nonlocal fail_first
            if args[0] == "first" and fail_first:
                fail_first = False
                raise RuntimeError("UI callback unavailable")
            delivered.append(args)

        tracker = studio.SourceOutputStatusTracker(post)
        tracker.register("book", ["first", "second"], ["first"], "m4b")
        with self.assertLogs(level="ERROR"):
            tracker.start("book")
        self.assertEqual(delivered, [("second", "waiting")])
        tracker.update([("book", "encoding")])
        self.assertEqual(delivered[-1], ("first", "encoding", "m4b"))
        tracker.update([("book", "success")])
        self.assertEqual(tracker.initial_completed_rows(), (
            ("first", "success"), ("second", "success")
        ))

    def test_activity_callback_failure_does_not_interrupt_terminal_state(self):
        events = []
        activity = mock.Mock(side_effect=RuntimeError("Activity unavailable"))
        tracker = studio.SourceOutputStatusTracker(lambda *args: events.append(args), activity)
        tracker.register("book", ["row"])
        with self.assertLogs(level="ERROR"):
            tracker.update([("book", "error")])
        self.assertEqual(events, [("row", "error")])
        self.assertEqual(tracker.initial_completed_rows(), (("row", "error"),))
        with self.assertLogs(level="ERROR"):
            tracker.finish("warning")
        self.assertEqual(tracker.initial_completed_rows(), (("row", "error"),))


class NormalizerLibraryIndexTests(unittest.TestCase):
    @staticmethod
    def entry(index, name):
        profile = studio.normalizer_profile_from_config({}, name=name)
        profile_id = studio._normalizer_custom_profile_id(profile)
        return {"id": profile_id[:-8] + f"{index:08x}", "profile": profile}

    def test_large_library_normalizes_each_saved_entry_once(self):
        entries = [self.entry(i, f"Профиль {i}") for i in range(300)]
        with mock.patch.object(
            studio, "normalize_normalizer_profile_entry",
            wraps=studio.normalize_normalizer_profile_entry,
        ) as normalize:
            result = studio.normalizer_profile_entries_from_config({"normalizer_profiles": entries})
        self.assertEqual([e["id"] for e in result[2:]], [e["id"] for e in entries])
        self.assertEqual([e["profile"]["name"] for e in result[2:]], [f"Профиль {i}" for i in range(300)])
        self.assertLessEqual(normalize.call_count, 305)

    def test_names_duplicates_reserved_values_and_ids_keep_stable_order(self):
        builtin = studio.builtin_normalizer_profile_entries()[0]["profile"]["name"]
        entries = [
            self.entry(1, "Профиль"), self.entry(2, "Профиль (3)"),
            self.entry(3, "профиль"), self.entry(4, "Профиль"),
            self.entry(5, "Пользовательский"), self.entry(6, builtin),
        ]
        duplicate = copy.deepcopy(entries[0])
        duplicate["profile"]["name"] = "Дубликат ID"
        raw = entries + [duplicate, {"id": "invalid", "profile": entries[0]["profile"]}]
        snapshot = copy.deepcopy(raw)
        result = studio.normalizer_profile_entries_from_config({"normalizer_profiles": raw})
        self.assertEqual([e["id"] for e in result[2:]], [e["id"] for e in entries])
        self.assertEqual([e["profile"]["name"] for e in result[2:]], [
            "Профиль", "Профиль (3)", "профиль (2)", "Профиль (4)",
            "Пользовательский профиль", builtin + " (2)",
        ])
        self.assertEqual(raw, snapshot)

    def test_unique_name_and_config_collision_resolution_match_library(self):
        entries = [self.entry(i, name) for i, name in enumerate([
            "Профиль", "Профиль (2)", "Профиль (4)", "Профиль"
        ], 1)]
        self.assertEqual(studio.unique_normalizer_profile_name("Профиль", entries), "Профиль (3)")
        self.assertFalse(studio.normalizer_profile_name_conflict("Профиль", entries[:1], exclude_id=entries[0]["id"]))
        with self.assertLogs(level="WARNING"):
            normalized = studio.normalize_config({"normalizer_profiles": entries})
        self.assertEqual([e["profile"]["name"] for e in normalized["normalizer_profiles"]], [
            "Профиль", "Профиль (2)", "Профиль (4)", "Профиль (3)"
        ])
        self.assertEqual(normalized["normalizer_profiles"],
                         studio.normalizer_profile_entries_from_config({"normalizer_profiles": entries})[2:])

    def test_long_conflicting_names_survive_export_and_settings_reload(self):
        name = "Я" * 128
        entries = [self.entry(i, value) for i, value in enumerate([
            name, name[:124] + " (2)", name,
        ], 1)]
        self.assertEqual(studio.unique_normalizer_profile_name(name, entries), name[:124] + " (3)")
        with self.assertLogs(level="WARNING"):
            normalized = studio.normalize_config({"normalizer_profiles": entries})
        names = [e["profile"]["name"] for e in normalized["normalizer_profiles"]]
        self.assertEqual(names, [name, name[:124] + " (2)", name[:124] + " (3)"])
        self.assertTrue(all(len(value) == 128 for value in names))
        library = studio.normalizer_profile_library_from_profiles(normalized["normalizer_profiles"])
        imported = studio.normalize_normalizer_profile_library(json.loads(json.dumps(library)))
        reloaded = studio.normalize_config({"normalizer_profiles": imported})
        self.assertEqual(reloaded["normalizer_profiles"], normalized["normalizer_profiles"])
        self.assertEqual([e["id"] for e in reloaded["normalizer_profiles"]], [e["id"] for e in entries])


if __name__ == "__main__":
    unittest.main()
