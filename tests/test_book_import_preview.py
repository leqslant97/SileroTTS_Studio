"""Полный предпросмотр книг совпадает с импортом и ограничивает кэш текста."""

import concurrent.futures
import os
import tempfile
import threading
import unittest
from contextlib import chdir
from pathlib import Path
from unittest import mock

from test_regressions import studio


class BookImportPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def txt(self, name="book.txt", text="Глава 1\nОдин.\nГлава 2\nДва."):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def format_sources(self):
        txt = self.txt()
        fb2 = self.directory / "book.fb2"
        fb2.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            '<description><title-info><author><first-name>Анна</first-name>'
            '<last-name>Иванова</last-name></author></title-info></description>'
            '<body><section><title><p>Первая глава</p></title><p>Текст один.</p>'
            '</section><section><title><p>Вторая глава</p></title>'
            '<p>Текст два.</p></section></body></FictionBook>',
            encoding="utf-8",
        )
        docx = self.directory / "book.docx"
        document = studio.docx.Document()
        for title, text in (("Начало", "Первый абзац."), ("Конец", "Последний абзац.")):
            document.add_heading(title, level=1)
            document.add_paragraph(text)
        document.save(docx)
        epub = self.directory / "book.epub"
        book = studio.epub.EpubBook()
        book.set_identifier("full-import-preview")
        book.set_title("Проверочная книга")
        book.set_language("ru")
        book.add_author("Лев Толстой")
        book.add_item(studio.epub.EpubNcx())
        book.add_item(studio.epub.EpubNav())
        items = []
        for number in (1, 2):
            item = studio.epub.EpubHtml(
                uid=f"chapter-{number}", title=f"Раздел {number}",
                file_name=f"chapter-{number}.xhtml", lang="ru",
            )
            item.content = f"<h1>Раздел {number}</h1><p>Текст {number}.</p>"
            book.add_item(item)
            items.append(item)
        book.spine = ["nav", *items]
        book.toc = tuple(studio.epub.Link(item.file_name, item.title, item.id) for item in items)
        studio.epub.write_epub(epub, book)
        return [txt, epub, fb2, docx]

    def test_real_formats_preview_matches_import_and_never_creates_output(self):
        sources = self.format_sources()
        for single_file in (False, True):
            with self.subTest(single_file=single_file):
                output = self.directory / f"output-{single_file}" / "nested"
                cache = studio.BookImportPreviewCache()
                template = "{book_index:8:03d}-{num:15:04d}-{author}-{title}"
                before = {path: path.read_bytes() for path in sources}
                with chdir(self.directory), mock.patch.object(studio, "_write_text_atomic", side_effect=AssertionError("preview wrote")):
                    rows = studio.BookExtractor.preview_files(
                        sources, output.relative_to(self.directory), template, regex_pattern=r"^Глава \d+",
                        single_file=single_file, preview_cache=cache,
                    )
                self.assertIsInstance(rows, tuple)
                self.assertTrue(all(Path(row["path"]).is_absolute() for row in rows))
                self.assertFalse(output.parent.exists())
                self.assertTrue(all(row["error"] is None for row in rows))
                self.assertEqual(len(rows), 4 if single_file else 8)
                self.assertEqual(before, {path: path.read_bytes() for path in sources})
                self.assertTrue(any("Лев Толстой" in row["result"] for row in rows))
                self.assertTrue(any("Анна Иванова" in row["result"] for row in rows))
                with mock.patch.object(studio.BookExtractor, "extract_file", side_effect=AssertionError("cache miss")):
                    results = studio.BookExtractor.import_files(
                        sources, output, template, regex_pattern=r"^Глава \d+",
                        single_file=single_file, preview_cache=cache,
                    )
                self.assertTrue(all(result["error"] is None for result in results))
                self.assertEqual(
                    [row["result"] for row in rows],
                    [name for result in results for name in result["saved_files"]],
                )
                self.assertTrue(all(Path(row["path"]).is_file() for row in rows))

    def test_errors_keep_queue_positions_and_unicode_collisions_match_import(self):
        first = self.txt("first.txt", "Йога\nйога\nß\nss")
        last = self.txt("last.txt", "Йога\nйога\nß\nss")
        missing = self.directory / "missing.txt"
        output = self.directory / "output"
        output.mkdir()
        existing = {"И\u0306ога.txt": "Старый текст", "SS.txt": "Existing"}
        for name, text in existing.items():
            (output / name).write_text(text, encoding="utf-8")
        progress = []
        rows = studio.BookExtractor.preview_files(
            [first, missing, last], output, "{title}", regex_pattern=r"^.+$",
            progress_callback=lambda *args: progress.append(args),
        )
        self.assertEqual([row["book_index"] for row in rows], [1] * 4 + [2] + [3] * 4)
        self.assertEqual([row["result"] for row in rows[:4]], [
            "Йога (2).txt", "йога (3).txt", "ß (2).txt", "ss (3).txt",
        ])
        self.assertEqual(rows[4]["result"], "Ошибка")
        self.assertIn("missing.txt", rows[4]["error"])
        self.assertEqual(rows[4]["path"], "")
        self.assertEqual(len(list(output.iterdir())), len(existing))
        results = studio.BookExtractor.import_files(
            [first, missing, last], output, "{title}", regex_pattern=r"^.+$",
        )
        self.assertEqual(
            [row["result"] for row in rows if row["error"] is None],
            [name for result in results for name in result["saved_files"]],
        )
        for name, text in existing.items():
            self.assertEqual((output / name).read_text(encoding="utf-8"), text)
        self.assertEqual([event[0] for event in progress if event[3] == "extract"], [1, 2, 3])
        self.assertTrue(all(len(event) == 5 and event[1] == 3 for event in progress))
        self.assertEqual(progress[-1][3:], ("complete", 4))

    def test_preview_numbers_include_failed_books_and_use_actual_chapter_count(self):
        first = self.txt("first.txt", "\n".join(f"Глава {number}" for number in range(1, 13)))
        missing = self.directory / "missing.txt"
        third = self.txt("third.txt", "Глава 1")
        rows = studio.BookExtractor.preview_files(
            [first, missing, third], self.directory / "output", "{book_index:8}-{num}",
            regex_pattern=r"^Глава \d+",
        )
        self.assertEqual(rows[0]["result"], "08-01.txt")
        self.assertEqual(rows[11]["result"], "08-12.txt")
        self.assertEqual(rows[-1]["result"], "10-1.txt")

    def test_cached_import_recalculates_names_after_template_directory_and_order_changes(self):
        first = self.txt("first.txt")
        second = self.txt("second.txt")
        cache = studio.BookImportPreviewCache()
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            studio.BookExtractor.preview_files(
                [first, second], self.directory / "preview", "{title}",
                regex_pattern=r"^Глава \d+", preview_cache=cache,
            )
            results = studio.BookExtractor.import_files(
                [second, first], self.directory / "import", "{book_index:8}-{num:03d}-{name}",
                regex_pattern=r"^Глава \d+", preview_cache=cache,
            )
        self.assertEqual(extract.call_count, 2)
        self.assertEqual([result["saved_files"] for result in results], [
            ["8-001-second.txt", "8-002-second.txt"],
            ["9-001-first.txt", "9-002-first.txt"],
        ])

    def test_invalid_template_does_not_extract_or_write(self):
        source = self.txt()
        output = self.directory / "output"
        with mock.patch.object(studio.BookExtractor, "extract_file") as extract:
            with self.assertRaises(studio.TemplateError):
                studio.BookExtractor.preview_files([source], output, "{unknown}")
        extract.assert_not_called()
        self.assertFalse(output.exists())

    def test_large_queue_import_reuses_preview_tail_without_scan_eviction(self):
        sources = [self.txt(f"book-{index}.txt", str(index) + "x" * 3000) for index in range(6)]
        cache = studio.BookImportPreviewCache(max_bytes=9000)
        output = self.directory / "output"
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            rows = studio.BookExtractor.preview_files(sources, output, "{name}", preview_cache=cache)
            self.assertEqual(extract.call_count, len(sources))
            extract.reset_mock()
            results = studio.BookExtractor.import_files(sources, output, "{name}", preview_cache=cache)
        self.assertEqual([call.args[0] for call in extract.call_args_list], sources[:4])
        self.assertEqual([row["result"] for row in rows], [result["saved_files"][0] for result in results])
        self.assertTrue(all(result["error"] is None for result in results))
        for index, result in enumerate(results):
            self.assertEqual((output / result["saved_files"][0]).read_text(), str(index) + "x" * 3000)

    def test_cancellation_never_returns_a_partial_success(self):
        first = self.txt("first.txt")
        second = self.txt("second.txt")
        original_extract = studio.BookExtractor.extract_file
        for stage in ("before", "reading", "complete"):
            with self.subTest(stage=stage):
                cancelled = threading.Event()
                output = self.directory / stage
                if stage == "before":
                    cancelled.set()

                def extract(*args, **kwargs):
                    result = original_extract(*args, **kwargs)
                    if stage == "reading":
                        cancelled.set()
                    return result

                def progress(_index, _total, _path, current_stage, _value):
                    if stage == "complete" and current_stage == "complete":
                        cancelled.set()

                with mock.patch.object(studio.BookExtractor, "extract_file", side_effect=extract) as read:
                    with self.assertRaises(concurrent.futures.CancelledError):
                        studio.BookExtractor.preview_files(
                            [first, second], output, "{name}",
                            progress_callback=progress, cancel_event=cancelled,
                        )
                self.assertEqual(read.call_count, 0 if stage == "before" else 1)
                self.assertFalse(output.exists())


class BookImportPreviewCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def txt(self, name="book.txt", text="Глава 1\nОдин.\nГлава 2\nДва."):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_file_changes_and_same_size_replacement_invalidate_cached_text(self):
        source = self.txt(text="first")
        cache = studio.BookImportPreviewCache()
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            self.assertEqual(cache.get_or_extract(source)[0], [("Текст", "first")])
            source.write_text("longer text", encoding="utf-8")
            self.assertEqual(cache.get_or_extract(source)[0], [("Текст", "longer text")])
            previous = source.stat()
            replacement = self.txt("replacement.txt", "other words")
            self.assertEqual(replacement.stat().st_size, previous.st_size)
            replacement.replace(source)
            os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertEqual(cache.get_or_extract(source)[0], [("Текст", "other words")])
        self.assertEqual(extract.call_count, 3)

    def test_regex_and_single_file_change_only_relevant_extraction_options(self):
        source = self.txt()
        cache = studio.BookImportPreviewCache()
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            self.assertEqual(len(cache.get_or_extract(source, r"^Глава \d+")[0]), 2)
            self.assertEqual(len(cache.get_or_extract(source, r"^Глава 1")[0]), 1)
            self.assertEqual(cache.get_or_extract(source, r"^Глава 1", True)[0][0][0], "Книга")
            cache.get_or_extract(source, "(", True)
            self.assertEqual(len(cache.get_or_extract(source, r"^Глава \d+")[0]), 2)
        self.assertEqual(extract.call_count, 3)

    def test_returns_independent_chapters_and_does_not_keep_mutable_parser_pairs(self):
        source = self.txt()
        cache = studio.BookImportPreviewCache()
        parser_chapters = [["Title", "Body"]]
        with mock.patch.object(studio.BookExtractor, "extract_file", return_value=(parser_chapters, "Author")) as extract:
            chapters, author = cache.get_or_extract(source)
            chapters[0] = ("Changed", "Changed")
            chapters.append(("Extra", "Extra"))
            parser_chapters[0][0] = "Mutated parser result"
            self.assertEqual(cache.get_or_extract(source), ([("Title", "Body")], "Author"))
        self.assertEqual(author, "Author")
        self.assertEqual(extract.call_count, 1)

    def test_lru_budget_evicts_text_and_never_retains_an_oversized_book(self):
        sources = [self.txt(f"book-{index}.txt", str(index) + "x" * 3000) for index in range(3)]
        cache = studio.BookImportPreviewCache(max_bytes=9000)
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            for index in (0, 1, 0, 2, 0, 1):
                chapters, _author = cache.get_or_extract(sources[index])
                self.assertTrue(chapters[0][1].startswith(str(index)))
                self.assertLessEqual(cache._retained_bytes, 9000)
            self.assertEqual(extract.call_count, 4)
        oversized = self.txt("oversized.txt", "x" * 15000)
        before = cache._retained_bytes
        with mock.patch.object(studio.BookExtractor, "extract_file", wraps=studio.BookExtractor.extract_file) as extract:
            for _ in range(2):
                self.assertEqual(len(cache.get_or_extract(oversized)[0][0][1]), 15000)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(cache._retained_bytes, before)

    def test_concurrent_requests_share_safe_cached_extraction(self):
        source = self.txt()
        cache = studio.BookImportPreviewCache()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original_extract = studio.BookExtractor.extract_file

        def extract(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("cache extraction was not released")
            return original_extract(*args, **kwargs)

        with mock.patch.object(studio.BookExtractor, "extract_file", side_effect=extract) as read:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(cache.get_or_extract, source) for _ in range(4)]
                self.assertTrue(entered.wait(1))
                release.set()
                results = [future.result(timeout=3) for future in futures]
        self.assertEqual(read.call_count, 1)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len({id(result[0]) for result in results}), len(results))

    def test_file_changed_during_extraction_is_rejected_and_not_cached(self):
        source = self.txt(text="old text")
        cache = studio.BookImportPreviewCache()
        original_extract = studio.BookExtractor.extract_file

        def changed_during_read(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            source.write_text("new, changed text", encoding="utf-8")
            return result

        with mock.patch.object(studio.BookExtractor, "extract_file", side_effect=changed_during_read):
            with self.assertRaisesRegex(OSError, "Файл изменился во время чтения"):
                cache.get_or_extract(source)
        self.assertEqual(cache._retained_bytes, 0)
        self.assertEqual(cache.get_or_extract(source)[0], [("Текст", "new, changed text")])


if __name__ == "__main__":
    unittest.main()
