"""Импорт EPUB сохраняет текст вне оглавления и порядок чтения книги."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


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


class EpubReadingOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.book = studio.epub.EpubBook()
        self.book.set_identifier("epub-reading-order")
        self.book.set_title("Проверочная книга")
        self.book.set_language("ru")
        self.book.add_item(studio.epub.EpubNcx())
        self.book.add_item(studio.epub.EpubNav())

    def add_document(self, name, title, text):
        item = studio.epub.EpubHtml(
            uid=name, title=title, file_name=f"{name}.xhtml", lang="ru"
        )
        item.content = f"<h1>{title}</h1><p>{text}</p>"
        self.book.add_item(item)
        return item

    def extract(self):
        path = self.directory / "book.epub"
        studio.epub.write_epub(path, self.book)
        return studio.BookExtractor.extract_epub(path)[0]

    def test_partial_toc_keeps_intro_and_spine_reading_order(self):
        intro = self.add_document("intro", "Предисловие", "Текст предисловия.")
        first = self.add_document("first", "Первый раздел", "Текст первой главы.")
        last = self.add_document("last", "Последний раздел", "Текст последней главы.")
        self.book.spine = [intro, first, last]
        self.book.toc = (
            studio.epub.Link("last.xhtml", "Глава вторая", "last"),
            studio.epub.Link("first.xhtml#one", "Глава первая", "first"),
            studio.epub.Link("./first.xhtml#two", "Повтор ссылки", "first-two"),
        )
        chapters = self.extract()
        self.assertEqual([title for title, _text in chapters], [
            "Глава", "Глава первая", "Глава вторая",
        ])
        self.assertEqual([text for _title, text in chapters], [
            "Предисловие\nТекст предисловия.",
            "Первый раздел\nТекст первой главы.",
            "Последний раздел\nТекст последней главы.",
        ])

    def test_partial_toc_excludes_navigation_and_explicit_cover(self):
        cover = self.add_document("cover", "Обложка", "Текст страницы обложки.")
        intro = self.add_document("intro", "Предисловие", "Неозаглавленный в TOC текст.")
        chapter = self.add_document("chapter", "Основной раздел", "Основной текст.")
        self.book.spine = ["nav", cover, intro, chapter]
        self.book.toc = (
            studio.epub.Link("cover.xhtml", "Обложка", "cover"),
            studio.epub.Link("chapter.xhtml", "Основная глава", "chapter"),
        )
        chapters = self.extract()
        self.assertEqual(chapters, [
            ("Глава", "Предисловие\nНеозаглавленный в TOC текст."),
            ("Основная глава", "Основной раздел\nОсновной текст."),
        ])

    def test_toc_document_outside_spine_does_not_displace_reading_order(self):
        first = self.add_document("first", "Основной текст", "Текст книги.")
        extra = self.add_document("extra", "Дополнение", "Текст приложения.")
        self.book.spine = [first]
        self.book.toc = (
            studio.epub.Link(extra.file_name, "Приложение", extra.id),
            studio.epub.Link(first.file_name, "Первая глава", first.id),
        )
        chapters = self.extract()
        self.assertEqual(chapters, [
            ("Первая глава", "Основной текст\nТекст книги."),
            ("Приложение", "Дополнение\nТекст приложения."),
        ])


if __name__ == "__main__":
    unittest.main()
