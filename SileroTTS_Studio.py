"""
Silero TTS Studio - Профессиональная среда для генерации аудиокниг, подкастов и озвучки текста, которая использует API от Silero.
Поддерживает кэширование, постобработку (FFmpeg), работу с глоссариями,
импорт электронных книг (EPUB, FB2, DOCX) и пакетную сборку аудиофайлов.
"""

import os
import re
import json
import copy
import time
import uuid
import base64
import hashlib
import struct
import math
import threading
import logging
import requests
import shutil
import tempfile
import platform
import subprocess
import sys
import urllib.parse
import unicodedata
import queue
import concurrent.futures
import warnings
from pathlib import Path, PurePosixPath

# === КРОССПЛАТФОРМЕННЫЙ ЗАПУСК ДОЧЕРНИХ ПРОЦЕССОВ ===
# Pydub импортирует Popen двумя способами: через модуль subprocess и напрямую
# из него. Поэтому политика задаётся до импорта Pydub. В Windows она скрывает
# консоль, а в многопоточном GUI macOS принудительно оставляет CPython на
# posix_spawn: обычный fork после инициализации Tk/CoreFoundation небезопасен.
_ORIGINAL_SUBPROCESS_POPEN = subprocess.Popen


def _resolve_external_binary(bin_name):
    """Находит внешний бинарник без зависимости от PATH Finder/macOS GUI."""
    bin_name = os.fsdecode(bin_name)
    resolved = shutil.which(bin_name)
    if resolved:
        return resolved

    if sys.platform == "darwin":
        # GUI и управляемые IDE могут запускать Python с минимальным PATH, в
        # котором нет Homebrew. Поддерживаем обе стандартные архитектуры Mac.
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            candidate = Path(prefix) / bin_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _resolve_macos_popen_command(command, *, shell=False):
    """Даёт posix_spawn абсолютный executable даже для ``ffprobe``/``pbcopy``."""
    if shell or command is None:
        return command

    is_sequence = isinstance(command, (list, tuple))
    executable = command[0] if is_sequence and command else command
    if not isinstance(executable, (str, bytes, os.PathLike)):
        return command

    executable_text = os.fsdecode(executable)
    if os.path.dirname(executable_text):
        return command
    resolved = _resolve_external_binary(executable_text)
    if not resolved:
        return command

    if is_sequence:
        updated = list(command)
        updated[0] = resolved
        return tuple(updated) if isinstance(command, tuple) else updated
    return resolved


def _patched_popen(*args, **kwargs):
    if platform.system() == "Windows":
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
    elif sys.platform == "darwin":
        if kwargs.get("preexec_fn") is not None:
            raise ValueError("preexec_fn небезопасен в многопоточном macOS GUI")
        kwargs.setdefault("close_fds", False)

        shell = bool(kwargs.get("shell", False))
        if args:
            args = (
                _resolve_macos_popen_command(args[0], shell=shell),
                *args[1:],
            )
        elif "args" in kwargs:
            kwargs["args"] = _resolve_macos_popen_command(
                kwargs["args"], shell=shell
            )
        if kwargs.get("executable") is not None:
            kwargs["executable"] = _resolve_macos_popen_command(
                kwargs["executable"], shell=shell
            )
    return _ORIGINAL_SUBPROCESS_POPEN(*args, **kwargs)


if platform.system() in {"Windows", "Darwin"}:
    subprocess.Popen = _patched_popen
# --------------------------------------------------------

is_frozen_mac = (sys.platform == "darwin") and getattr(sys, 'frozen', False)

# === ЗАЩИТА ОТ СБРОСА В PYINSTALLER --windowed ===
class NullWriter:
    """Минимальный file-like объект для сборок PyInstaller без консоли."""

    def write(self, value="", *args, **kwargs):
        return len(str(value))

    def flush(self, *args, **kwargs):
        return None

    def isatty(self):
        return False
    
if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()
# ------------------------------
      
# Попытка импорта библиотек для работы с электронными книгами. До настройки
# обработчиков logging ничего не выводим: первый logging.warning() иначе сам
# создаст root-handler и помешает подключить tts_processor.log.
IMPORT_LIBS_ERROR = None
try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import docx
    IMPORT_LIBS_AVAILABLE = True
except Exception as exc:
    IMPORT_LIBS_AVAILABLE = False
    IMPORT_LIBS_ERROR = exc

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont
from collections import deque
from razdel import sentenize

APP_VERSION = "1.5.0"
# Публичный релиз ветки с нулевым patch называется короче: v1.5. Внутренняя
# трёхкомпонентная версия остаётся нужна метаданным Windows/macOS.
APP_PUBLIC_VERSION = APP_VERSION.removesuffix(".0")

# Для воспроизведения аудио на Windows
if platform.system() == "Windows":
    import winsound

# ================= ИНИЦИАЛИЗАЦИЯ ПАПКИ ДАННЫХ =================
# Проверяем, запущены ли мы как скомпилированное приложение (.app) на macOS
if is_frozen_mac:
    # Режим .app на macOS -> корень в Документах
    BASE_DIR = Path.home() / "Documents" / "SileroTTS_Studio"
else:
    # Режим консоли (python3) или Portable .exe на Windows -> корень в папке со скриптом
    if getattr(sys, 'frozen', False):
        BASE_DIR = Path(sys.executable).parent
    else:
        BASE_DIR = Path(__file__).parent.resolve()

# Служебная папка ТОЛЬКО для настроек и логов
APP_DATA_DIR = BASE_DIR / "SileroTTS_Studio_data"

# Рабочие папки создаются в BASE_DIR РЯДОМ с SileroTTS_Studio_data, а не ВНУТРИ неё
DEFAULT_INPUT_DIR = str(BASE_DIR / "input_texts")
DEFAULT_OUTPUT_DIR = str(BASE_DIR / "output_audio")
DEFAULT_DIRECT_OUTPUT_DIR = str(BASE_DIR / "direct_audio")
DEFAULT_CACHE_DIR = str(BASE_DIR / "cache_audio")

# Пути, которые приложение обязано уметь создать. История диалогов и
# необязательная папка экспорта сюда намеренно не входят: исчезнувший путь в
# них не должен переписывать пользовательские настройки.
REQUIRED_DIRECTORY_DEFAULTS = {
    "input_dir": DEFAULT_INPUT_DIR,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "direct_output_dir": DEFAULT_DIRECT_OUTPUT_DIR,
    "cache_dir": DEFAULT_CACHE_DIR,
    "import_outdir": DEFAULT_INPUT_DIR,
}


def ensure_config_directories(config, keys=None):
    """Создаёт рабочие папки и точечно откатывает недоступные пути.

    Сохранённый каталог может исчезнуть вместе с внешним диском либо содержать
    синтаксически неверный для текущей ОС путь. В таком случае только этот ключ
    получает портативное значение по умолчанию. Корректные пользовательские
    пути, включая ещё не существующие каталоги, создаются и остаются без
    изменений. Возвращает ``(config, recovered)``; исходный dict меняется на
    месте, чтобы UI и последующее автосохранение видели восстановленные пути.
    """
    if not isinstance(config, dict):
        config = {}

    selected_keys = tuple(keys) if keys is not None else tuple(
        REQUIRED_DIRECTORY_DEFAULTS
    )
    recovered = {}
    for key in selected_keys:
        if key not in REQUIRED_DIRECTORY_DEFAULTS:
            continue
        fallback = REQUIRED_DIRECTORY_DEFAULTS[key]
        raw_value = config.get(key, fallback)
        value = (
            str(raw_value).strip()
            if isinstance(raw_value, (str, os.PathLike))
            else ""
        )
        if not value:
            value = fallback

        try:
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            recovered[key] = (value, fallback, exc)
            logging.getLogger(__name__).warning(
                "Недоступный путь %s=%r заменён на %r: %s",
                key, value, fallback, exc,
            )
            value = fallback
            # Если даже локальный portable-каталог создать нельзя, исключение
            # важно не маскировать: продолжение синтеза всё равно невозможно.
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)

        config[key] = value
    return config, recovered


# Создаем служебную папку и переходим в корень проекта
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(BASE_DIR) # Рабочей директорией делаем BASE_DIR!

# Временные файлы изолированы на уровне процесса. Общая папка внутри
# SileroTTS_Studio_data приводила к гонке: другой экземпляр приложения мог
# удалить concat-манифест между запуском FFmpeg и фактическим открытием файла.
SESSION_TEMP_DIR = Path(
    tempfile.mkdtemp(prefix=f"SileroTTS_Studio_{os.getpid()}_")
)

# Не удаляем SESSION_TEMP_DIR рекурсивно ни в on_closing(), ни через atexit.
# Фоновые потоки кодирования являются daemon-потоками и в момент закрытия GUI
# всё ещё могут ждать FFmpeg. Рекурсивная очистка тогда удаляла собственный
# concat-манифест раньше, чем FFmpeg успевал его открыть. Каждая операция
# удаляет принадлежащий ей манифест только после subprocess.run(); оставшийся
# каталог находится в системной temp-папке и очищается самой ОС.

SETTINGS_FILE = APP_DATA_DIR / "settings.json"
LOG_FILE = APP_DATA_DIR / "tts_processor.log"

SAFE_LIMIT = 30000 # Лимит символов для авто-разрыва в режиме full

# Экспериментальный параметр облачного API Silero. На момент выпуска значение
# 16 используется как базовый ориентир EnhancedTTS. Текущий endpoint отклоняет
# значения выше 72 с HTTP 422, поэтому не отправляем заведомо невалидный запрос.
API_STEPS_PRESETS = (4, 8, 12, 16)
API_STEPS_MIN = 1
API_STEPS_MAX = 72
API_STEPS_SOFT_WARNING = 17
API_STEPS_STRONG_WARNING = 32

# Политики очистки вариантов Steps при явной оптимизации кэша. Они не
# сохраняются в settings.json: пользователь выбирает безопасное поведение для
# каждой операции отдельно.
CACHE_VARIANT_KEEP_ALL = "keep_all"
CACHE_VARIANT_KEEP_LEGACY = "keep_legacy"
CACHE_VARIANT_KEEP_LEGACY_CURRENT = "keep_legacy_current"
CACHE_VARIANT_KEEP_CURRENT = "keep_current"

# Официальный HTTPS endpoint используется только как значение по умолчанию.
# Любой непустой адрес из настроек передаётся без скрытого переписывания:
# пользователь может указать собственный сервер, прокси или HTTP endpoint.
DEFAULT_API_URL = "https://iq3g.silero.ai/enhanced_voice"

CACHE_OPERATION_LABELS = {
    "load": "обновление таблицы",
    "delete": "удаление записей",
    "clear": "очистка кэша",
    "optimize": "оптимизация кэша",
    "transcode": "перекодирование кэша в Opus",
    "archive": "архивация кэша",
    "analyze": "анализ вариантов Steps",
}

# Внутренний кэш предназначен прежде всего для моно-речи и пауз. Opus 48 кбит/с
# заметно компактнее прежнего Vorbis на реальном книжном кэше, оставаясь выше
# типичных speech-настроек. Миграция уже сжатого Vorbis всё равно является
# потерей качества, поэтому запускается только отдельной явной командой.
CACHE_AUDIO_CODEC = "opus"
CACHE_AUDIO_FFMPEG_CODEC = "libopus"
CACHE_AUDIO_BITRATE = "48k"
CACHE_AUDIO_SAMPLE_RATE = 48000
CACHE_AUDIO_CHANNELS = 1

_DIALOGUE_PREFIX_PATTERN = (
    r"^[ \t]*(?:[-–—−]{2,}|-(?=[ \t]+\d+[ \t]*-[А-Яа-яЁё]{1,4}\b)|"
    r"-(?![ \t]*\d)|[–—]|−(?![ \t]*\d))[ \t]*"
)

# Кавычки, которыми может начинаться отдельный абзац с прямой речью или
# мыслями персонажа. Большинство двойных вариантов заранее приводится к `"`,
# но полный набор нужен и для пользовательских RegEx, применяемых позднее.
_QUOTED_SPEECH_OPENERS = frozenset('"\'«“„‟‹‘‚「『')
_COLON_TRAILING_CLOSERS = frozenset('"\'»”’›」』)]}')


_SILERO_SUPPORTED_TEXT_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё]")


def contains_synthesizable_text(value):
    """Проверяет, останется ли в тексте поддерживаемый API материал.

    После нормализации отдельный чанк может состоять только из пунктуации,
    символов или неподдерживаемой письменности. EnhancedTTS удаляет такой
    материал и отвечает HTTP 422 ``Your text is empty!``. Поэтому после
    нормализации нужна хотя бы одна русская или ASCII-латинская буква. Числа в
    штатном пути уже записаны нормализатором словами; если нормализатор
    недоступен, чанк только из цифр тоже не отправляется и не вызывает 422.
    Смешанные фразы здесь не очищаются: их исходный Unicode-текст и ключ
    кэша должны остаться неизменными.
    """
    return bool(_SILERO_SUPPORTED_TEXT_PATTERN.search(str(value or "")))


def _log_text_preview(value, limit=180):
    """Возвращает однострочное безопасное превью текста для журнала."""
    return " ".join(str(value or "").split())[:limit]


def strip_leading_text_bom(value):
    """Удаляет только служебный BOM в начале текста.

    UTF-8 с BOM декодируется в начальный ``U+FEFF``, который способен сломать
    пользовательские RegEx с якорем ``^``. Такой маркер не является частью
    текста, но внутренние ``U+FEFF`` не трогаются: они могут появиться после
    склейки источников или быть осознанным символом.
    """
    text = str(value or "")
    return text.removeprefix("\ufeff")


def _config_bool(value, default=False):
    """Безопасно читает bool из JSON, импортированных профилей и Tk."""
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off", ""):
            return False
        return default
    return bool(value)


GLOSSARY_SECTION_DEFAULTS = {
    "accents_ignore_case": [],
    "accents_strict_case": [],
    "terms_ignore_case": {},
    "terms_strict_case": {},
    "regex_rules": [],
}


def empty_glossary_data():
    """Возвращает новый пустой глоссарий без общих изменяемых значений."""
    return copy.deepcopy(GLOSSARY_SECTION_DEFAULTS)


def canonicalize_glossary_data(data, *, strict=True):
    """Проверяет переносимый JSON глоссария и дополняет пустые секции.

    Неизвестные корневые поля сохраняются для прямой совместимости с будущими
    версиями. В пользовательских операциях используется строгий режим с
    понятной ошибкой; мягкий режим пригоден только для восстановления данных.
    """
    if not isinstance(data, dict):
        raise ValueError("корень глоссария должен быть JSON-объектом")

    result = copy.deepcopy(data)

    def invalid(message, fallback):
        if strict:
            raise ValueError(message)
        return copy.deepcopy(fallback)

    for key in ("accents_ignore_case", "accents_strict_case"):
        values = result.get(key, [])
        if not isinstance(values, list):
            values = invalid(f"секция {key} должна быть JSON-массивом", [])
        cleaned = []
        for index, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                if strict:
                    raise ValueError(
                        f"{key}[{index}] должен быть непустой строкой"
                    )
                continue
            cleaned.append(value)
        result[key] = cleaned

    for key in ("terms_ignore_case", "terms_strict_case"):
        values = result.get(key, {})
        if not isinstance(values, dict):
            values = invalid(f"секция {key} должна быть JSON-объектом", {})
        cleaned = {}
        for source, value in values.items():
            if not isinstance(source, str) or not source.strip():
                if strict:
                    raise ValueError(
                        f"ключи секции {key} должны быть непустыми строками"
                    )
                continue
            if isinstance(value, dict):
                for flag in ("verbatim", "whole_word"):
                    if flag in value and not isinstance(value[flag], (bool, str, int)):
                        if strict:
                            raise ValueError(
                                f"{key}.{source}.{flag} должен быть логическим значением"
                            )
                replacement = value.get(
                    "replacement", value.get("repl", value.get("value", ""))
                )
                if isinstance(replacement, (dict, list)):
                    if strict:
                        raise ValueError(
                            f"замена для {key}.{source} должна быть строкой"
                        )
                    continue
            elif isinstance(value, (list, tuple)):
                if strict:
                    raise ValueError(
                        f"замена для {key}.{source} должна быть строкой или объектом"
                    )
                continue
            cleaned[source] = copy.deepcopy(value)
        result[key] = cleaned

    rules = result.get("regex_rules", [])
    if not isinstance(rules, list):
        rules = invalid("секция regex_rules должна быть JSON-массивом", [])
    cleaned_rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            if strict:
                raise ValueError(f"regex_rules[{index}] должен быть JSON-объектом")
            continue
        pattern = rule.get("pattern") or rule.get("regex")
        if not isinstance(pattern, str) or not pattern:
            if strict:
                raise ValueError(
                    f"regex_rules[{index}] должен содержать непустой pattern"
                )
            continue
        replacement = rule.get("repl", "")
        if isinstance(replacement, (dict, list)):
            if strict:
                raise ValueError(f"regex_rules[{index}].repl должен быть строкой")
            continue
        try:
            re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            if strict:
                raise ValueError(
                    f"ошибка в regex_rules[{index}] ({pattern}): {exc}"
                ) from exc
            continue
        cleaned_rules.append(copy.deepcopy(rule))
    result["regex_rules"] = cleaned_rules

    for key, default in GLOSSARY_SECTION_DEFAULTS.items():
        result.setdefault(key, copy.deepcopy(default))
    return result


def glossary_term_spec(value):
    """Читает legacy-строку либо расширенное правило термина v1.5.

    Старые ``"исходное": "замена"`` продолжают работать байт-в-байт.
    Объект добавляет два необязательных свойства: ``verbatim`` защищает
    замену от нормализатора и финальной очистки, а ``whole_word=false``
    разрешает совпадение внутри слова. По умолчанию границы слова сохраняются.
    """
    if not isinstance(value, dict):
        return {
            "replacement": str(value if value is not None else ""),
            "verbatim": False,
            "whole_word": True,
        }

    replacement = value.get("replacement")
    if replacement is None:
        replacement = value.get("repl")
    if replacement is None:
        replacement = value.get("value", "")

    match_mode = str(value.get("match", "")).strip().lower()
    if match_mode in {"substring", "part", "inside"}:
        whole_word = False
    elif match_mode in {"word", "whole", "whole_word"}:
        whole_word = True
    else:
        whole_word = _config_bool(value.get("whole_word"), default=True)

    return {
        "replacement": str(replacement if replacement is not None else ""),
        "verbatim": _config_bool(value.get("verbatim"), default=False),
        "whole_word": whole_word,
    }


def make_glossary_term_value(replacement, *, verbatim=False, whole_word=True):
    """Сохраняет простой legacy-вид, пока расширенные флаги не нужны."""
    replacement = str(replacement if replacement is not None else "")
    if not verbatim and whole_word:
        return replacement
    return {
        "replacement": replacement,
        "verbatim": bool(verbatim),
        "whole_word": bool(whole_word),
    }


class _VerbatimText(str):
    """Точная замена вместе с исходным фрагментом для контекстного прохода.

    Строковый подкласс оставляет прежний внутренний контракт сегментов:
    существующий код может объединять и показывать значение как обычный
    ``str``. Дополнительное поле нужно только нормализатору — сначала он видит
    исходный термин и сохраняет грамматический контекст, а точная замена
    возвращается уже после его обработки.
    """

    def __new__(cls, replacement, source):
        value = super().__new__(cls, str(replacement))
        value.source = str(source)
        return value


def _stable_unique(values, *, ignore_case=False):
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        key = value.casefold() if ignore_case else value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def merge_glossary_data(
    current,
    imported,
    selected_sections=("accents", "terms", "regex"),
    *,
    replace_existing=False,
):
    """Объединяет переносимый глоссарий, не затирая правила по умолчанию.

    Централизованный набор исправлений можно импортировать поверх личного
    файла: при конфликте сохраняется уже существующее правило. Явный режим
    ``replace_existing`` оставляет прежнюю возможность заменить его импортом.
    Возвращаемая статистика используется диалогом и тестами.
    """
    if not isinstance(current, dict) or not isinstance(imported, dict):
        raise ValueError("корень глоссария должен быть JSON-объектом")

    merged = copy.deepcopy(current)
    selected = set(selected_sections)
    stats = {"added": 0, "replaced": 0, "kept": 0, "duplicates": 0}

    if "accents" in selected:
        for key, ignore_case in (
            ("accents_ignore_case", True),
            ("accents_strict_case", False),
        ):
            existing = _stable_unique(merged.get(key, []), ignore_case=ignore_case)
            seen = {
                value.casefold() if ignore_case else value for value in existing
            }
            imported_values = imported.get(key, [])
            if not isinstance(imported_values, (list, tuple)):
                imported_values = []
            for value in imported_values:
                if not isinstance(value, str) or not value.strip():
                    continue
                identity = value.casefold() if ignore_case else value
                if identity in seen:
                    stats["duplicates"] += 1
                    continue
                existing.append(value)
                seen.add(identity)
                stats["added"] += 1
            merged[key] = existing

    if "terms" in selected:
        for key, ignore_case in (
            ("terms_ignore_case", True),
            ("terms_strict_case", False),
        ):
            existing = merged.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            else:
                existing = copy.deepcopy(existing)
            identities = {
                (term.casefold() if ignore_case else term): term
                for term in existing
                if isinstance(term, str)
            }
            incoming = imported.get(key, {})
            if not isinstance(incoming, dict):
                incoming = {}
            for term, value in incoming.items():
                if not isinstance(term, str) or not term.strip():
                    continue
                identity = term.casefold() if ignore_case else term
                previous_key = identities.get(identity)
                if previous_key is None:
                    existing[term] = copy.deepcopy(value)
                    identities[identity] = term
                    stats["added"] += 1
                elif replace_existing:
                    existing[previous_key] = copy.deepcopy(value)
                    stats["replaced"] += 1
                else:
                    stats["kept"] += 1
            merged[key] = existing

    if "regex" in selected:
        existing = merged.get("regex_rules", [])
        existing = copy.deepcopy(existing) if isinstance(existing, list) else []
        pattern_positions = {}
        for index, rule in enumerate(existing):
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern") or rule.get("regex")
            if isinstance(pattern, str) and pattern not in pattern_positions:
                pattern_positions[pattern] = index
        incoming = imported.get("regex_rules", [])
        if not isinstance(incoming, list):
            incoming = []
        for rule in incoming:
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern") or rule.get("regex")
            if not isinstance(pattern, str) or not pattern:
                continue
            previous_index = pattern_positions.get(pattern)
            if previous_index is None:
                pattern_positions[pattern] = len(existing)
                existing.append(copy.deepcopy(rule))
                stats["added"] += 1
            elif replace_existing:
                existing[previous_index] = copy.deepcopy(rule)
                stats["replaced"] += 1
            else:
                stats["kept"] += 1
        merged["regex_rules"] = existing

    for key, default in GLOSSARY_SECTION_DEFAULTS.items():
        merged.setdefault(key, copy.deepcopy(default))
    return merged, stats


def glossary_shadowed_accent_rules(data):
    """Находит ударения, которые перекрываются термином того же регистра.

    Исторически правило термина имеет приоритет над ударением с тем же
    исходным словом. Сохраняем это совместимое поведение, но возвращаем
    точные ключи для предупреждений и менеджера правил вместо молчаливого
    исчезновения ударения во время компиляции.
    """
    normalized = canonicalize_glossary_data(data)
    conflicts = []
    for accent_section, term_section, ignore_case in (
        ("accents_ignore_case", "terms_ignore_case", True),
        ("accents_strict_case", "terms_strict_case", False),
    ):
        terms = {}
        for term in normalized[term_section]:
            identity = term.lower() if ignore_case else term
            terms.setdefault(identity, term)
        for index, accent in enumerate(normalized[accent_section]):
            source = accent.replace("+", "")
            identity = source.lower() if ignore_case else source
            term = terms.get(identity)
            if term is not None:
                conflicts.append(
                    {
                        "key": (accent_section, index),
                        "accent": accent,
                        "term": term,
                        "ignore_case": ignore_case,
                    }
                )
    return tuple(conflicts)


def glossary_rule_records(data):
    """Возвращает стабильные записи для менеджера глоссария.

    ``key`` указывает на точное место правила в JSON. Индексы
    нужны для списков: одинаковые RegEx или ударения всё ещё
    можно удалить по одному, не затрагивая соседние дубли.
    """
    normalized = canonicalize_glossary_data(data)
    records = []
    shadowed_accents = {
        conflict["key"] for conflict in glossary_shadowed_accent_rules(normalized)
    }

    for section, strict_case in (
        ("accents_ignore_case", False),
        ("accents_strict_case", True),
    ):
        for index, source in enumerate(normalized[section]):
            flags = "регистр: строгий" if strict_case else "регистр: любой"
            if (section, index) in shadowed_accents:
                flags += ", не применяется: приоритет термина"
            records.append(
                {
                    "key": (section, index),
                    "group": "accents",
                    "kind": "Ударение",
                    "source": source,
                    "replacement": "",
                    "flags": flags,
                }
            )

    for section, strict_case in (
        ("terms_ignore_case", False),
        ("terms_strict_case", True),
    ):
        for source, value in normalized[section].items():
            spec = glossary_term_spec(value)
            flags = [
                "регистр: строгий" if strict_case else "регистр: любой",
                "слово целиком" if spec["whole_word"] else "часть слова",
            ]
            if spec["verbatim"]:
                flags.append("verbatim")
            records.append(
                {
                    "key": (section, source),
                    "group": "terms",
                    "kind": "Термин",
                    "source": source,
                    "replacement": spec["replacement"],
                    "flags": ", ".join(flags),
                }
            )

    for index, rule in enumerate(normalized["regex_rules"]):
        records.append(
            {
                "key": ("regex_rules", index),
                "group": "regex",
                "kind": "RegEx",
                "source": str(rule.get("pattern") or rule.get("regex") or ""),
                "replacement": str(rule.get("repl", "")),
                "flags": "verbatim" if _config_bool(rule.get("verbatim")) else "обычное",
            }
        )

    for record in records:
        record["search_text"] = " ".join(
            str(record[field])
            for field in ("kind", "source", "replacement", "flags")
        ).casefold()
    return tuple(records)


def remove_glossary_rules(data, rule_keys):
    """Удаляет точно выбранные правила и сохраняет будущие поля JSON."""
    result = canonicalize_glossary_data(data)
    selected = {
        tuple(key)
        for key in rule_keys
        if isinstance(key, (list, tuple)) and len(key) == 2
    }
    removed = 0

    for section in ("accents_ignore_case", "accents_strict_case"):
        previous = result[section]
        result[section] = [
            value
            for index, value in enumerate(previous)
            if (section, index) not in selected
        ]
        removed += len(previous) - len(result[section])

    for section in ("terms_ignore_case", "terms_strict_case"):
        previous = result[section]
        result[section] = {
            source: value
            for source, value in previous.items()
            if (section, source) not in selected
        }
        removed += len(previous) - len(result[section])

    previous_regex = result["regex_rules"]
    result["regex_rules"] = [
        rule
        for index, rule in enumerate(previous_regex)
        if ("regex_rules", index) not in selected
    ]
    removed += len(previous_regex) - len(result["regex_rules"])
    return result, removed


def clear_glossary_rules(data):
    """Очищает все известные секции, но не стирает будущие корневые поля."""
    result = canonicalize_glossary_data(data)
    removed = len(glossary_rule_records(result))
    for section, default in GLOSSARY_SECTION_DEFAULTS.items():
        result[section] = copy.deepcopy(default)
    return result, removed


def paragraph_starts_with_speech(text):
    """Распознаёт абзац-реплику по тире либо открывающей кавычке."""
    value = str(text or "").lstrip()
    if not value:
        return False
    if value[0] in _QUOTED_SPEECH_OPENERS:
        return True
    return re.match(_DIALOGUE_PREFIX_PATTERN, value) is not None


def paragraph_ends_with_colon(text):
    """Учитывает двоеточие и перед закрывающими кавычками/скобками."""
    value = str(text or "").rstrip()
    while value and value[-1] in _COLON_TRAILING_CLOSERS:
        value = value[:-1].rstrip()
    return value.endswith(":")


def paragraph_boundary_pause(config, paragraph, previous_ended_with_colon=False):
    """Выбирает одну максимальную паузу для границы двух абзацев.

    Пауза между абзацами, пауза перед репликой/цитатой и пауза после
    двоеточия являются альтернативными требованиями к одной и той же границе,
    поэтому никогда не суммируются.
    """
    candidates = [int(config.get("pause_paragraph", 0))]
    if paragraph_starts_with_speech(paragraph):
        candidates.append(int(config.get("pause_speech", 0)))
    if previous_ended_with_colon:
        candidates.append(int(config.get("pause_colon", 0)))
    return max(candidates)


def _existing_directory(path, *, file_path=False):
    """Возвращает существующую папку либо ``None`` без создания каталогов."""
    if path is None:
        return None
    try:
        raw_path = str(path).strip()
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        if file_path:
            candidate = candidate.parent
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return None


def resolve_dialog_initial_dir(*candidates, file_path=False):
    """Единое правило старта файловых диалогов.

    Сначала используется первое существующее значение поля/истории, затем
    папка проекта. Это исключает платформенный fallback к корню диска при
    пустой или уже удалённой директории.
    """
    for candidate in candidates:
        directory = _existing_directory(candidate, file_path=file_path)
        if directory is not None:
            return str(directory)
    return str(BASE_DIR)


def format_sequence_number(number, total_count, start_index=1):
    """Адаптивный номер по последнему отображаемому значению диапазона.

    ``total_count`` — количество элементов, а ``start_index`` — первый номер.
    Поэтому диапазон 8..10 должен иметь ширину 2, хотя элементов всего три.
    """
    number = int(number)
    total_count = max(1, int(total_count))
    start_index = int(start_index)
    last_number = start_index + total_count - 1
    width = max(1, len(str(max(abs(start_index), abs(last_number)))))
    if number < 0:
        return "-" + str(abs(number)).zfill(width)
    return str(number).zfill(width)


def next_sequence_name(template, existing_names):
    """Возвращает первое свободное имя, сохраняя разрядность серии.

    Значения ``1`` и ``01`` считаются одним логическим номером. Параметр в
    ``{num:N}`` остаётся стартовым номером, как и в шаблонах импорта книг.
    """
    template = str(template or "").strip() or "Группа {num}"
    names = {str(name) for name in existing_names}
    placeholder = re.compile(r"\{num(?::(\d+))?\}")
    matches = list(placeholder.finditer(template))

    if not matches:
        if template not in names:
            return template
        suffix = 2
        while f"{template} {suffix}" in names:
            suffix += 1
        return f"{template} {suffix}"

    start_index = int(matches[0].group(1) or 1)
    pattern_parts = ["^"]
    cursor = 0
    for index, match in enumerate(matches):
        pattern_parts.append(re.escape(template[cursor:match.start()]))
        pattern_parts.append(
            r"(?P<number>\d+)" if index == 0 else r"(?P=number)"
        )
        cursor = match.end()
    pattern_parts.extend((re.escape(template[cursor:]), "$"))
    rendered_pattern = re.compile("".join(pattern_parts))

    used_numbers = set()
    existing_width = 1
    for name in names:
        rendered_match = rendered_pattern.fullmatch(name)
        if rendered_match is None:
            continue
        number_text = rendered_match.group("number")
        used_numbers.add(int(number_text))
        existing_width = max(existing_width, len(number_text))

    number = start_index
    while True:
        if number not in used_numbers:
            width = max(existing_width, len(str(number)))
            candidate = placeholder.sub(str(number).zfill(width), template)
            if candidate not in names:
                return candidate
        number += 1


def ordered_export_file_ids(root_items, group_children, file_ids):
    """Разворачивает корневые файлы и группы в текущем порядке дерева."""
    known_files = set(file_ids)
    ordered = []
    seen = set()

    def add(file_id):
        if file_id in known_files and file_id not in seen:
            seen.add(file_id)
            ordered.append(file_id)

    for item_id in root_items:
        if item_id in known_files:
            add(item_id)
        else:
            for file_id in group_children.get(item_id, ()):
                add(file_id)

    # Защита от временно рассинхронизированного дерева: не теряем известные
    # файлы, даже если вызывающий код передал неполный снимок структуры.
    for file_id in file_ids:
        add(file_id)
    return ordered


def plan_export_merge(
    root_items,
    group_children,
    selected_items,
    group_ids,
    file_ids,
):
    """Строит безопасный план слияния в видимом порядке дерева.

    Первая выбранная по дереву группа становится целевой. Если
    групп нет, GUI создаст новую. Выбор группы означает всех её
    детей; отдельно выбранный ребёнок не дублируется.
    """
    roots = tuple(root_items)
    children = {
        group_id: tuple(items)
        for group_id, items in dict(group_children).items()
    }
    selected_order = tuple(selected_items)
    selected = set(selected_order)
    known_groups = set(group_ids)
    known_files = set(file_ids)
    selected_groups = []
    ordered_files = []
    seen_files = set()

    def add_file(file_id):
        if file_id in known_files and file_id not in seen_files:
            seen_files.add(file_id)
            ordered_files.append(file_id)

    def add_group(group_id):
        if group_id not in selected_groups:
            selected_groups.append(group_id)
        for file_id in children.get(group_id, ()):
            add_file(file_id)

    for item_id in roots:
        if item_id in known_groups:
            if item_id in selected:
                add_group(item_id)
            else:
                for file_id in children.get(item_id, ()):
                    if file_id in selected:
                        add_file(file_id)
        elif item_id in selected:
            add_file(item_id)

    # Временно рассинхронизированная модель не должна терять
    # выбр: сначала добавляем осиротевшие группы, затем файлы.
    for item_id in selected_order:
        if item_id in known_groups and item_id not in selected_groups:
            add_group(item_id)
    for item_id in selected_order:
        add_file(item_id)

    target_group = selected_groups[0] if selected_groups else None
    return {
        "target_group": target_group,
        "source_groups": tuple(selected_groups[1:]),
        "file_ids": tuple(ordered_files),
    }


def split_export_file_ids(ordered_file_ids, durations, limit_seconds):
    """Последовательно разбивает файлы по максимальной длительности группы."""
    limit_seconds = int(limit_seconds)
    if limit_seconds <= 0:
        raise ValueError("Максимальная длительность должна быть больше нуля")

    groups = []
    current_group = []
    current_duration = 0
    for file_id in ordered_file_ids:
        duration = max(0, int(durations.get(file_id, 0)))
        if current_group and current_duration + duration > limit_seconds:
            groups.append(current_group)
            current_group = []
            current_duration = 0
        current_group.append(file_id)
        current_duration += duration
    if current_group:
        groups.append(current_group)
    return groups


def effective_group_subfolder(merge_files, subfolder_requested):
    """Подпапка имеет смысл только при экспорте отдельных файлов группы."""
    return bool(subfolder_requested) and not bool(merge_files)


def normalize_dialogue_line_starts(text):
    """Унифицирует тире реплики, не превращая знак числа в тире диалога.

    Обычный минус сохраняется перед количественным числом как слитно, так и
    через пробелы. Неоднозначный ASCII-дефис перед порядковым числительным
    считается маркером реплики только при явном пробеле (``- 62-й ранг``), а
    слитное ``-62-й`` остаётся отрицательным числом. Семантический Unicode-минус
    ``−`` перед цифрой всегда приводится к ``-`` уже после распознавания реплик,
    поэтому не теряет смысл даже в ``− 62-й``. Короткое/длинное тире ``–``/``—``
    всегда остаётся оформлением прямой речи. Строки-разделители к этому моменту
    уже защищены служебным токеном.
    """
    text = re.sub(
        _DIALOGUE_PREFIX_PATTERN,
        "— ",
        str(text),
        flags=re.MULTILINE,
    )
    return re.sub(
        r"^([ \t]*)−(?=[ \t]*\d)",
        r"\1-",
        text,
        flags=re.MULTILINE,
    )


def strip_dialogue_prefix(text):
    """Удаляет тире реплики, сохраняя минус перед количественным числом."""
    text = re.sub(
        _DIALOGUE_PREFIX_PATTERN,
        "",
        str(text),
    )
    return re.sub(
        r"^([ \t]*)−(?=[ \t]*\d)",
        r"\1-",
        text,
    )


def sanitize_filename_component(value, fallback="Без названия", max_length=180):
    """Возвращает безопасное имя одного файла/каталога для всех целевых ОС.

    Имена экспортного проекта могут редактироваться на macOS, а затем
    использоваться в Windows/Linux-сборке. Поэтому заранее исключаем не только
    разделители текущей ОС, но и управляющие символы, зарезервированные имена
    Windows и завершающие точки/пробелы.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")

    fallback_text = unicodedata.normalize("NFC", str(fallback or "Без названия"))
    fallback_text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fallback_text)
    fallback_text = re.sub(r"\s+", " ", fallback_text).strip().rstrip(". ")
    if not fallback_text:
        fallback_text = "Без названия"
    if not text:
        text = fallback_text

    # Windows считает эти basename зарезервированными даже с расширением.
    if re.fullmatch(
        r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?",
        text,
    ):
        text = f"_{text}"

    max_length = max(1, int(max_length))
    text = text[:max_length].rstrip(". ")
    return text or fallback_text[:max_length] or "_"


def normalize_display_text(value, fallback="Без названия"):
    """Готовит однострочную UI-подпись, не меняя исходные метаданные."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        return text
    fallback_text = unicodedata.normalize("NFC", str(fallback or "Без названия"))
    fallback_text = re.sub(
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f]+", "", fallback_text
    )
    fallback_text = re.sub(r"\s+", " ", fallback_text).strip()
    return fallback_text or "Без названия"


def middle_ellipsize(value, max_chars=56, fallback="Без названия"):
    """Сокращает длинную подпись посередине, сохраняя начало и окончание."""
    text = normalize_display_text(value, fallback=fallback)
    max_chars = max(1, int(max_chars))
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    remaining = max_chars - 1
    head_length = (remaining + 1) // 2
    tail_length = remaining - head_length
    if tail_length <= 0:
        return text[:head_length] + "…"
    return text[:head_length] + "…" + text[-tail_length:]


def middle_ellipsize_to_width(value, max_width, measure, fallback="Без названия"):
    """Сокращает подпись по фактической ширине текущего шрифта в пикселях."""
    text = normalize_display_text(value, fallback=fallback)
    max_width = max(0, int(max_width))
    if measure(text) <= max_width:
        return text
    if measure("…") > max_width:
        return ""

    low, high = 1, len(text)
    best = "…"
    while low <= high:
        middle = (low + high) // 2
        candidate = middle_ellipsize(text, max_chars=middle, fallback=fallback)
        if measure(candidate) <= max_width:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def format_export_activity_status(action, subject, max_subject_chars=56):
    """Формирует компактный однострочный статус экспортной вкладки."""
    action = normalize_display_text(action, fallback="Обработка")
    subject = middle_ellipsize(subject, max_chars=max_subject_chars)
    return f"{action}: {subject}"


def _config_group_rules_data():
    """Точные группы профиля; UI-история намеренно сюда не входит."""
    return {
        "api": {
            "api_token", "api_url", "speaker", "api_steps_enabled",
            "api_steps", "cache_include_steps", "api_max_requests",
            "api_time_window", "max_retries", "max_parallel_encodes",
        },
        "folders": {
            "input_dir", "output_dir", "direct_output_dir", "cache_dir",
            "export_dir", "import_outdir",
        },
        "pauses": {
            "pause_file_start", "pause_file_end", "pause_sentence",
            "pause_paragraph", "pause_speech", "pause_colon",
            "pause_separator", "separator_symbols", "default_group_pause",
        },
        "cache": {
            "auto_trim_silence", "auto_abbreviations", "auto_short_words",
            "silence_threshold", "use_cache", "cache_save_frequency",
            "enable_cache_lru", "enable_cache_ttl", "cache_max_entries",
            "cache_ttl_hours", "skip_existing",
        },
        "normalizer": {
            "normalizer_enabled", "normalizer_mode", "normalizer_options",
            "glossary_enabled", "auto_abbreviations", "auto_short_words",
        },
        "effects": {
            "fx_speed", "fx_pitch", "fx_echo", "fx_echo_delay",
            "fx_echo_decay", "export_apply_fx", "export_fx_speed",
            "export_fx_pitch", "export_fx_echo", "export_fx_echo_delay",
            "export_fx_echo_decay",
        },
        "tags": {
            "output_format", "output_bitrate", "output_sample_rate",
            "output_channels", "export_format", "export_bitrate",
            "export_sample_rate", "export_channels", "audio_profiles",
            "synthesis_mode",
            "tag_title", "tag_artist", "tag_album_artist", "tag_album",
            "tag_genre", "tag_composer", "tag_year", "tag_cover",
            "default_group_name",
        },
        # Значения по умолчанию вкладок — часть переносимого рабочего профиля,
        # но отделены от движка и путей, чтобы их можно было исключить одним
        # понятным флажком.
        "workspace": {
            "direct_filename", "direct_save", "direct_force",
            "direct_autoplay", "import_template", "import_regex",
            "import_single_file",
        },
    }


def select_config_values(config, selected_groups, *, include_api_token=True):
    """Формирует переносимый профиль только из выбранных известных ключей."""
    rules = _config_group_rules_data()
    selected_keys = set()
    for group_name in selected_groups:
        selected_keys.update(rules.get(group_name, ()))
    selected = {
        key: copy.deepcopy(value)
        for key, value in config.items()
        if key in selected_keys
    }
    if not include_api_token:
        selected.pop("api_token", None)
    return selected


def merge_config_values(
    current_config,
    imported_config,
    selected_groups,
    *,
    include_api_token=True,
):
    """Возвращает копию конфига с выбранными полями импортируемого профиля."""
    selected_groups = tuple(selected_groups)
    merged = dict(current_config)
    selected = select_config_values(
        imported_config,
        selected_groups,
        include_api_token=include_api_token,
    )
    # До v1.5 output_format одновременно управлял книгой и универсальным
    # экспортом. Та же миграция, что выполняется при загрузке settings.json,
    # нужна и при выборочном импорте старого профиля в уже новый конфиг.
    if (
        "tags" in selected_groups
        and "output_format" in selected
        and "export_format" not in imported_config
    ):
        selected["export_format"] = copy.deepcopy(selected["output_format"])
    merged.update(selected)
    return merged


def resolve_cache_audio_path(cache_dir, file_name):
    """Возвращает путь только к обычному имени файла внутри ``cache/audio``.

    Индекс кэша является локальным JSON, но его можно импортировать из архива
    или отредактировать вручную. Запрещаем абсолютные пути и ``..``, чтобы
    удаление/прослушивание записи не затронуло произвольный файл пользователя.
    """
    if not isinstance(file_name, str) or not file_name.strip():
        return None
    raw_name = file_name.strip()
    # Path на POSIX не считает обратную косую черту разделителем. Проверяем
    # оба варианта явно, поскольку индекс может быть перенесён с Windows.
    if "/" in raw_name or "\\" in raw_name or "\x00" in raw_name:
        return None
    candidate = Path(raw_name)
    if candidate.is_absolute() or candidate.name != str(candidate):
        return None
    if candidate.name in (".", ".."):
        return None
    return Path(cache_dir) / "audio" / candidate.name


def audio_export_kwargs(output_format, bitrate=None, tags=None, cover=None):
    """Формирует безопасные аргументы Pydub для выбранного контейнера."""
    fmt = str(output_format).lower().strip()
    kwargs = {"format": fmt}
    if fmt in {"mp3", "ogg", "opus"} and bitrate:
        kwargs["bitrate"] = bitrate
    if tags:
        kwargs["tags"] = tags
    # Pydub передаёт cover как attached picture, что поддерживает его MP3-
    # backend. WAV/OGG требуют другого представления обложек и иначе падают.
    if fmt == "mp3" and cover and Path(cover).is_file():
        kwargs["cover"] = str(cover)
    return kwargs


def _cover_mime_type(path):
    """Определяет MIME JPEG/PNG по сигнатуре, а не по расширению файла."""
    path = Path(path)
    try:
        with path.open("rb") as cover_file:
            signature = cover_file.read(16)
    except OSError:
        return None
    if signature.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _xiph_metadata_block_picture(cover):
    """Возвращает base64 FLAC-picture block для VorbisComment/OpusTags.

    Ogg Opus и Ogg Vorbis не хранят обложку как обычный видеопоток в файле.
    Стандартный способ Xiph — бинарная структура FLAC PICTURE, закодированная
    base64 в комментарии ``METADATA_BLOCK_PICTURE``. FFmpeg при чтении
    представляет такой комментарий как виртуальный ``attached_pic``-поток,
    поэтому существующее извлечение обложек продолжает работать.

    В 1.4.1 helper применяется к Opus. Ogg/Vorbis пока сохраняет прежнюю
    политику безопасного пропуска обложки, чтобы не менять его release-поведение.
    """
    path = Path(cover)
    mime_type = _cover_mime_type(path)
    if mime_type is None:
        raise ValueError("Обложка должна быть файлом JPEG или PNG")
    image_data = path.read_bytes()
    mime_bytes = mime_type.encode("ascii")
    description = b""
    picture = b"".join(
        (
            struct.pack(">I", 3),  # front cover
            struct.pack(">I", len(mime_bytes)),
            mime_bytes,
            struct.pack(">I", len(description)),
            description,
            # Размеры/глубина необязательны в Xiph picture block. Нули не
            # требуют декодировать изображение в Python и валидны для JPEG/PNG.
            struct.pack(">IIIII", 0, 0, 0, 0, len(image_data)),
            image_data,
        )
    )
    return base64.b64encode(picture).decode("ascii")


def _xiph_cover_metadata(cover, output_name=None):
    """Безопасно готовит обложку Xiph; ошибочная картинка не срывает звук."""
    if not cover or not Path(cover).is_file():
        return None
    try:
        return _xiph_metadata_block_picture(cover)
    except (OSError, ValueError) as exc:
        logging.warning(
            "Обложка пропущена%s: %s",
            f" для {output_name}" if output_name else "",
            exc,
        )
        return None


def _create_xiph_cover_metadata_file(cover, output_name=None):
    """Создаёт короткоживущий FFmetadata input без лимита командной строки.

    Base64 обычной книжной обложки легко превышает предел аргументов Windows
    или macOS. Поэтому передаём ``METADATA_BLOCK_PICTURE`` FFmpeg через файл,
    закрытый до запуска дочернего процесса, а вызывающий код удаляет его после
    завершения операции.
    """
    encoded_picture = _xiph_cover_metadata(cover, output_name)
    if not encoded_picture:
        return None
    SESSION_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    metadata_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            newline="\n",
            prefix="opus_cover_",
            suffix=".ffmeta",
            dir=SESSION_TEMP_DIR,
            delete=False,
        ) as metadata_file:
            metadata_path = Path(metadata_file.name)
            metadata_file.write(";FFMETADATA1\n")
            metadata_file.write(
                f"METADATA_BLOCK_PICTURE={encoded_picture}\n"
            )
        return metadata_path
    except Exception:
        if metadata_path is not None:
            metadata_path.unlink(missing_ok=True)
        raise


def normalize_clipboard_text(value):
    """Декодирует пути из Finder, не изменяя обычный текст буфера обмена."""
    text = str(value)
    candidate = text.strip()
    if not candidate:
        return text

    if (
        len(candidate) >= 2
        and candidate[0] in {'"', "'"}
        and candidate[-1] == candidate[0]
    ):
        path_candidate = candidate[1:-1]
    else:
        path_candidate = candidate

    if path_candidate.lower().startswith("file://"):
        try:
            parsed = urllib.parse.urlsplit(path_candidate)
        except ValueError:
            return text
        path = urllib.parse.unquote(parsed.path)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        if platform.system() == "Windows" and re.match(r"^/[A-Za-z]:[\\/]", path):
            path = path[1:]
        return unicodedata.normalize("NFC", path)

    looks_like_path = (
        path_candidate.startswith(("/", "~/", "\\\\"))
        or re.match(r"^[A-Za-z]:[\\/]", path_candidate) is not None
    )
    if looks_like_path:
        return unicodedata.normalize(
            "NFC",
            urllib.parse.unquote(path_candidate),
        )
    return text


def duplicate_paths(paths):
    """Возвращает пути, конфликтующие на Windows и типичной macOS FS."""
    groups = {}
    for path in paths:
        value = str(path)
        key = unicodedata.normalize("NFC", value).casefold()
        groups.setdefault(key, []).append(value)
    duplicates = {
        value
        for values in groups.values()
        if len(values) > 1
        for value in values
    }
    return sorted(
        duplicates,
        key=lambda value: (unicodedata.normalize("NFC", value).casefold(), value),
    )


def unique_new_file_paths(paths, existing_paths=()):
    """Оставляет новые локальные пути в исходном порядке без дублей.

    Дедупликация выполняется до фонового чтения метаданных. Это гарантирует,
    что последний уже существующий элемент не помешает отправить в UI
    накопившуюся неполную пачку действительно новых файлов.
    """

    def identity(value):
        try:
            normalized = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            normalized = str(value)
        normalized = unicodedata.normalize("NFC", normalized)
        return normalized.casefold() if platform.system() == "Windows" else normalized

    seen = {identity(path) for path in existing_paths}
    result = []
    for path in paths:
        key = identity(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path))
    return tuple(result)


def normalize_output_filename(value, output_format, fallback="direct_output"):
    """Нормализует пользовательское имя direct-файла для всех целевых ОС."""
    raw_name = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(raw_name).stem if raw_name else fallback
    safe_stem = sanitize_filename_component(stem, fallback=fallback)
    safe_format = str(output_format or "mp3").lower().strip()
    if safe_format not in {"mp3", "wav", "ogg", "opus"}:
        safe_format = "mp3"
    return f"{safe_stem}.{safe_format}"


def remove_cache_index_files(cache_dir):
    """Удаляет рабочий индекс кэша, резервную копию и временные снимки."""
    cache_dir = Path(cache_dir)
    for pattern in (
        "sentence_cache.json",
        "sentence_cache.json.bak",
        "sentence_cache.json.tmp",
        ".sentence_cache.json.*.tmp",
    ):
        for path in cache_dir.glob(pattern):
            path.unlink(missing_ok=True)


def validate_cache_index(data):
    """Проверяет актуальный формат ``sentence_cache.json``.

    Публичные GUI-релизы уже используют словарь метаданных. Древний формат
    ``hash -> строка пути`` намеренно не мигрируется: работа с ним скрывала бы
    повреждённый/чужой индекс и мешала безопасной оптимизации.
    """
    if not isinstance(data, dict):
        raise ValueError("корень индекса должен быть JSON-объектом")
    for cache_key, cache_info in data.items():
        if not isinstance(cache_key, str):
            raise ValueError("хэш записи кэша должен быть строкой")
        if not isinstance(cache_info, dict):
            raise ValueError(
                f"запись {cache_key!r} не является JSON-объектом метаданных"
            )
    return data


def read_cache_index_with_backup(cache_path):
    """Читает основной индекс, затем ``.bak``.

    Возвращает ``(data, source_path, errors)``. Ошибки не скрываются, но
    вызывающий код может продолжить с валидной резервной копией.
    """
    cache_path = Path(cache_path)
    backup_path = cache_path.with_suffix(cache_path.suffix + ".bak")
    errors = []
    for candidate in (cache_path, backup_path):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8-sig") as file:
                return validate_cache_index(json.load(file)), candidate, errors
        except Exception as exc:
            errors.append((candidate, exc))
    return {}, None, errors


def write_cache_index_atomic(cache_dir, cache):
    """Атомарно публикует индекс и зеркальную валидную ``.bak``-копию."""
    cache_dir = Path(cache_dir)
    cache = validate_cache_index(cache)
    if not cache:
        remove_cache_index_files(cache_dir)
        return

    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / "sentence_cache.json"
    backup_path = index_path.with_suffix(index_path.suffix + ".bak")
    temp_path = index_path.with_name(
        f".{index_path.name}.{uuid.uuid4().hex}.tmp"
    )
    backup_temp_path = backup_path.with_name(
        f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False, indent=4)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, index_path)
        try:
            # Для кэша резервная копия должна отражать уже подтверждённое
            # состояние. Иначе восстановление могло воскресить удалённые ключи.
            # Пишем её через отдельный временный файл: авария во время copy2
            # оставит предыдущую валидную .bak, а не обрезанную копию.
            shutil.copy2(index_path, backup_temp_path)
            os.replace(backup_temp_path, backup_path)
        except OSError as exc:
            logging.warning("Не удалось обновить резервную копию кэша: %s", exc)
    finally:
        temp_path.unlink(missing_ok=True)
        backup_temp_path.unlink(missing_ok=True)


def unreferenced_cache_audio_paths(cache_dir, removed_entries, retained_cache):
    """Возвращает безопасные файлы, на которые больше не ссылается индекс."""
    retained_names = {
        info.get("file_name")
        for info in retained_cache.values()
        if isinstance(info, dict) and isinstance(info.get("file_name"), str)
    }
    paths = []
    seen = set()
    for cache_info in removed_entries:
        if not isinstance(cache_info, dict):
            continue
        filename = cache_info.get("file_name")
        if filename in retained_names:
            continue
        filepath = resolve_cache_audio_path(cache_dir, filename)
        if filepath is not None and filepath not in seen:
            seen.add(filepath)
            paths.append(filepath)
        elif filename:
            logging.warning(
                "Пропущено небезопасное имя файла в индексе кэша: %r",
                filename,
            )
    return paths


def clear_cache_storage(cache_dir):
    """Удаляет только принадлежащие приложению файлы синтезированного кэша.

    Нельзя безусловно делать ``rmtree(cache_dir / 'audio')``: пользователь
    может случайно выбрать общей папкой кэша домашний каталог, где уже есть
    собственная папка ``audio``. Формат имён приложения достаточно строг,
    чтобы удалить кэш и сохранить посторонние файлы вместе с glossary.json.
    """
    cache_dir = Path(cache_dir)
    # Сначала убираем индекс: даже частичный сбой последующего rmtree не
    # оставит JSON, указывающий на уже удалённые аудиофайлы.
    remove_cache_index_files(cache_dir)
    audio_dir = cache_dir / "audio"
    silence_dir = cache_dir / "silences"
    owned_patterns = (
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f].ogg",
        "temp_*.ogg",
        "prepared_*.ogg",
    )
    if audio_dir.is_dir():
        for pattern in owned_patterns:
            for path in audio_dir.glob(pattern):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)

    if silence_dir.is_dir():
        for pattern in ("silence_*ms.ogg", ".silence_*.tmp.ogg"):
            for path in silence_dir.glob(pattern):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        try:
            silence_dir.rmdir()
        except OSError:
            # Посторонние файлы/папки намеренно сохраняются.
            pass

    # Очень ранние локальные сборки могли оставлять рабочие OGG в корне.
    for pattern in ("temp_*.ogg", "prepared_*.ogg"):
        for path in cache_dir.glob(pattern):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)

    audio_dir.mkdir(parents=True, exist_ok=True)


def create_zip_archive_atomic(source_dir, destination):
    """Создаёт ZIP рядом с целью и публикует его одной заменой."""
    source_dir = Path(source_dir).resolve()
    destination = Path(destination).expanduser().with_suffix(".zip").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_base = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp"
    )
    temp_archive = temp_base.with_suffix(temp_base.suffix + ".zip")
    try:
        created_path = Path(
            shutil.make_archive(
                str(temp_base),
                "zip",
                root_dir=source_dir.parent,
                base_dir=source_dir.name,
            )
        )
        if not created_path.is_file():
            raise OSError("Архиватор не создал выходной ZIP-файл")
        os.replace(created_path, destination)
        return destination
    finally:
        temp_archive.unlink(missing_ok=True)


def resolve_api_steps(config):
    """Возвращает включённый ``steps`` либо ``None`` для серверного значения по умолчанию.

    Отсутствующий ключ (старый конфиг) всегда означает прежнее поведение:
    параметр не добавляется в payload. Проверка централизована, чтобы API и
    ключ кэша гарантированно использовали одно и то же целое значение.
    """
    enabled = _config_bool(config.get("api_steps_enabled", False))
    if not enabled:
        return None

    raw_value = config.get("api_steps", 16)
    if isinstance(raw_value, bool):
        raise ValueError("Steps должен быть положительным целым числом")

    try:
        if isinstance(raw_value, int):
            steps = raw_value
        elif isinstance(raw_value, float):
            if not raw_value.is_integer():
                raise ValueError
            steps = int(raw_value)
        else:
            raw_text = str(raw_value).strip()
            if not re.fullmatch(r"[+-]?\d+", raw_text):
                raise ValueError
            steps = int(raw_text)
    except (TypeError, ValueError):
        raise ValueError("Steps должен быть положительным целым числом") from None

    if steps < API_STEPS_MIN:
        raise ValueError("Steps должен быть положительным целым числом (1 или больше)")
    if steps > API_STEPS_MAX:
        raise ValueError(
            f"Steps должен быть не больше {API_STEPS_MAX}. "
            "Текущий Silero EnhancedTTS отклоняет большие значения с ошибкой HTTP 422."
        )
    return steps


def get_api_steps_warning(steps):
    """Возвращает предупреждение для большого steps, не запрещая запрос."""
    if steps is None or steps < API_STEPS_SOFT_WARNING:
        return None
    if steps >= API_STEPS_STRONG_WARNING:
        return (
            f"Вы выбрали Steps = {steps}. Начиная примерно с 32 заметного "
            "прироста качества обычно уже нет, а результат может стать менее "
            "стабильным. Синтез при этом займёт значительно больше времени."
        )
    return (
        f"Вы выбрали Steps = {steps}. Значения выше 16 экспериментальны: "
        "обработка станет дольше, а прирост качества не гарантирован."
    )


def analyze_cache_step_variants(cache):
    """Возвращает статистику legacy и явных Steps-вариантов индекса."""
    stats = {
        "total": 0,
        "legacy": 0,
        "steps": 0,
        "steps_by_value": {},
        "shared_steps": 0,
    }
    if not isinstance(cache, dict):
        return stats

    for cache_info in cache.values():
        if not isinstance(cache_info, dict):
            continue
        stats["total"] += 1
        if cache_info.get("steps") is None:
            stats["legacy"] += 1
            continue

        stats["steps"] += 1
        raw_steps = cache_info.get("steps")
        try:
            steps_value = int(raw_steps)
        except (TypeError, ValueError):
            steps_value = str(raw_steps)
        stats["steps_by_value"][steps_value] = (
            stats["steps_by_value"].get(steps_value, 0) + 1
        )
        if not _config_bool(
            cache_info.get("steps_in_cache_key", True), default=True
        ):
            stats["shared_steps"] += 1
    return stats


def should_keep_cache_variant(cache_info, policy, current_steps=None):
    """Определяет, разрешает ли выбранная политика данный вариант качества.

    Эта проверка применяется после проверки актуальности текста. Записи старого
    формата без ``steps`` считаются legacy; известное значение сравнивается как
    целое число, чтобы JSON-строки старых промежуточных сборок не терялись.
    """
    if policy == CACHE_VARIANT_KEEP_ALL:
        return True

    raw_steps = cache_info.get("steps") if isinstance(cache_info, dict) else None
    is_legacy = raw_steps is None
    if policy == CACHE_VARIANT_KEEP_LEGACY:
        return is_legacy

    try:
        cached_steps = int(raw_steps) if raw_steps is not None else None
    except (TypeError, ValueError):
        cached_steps = raw_steps
    is_current = current_steps is not None and cached_steps == current_steps

    if policy == CACHE_VARIANT_KEEP_LEGACY_CURRENT:
        return is_legacy or is_current
    if policy == CACHE_VARIANT_KEEP_CURRENT:
        return is_current
    raise ValueError(f"Неизвестная политика вариантов кэша: {policy}")


def cache_content_hash(normalized_text, speaker):
    """Возвращает канонический хэш содержимого без параметров качества."""
    return hashlib.md5(
        f"{normalized_text}_{speaker}".encode("utf-8")
    ).hexdigest()


def cache_entry_matches_required_text(cache_info, required_hashes):
    """Проверяет запись по metadata, не учитывая её namespace Steps."""
    if not isinstance(cache_info, dict):
        return False
    normalized_text = cache_info.get("normalized_text")
    speaker = cache_info.get("speaker")
    if (
        not isinstance(normalized_text, str)
        or not normalized_text
        or not isinstance(speaker, str)
        or not speaker
    ):
        return False
    return cache_content_hash(normalized_text, speaker) in required_hashes

def _find_bundled_binary(bin_name):
    if not getattr(sys, 'frozen', False):
        return None

    exe_dir = Path(sys.executable).resolve().parent
    bundle_dir = Path(getattr(sys, '_MEIPASS', exe_dir)).resolve()
    candidates = [
        bundle_dir / bin_name,
        exe_dir / bin_name,
        exe_dir.parent / "Frameworks" / bin_name,
        exe_dir.parent / "Resources" / bin_name,
        exe_dir.parent / "MacOS" / bin_name,
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None

def _get_media_binary_path(tool_name):
    """Сначала ищет FFmpeg/FFprobe внутри пакета, затем в системе."""
    bin_name = f"{tool_name}.exe" if platform.system() == "Windows" else tool_name
    if getattr(sys, "frozen", False):
        local_bin = _find_bundled_binary(bin_name)
        if local_bin:
            return local_bin
    return _resolve_external_binary(bin_name) or bin_name


def get_ffmpeg_path():
    return _get_media_binary_path("ffmpeg")


def get_ffprobe_path():
    return _get_media_binary_path("ffprobe")


# === ИМПОРТ PYDUB И НАСТРОЙКА ПУТЕЙ FFMPEG ===
# При импорте Pydub один раз проверяет только PATH и успевает вывести ложное
# предупреждение в frozen/GUI-сборке, хотя встроенный FFmpeg уже найден нами по
# абсолютному пути. Подавляем исключительно это сообщение и только когда файл
# действительно существует; при реальном отсутствии зависимость по-прежнему
# явно диагностируется.
_PRE_RESOLVED_FFMPEG = get_ffmpeg_path()
if Path(_PRE_RESOLVED_FFMPEG).is_file():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Couldn't find ffmpeg or avconv.*",
            category=RuntimeWarning,
        )
        from pydub import AudioSegment
else:
    from pydub import AudioSegment
from pydub.silence import detect_nonsilent


def _create_ffmpeg_concat_manifest(audio_files):
    """Создаёт закрытый перед запуском FFmpeg concat-манифест.

    Каждый вызов владеет только своим файлом. Общую папку ``temp`` нельзя
    рекурсивно очищать во время работы: другой поток или второй экземпляр
    приложения мог уже передать расположенный в ней манифест процессу FFmpeg,
    который ещё не успел открыть файл.
    """
    SESSION_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    list_path = None
    try:
        # NamedTemporaryFile атомарно резервирует имя. delete=False необходим
        # на Windows: FFmpeg открывает файл уже после закрытия Python-дескриптора.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="concat_",
            suffix=".txt",
            dir=SESSION_TEMP_DIR,
            delete=False,
        ) as manifest:
            list_path = Path(manifest.name)
            for audio_path in audio_files:
                safe_path = Path(audio_path).resolve().as_posix().replace("'", "'\\''")
                manifest.write(f"file '{safe_path}'\n")
        return list_path
    except Exception:
        if list_path is not None:
            list_path.unlink(missing_ok=True)
        raise


def _export_audio_atomic(audio_segment, out_path, **export_kwargs):
    """Экспортирует Pydub-сегмент рядом с целью и атомарно заменяет её."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(
        f".{out_path.stem}.{uuid.uuid4().hex}.tmp{out_path.suffix}"
    )
    exported = None
    try:
        exported = audio_segment.export(temp_path, **export_kwargs)
        if hasattr(exported, "flush"):
            exported.flush()
        if hasattr(exported, "close"):
            exported.close()
            exported = None
        if not temp_path.exists():
            raise OSError(f"Кодировщик не создал временный файл {temp_path.name}")
        os.replace(temp_path, out_path)
        return out_path
    finally:
        if exported is not None and hasattr(exported, "close"):
            try:
                exported.close()
            except OSError:
                pass
        temp_path.unlink(missing_ok=True)


def _probe_audio_stream_profile(path):
    """Возвращает кодек, частоту, каналы и битрейт первого аудиопотока.

    Для групповой сборки нельзя безусловно сводить любой материал к профилю
    внутреннего TTS-кэша (48 кГц mono): пользователь может объединять музыку,
    stereo MP3 и WAV с более высокой частотой. Короткий ffprobe-проход читает
    только заголовки и позволяет построить единый, но не заниженный профиль для
    concat-фильтра. Ошибка пробинга не скрывает последующую диагностику FFmpeg:
    вызывающий код применит консервативный fallback.
    """
    startupinfo = None
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    command = [
        get_ffprobe_path(),
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
        "-of", "json",
        str(Path(path)),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            startupinfo=startupinfo,
        )
        streams = json.loads(completed.stdout or "{}").get("streams", ())
        if not streams:
            return None
        sample_rate = int(streams[0].get("sample_rate", 0))
        channels = int(streams[0].get("channels", 0))
        if sample_rate <= 0 or channels <= 0:
            return None
        raw_bitrate = streams[0].get("bit_rate")
        try:
            bitrate = int(raw_bitrate) if raw_bitrate is not None else None
        except (TypeError, ValueError):
            bitrate = None
        # FFmpeg/ffprobe сообщает UINT32_MAX-1 для некоторых VBR Vorbis как
        # маркер «битрейт неизвестен». Это не реальный 4,29-Гбит/с поток и его
        # нельзя переносить в ``-b:a`` режима Auto.
        if bitrate is not None and bitrate >= 0xFFFFFF00:
            bitrate = None
        return {
            "codec": str(streams[0].get("codec_name", "")).strip().lower(),
            "sample_rate": sample_rate,
            "channels": channels,
            "bitrate": bitrate if bitrate and bitrate > 0 else None,
        }
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def _mp3_compatible_sample_rate(sample_rate):
    """Выбирает поддерживаемую libmp3lame частоту без лишнего downsample."""
    supported = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
    sample_rate = max(1, int(sample_rate))
    for candidate in supported:
        if candidate >= sample_rate:
            return candidate
    return supported[-1]


def _opus_compatible_sample_rate(sample_rate):
    """Выбирает ближайшую частоту, поддерживаемую libopus."""
    supported = (8000, 12000, 16000, 24000, 48000)
    sample_rate = max(1, int(sample_rate))
    for candidate in supported:
        if candidate >= sample_rate:
            return candidate
    return supported[-1]


def _audio_profile_compatibility_error(
    output_format,
    sample_rate,
    channels,
    bitrate,
):
    """Объясняет несовместимый профиль до запуска FFmpeg.

    Поля экспорта общие для всех контейнеров, но кодеки принимают разные
    сочетания. Не оставляем эту проверку поздней ошибке ``Error while opening
    encoder`` и не подменяем явно выбранный пользователем битрейт молча.
    """
    fmt = str(output_format).strip().lower()
    sample_rate = int(sample_rate)
    channels = int(channels)
    bitrate_text = str(bitrate or "").strip().lower()
    if fmt == "wav" or not bitrate_text:
        return None
    match = re.fullmatch(r"([1-9]\d*)k", bitrate_text)
    if not match:
        return "Некорректный битрейт аудиопрофиля."
    bitrate_kbps = int(match.group(1))

    if fmt == "mp3" and not 8 <= bitrate_kbps <= 320:
        return "MP3 поддерживает битрейт от 8 до 320 кбит/с."
    if fmt == "mp3" and sample_rate <= 12000 and bitrate_kbps > 64:
        return (
            "MP3 с частотой 8–12 кГц поддерживает битрейт не выше "
            "64 кбит/с. Увеличьте частоту либо используйте режим «Авто»."
        )
    if fmt == "mp3" and sample_rate <= 24000 and bitrate_kbps > 160:
        return (
            "MP3 с частотой 16–24 кГц поддерживает битрейт не выше "
            "160 кбит/с. Выберите 128 кбит/с, увеличьте частоту до 32 кГц "
            "или используйте режим «Авто»."
        )

    if fmt == "opus" and not 6 <= bitrate_kbps <= 510:
        return (
            "Opus поддерживает битрейт от 6 до 510 кбит/с. Выберите "
            "совместимое значение либо используйте режим «Авто»."
        )

    if fmt == "ogg":
        if not 32 <= bitrate_kbps <= 500:
            return "OGG/Vorbis поддерживает битрейт от 32 до 500 кбит/с."
        if sample_rate > 48000:
            return (
                "OGG/Vorbis 88,2/96 кГц доступен только с битрейтом "
                "«Авто» (quality-режим кодировщика). Выберите «Авто» либо "
                "частоту не выше 48 кГц."
            )
        maximums = {
            (8000, 1): 32,
            (8000, 2): 64,
            (12000, 1): 48,
            (12000, 2): 96,
            (16000, 1): 96,
            (16000, 2): 192,
            (22050, 1): 64,
            (22050, 2): 128,
            (24000, 1): 64,
            (24000, 2): 128,
            (32000, 1): 128,
            (32000, 2): 320,
            (44100, 1): 192,
            (48000, 1): 192,
        }
        maximum = maximums.get((sample_rate, channels))
        if maximum is not None and bitrate_kbps > maximum:
            channel_text = "mono" if channels == 1 else "stereo"
            return (
                f"OGG/Vorbis {sample_rate / 1000:g} кГц {channel_text} "
                f"поддерживает выбранный режим битрейта не выше "
                f"{maximum} кбит/с. Уменьшите битрейт, увеличьте частоту "
                "или используйте режим «Авто»."
            )
    return None


def _select_book_audio_profile(
    output_format,
    *,
    sample_rate="auto",
    channels="auto",
    bitrate="128k",
):
    """Разрешает профиль финальной книги из канонического 48 kHz mono-кэша."""
    output_format = str(output_format).strip().lower()
    if output_format not in AUDIO_PROFILE_FORMATS:
        raise ValueError("Выбран неподдерживаемый формат финальной книги.")

    requested_rate = str(sample_rate).strip().lower()
    selected_rate = (
        CACHE_AUDIO_SAMPLE_RATE if requested_rate == "auto" else int(requested_rate)
    )
    if output_format == "mp3":
        compatible = _mp3_compatible_sample_rate(selected_rate)
        if requested_rate != "auto" and compatible != selected_rate:
            raise ValueError(
                "MP3/libmp3lame не поддерживает выбранную частоту. "
                "Выберите не более 48 кГц либо режим «Авто»."
            )
        selected_rate = compatible
    elif output_format == "opus":
        compatible = _opus_compatible_sample_rate(selected_rate)
        if requested_rate != "auto" and compatible != selected_rate:
            raise ValueError(
                "Opus поддерживает только 8, 12, 16, 24 и 48 кГц."
            )
        selected_rate = compatible

    requested_channels = str(channels).strip().lower()
    if requested_channels not in AUDIO_PROFILE_CHANNELS:
        raise ValueError("Некорректный режим каналов финальной книги.")
    selected_channels = 2 if requested_channels == "stereo" else 1

    requested_bitrate = str(bitrate).strip().lower()
    if output_format == "wav":
        selected_bitrate = None
    elif requested_bitrate == "auto":
        selected_bitrate = {
            "mp3": "128k",
            "opus": "48k",
            # Для Vorbis отсутствие -b:a оставляет штатный quality/VBR режим.
            "ogg": None,
        }[output_format]
    else:
        selected_bitrate = requested_bitrate
    error = _audio_profile_compatibility_error(
        output_format,
        selected_rate,
        selected_channels,
        selected_bitrate,
    )
    if error:
        raise ValueError(error)
    return {
        "sample_rate": selected_rate,
        "channels": selected_channels,
        "bitrate": selected_bitrate,
    }


def _select_merge_audio_profile(
    sources,
    output_format,
    *,
    sample_rate="auto",
    channels="auto",
    bitrate="auto",
    cancelled=None,
):
    """Выбирает общий профиль для безопасного concat разнородных потоков.

    Максимальная входная частота не занижается, если это допускает выбранный
    контейнер/кодек. Полностью mono-набор остаётся mono; наличие stereo или
    многоканального входа сохраняет stereo-результат. Если хотя бы один файл не
    удалось исследовать, неизвестный поток консервативно считается 48 кГц
    stereo, а сам FFmpeg всё равно выполнит окончательную проверку/декодирование.
    """
    profiles = []
    unknown = []
    for source in sources:
        if cancelled and cancelled():
            raise InterruptedError("Сборка остановлена пользователем")
        profile = _probe_audio_stream_profile(source)
        if profile is None:
            unknown.append(Path(source).name)
        else:
            profiles.append(profile)

    if cancelled and cancelled():
        raise InterruptedError("Сборка остановлена пользователем")

    sample_rates = [profile["sample_rate"] for profile in profiles]
    if unknown or not sample_rates:
        sample_rates.append(CACHE_AUDIO_SAMPLE_RATE)
    requested_sample_rate = str(sample_rate).strip().lower()
    if requested_sample_rate != "auto":
        sample_rate = int(sample_rate)
    else:
        sample_rate = max(sample_rates)
    output_format = str(output_format).strip().lower()
    if output_format == "mp3":
        compatible_sample_rate = _mp3_compatible_sample_rate(sample_rate)
        if requested_sample_rate != "auto" and compatible_sample_rate != sample_rate:
            raise ValueError(
                "MP3/libmp3lame не поддерживает выбранную частоту "
                f"{sample_rate} Гц. Выберите частоту не выше 48 кГц либо "
                "режим «Авто»."
            )
        sample_rate = compatible_sample_rate
    elif output_format == "opus":
        compatible_sample_rate = _opus_compatible_sample_rate(sample_rate)
        if requested_sample_rate != "auto" and compatible_sample_rate != sample_rate:
            raise ValueError(
                "Opus/libopus поддерживает только 8, 12, 16, 24 и 48 кГц. "
                "Выберите поддерживаемую частоту либо режим «Авто»."
            )
        sample_rate = compatible_sample_rate

    requested_channels = str(channels).strip().lower()
    if requested_channels == "mono":
        channels = 1
    elif requested_channels == "stereo":
        channels = 2
    else:
        channels = 2 if unknown or any(
            profile["channels"] > 1 for profile in profiles
        ) else 1
    channel_layout = "stereo" if channels == 2 else "mono"

    requested_bitrate = str(bitrate).strip().lower()
    if output_format == "wav":
        selected_bitrate = None
    elif requested_bitrate != "auto":
        selected_bitrate = requested_bitrate
    else:
        # Битрейт имеет одинаковый смысл лишь у сжатых входов. Например,
        # ``bit_rate`` PCM/WAV описывает поток несжатых отсчётов и не должен
        # внезапно становиться целевым битрейтом MP3/OGG в режиме ``auto``.
        bitrates = [
            profile.get("bitrate")
            for profile in profiles
            if profile.get("codec") not in {
                "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"
            }
        ]
        selected_bitrate = (
            f"{round(bitrates[0] / 1000)}k"
            if (
                not unknown
                and bitrates
                and len(bitrates) == len(profiles)
                and all(value for value in bitrates)
                and len(set(bitrates)) == 1
            )
            else None
        )

    compatibility_error = _audio_profile_compatibility_error(
        output_format,
        sample_rate,
        channels,
        selected_bitrate,
    )
    if compatibility_error:
        if requested_bitrate == "auto":
            # В auto параметры потока являются подсказкой, а не приказом.
            # Сохраняем частоту и каналы, но даём кодировщику выбрать
            # совместимый битрейт вместо заведомого падения.
            selected_bitrate = None
        else:
            raise ValueError(compatibility_error)

    if unknown:
        preview = ", ".join(unknown[:3])
        suffix = "…" if len(unknown) > 3 else ""
        logging.warning(
            "Не удалось определить аудиопрофиль %d входных файлов (%s%s); "
            "для них используется безопасный fallback 48 кГц stereo.",
            len(unknown),
            preview,
            suffix,
        )
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout,
        "bitrate": selected_bitrate,
    }


def _ffmpeg_audio_effect_filters(
    *,
    speed=1.0,
    pitch=1.0,
    echo=False,
    echo_delay=300,
    echo_decay=0.3,
    sample_rate=CACHE_AUDIO_SAMPLE_RATE,
):
    """Возвращает общий FFmpeg-граф эффектов без промежуточного WAV."""
    filters = []
    pitch = float(pitch)
    speed = float(speed)
    sample_rate = max(1, int(sample_rate))
    if pitch != 1.0:
        filters.append(f"asetrate={int(sample_rate * pitch)}")
        filters.extend(AudioEffects._atempo_filters(1 / pitch))
        # asetrate меняет заявленную частоту потока; перед кодированием снова
        # приводим её к общему профилю текущей группы.
        filters.append(f"aresample={sample_rate}")
    if speed != 1.0:
        filters.extend(AudioEffects._atempo_filters(speed))
    if echo:
        filters.append(
            f"aecho=0.8:0.8:{int(echo_delay)}:{float(echo_decay)}"
        )
    return filters


def _validate_windows_command_length(command, *, system_name=None, limit=30000):
    """Даёт понятную ошибку до CreateProcess при слишком длинной FFmpeg-команде."""
    current_system = system_name or platform.system()
    if current_system != "Windows":
        return
    rendered_length = len(subprocess.list2cmdline([str(part) for part in command]))
    if rendered_length >= int(limit):
        raise ValueError(
            "Слишком много файлов или слишком длинные пути для одной сборки "
            "в Windows. Разделите группу на несколько частей или сократите "
            "пути к исходным файлам."
        )


def _export_merged_audio_ffmpeg(
    audio_files,
    out_path,
    *,
    output_format,
    bitrate=None,
    pause_ms=0,
    speed=1.0,
    pitch=1.0,
    echo=False,
    echo_delay=300,
    echo_decay=0.3,
    tags=None,
    cover=None,
    sample_rate="auto",
    channels="auto",
    bitrate_mode=None,
    cancelled=None,
):
    """Потоково объединяет разнородные файлы через ``-filter_complex``.

    Каждый вход сначала декодируется и приводится к одинаковым sample rate,
    channel layout и sample format. Поэтому concat-фильтр безопасен для смеси
    MP3/WAV/OGG/Opus и не создаёт единый PCM ``AudioSegment``/временный RIFF. Это
    снимает как расход RAM на всю книгу, так и 4-ГиБ лимит WAV-заголовка.
    """
    sources = tuple(Path(path).expanduser().resolve() for path in audio_files)
    if not sources:
        raise ValueError("Нет аудиофайлов для объединения")
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise FileNotFoundError(missing[0])

    fmt = str(output_format).strip().lower()
    if fmt not in {"mp3", "ogg", "opus", "wav"}:
        raise ValueError(f"Неподдерживаемый формат экспорта: {fmt}")

    requested_bitrate = (
        str(bitrate_mode).strip().lower()
        if bitrate_mode is not None
        else str(bitrate or "auto").strip().lower()
    )
    merge_profile = _select_merge_audio_profile(
        sources,
        fmt,
        sample_rate=sample_rate,
        channels=channels,
        bitrate=requested_bitrate,
        cancelled=cancelled,
    )
    sample_rate = merge_profile["sample_rate"]
    channels = merge_profile["channels"]
    channel_layout = merge_profile["channel_layout"]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(
        f".{out_path.stem}.{uuid.uuid4().hex}.tmp{out_path.suffix}"
    )
    command = [get_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        command.extend(["-i", str(source)])

    has_cover = bool(fmt == "mp3" and cover and Path(cover).is_file())
    xiph_metadata_path = (
        _create_xiph_cover_metadata_file(cover, out_path.name)
        if fmt == "opus"
        else None
    )
    if xiph_metadata_path:
        command.extend(["-f", "ffmetadata", "-i", str(xiph_metadata_path)])
    if has_cover:
        command.extend(["-i", str(Path(cover).resolve())])
    elif cover and Path(cover).is_file() and fmt in {"ogg", "wav"}:
        logging.warning(
            "Обложка пропущена для %s: формат %s не поддерживает JPEG/PNG "
            "attached_pic этим способом.",
            out_path.name,
            fmt,
        )

    # concat требует одинаковых параметров. apad здесь не используется:
    # он сделал бы каждый вход бесконечным. Сбрасываем PTS и нормализуем все
    # потоки отдельно, а паузы создаём конечным anullsrc+atrim внутри графа.
    filters = []
    concat_labels = []
    for index in range(len(sources)):
        label = f"a{index}"
        filters.append(
            f"[{index}:a:0]aresample={sample_rate},"
            "aformat=sample_fmts=fltp:"
            f"sample_rates={sample_rate}:channel_layouts={channel_layout},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        concat_labels.append(f"[{label}]")
        if index < len(sources) - 1 and int(pause_ms) > 0:
            pause_label = f"p{index}"
            pause_seconds = int(pause_ms) / 1000.0
            filters.append(
                "anullsrc="
                f"r={sample_rate}:cl={channel_layout},"
                f"atrim=duration={pause_seconds:.6f},"
                f"asetpts=PTS-STARTPTS[{pause_label}]"
            )
            concat_labels.append(f"[{pause_label}]")

    filters.append(
        "".join(concat_labels)
        + f"concat=n={len(concat_labels)}:v=0:a=1[merged]"
    )
    effect_filters = _ffmpeg_audio_effect_filters(
        speed=speed,
        pitch=pitch,
        echo=echo,
        echo_delay=echo_delay,
        echo_decay=echo_decay,
        sample_rate=sample_rate,
    )
    output_label = "merged"
    if effect_filters:
        filters.append(f"[merged]{','.join(effect_filters)}[processed]")
        output_label = "processed"

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{output_label}]",
        ]
    )
    if has_cover:
        command.extend(
            [
                "-map",
                f"{len(sources)}:v:0",
                # JPEG внутри ID3v2.3/APIC даёт наиболее предсказуемое
                # отображение в Проводнике и Media Player Windows. FFmpeg
                # примет и JPEG, и PNG на входе, но всегда запишет APIC как
                # image/jpeg вместо менее совместимого image/png.
                "-c:v",
                "mjpeg",
                "-id3v2_version",
                "3",
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
        )
    else:
        command.extend(["-vn"])
    command.extend(["-sn", "-dn", "-map_metadata", "-1"])

    if fmt == "mp3":
        command.extend([
            "-c:a", "libmp3lame", "-b:a",
            str(merge_profile["bitrate"] or "128k"),
        ])
    elif fmt == "ogg":
        command.extend(["-c:a", "libvorbis"])
        if merge_profile["bitrate"]:
            command.extend(["-b:a", str(merge_profile["bitrate"])])
        else:
            # libvorbis поддерживает 88,2/96 кГц в quality/VBR-режиме, но
            # не принимает для них явный номинальный bitrate. Для Auto
            # quality=4 даёт предсказуемый профиль без скрытого downsample.
            command.extend(["-q:a", "4"])
    elif fmt == "opus":
        command.extend(["-c:a", "libopus"])
        if merge_profile["bitrate"]:
            command.extend(["-b:a", str(merge_profile["bitrate"])])
    else:
        # Обычный RIFF хранит размеры в 32 битах и перестаёт быть валидным
        # после ~4 ГиБ PCM. FFmpeg сам выберет классический RIFF для короткого
        # результата и RF64 для длинного, поэтому единая потоковая склейка
        # работает и для многотомных книг без прежней ошибки struct '<L'.
        command.extend(["-c:a", "pcm_s16le", "-rf64", "auto"])
    command.extend(["-ar", str(sample_rate), "-ac", str(channels)])
    for key, value in (tags or {}).items():
        if value:
            command.extend(["-metadata", f"{key}={value}"])
    if xiph_metadata_path:
        command.extend(["-map_metadata", str(len(sources))])
    command.append(str(temp_path))

    startupinfo = None
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    try:
        _validate_windows_command_length(command)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        while process.poll() is None:
            if cancelled and cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise InterruptedError("Сборка остановлена пользователем")
            time.sleep(0.05)
        stderr = process.stderr.read() if process.stderr else b""
        if process.stderr:
            process.stderr.close()
        if process.returncode != 0 or not temp_path.is_file():
            detail = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                "FFmpeg не смог объединить аудиофайлы"
                + (f": {detail}" if detail else "")
            )
        os.replace(temp_path, out_path)
        return out_path
    finally:
        temp_path.unlink(missing_ok=True)
        if xiph_metadata_path is not None:
            xiph_metadata_path.unlink(missing_ok=True)


def _export_single_audio_ffmpeg(source, out_path, **kwargs):
    """Экспортирует один файл с тем же профилем, что и групповая сборка.

    Отдельный файл не должен обходить выбранные в UI частоту, каналы и режим
    битрейта. Один вход поэтому проходит через тот же ``filter_complex`` и
    потоковый FFmpeg-путь, но без паузы и без промежуточного PCM/WAV.
    """
    return _export_merged_audio_ffmpeg([source], out_path, **kwargs)


def _prepare_api_audio_file(
    source_path,
    destination_path,
    *,
    trim_silence=True,
    silence_threshold=-55.0,
):
    """Приводит ответ API к единому Ogg/Opus 48 kHz mono.

    При выключенной обрезке уже совместимый ответ EnhancedTTS публикуется без
    повторного lossy-кодирования. Если требуется обрезка либо сервер вернул
    другой кодек/параметры, результат атомарно кодируется в Opus 48 кбит/с.
    """
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    source_codec, source_channels = _inspect_ogg_audio_header(source_path)

    # Opus сам декодируется в 48 кГц, но число каналов берётся из OpusHead.
    # Поэтому без повторного lossy-кодирования можно публиковать только уже
    # канонический mono-ответ. Неожиданный stereo/multichannel Opus проходит
    # обычный decode/downmix/encode ниже, даже когда автообрезка выключена.
    if (
        not trim_silence
        and source_codec == CACHE_AUDIO_CODEC
        and source_channels == CACHE_AUDIO_CHANNELS
    ):
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() == destination_path.resolve():
            return destination_path
        temp_path = destination_path.with_name(
            f".{destination_path.stem}.{uuid.uuid4().hex}.tmp{destination_path.suffix}"
        )
        try:
            shutil.copyfile(source_path, temp_path)
            _require_opus_audio_file(temp_path)
            os.replace(temp_path, destination_path)
            return destination_path
        finally:
            temp_path.unlink(missing_ok=True)

    audio_segment = _load_audio_segment(source_path, known_codec=source_codec)
    prepared_segment = audio_segment

    if trim_silence:
        nonsilent_ranges = detect_nonsilent(
            audio_segment,
            min_silence_len=50,
            silence_thresh=float(silence_threshold),
        )
        if nonsilent_ranges:
            start_trim = max(0, nonsilent_ranges[0][0] - 20)
            end_trim = min(len(audio_segment), nonsilent_ranges[-1][1] + 20)
            prepared_segment = audio_segment[start_trim:end_trim]

    prepared_segment = prepared_segment.set_frame_rate(
        CACHE_AUDIO_SAMPLE_RATE
    ).set_channels(CACHE_AUDIO_CHANNELS)
    result_path = _export_audio_atomic(
        prepared_segment,
        destination_path,
        format="ogg",
        codec=CACHE_AUDIO_FFMPEG_CODEC,
        bitrate=CACHE_AUDIO_BITRATE,
        parameters=[
            "-vbr", "on",
            "-compression_level", "10",
            "-application", "audio",
        ],
    )
    _require_opus_audio_file(result_path)
    return result_path


def _inspect_ogg_audio_header(path):
    """Читает кодек и число каналов из идентификационного пакета Ogg.

    Разбор capture pattern и lacing-таблицы не позволяет случайной строке
    ``OpusHead`` в комментариях или аудиоданных выдать повреждённый/другой Ogg
    за канонический внутренний фрагмент. Читаются только первые 4 КиБ. Число
    каналов нужно для безопасного byte-for-byte fast path ответа API: сам
    контейнер Opus всегда декодируется в 48 кГц, но может быть не mono.
    """
    try:
        with open(path, "rb") as audio_file:
            header = audio_file.read(4096)
    except OSError:
        return None, None

    if len(header) < 27 or header[:4] != b"OggS" or header[4] != 0:
        return None, None
    segment_count = header[26]
    segment_table_end = 27 + segment_count
    if len(header) < segment_table_end:
        return None, None

    first_packet_size = 0
    packet_complete = False
    for lace_value in header[27:segment_table_end]:
        first_packet_size += lace_value
        if lace_value < 255:
            packet_complete = True
            break
    packet_end = segment_table_end + first_packet_size
    if not packet_complete or packet_end > len(header):
        return None, None

    first_packet = header[segment_table_end:packet_end]
    if first_packet.startswith(b"OpusHead"):
        channels = first_packet[9] if len(first_packet) >= 10 else None
        return "opus", channels if channels else None
    if first_packet.startswith(b"\x01vorbis"):
        channels = first_packet[11] if len(first_packet) >= 12 else None
        return "vorbis", channels if channels else None
    return None, None


def _detect_ogg_audio_codec(path):
    """Быстро определяет Opus/Vorbis по идентификационному пакету Ogg."""
    codec, _channels = _inspect_ogg_audio_header(path)
    return codec


def _require_opus_audio_file(path):
    """Проверяет физический кодек внутреннего фрагмента перед публикацией."""
    path = Path(path)
    codec = _detect_ogg_audio_codec(path)
    if codec != CACHE_AUDIO_CODEC:
        raise ValueError(
            f"Внутренний фрагмент {path.name} должен быть Ogg/Opus, "
            f"найден {codec or 'неизвестный/повреждённый формат'}"
        )
    return path


def _require_opus_audio_files(paths):
    """Не допускает смешанный Vorbis/Opus в concat внутреннего кэша."""
    verified = tuple(Path(path) for path in paths)
    for path in verified:
        _require_opus_audio_file(path)
    return verified


def _load_audio_segment(path, *, known_codec=None):
    """Декодирует известный Ogg без отдельного запуска FFprobe.

    FFmpeg сам читает контейнер, а явный decoder позволяет Pydub не вызывать
    ``mediainfo_json``. Чтение Vorbis здесь предназначено для legacy-миграции,
    предпрослушивания и пользовательских файлов; внутренний concat принимает
    только уже канонизированный Opus. Для прочих форматов сохраняется обычное
    автоопределение.
    """
    path = Path(path)
    codec = str(known_codec or "").strip().lower()
    if not codec and path.suffix.lower() in {".ogg", ".oga", ".opus"}:
        codec = _detect_ogg_audio_codec(path) or ""
    if codec in {"opus", "vorbis"}:
        return AudioSegment.from_file(path, format="ogg", codec=codec)
    return AudioSegment.from_file(path)


def _cache_file_identity(path):
    """Возвращает признаки конкретной опубликованной версии cache-файла."""
    stat_result = Path(path).stat()
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _allocated_file_size(path):
    """Возвращает реально выделенный размер, где его сообщает ОС."""
    stat_result = Path(path).stat()
    blocks = getattr(stat_result, "st_blocks", 0)
    return blocks * 512 if blocks else stat_result.st_size


def _transcode_cache_audio_to_opus(path, publish_lock=None):
    """Атомарно перекодирует один Vorbis-фрагмент в канонический Ogg/Opus.

    Возвращает ``(status, old_size, new_size, old_allocated, new_allocated)``.
    При параллельной замене файла устаревший результат никогда не публикуется
    поверх новой версии.
    """
    path = Path(path)
    codec = _detect_ogg_audio_codec(path)
    old_size = path.stat().st_size
    old_allocated = _allocated_file_size(path)
    if codec == CACHE_AUDIO_CODEC:
        return (
            "already_opus",
            old_size,
            old_size,
            old_allocated,
            old_allocated,
        )
    if codec != "vorbis":
        raise ValueError(
            f"Ожидался Ogg/Vorbis, найден {codec or 'неизвестный формат'}"
        )

    source_identity = _cache_file_identity(path)
    converted_path = path.with_name(
        f".{path.stem}.{uuid.uuid4().hex}.opus{path.suffix}"
    )
    try:
        startupinfo = None
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(
            [
                get_ffmpeg_path(),
                "-y", "-v", "error",
                "-i", str(path),
                "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-map_metadata", "-1",
                "-ar", str(CACHE_AUDIO_SAMPLE_RATE),
                "-ac", str(CACHE_AUDIO_CHANNELS),
                "-c:a", CACHE_AUDIO_FFMPEG_CODEC,
                "-b:a", CACHE_AUDIO_BITRATE,
                "-vbr", "on",
                "-compression_level", "10",
                "-application", "audio",
                "-threads", "1",
                str(converted_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        if (
            result.returncode != 0
            or not converted_path.is_file()
            or converted_path.stat().st_size <= 0
            or _detect_ogg_audio_codec(converted_path) != CACHE_AUDIO_CODEC
        ):
            details = (
                result.stderr.decode("utf-8", errors="replace").strip()
                if result.stderr
                else ""
            )
            raise RuntimeError(
                details or "FFmpeg не создал корректный Ogg/Opus"
            )
        new_size = converted_path.stat().st_size
        new_allocated = _allocated_file_size(converted_path)

        def publish_if_unchanged():
            if _cache_file_identity(path) != source_identity:
                current_codec = _detect_ogg_audio_codec(path)
                if current_codec == CACHE_AUDIO_CODEC:
                    return (
                        "changed_to_opus",
                        old_size,
                        path.stat().st_size,
                        old_allocated,
                        _allocated_file_size(path),
                    )
                raise RuntimeError(
                    f"Файл {path.name} изменился во время перекодирования"
                )
            os.replace(converted_path, path)
            return (
                "converted",
                old_size,
                new_size,
                old_allocated,
                new_allocated,
            )

        if publish_lock is None:
            return publish_if_unchanged()
        with publish_lock:
            return publish_if_unchanged()
    finally:
        converted_path.unlink(missing_ok=True)


def transcode_cache_entries_to_opus(
    cache_dir,
    cache_data,
    *,
    cancel_event=None,
    max_workers=None,
    progress_callback=None,
    checkpoint_callback=None,
    checkpoint_every=None,
):
    """Перекодирует все безопасные уникальные cache-файлы и меняет metadata.

    Индекс на диск не записывает: вызывающий код публикует его один раз после
    завершения файлов. Это делает функцию пригодной и для GUI, и для тестов.
    """
    cache_dir = Path(cache_dir)
    cancel_event = cancel_event or threading.Event()
    if max_workers is None:
        # Каждый FFmpeg ниже принудительно однопоточный. На больших кэшах
        # выгодно запускать до 24 коротких процессов параллельно; выше на M5 Max
        # прироста уже не было, а на слабых машинах предел автоматически ниже.
        max_workers = max(1, min(24, os.cpu_count() or 1))
    else:
        max_workers = max(1, int(max_workers))
    if checkpoint_every is not None:
        checkpoint_every = max(1, int(checkpoint_every))

    stats = {
        "converted": 0,
        "already_opus": 0,
        "missing": 0,
        "failed": 0,
        "old_size": 0,
        "new_size": 0,
        "old_allocated": 0,
        "new_allocated": 0,
        "cancelled": False,
        "index_changed": False,
        "index_dirty": False,
        "errors": [],
    }
    file_entries = []
    keys_by_path = {}
    for cache_key, cache_info in cache_data.items():
        if not isinstance(cache_info, dict):
            stats["failed"] += 1
            stats["errors"].append((str(cache_key), "некорректные метаданные"))
            continue
        filepath = resolve_cache_audio_path(
            cache_dir, cache_info.get("file_name", "")
        )
        if filepath is None:
            stats["failed"] += 1
            stats["errors"].append((str(cache_key), "небезопасный путь"))
            continue
        if filepath in keys_by_path:
            keys_by_path[filepath].append(cache_key)
            continue
        keys_by_path[filepath] = [cache_key]
        file_entries.append((cache_key, filepath))

    total = len(file_entries)
    completed = 0
    if progress_callback:
        progress_callback(completed, total, stats)
    iterator = iter(file_entries)
    pending = {}

    def submit_next(executor):
        if cancel_event.is_set():
            return False
        try:
            cache_key, filepath = next(iterator)
        except StopIteration:
            return False
        if not filepath.is_file():
            future = executor.submit(lambda: ("missing", None))
        else:
            future = executor.submit(_transcode_cache_audio_to_opus, filepath)
        pending[future] = (cache_key, filepath)
        return True

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="cache-opus",
    ) as executor:
        # В очереди не держим больше одной задачи на worker: после Stop должны
        # завершиться только реально работающие FFmpeg, а не скрытый запас.
        for _ in range(max_workers):
            if not submit_next(executor):
                break

        while pending:
            done_futures, _ = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done_futures:
                cache_key, filepath = pending.pop(future)
                completed += 1
                try:
                    result = future.result()
                    if result[0] == "missing":
                        stats["missing"] += 1
                    else:
                        (
                            status,
                            old_size,
                            new_size,
                            old_allocated,
                            new_allocated,
                        ) = result
                        stats["old_size"] += old_size
                        stats["new_size"] += new_size
                        stats["old_allocated"] += old_allocated
                        stats["new_allocated"] += new_allocated
                        if status in {"converted", "changed_to_opus"}:
                            stats["converted"] += 1
                        else:
                            stats["already_opus"] += 1
                        for referenced_key in keys_by_path.get(
                            filepath, (cache_key,)
                        ):
                            cache_info = cache_data.get(referenced_key)
                            if (
                                isinstance(cache_info, dict)
                                and cache_info.get("audio_codec")
                                != CACHE_AUDIO_CODEC
                            ):
                                cache_info["audio_codec"] = CACHE_AUDIO_CODEC
                                stats["index_changed"] = True
                                stats["index_dirty"] = True
                except Exception as exc:
                    stats["failed"] += 1
                    stats["errors"].append((str(filepath), str(exc)))

                if progress_callback and (
                    completed == total or completed % 100 == 0
                ):
                    progress_callback(completed, total, stats)
                if (
                    checkpoint_callback
                    and checkpoint_every is not None
                    and stats["index_dirty"]
                    and completed % checkpoint_every == 0
                ):
                    checkpoint_callback(cache_data)
                    stats["index_dirty"] = False
                if not cancel_event.is_set():
                    submit_next(executor)

            if cancel_event.is_set():
                stats["cancelled"] = completed < total

    if completed < total:
        stats["cancelled"] = True
    if checkpoint_callback and stats["index_dirty"]:
        checkpoint_callback(cache_data)
        stats["index_dirty"] = False
    return stats


def _canonicalize_cached_audio_if_needed(
    path,
    known_codec=None,
    publish_lock=None,
):
    """Лениво приводит старый Vorbis cache-hit к Opus для безопасной склейки.

    Для быстрой пересборки большого старого кэша лучше заранее запустить
    массовую миграцию во вкладке «Кэш». Этот fallback гарантирует корректность
    смешанного/частично мигрированного кэша и никогда не меняет уже готовый Opus.
    """
    path = Path(path)
    # Заголовок является источником истины: метаданные индекса могут отставать,
    # если приложение завершили после атомарной замены файла, но до записи JSON,
    # либо ошибочно утверждать ``audio_codec: opus`` для старого Vorbis.
    codec = _detect_ogg_audio_codec(path)
    if codec == CACHE_AUDIO_CODEC:
        return CACHE_AUDIO_CODEC
    if codec != "vorbis":
        raise ValueError(f"Не удалось определить кодек Ogg-файла {path.name}")
    _transcode_cache_audio_to_opus(path, publish_lock=publish_lock)
    _require_opus_audio_file(path)
    return CACHE_AUDIO_CODEC


def _write_text_atomic(path, content):
    """Атомарно записывает UTF-8 текст, не оставляя обрезанный файл."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            file.write(str(content))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        return path
    finally:
        temp_path.unlink(missing_ok=True)

# Pydub использует ``AudioSegment.converter`` для декодера, но путь к
# ffprobe в pydub 0.25.1 каждый раз ищет функцией utils.get_prober_name().
# Поэтому одного исторического атрибута ``AudioSegment.ffprobe`` недостаточно:
# передаём обоим путям абсолютные бинарники, не меняя глобальный PATH процесса.
AudioSegment.converter = _PRE_RESOLVED_FFMPEG
AudioSegment.ffprobe = get_ffprobe_path()

import pydub.utils as _pydub_utils

_PYDUB_ORIGINAL_GET_PROBER_NAME = _pydub_utils.get_prober_name


def _pydub_get_prober_name():
    return get_ffprobe_path()


_pydub_utils.get_prober_name = _pydub_get_prober_name
# -------------------------------------------------------------------------

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
class ReopeningFileHandler(logging.FileHandler):
    """Переоткрывает лог, если пользователь удалил/заменил его во время работы.

    Обычный FileHandler продолжает писать в уже удалённый inode (Unix/macOS),
    поэтому путь визуально исчезает до перезапуска приложения. Проверка перед
    emit дешёвая по сравнению с самим файловым I/O и работает на всех ОС.
    """

    def _stream_matches_path(self):
        if self.stream is None:
            return False
        try:
            path_stat = os.stat(self.baseFilename)
            stream_stat = os.fstat(self.stream.fileno())
            return (
                path_stat.st_dev == stream_stat.st_dev
                and path_stat.st_ino == stream_stat.st_ino
            )
        except (FileNotFoundError, OSError, ValueError):
            return False

    def _reopen_if_needed(self):
        if self._stream_matches_path():
            return
        if self.stream is not None:
            try:
                self.stream.flush()
                self.stream.close()
            except (OSError, ValueError):
                pass
            self.stream = None
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        self.stream = self._open()

    def emit(self, record):
        try:
            self._reopen_if_needed()
        except Exception:
            self.handleError(record)
            return
        super().emit(record)


file_handler = ReopeningFileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%d.%m.%Y %H:%M:%S")
file_handler.setFormatter(file_formatter)

handlers = [file_handler]

# Добавляем вывод в консоль ТОЛЬКО если она физически существует (не скомпилировано в --windowed)
if not isinstance(sys.stderr, NullWriter):
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_formatter)
    handlers.append(console_handler)

logging.basicConfig(level=logging.INFO, handlers=handlers)
if IMPORT_LIBS_ERROR is not None:
    logging.warning(
        "Библиотеки для импорта книг (EbookLib, bs4, docx) недоступны: %s",
        IMPORT_LIBS_ERROR,
    )
# =========================================================
try:
    from ru_normalizr import NormalizeOptions, Normalizer
    normalizer = Normalizer(NormalizeOptions.tts())
    logging.info("ru-normalizr загружен.")
except ImportError:
    logging.warning("ru-normalizr не найден. Нормализация пропущена.")
    NormalizeOptions = None
    Normalizer = None
    normalizer = None


NORMALIZER_PROFILE_SCHEMA = "silero-tts-studio-normalizer-profile"
NORMALIZER_PROFILE_VERSION = 1
NORMALIZER_MODES = ("tts", "safe")
NORMALIZER_BOOLEAN_OPTIONS = (
    "enable_caps_normalization",
    "enable_first_word_decap",
    "remove_links",
    "enable_url_normalization",
    "enable_year_normalization",
    "enable_roman_normalization",
    "enable_dates_time_normalization",
    "enable_numeral_normalization",
    "enable_abbreviation_expansion",
    "enable_contextual_abbreviation_expansion",
    "enable_years_ago_expansion",
    "enable_initials_expansion",
    "enable_letter_abbreviation_expansion",
    "enable_dictionary_normalization",
    "enable_latinization",
    "enable_latinization_stress_marks",
)
NORMALIZER_ENUM_OPTIONS = {
    "initials_vowel_mode": ("single", "double"),
    "initials_pause_mode": ("preserve", "comma"),
    "latinization_backend": ("ipa", "dictionary"),
}
NORMALIZER_SEQUENCE_OPTIONS = (
    "dictionary_include_files",
    "dictionary_exclude_files",
)

_NORMALIZER_FALLBACK_DEFAULTS = {
    "safe": {
        "enable_caps_normalization": False,
        "enable_first_word_decap": False,
        "remove_links": False,
        "enable_url_normalization": False,
        "enable_year_normalization": True,
        "enable_roman_normalization": True,
        "enable_dates_time_normalization": True,
        "enable_numeral_normalization": True,
        "enable_abbreviation_expansion": True,
        "enable_contextual_abbreviation_expansion": True,
        "enable_years_ago_expansion": True,
        "enable_initials_expansion": False,
        "initials_vowel_mode": "single",
        "initials_pause_mode": "preserve",
        "enable_letter_abbreviation_expansion": False,
        "enable_dictionary_normalization": True,
        "enable_latinization": False,
        "latinization_backend": "ipa",
        "enable_latinization_stress_marks": False,
    },
    "tts": {
        "enable_caps_normalization": True,
        "enable_first_word_decap": True,
        "remove_links": True,
        "enable_url_normalization": True,
        "enable_year_normalization": True,
        "enable_roman_normalization": True,
        "enable_dates_time_normalization": True,
        "enable_numeral_normalization": True,
        "enable_abbreviation_expansion": True,
        "enable_contextual_abbreviation_expansion": True,
        "enable_years_ago_expansion": True,
        "enable_initials_expansion": True,
        "initials_vowel_mode": "double",
        "initials_pause_mode": "comma",
        "enable_letter_abbreviation_expansion": True,
        "enable_dictionary_normalization": True,
        "enable_latinization": True,
        "latinization_backend": "ipa",
        "enable_latinization_stress_marks": False,
    },
}
for _mode_defaults in _NORMALIZER_FALLBACK_DEFAULTS.values():
    _mode_defaults.update(
        {
            "remove_links_ignore_interval": [1000, 2200],
            "latin_dictionary_filename": "latinization_rules.dic",
            "dictionary_include_files": [],
            "dictionary_exclude_files": [],
            "dictionaries_path": "",
        }
    )


def normalizer_mode_defaults(mode="tts"):
    """Возвращает JSON-совместимые значения профиля ru-normalizr 0.3.x."""
    mode = str(mode or "tts").lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    defaults = copy.deepcopy(_NORMALIZER_FALLBACK_DEFAULTS[mode])
    if NormalizeOptions is None:
        return defaults
    try:
        options = (
            NormalizeOptions.tts() if mode == "tts" else NormalizeOptions.safe()
        )
        for key in NORMALIZER_BOOLEAN_OPTIONS:
            defaults[key] = bool(getattr(options, key))
        for key in NORMALIZER_ENUM_OPTIONS:
            defaults[key] = str(getattr(options, key))
        interval = getattr(options, "remove_links_ignore_interval")
        defaults["remove_links_ignore_interval"] = [
            int(interval[0]), int(interval[1])
        ]
        defaults["latin_dictionary_filename"] = str(
            getattr(options, "latin_dictionary_filename")
        )
        for key in NORMALIZER_SEQUENCE_OPTIONS:
            defaults[key] = [str(value) for value in getattr(options, key)]
        path = getattr(options, "dictionaries_path")
        defaults["dictionaries_path"] = str(path) if path is not None else ""
    except Exception as exc:
        logging.warning(
            "Не удалось прочитать настройки ru-normalizr %s: %s", mode, exc
        )
    return defaults


def normalize_normalizer_options(raw_options, mode="tts", *, full=False):
    """Валидирует только публичные опции ru-normalizr.

    В ``settings.json`` хранятся лишь явные переопределения, поэтому будущие
    исправления библиотечных пресетов не блокируются копией старых defaults.
    Песочница и экспорт профиля запрашивают ``full=True`` и получают полный
    воспроизводимый снимок.
    """
    mode = str(mode or "tts").lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    defaults = normalizer_mode_defaults(mode)
    raw = raw_options if isinstance(raw_options, dict) else {}
    result = copy.deepcopy(defaults) if full else {}

    for key in NORMALIZER_BOOLEAN_OPTIONS:
        if key in raw:
            result[key] = _config_bool(raw.get(key), default=defaults[key])

    for key, allowed in NORMALIZER_ENUM_OPTIONS.items():
        if key not in raw:
            continue
        value = str(raw.get(key, defaults[key])).lower().strip()
        result[key] = value if value in allowed else defaults[key]

    if "remove_links_ignore_interval" in raw:
        interval = raw.get("remove_links_ignore_interval")
        try:
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError
            start, end = int(interval[0]), int(interval[1])
            if start < 0 or end < start:
                raise ValueError
            result["remove_links_ignore_interval"] = [start, end]
        except (TypeError, ValueError, OverflowError):
            result["remove_links_ignore_interval"] = copy.deepcopy(
                defaults["remove_links_ignore_interval"]
            )

    if "latin_dictionary_filename" in raw:
        filename = str(raw.get("latin_dictionary_filename") or "").strip()
        result["latin_dictionary_filename"] = (
            filename or defaults["latin_dictionary_filename"]
        )

    for key in NORMALIZER_SEQUENCE_OPTIONS:
        if key not in raw:
            continue
        values = raw.get(key)
        if isinstance(values, str):
            values = re.split(r"[,;\n]", values)
        if not isinstance(values, (list, tuple)):
            values = []
        result[key] = _stable_unique(
            [str(value).strip() for value in values if str(value).strip()]
        )

    if "dictionaries_path" in raw:
        path = raw.get("dictionaries_path")
        result["dictionaries_path"] = (
            str(path).strip() if isinstance(path, (str, os.PathLike)) else ""
        )

    return result


def resolved_normalizer_options(config):
    mode = str(config.get("normalizer_mode", "tts")).lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    return normalize_normalizer_options(
        config.get("normalizer_options", {}), mode, full=True
    )


def normalizer_settings_snapshot(config):
    """Канонический снимок настроек, влияющих на pipeline нормализации."""
    source = config if isinstance(config, dict) else {}
    mode = str(source.get("normalizer_mode", "tts")).lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    return {
        "normalizer_enabled": _config_bool(
            source.get("normalizer_enabled", True), default=True
        ),
        "normalizer_mode": mode,
        "normalizer_options": resolved_normalizer_options(source),
        "glossary_enabled": _config_bool(
            source.get("glossary_enabled", True), default=True
        ),
        "auto_abbreviations": _config_bool(
            source.get("auto_abbreviations", True), default=True
        ),
        "auto_short_words": _config_bool(
            source.get("auto_short_words", True), default=True
        ),
    }


def describe_normalizer_settings(config):
    """Короткое честное описание глобального профиля для интерфейса."""
    state = normalizer_settings_snapshot(config)
    mode = state["normalizer_mode"]
    base_title = "TTS (для озвучки)" if mode == "tts" else "Safe (бережный)"
    base_short = "TTS" if mode == "tts" else "Safe"
    defaults = normalizer_mode_defaults(mode)
    changed_options = sum(
        state["normalizer_options"].get(key) != value
        for key, value in defaults.items()
    )
    is_builtin = (
        changed_options == 0
        and state["normalizer_enabled"]
        and state["glossary_enabled"]
        and state["auto_abbreviations"]
        and state["auto_short_words"]
    )
    profile = (
        base_title if is_builtin else f"Пользовательский (база {base_short})"
    )
    studio_parts = []
    if state["auto_abbreviations"]:
        studio_parts.append("аббревиатуры")
    if state["auto_short_words"]:
        studio_parts.append("короткие сокращения")
    studio_text = ", ".join(studio_parts) if studio_parts else "выкл."
    details = (
        f"ru-normalizr: {'вкл.' if state['normalizer_enabled'] else 'выкл.'}; "
        f"глоссарий: {'вкл.' if state['glossary_enabled'] else 'выкл.'}; "
        f"предобработка Studio: {studio_text}; "
        f"изменено опций ru-normalizr: {changed_options}"
    )
    return {"profile": profile, "details": details}


def _strict_normalizer_profile_options(raw_options, mode):
    """Проверяет JSON-профиль, не маскируя опечатки значениями preset.

    ``normalize_normalizer_options`` намеренно мягок при восстановлении
    ``settings.json``: повреждённая пользовательская настройка не должна
    мешать запуску Studio. Импорт отдельного переносимого профиля — явная
    операция, поэтому неверный тип или неизвестное имя опции здесь должны
    быть показаны пользователю, а не незаметно менять результат нормализации.
    """
    if not isinstance(raw_options, dict):
        raise ValueError("normalizer.options должен быть JSON-объектом")

    known_options = set(NORMALIZER_BOOLEAN_OPTIONS)
    known_options.update(NORMALIZER_ENUM_OPTIONS)
    known_options.update(NORMALIZER_SEQUENCE_OPTIONS)
    known_options.update(
        {
            "remove_links_ignore_interval",
            "latin_dictionary_filename",
            "dictionaries_path",
        }
    )
    unknown = sorted(set(raw_options) - known_options)
    if unknown:
        raise ValueError(
            "неизвестная опция normalizer.options: " + ", ".join(unknown)
        )
    missing = sorted(known_options - set(raw_options))
    if missing:
        raise ValueError(
            "в normalizer.options отсутствуют обязательные поля: "
            + ", ".join(missing)
        )

    for key in NORMALIZER_BOOLEAN_OPTIONS:
        if key in raw_options and not isinstance(raw_options[key], bool):
            raise ValueError(f"normalizer.options.{key} должен быть boolean")

    for key, allowed in NORMALIZER_ENUM_OPTIONS.items():
        if key not in raw_options:
            continue
        value = raw_options[key]
        if not isinstance(value, str) or value.strip().lower() not in allowed:
            choices = ", ".join(allowed)
            raise ValueError(
                f"normalizer.options.{key} должен быть одним из: {choices}"
            )

    if "remove_links_ignore_interval" in raw_options:
        interval = raw_options["remove_links_ignore_interval"]
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise ValueError(
                "normalizer.options.remove_links_ignore_interval "
                "должен содержать два целых числа"
            )
        start, end = interval
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
        ):
            raise ValueError(
                "normalizer.options.remove_links_ignore_interval "
                "должен содержать неотрицательные границы start <= end"
            )

    def validate_dictionary_name(value, field_name, *, basename_only=False):
        location = f"normalizer.options.{field_name}"
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or "\\" in value
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
        ):
            raise ValueError(
                f"{location} должен быть переносимым именем .dic-файла"
            )
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.lower() != ".dic"
            or (basename_only and len(path.parts) != 1)
        ):
            raise ValueError(
                f"{location} должен быть относительным именем .dic-файла "
                "без '.' и '..'"
            )
        return value

    validate_dictionary_name(
        raw_options["latin_dictionary_filename"],
        "latin_dictionary_filename",
        basename_only=True,
    )

    for key in NORMALIZER_SEQUENCE_OPTIONS:
        if key not in raw_options:
            continue
        values = raw_options[key]
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"normalizer.options.{key} должен быть массивом")
        for value in values:
            validate_dictionary_name(value, key)
        if len(set(values)) != len(values):
            raise ValueError(
                f"normalizer.options.{key} не должен содержать дубликаты"
            )

    if raw_options["dictionaries_path"] != "":
        raise ValueError(
            "normalizer.options.dictionaries_path должен быть пустым: "
            "локальный путь не переносится в JSON-профиле"
        )

    return normalize_normalizer_options(raw_options, mode, full=True)


def _strict_profile_bool(mapping, key, section_name):
    value = mapping[key]
    if not isinstance(value, bool):
        raise ValueError(f"{section_name}.{key} должен быть boolean")
    return value


def _strict_profile_keys(mapping, expected, section_name):
    keys = set(mapping)
    expected = set(expected)
    unknown = sorted(keys - expected)
    if unknown:
        raise ValueError(
            f"неизвестные поля {section_name}: " + ", ".join(unknown)
        )
    missing = sorted(expected - keys)
    if missing:
        raise ValueError(
            f"в {section_name} отсутствуют обязательные поля: "
            + ", ".join(missing)
        )


def normalizer_profile_from_config(
    config,
    *,
    name="Пользовательский",
):
    """Создаёт переносимый профиль без путей проекта и API-секретов."""
    mode = str(config.get("normalizer_mode", "tts")).lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    options = resolved_normalizer_options(config)
    options["dictionaries_path"] = ""
    profile = {
        "schema": NORMALIZER_PROFILE_SCHEMA,
        "version": NORMALIZER_PROFILE_VERSION,
        "name": name,
        "normalizer": {
            "enabled": _config_bool(
                config.get("normalizer_enabled", True), default=True
            ),
            "mode": mode,
            "options": options,
        },
        "preprocessing": {
            "auto_abbreviations": _config_bool(
                config.get("auto_abbreviations", True), default=True
            ),
            "auto_short_words": _config_bool(
                config.get("auto_short_words", True), default=True
            ),
            "glossary_enabled": _config_bool(
                config.get("glossary_enabled", True), default=True
            ),
        },
    }
    return normalize_normalizer_profile(profile)


def normalize_normalizer_profile(profile):
    """Читает профиль v1.5 и возвращает его канонический переносимый вид."""
    if not isinstance(profile, dict):
        raise ValueError("профиль нормализации должен быть JSON-объектом")
    _strict_profile_keys(
        profile,
        {"schema", "version", "name", "normalizer", "preprocessing"},
        "корне профиля",
    )
    schema = profile["schema"]
    if schema != NORMALIZER_PROFILE_SCHEMA:
        raise ValueError("неподдерживаемый формат профиля нормализации")
    version = profile["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("версия профиля нормализации должна быть целым числом")
    if version != NORMALIZER_PROFILE_VERSION:
        raise ValueError(
            f"неподдерживаемая версия профиля нормализации: {version}"
        )

    section = profile["normalizer"]
    if not isinstance(section, dict):
        raise ValueError("секция normalizer должна быть JSON-объектом")
    _strict_profile_keys(
        section, {"enabled", "mode", "options"}, "normalizer"
    )
    mode_value = section["mode"]
    if not isinstance(mode_value, str):
        raise ValueError("режим нормализатора должен быть строкой")
    mode = mode_value.lower().strip()
    if mode not in NORMALIZER_MODES:
        raise ValueError("режим нормализатора должен быть 'tts' или 'safe'")
    options = _strict_normalizer_profile_options(section["options"], mode)

    preprocessing = profile["preprocessing"]
    if not isinstance(preprocessing, dict):
        raise ValueError("секция preprocessing должна быть JSON-объектом")
    _strict_profile_keys(
        preprocessing,
        {
            "auto_abbreviations",
            "auto_short_words",
            "glossary_enabled",
        },
        "preprocessing",
    )
    raw_name = profile["name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("имя профиля нормализации не должно быть пустым")
    name = raw_name.strip()
    if len(name) > 128 or any(
        unicodedata.category(char) in {"Cc", "Cs"} for char in name
    ):
        raise ValueError(
            "имя профиля нормализации должно быть короче 129 символов "
            "и не содержать управляющих знаков"
        )
    return {
        "schema": NORMALIZER_PROFILE_SCHEMA,
        "version": NORMALIZER_PROFILE_VERSION,
        "name": name,
        "normalizer": {
            "enabled": _strict_profile_bool(section, "enabled", "normalizer"),
            "mode": mode,
            "options": options,
        },
        "preprocessing": {
            "auto_abbreviations": _strict_profile_bool(
                preprocessing, "auto_abbreviations", "preprocessing"
            ),
            "auto_short_words": _strict_profile_bool(
                preprocessing, "auto_short_words", "preprocessing"
            ),
            "glossary_enabled": _strict_profile_bool(
                preprocessing, "glossary_enabled", "preprocessing"
            ),
        },
    }


def apply_normalizer_profile(config, profile):
    """Применяет только параметры текста, не затрагивая остальные настройки."""
    normalized = normalize_normalizer_profile(profile)
    section = normalized["normalizer"]
    preprocessing = normalized["preprocessing"]
    merged = dict(config) if isinstance(config, dict) else {}
    options = copy.deepcopy(section["options"])
    options["dictionaries_path"] = resolved_normalizer_options(merged).get(
        "dictionaries_path", ""
    )
    merged.update(
        {
            "normalizer_enabled": section["enabled"],
            "normalizer_mode": section["mode"],
            "normalizer_options": options,
            "auto_abbreviations": preprocessing["auto_abbreviations"],
            "auto_short_words": preprocessing["auto_short_words"],
            "glossary_enabled": preprocessing["glossary_enabled"],
        }
    )
    return merged


AUDIO_PROFILE_SCHEMA = "silero-tts-studio-audio-profile"
AUDIO_PROFILE_VERSION = 1
AUDIO_PROFILE_FORMATS = ("mp3", "wav", "ogg", "opus")
AUDIO_PROFILE_BITRATES = (
    "auto", "32k", "48k", "64k", "96k", "128k", "192k", "256k", "320k"
)
AUDIO_PROFILE_SAMPLE_RATES = (
    "auto", "8000", "12000", "16000", "22050", "24000", "32000",
    "44100", "48000", "88200", "96000",
)
AUDIO_PROFILE_CHANNELS = ("auto", "mono", "stereo")


def _audio_profile_number(value, name, minimum, maximum, *, integer=False):
    try:
        number = int(float(value)) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"некорректное значение {name}") from None
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(
            f"{name} должен быть в диапазоне {minimum:g}–{maximum:g}"
        )
    return number


def normalize_audio_profile(profile, *, require_envelope=False):
    """Проверяет один аудиопрофиль v1.5.

    ``require_envelope`` используется для отдельного импортируемого JSON:
    внутренний settings.json остаётся совместим с ранними prerelease-снимками,
    а переносимый файл обязан явно объявить schema/version.
    """
    if not isinstance(profile, dict):
        raise ValueError("аудиопрофиль должен быть JSON-объектом")
    if require_envelope and "schema" not in profile:
        raise ValueError("в аудиопрофиле отсутствует поле schema")
    if require_envelope and "version" not in profile:
        raise ValueError("в аудиопрофиле отсутствует поле version")
    schema = profile.get("schema", AUDIO_PROFILE_SCHEMA)
    if schema != AUDIO_PROFILE_SCHEMA:
        raise ValueError("неподдерживаемый формат аудиопрофиля")
    raw_version = profile.get("version", AUDIO_PROFILE_VERSION)
    if isinstance(raw_version, bool) or (
        require_envelope and not isinstance(raw_version, int)
    ):
        raise ValueError("некорректная версия аудиопрофиля") from None
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        raise ValueError("некорректная версия аудиопрофиля") from None
    if version != AUDIO_PROFILE_VERSION:
        raise ValueError(f"неподдерживаемая версия аудиопрофиля: {version}")

    raw_name = profile.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("имя аудиопрофиля не должно быть пустым")
    name = raw_name.strip()
    if require_envelope and "audio" not in profile:
        raise ValueError("в аудиопрофиле отсутствует секция audio")
    audio = profile.get("audio", profile.get("settings", {}))
    if not isinstance(audio, dict):
        raise ValueError("секция audio должна быть JSON-объектом")
    if require_envelope:
        required_audio_fields = {
            "format", "bitrate", "sample_rate", "channels", "effects"
        }
        missing_audio_fields = sorted(required_audio_fields.difference(audio))
        if missing_audio_fields:
            raise ValueError(
                "в секции audio отсутствуют поля: "
                + ", ".join(missing_audio_fields)
            )

    output_format = str(audio.get("format", "mp3")).strip().lower()
    if output_format not in AUDIO_PROFILE_FORMATS:
        raise ValueError("формат аудиопрофиля должен быть mp3, wav, ogg или opus")
    bitrate = str(audio.get("bitrate", "auto")).strip().lower()
    if bitrate != "auto" and not re.fullmatch(r"[1-9]\d*k", bitrate):
        raise ValueError("битрейт должен иметь вид 48k либо значение auto")
    if bitrate != "auto" and output_format != "wav":
        bitrate_kbps = int(bitrate[:-1])
        bitrate_limits = {
            "mp3": (8, 320),
            "ogg": (32, 500),
            "opus": (6, 510),
        }
        minimum, maximum = bitrate_limits[output_format]
        if not minimum <= bitrate_kbps <= maximum:
            raise ValueError(
                f"битрейт {output_format.upper()} должен быть в диапазоне "
                f"{minimum}–{maximum} кбит/с"
            )
    sample_rate = str(audio.get("sample_rate", "auto")).strip().lower()
    if sample_rate not in AUDIO_PROFILE_SAMPLE_RATES:
        raise ValueError("аудиопрофиль содержит неподдерживаемую частоту")
    channels = str(audio.get("channels", "auto")).strip().lower()
    if channels not in AUDIO_PROFILE_CHANNELS:
        raise ValueError("каналы должны иметь значение auto, mono или stereo")

    effects = audio.get("effects", {})
    if not isinstance(effects, dict):
        raise ValueError("секция audio.effects должна быть JSON-объектом")
    if require_envelope:
        required_effect_fields = {
            "enabled", "speed", "pitch", "echo", "echo_delay", "echo_decay"
        }
        missing_effect_fields = sorted(
            required_effect_fields.difference(effects)
        )
        if missing_effect_fields:
            raise ValueError(
                "в секции audio.effects отсутствуют поля: "
                + ", ".join(missing_effect_fields)
            )
    def effect_bool(key, default=False):
        value = effects.get(key, default)
        if require_envelope and not isinstance(value, bool):
            raise ValueError(f"audio.effects.{key} должен быть true или false")
        return _config_bool(value, default=default)

    enabled = effect_bool("enabled")
    speed = _audio_profile_number(
        effects.get("speed", 1.0), "скорость", 0.5, 3.0
    )
    pitch = _audio_profile_number(
        effects.get("pitch", 1.0), "тон", 0.5, 2.0
    )
    echo_delay = _audio_profile_number(
        effects.get("echo_delay", 300),
        "задержка эхо",
        50,
        1000,
        integer=True,
    )
    echo_decay = _audio_profile_number(
        effects.get("echo_decay", 0.3), "сила эхо", 0.1, 0.8
    )

    return {
        "schema": AUDIO_PROFILE_SCHEMA,
        "version": AUDIO_PROFILE_VERSION,
        "name": name,
        "audio": {
            "format": output_format,
            "bitrate": "auto" if output_format == "wav" else bitrate,
            "sample_rate": sample_rate,
            "channels": channels,
            "effects": {
                "enabled": enabled,
                "speed": speed,
                "pitch": pitch,
                "echo": effect_bool("echo"),
                "echo_delay": echo_delay,
                "echo_decay": echo_decay,
            },
        },
    }


def make_audio_profile(
    name,
    *,
    output_format="mp3",
    bitrate="auto",
    sample_rate="auto",
    channels="auto",
    effects_enabled=False,
    speed=1.0,
    pitch=1.0,
    echo=False,
    echo_delay=300,
    echo_decay=0.3,
):
    return normalize_audio_profile(
        {
            "schema": AUDIO_PROFILE_SCHEMA,
            "version": AUDIO_PROFILE_VERSION,
            "name": name,
            "audio": {
                "format": output_format,
                "bitrate": bitrate,
                "sample_rate": sample_rate,
                "channels": channels,
                "effects": {
                    "enabled": effects_enabled,
                    "speed": speed,
                    "pitch": pitch,
                    "echo": echo,
                    "echo_delay": echo_delay,
                    "echo_decay": echo_decay,
                },
            },
        }
    )


def builtin_audio_profiles():
    """Новые объекты встроенных профилей без общей изменяемой структуры."""
    return [
        make_audio_profile(
            "Opus · речь 32 кбит/с",
            output_format="opus",
            bitrate="32k",
            sample_rate="48000",
            channels="mono",
        ),
        make_audio_profile(
            "Opus · речь 48 кбит/с",
            output_format="opus",
            bitrate="48k",
            sample_rate="48000",
            channels="mono",
        ),
        make_audio_profile(
            "MP3 · совместимый 128 кбит/с",
            output_format="mp3",
            bitrate="128k",
            sample_rate="auto",
            channels="auto",
        ),
        make_audio_profile(
            "OGG/Vorbis · авто",
            output_format="ogg",
            bitrate="auto",
            sample_rate="auto",
            channels="auto",
        ),
        make_audio_profile(
            "WAV · без потерь",
            output_format="wav",
            bitrate="auto",
            sample_rate="auto",
            channels="auto",
        ),
    ]


def _effective_audio_profile_identity(profile):
    """Сравнимые слышимые параметры без имени и неактивных эффектов."""
    audio = copy.deepcopy(normalize_audio_profile(profile)["audio"])
    if not audio["effects"]["enabled"]:
        # Значения выключенных эффектов являются лишь заготовкой редактора и
        # не меняют результат. Из-за них эквивалентные профили не должны
        # ошибочно отображаться как разные.
        audio["effects"] = {"enabled": False}
    return audio


def matching_audio_profile_name(profile, custom_profiles=None):
    """Имя единственного точного совпадения либо ``None`` при неоднозначности."""
    identity = _effective_audio_profile_identity(profile)
    candidates = list(builtin_audio_profiles())
    for candidate in custom_profiles or []:
        try:
            candidates.append(normalize_audio_profile(candidate))
        except (TypeError, ValueError):
            continue
    matches = [
        candidate["name"]
        for candidate in candidates
        if _effective_audio_profile_identity(candidate) == identity
    ]
    return matches[0] if len(matches) == 1 else None


def describe_audio_profile(profile):
    """Компактная расшифровка профиля без неоднозначного слова «активный»."""
    audio = normalize_audio_profile(profile)["audio"]
    format_names = {
        "mp3": "MP3",
        "wav": "WAV",
        "ogg": "OGG/Vorbis",
        "opus": "Opus (Ogg)",
    }
    parts = [format_names.get(audio["format"], audio["format"].upper())]
    if audio["format"] == "wav":
        parts.append("без потерь")
    else:
        bitrate = audio["bitrate"]
        parts.append("битрейт: авто" if bitrate == "auto" else f"{bitrate[:-1]} кбит/с")

    rate = audio["sample_rate"]
    if rate == "auto":
        parts.append("частота: авто")
    else:
        rate_khz = int(rate) / 1000
        rate_text = f"{rate_khz:g}".replace(".", ",")
        parts.append(f"{rate_text} кГц")

    channel_names = {"auto": "каналы: авто", "mono": "моно", "stereo": "стерео"}
    parts.append(channel_names[audio["channels"]])
    effects = audio["effects"]
    if not effects["enabled"]:
        parts.append("эффекты: выкл.")
    else:
        effect_parts = []
        if effects["speed"] != 1.0:
            effect_parts.append(f"скорость {effects['speed']:g}×")
        if effects["pitch"] != 1.0:
            effect_parts.append(f"тон {effects['pitch']:g}")
        if effects["echo"]:
            effect_parts.append("эхо")
        parts.append(
            "эффекты: " + (", ".join(effect_parts) if effect_parts else "вкл.")
        )
    return " · ".join(parts)


def audio_profile_name_conflict(name, custom_profiles, *, exclude_index=None):
    identity = str(name or "").strip().casefold()
    if not identity:
        return True
    if any(profile["name"].casefold() == identity for profile in builtin_audio_profiles()):
        return True
    for index, profile in enumerate(custom_profiles or []):
        if index == exclude_index or not isinstance(profile, dict):
            continue
        if str(profile.get("name", "")).strip().casefold() == identity:
            return True
    return False


def unique_audio_profile_name(name, custom_profiles):
    base_name = str(name or "Пользовательский").strip() or "Пользовательский"
    if not audio_profile_name_conflict(base_name, custom_profiles):
        return base_name
    suffix = 2
    while audio_profile_name_conflict(f"{base_name} ({suffix})", custom_profiles):
        suffix += 1
    return f"{base_name} ({suffix})"


def audio_profile_config_values(profile, target):
    """Возвращает независимые ключи для book либо универсального export."""
    normalized = normalize_audio_profile(profile)
    audio = normalized["audio"]
    effects = audio["effects"]
    if target == "book":
        values = {
            "output_format": audio["format"],
            "output_bitrate": audio["bitrate"],
            "output_sample_rate": audio["sample_rate"],
            "output_channels": audio["channels"],
            "fx_speed": effects["speed"] if effects["enabled"] else 1.0,
            "fx_pitch": effects["pitch"] if effects["enabled"] else 1.0,
            "fx_echo": effects["echo"] if effects["enabled"] else False,
            "fx_echo_delay": effects["echo_delay"],
            "fx_echo_decay": effects["echo_decay"],
        }
    elif target == "export":
        values = {
            "export_format": audio["format"],
            "export_bitrate": audio["bitrate"],
            "export_sample_rate": audio["sample_rate"],
            "export_channels": audio["channels"],
            "export_apply_fx": effects["enabled"],
            "export_fx_speed": effects["speed"],
            "export_fx_pitch": effects["pitch"],
            "export_fx_echo": effects["echo"],
            "export_fx_echo_delay": effects["echo_delay"],
            "export_fx_echo_decay": effects["echo_decay"],
        }
    else:
        raise ValueError("target должен быть 'book' или 'export'")
    return values


def build_ru_normalizer(config):
    """Создаёт экземпляр для снимка настроек конкретной операции."""
    if Normalizer is None or NormalizeOptions is None:
        return None
    if _config_bool(config.get("text_is_prepared", False), default=False):
        return None
    if not _config_bool(config.get("normalizer_enabled", True), default=True):
        return None
    mode = str(config.get("normalizer_mode", "tts")).lower().strip()
    if mode not in NORMALIZER_MODES:
        mode = "tts"
    values = resolved_normalizer_options(config)
    kwargs = dict(values)
    kwargs["remove_links_ignore_interval"] = tuple(
        kwargs["remove_links_ignore_interval"]
    )
    for key in NORMALIZER_SEQUENCE_OPTIONS:
        kwargs[key] = tuple(kwargs[key])
    dictionaries_path = kwargs.get("dictionaries_path")
    kwargs["dictionaries_path"] = (
        Path(dictionaries_path).expanduser() if dictionaries_path else None
    )
    options = NormalizeOptions(mode=mode, **kwargs)
    return Normalizer(options)

# ================= КОНФИГУРАЦИЯ ПО УМОЛЧАНИЮ =================
DEFAULT_CONFIG = {
    "api_token": "",
    "api_url": DEFAULT_API_URL,
    "speaker": "arthas",
    # False сохраняет прежний payload и совместимость со старыми конфигами.
    "api_steps_enabled": False,
    "api_steps": 16,
    # При включённом steps безопаснее разделять результаты разного качества.
    # Флажок можно снять вручную ради использования общего legacy-кэша.
    "cache_include_steps": True,
    "input_dir": DEFAULT_INPUT_DIR,  
    "output_dir": DEFAULT_OUTPUT_DIR,
    "direct_output_dir": DEFAULT_DIRECT_OUTPUT_DIR,
    "cache_dir": DEFAULT_CACHE_DIR,
    "export_dir": "",
    "last_browse_dir": "",
    # UI-история диалогов хранится локально, но не входит ни в одну группу
    # экспортируемого профиля и поэтому не переезжает на другой компьютер.
    "last_config_dir": "",
    "last_glossary_dir": "",
    "last_audio_profile_dir": "",
    "last_normalizer_text_dir": "",
    
    "output_format": "mp3",
    "output_bitrate": "128k",
    "output_sample_rate": "auto",
    "output_channels": "auto",
    # Параметры универсальной вкладки экспорта. ``auto`` сохраняет общий
    # профиль исходников, когда он однороден, и выбирает совместимый профиль
    # для смешанного набора. Это не связано с каноническим 48 kHz mono-кэшем.
    "export_format": "mp3",
    "export_bitrate": "auto",
    "export_sample_rate": "auto",
    "export_channels": "auto",
    "export_apply_fx": False,
    "export_fx_speed": 1.0,
    "export_fx_pitch": 1.0,
    "export_fx_echo": False,
    "export_fx_echo_delay": 300,
    "export_fx_echo_decay": 0.3,
    "audio_profiles": [],
    "synthesis_mode": "sentence",
    
    "tag_title": "{filename}",
    "tag_artist": "",
    "tag_album_artist": "",
    "tag_album": "",
    "tag_genre": "",
    "tag_composer": "",
    "tag_year": "",
    "tag_cover": "",
    
    "pause_file_start": 0,
    "pause_file_end": 1000,
    "pause_sentence": 200,
    "pause_paragraph": 350,
    "pause_speech": 500,
    "pause_colon": 500,
    "pause_separator": 1000,
    
    "auto_trim_silence": True,
    "silence_threshold": -55.0,
    "auto_abbreviations": True,
    "auto_short_words": True,
    # Профиль ru-normalizr v1.5. Пустой словарь означает актуальные значения
    # встроенного TTS-пресета; явные overrides появляются только после того,
    # как пользователь применил настройки из песочницы.
    "normalizer_enabled": True,
    "normalizer_mode": "tts",
    "normalizer_options": {},
    "glossary_enabled": True,
    
    "use_cache": True,
    "cache_save_frequency": 100,
    "enable_cache_lru": False,
    "enable_cache_ttl": False,
    "cache_max_entries": 10000,
    "cache_ttl_hours": 720.0,
    
    "separator_symbols": "☆☆☆\n***\n###\n---", 
    "api_max_requests": 15,
    "api_time_window": 15.0,
    "max_retries": 5,
    "max_parallel_encodes": 0,

    "fx_speed": 1.0,
    "fx_pitch": 1.0,
    "fx_echo": False,
    "fx_echo_delay": 300,
    "fx_echo_decay": 0.3,
    
    "default_group_name": "Том {num}",
    "default_group_pause": 1000,
    
    "ui_font_size": 10,
    
    "direct_filename": "direct_output.mp3",
    "direct_save": True,
    "direct_force": False,
    "direct_autoplay": True,

    "skip_existing": True,
    
    "import_outdir": DEFAULT_INPUT_DIR,
    "import_template": "{num} - {name} - {title}",
    "import_regex": r"^Глава \d+",
    "import_single_file": False
}


def normalize_config(config):
    """Нормализует известные типы, сохраняя неизвестные ключи совместимости."""
    raw_config = config if isinstance(config, dict) else {}
    had_export_format = "export_format" in raw_config
    normalized = DEFAULT_CONFIG.copy()
    if isinstance(config, dict):
        normalized.update(config)

    boolean_keys = (
        "api_steps_enabled", "cache_include_steps", "auto_trim_silence",
        "auto_abbreviations", "auto_short_words", "use_cache",
        "normalizer_enabled", "glossary_enabled",
        "enable_cache_lru", "enable_cache_ttl", "fx_echo",
        "export_apply_fx", "export_fx_echo",
        "direct_save", "direct_force", "direct_autoplay",
        "skip_existing", "import_single_file",
    )
    for key in boolean_keys:
        normalized[key] = _config_bool(
            normalized.get(key), default=DEFAULT_CONFIG[key]
        )

    int_minimums = {
        "pause_file_start": 0, "pause_file_end": 0, "pause_sentence": 0,
        "pause_paragraph": 0, "pause_speech": 0, "pause_colon": 0,
        "pause_separator": 0, "api_max_requests": 1, "max_retries": 1,
        "max_parallel_encodes": 0, "cache_save_frequency": 1,
        "cache_max_entries": 1, "fx_echo_delay": 1,
        "export_fx_echo_delay": 1,
        "default_group_pause": 0, "ui_font_size": 6,
    }
    float_minimums = {
        "api_time_window": 0.0, "cache_ttl_hours": 0.000001,
        "fx_speed": 0.000001, "fx_pitch": 0.000001,
        "fx_echo_decay": 0.0, "export_fx_speed": 0.000001,
        "export_fx_pitch": 0.000001, "export_fx_echo_decay": 0.0,
    }

    for key, minimum in int_minimums.items():
        try:
            value = int(float(normalized.get(key, DEFAULT_CONFIG[key])))
            if value < minimum:
                raise ValueError
            normalized[key] = value
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "Некорректное значение %s=%r заменено на %r",
                key, normalized.get(key), DEFAULT_CONFIG[key],
            )
            normalized[key] = DEFAULT_CONFIG[key]

    for key, minimum in float_minimums.items():
        try:
            value = float(normalized.get(key, DEFAULT_CONFIG[key]))
            if value < minimum or value != value or value in (float("inf"), float("-inf")):
                raise ValueError
            normalized[key] = value
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "Некорректное значение %s=%r заменено на %r",
                key, normalized.get(key), DEFAULT_CONFIG[key],
            )
            normalized[key] = DEFAULT_CONFIG[key]

    try:
        normalized["silence_threshold"] = float(
            normalized.get("silence_threshold", DEFAULT_CONFIG["silence_threshold"])
        )
    except (TypeError, ValueError, OverflowError):
        normalized["silence_threshold"] = DEFAULT_CONFIG["silence_threshold"]

    for key, allowed in (
        ("output_format", set(AUDIO_PROFILE_FORMATS)),
        ("export_format", set(AUDIO_PROFILE_FORMATS)),
        ("synthesis_mode", {"sentence", "paragraph", "full"}),
        ("normalizer_mode", set(NORMALIZER_MODES)),
    ):
        value = str(normalized.get(key, DEFAULT_CONFIG[key])).lower().strip()
        normalized[key] = value if value in allowed else DEFAULT_CONFIG[key]

    # До v1.5 универсальный экспорт ошибочно делил format с книжной сборкой.
    # Однократная миграция сохраняет выбранное старое значение в обеих целях.
    if not had_export_format:
        normalized["export_format"] = normalized["output_format"]

    normalized["normalizer_options"] = normalize_normalizer_options(
        normalized.get("normalizer_options", {}),
        normalized["normalizer_mode"],
        full=False,
    )

    bitrate = str(normalized.get("output_bitrate", "")).strip().lower()
    normalized["output_bitrate"] = (
        bitrate
        if bitrate == "auto" or re.fullmatch(r"[1-9]\d*k", bitrate)
        else DEFAULT_CONFIG["output_bitrate"]
    )

    for prefix in ("output", "export"):
        sample_key = f"{prefix}_sample_rate"
        sample_rate = str(normalized.get(sample_key, "auto")).strip().lower()
        normalized[sample_key] = (
            sample_rate
            if sample_rate in AUDIO_PROFILE_SAMPLE_RATES
            else DEFAULT_CONFIG[sample_key]
        )
        channels_key = f"{prefix}_channels"
        channels = str(normalized.get(channels_key, "auto")).strip().lower()
        normalized[channels_key] = (
            channels
            if channels in AUDIO_PROFILE_CHANNELS
            else DEFAULT_CONFIG[channels_key]
        )

    export_bitrate = str(normalized.get("export_bitrate", "auto")).strip().lower()
    normalized["export_bitrate"] = (
        export_bitrate
        if export_bitrate == "auto" or re.fullmatch(r"[1-9]\d*k", export_bitrate)
        else DEFAULT_CONFIG["export_bitrate"]
    )
    custom_profiles = normalized.get("audio_profiles", [])
    normalized_profiles = []
    if isinstance(custom_profiles, list):
        for index, profile in enumerate(custom_profiles):
            try:
                normalized_profiles.append(normalize_audio_profile(profile))
            except ValueError as exc:
                logging.warning(
                    "Некорректный пользовательский аудиопрофиль %d пропущен: %s",
                    index + 1,
                    exc,
                )
    normalized["audio_profiles"] = normalized_profiles

    for key, fallback in REQUIRED_DIRECTORY_DEFAULTS.items():
        value = normalized.get(key)
        normalized[key] = str(value).strip() if isinstance(value, (str, Path)) else ""
        if not normalized[key]:
            normalized[key] = fallback

    for key in (
        "api_token", "api_url", "speaker", "export_dir",
        "last_browse_dir", "last_config_dir", "last_glossary_dir",
        "last_audio_profile_dir", "last_normalizer_text_dir",
        "separator_symbols", "tag_title", "tag_artist",
        "tag_album_artist", "tag_album", "tag_genre", "tag_composer",
        "tag_year", "tag_cover", "default_group_name",
        "direct_filename", "import_template", "import_regex",
    ):
        value = normalized.get(key, DEFAULT_CONFIG.get(key, ""))
        normalized[key] = str(value) if value is not None else ""

    if not normalized["api_url"].strip():
        normalized["api_url"] = DEFAULT_CONFIG["api_url"]
    if not normalized["default_group_name"].strip():
        normalized["default_group_name"] = DEFAULT_CONFIG["default_group_name"]
    return normalized

# ================= ОБРАБОТКА ЭФФЕКТОВ (FFmpeg) =================
class AudioEffects:
    """Класс для применения аудиоэффектов (скорость, тон, эхо) через системный FFmpeg."""

    @staticmethod
    def _atempo_filters(factor):
        """Разбивает tempo на допустимые для FFmpeg звенья диапазона 0.5..2.0."""
        factor = float(factor)
        if factor <= 0:
            raise ValueError("Коэффициент темпа должен быть больше нуля")

        filters = []
        while factor > 2.0:
            filters.append("atempo=2.0")
            factor /= 2.0
        while factor < 0.5:
            filters.append("atempo=0.5")
            factor /= 0.5
        if not filters or abs(factor - 1.0) > 1e-9:
            filters.append(f"atempo={factor:.8g}")
        return filters
    
    @staticmethod
    def apply_effects(
        audio_segment,
        speed=1.0,
        pitch=1.0,
        echo=False,
        echo_delay=300,
        echo_decay=0.3,
        *,
        strict=False,
    ):
        """Применяет эффекты; ``strict`` запрещает молча отдавать оригинал.

        Для предпрослушивания безопаснее сохранить возможность проиграть
        исходник при недоступном FFmpeg. При экспорте такое молчаливое
        продолжение вводило пользователя в заблуждение: файл объявлялся
        готовым, хотя выбранные эффекты не применились.
        """
        if speed == 1.0 and pitch == 1.0 and not echo:
            return audio_segment

        filters = []
        if pitch != 1.0:
            new_sr = int(48000 * pitch)
            filters.append(f"asetrate={new_sr}")
            filters.extend(AudioEffects._atempo_filters(1 / pitch))
        if speed != 1.0:
            filters.extend(AudioEffects._atempo_filters(speed))
        if echo:
            filters.append(f"aecho=0.8:0.8:{int(echo_delay)}:{float(echo_decay)}")

        filter_str = ",".join(filters)

        in_path = None
        out_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in, \
                 tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
                in_path = f_in.name
                out_path = f_out.name

            exported_input = audio_segment.export(in_path, format="wav")
            if hasattr(exported_input, "flush"):
                exported_input.flush()
            if hasattr(exported_input, "close"):
                exported_input.close()
            command = [get_ffmpeg_path(), "-y", "-i", in_path, "-af", filter_str, out_path]
            
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo, check=True)
            return AudioSegment.from_file(out_path, format="wav")
        except Exception as e:
            logging.error(f"Ошибка применения эффектов FFmpeg: {e}")
            if strict:
                raise RuntimeError(
                    f"Не удалось применить выбранные эффекты через FFmpeg: {e}"
                ) from e
            return audio_segment
        finally:
            # Гарантированное удаление файлов при любом исходе
            if in_path and os.path.exists(in_path):
                try:
                    os.remove(in_path)
                except OSError:
                    pass
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
# ================= ЯДРО СИНТЕЗА =================
class RateLimiter:
    """Контроллер частоты запросов к API."""
    def __init__(self, max_requests, time_window):
        self.max_requests = max(1, int(max_requests))
        self.time_window = max(0.0, float(time_window))
        self.timestamps = deque()
        self._lock = threading.Lock()

    def update_limits(self, max_requests, time_window):
        """Атомарно обновляет лимиты, пока другие потоки делают запросы."""
        with self._lock:
            self.max_requests = max(1, int(max_requests))
            self.time_window = max(0.0, float(time_window))

    def wait(self, cancelled=None):
        # Один lock нужен не только для deque: без него два параллельных
        # синтеза могли одновременно пройти проверку и превысить API-лимит.
        while True:
            if cancelled and cancelled():
                return False
            with self._lock:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.time_window:
                    self.timestamps.popleft()

                if len(self.timestamps) < self.max_requests:
                    if cancelled and cancelled():
                        return False
                    self.timestamps.append(now)
                    return True

                sleep_time = self.time_window - (now - self.timestamps[0])

            # Не держим lock во сне: настройки лимитера и другие ожидающие
            # потоки остаются отзывчивыми. При остановке просыпаемся не реже
            # десяти раз в секунду, а не ждём всё окно лимитера.
            if sleep_time > 0:
                time.sleep(min(sleep_time, 0.1) if cancelled else sleep_time)

class TTSProcessor:
    """Главный процессор для обработки текста и взаимодействия с API Silero."""
    def __init__(
        self,
        config,
        shared_rate_limiter=None,
        error_callback=None,
        shared_cache=None,
        shared_cache_lock=None,
        shared_processing_statuses=None,
    ):
        # Основной UI уже хранит нормализованный конфиг, но TTSProcessor также
        # создаётся напрямую оптимизатором кэша и может использоваться отдельно
        # от GUI. Локальная нормализация гарантирует, что JSON-строки вроде
        # "false" не превратятся в True при обычном bool("false").
        self.cfg = normalize_config(config)
        _, recovered_paths = ensure_config_directories(
            self.cfg, keys=("input_dir", "output_dir", "cache_dir")
        )
        # GUI должен сохранить те же восстановленные значения, иначе следующий
        # запуск снова попробует путь с отключённого диска. Прямые пользователи
        # TTSProcessor получают только локальную нормализованную копию.
        if isinstance(config, dict):
            for key in recovered_paths:
                config[key] = self.cfg[key]
        config = self.cfg
        self.error_callback = error_callback

        # Используем общий лимитер, если передан
        self.rate_limiter = shared_rate_limiter or RateLimiter(int(config["api_max_requests"]), float(config["api_time_window"]))
        
        self.cache_dir = Path(self.cfg["cache_dir"]).expanduser()
        self.cache_audio_dir = self.cache_dir / "audio"
        self.cache_audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_index_path = self.cache_dir / "sentence_cache.json"
        self.glossary_path = self.cache_dir / "glossary.json"

        self.session = requests.Session()
        self.cache_lock = shared_cache_lock or threading.RLock()

        max_enc = int(self.cfg.get("max_parallel_encodes", 0))
        self.encode_semaphore = threading.Semaphore(max_enc) if max_enc > 0 else None
        
        # Одновременно работающие вкладки с одной cache_dir используют один
        # RAM-словарь. Иначе две независимые финальные записи могли потерять
        # элементы, добавленные соседним процессором.
        self.cache = shared_cache if shared_cache is not None else self._load_cache()
        self.unsaved_cache_items = 0
        self.cache_metadata_dirty = False
        self.active_threads = []
        # Снимок последней команды полезен для диагностики ошибок FFmpeg и
        # регрессионных тестов. До первой сборки у него есть явное значение.
        self._last_ffmpeg_save_command = None
        self._normalizer = build_ru_normalizer(self.cfg)
        
        self.glossary_ignore_case = {}
        self.glossary_strict_case = {}
        self.glossary_regex = []
        self.glossary_verbatim_regex = []
        self.load_glossary_file()
        
        self.is_stopped = False
        
        raw_seps = str(self.cfg.get("separator_symbols", ""))
        if "," in raw_seps and "\n" not in raw_seps: raw_seps = raw_seps.replace(",", "\n")
        self.separators = [s.strip() for s in raw_seps.split("\n") if s.strip()]

        self.processing_statuses_ram = shared_processing_statuses
        if self.processing_statuses_ram is None:
            self.processing_statuses_ram = {}
            status_file = APP_DATA_DIR / "processing_statuses.json"
            if status_file.exists():
                try:
                    with open(status_file, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        # Очищаем старый мусор, загружаем только проблемные статусы.
                        self.processing_statuses_ram = {
                            key: value
                            for key, value in loaded.items()
                            if value in ("warning", "error")
                        }
                except Exception as exc:
                    logging.error(f"Ошибка загрузки processing_statuses.json: {exc}")

            # Не оставляем устаревший файл с success или пустым объектом до
            # следующего синтеза. Непустые ошибки останутся для resume.
            if not self.processing_statuses_ram:
                try:
                    status_file.unlink(missing_ok=True)
                except OSError as exc:
                    logging.error(f"Не удалось удалить пустой processing_statuses.json: {exc}")

    def _mark_output_status(self, out_filepath, status):
        """Запоминает только проблемные результаты для корректного resume."""
        key = str(Path(out_filepath).resolve())
        with self.cache_lock:
            if status in ("warning", "error"):
                self.processing_statuses_ram[key] = status
            else:
                self.processing_statuses_ram.pop(key, None)

    def _save_processing_statuses(self):
        """Сохраняет ошибки resume; пустой список на диске не держим."""
        status_file = APP_DATA_DIR / "processing_statuses.json"
        temp_file = status_file.with_name(
            f".{status_file.name}.{uuid.uuid4().hex}.tmp"
        )

        with self.cache_lock:
            # Миграция старых файлов: success и неизвестные значения не нужны.
            statuses = {
                key: value
                for key, value in self.processing_statuses_ram.items()
                if value in ("warning", "error")
            }
            self.processing_statuses_ram.clear()
            self.processing_statuses_ram.update(statuses)

            try:
                if not statuses:
                    status_file.unlink(missing_ok=True)
                    temp_file.unlink(missing_ok=True)
                    return

                status_file.parent.mkdir(parents=True, exist_ok=True)
                with open(temp_file, "w", encoding="utf-8") as file:
                    json.dump(statuses, file, ensure_ascii=False, indent=4)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_file, status_file)
            except Exception as exc:
                logging.error(f"Не удалось сохранить processing_statuses.json: {exc}")
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_cache(self):
        data, source_path, errors = read_cache_index_with_backup(
            self.cache_index_path
        )
        for path, exc in errors:
            logging.error(
                "Не удалось прочитать индекс кэша %s: %s", path.name, exc
            )
        if source_path is not None and source_path != self.cache_index_path:
            logging.warning(
                "Рабочий индекс кэша недоступен; загружена резервная копия %s",
                source_path.name,
            )
        return data

    def stop(self):
        """Сигнализирует остановку и закрывает текущие сетевые соединения."""
        self.is_stopped = True
        try:
            self.session.close() # Разрывает висящие HTTP-запросы за 1 миллисекунду
        except Exception as exc:
            logging.debug("Ошибка закрытия HTTP-сессии при остановке: %s", exc)
    
    def _enforce_cache_limits(self):
        """Удаляет ключи только из RAM и возвращает их метаданные.

        Физические файлы удаляются после успешной атомарной публикации нового
        индекса. Так сбой записи не оставит JSON со ссылкой на уже удалённый OGG.
        """
        now = time.time()
        removed_entries = []

        if _config_bool(self.cfg.get("enable_cache_ttl", False)):
            ttl_sec = float(self.cfg.get("cache_ttl_hours", 720.0)) * 3600
            keys_to_delete = [
                key
                for key, value in self.cache.items()
                if now - float(
                    value.get("last_accessed", value.get("created_at", now))
                ) > ttl_sec
            ]
            for k in keys_to_delete:
                cache_info = self.cache.pop(k, None)
                if cache_info is not None:
                    removed_entries.append(cache_info)
                    self.unsaved_cache_items += 1
                
        if _config_bool(self.cfg.get("enable_cache_lru", False)):
            max_entries = int(self.cfg.get("cache_max_entries", 10000))
            if len(self.cache) > max_entries:
                sorted_keys = sorted(
                    self.cache.keys(),
                    key=lambda key: float(
                        self.cache[key].get(
                            "last_accessed",
                            self.cache[key].get("created_at", 0),
                        )
                    ),
                )
                excess = len(self.cache) - max_entries
                for k in sorted_keys[:excess]:
                    cache_info = self.cache.pop(k, None)
                    if cache_info is not None:
                        removed_entries.append(cache_info)
                        self.unsaved_cache_items += 1
        return removed_entries

    def _save_cache(self, force=False):
        """Атомарно сохраняет индекс; force также сбрасывает hit-статистику."""
        with self.cache_lock:
            should_save = self.unsaved_cache_items > 0 or (force and self.cache_metadata_dirty)
            if should_save:
                removed_entries = self._enforce_cache_limits()
                try:
                    write_cache_index_atomic(self.cache_dir, self.cache)
                except Exception as exc:
                    logging.error("Ошибка при атомарном сохранении кэша: %s", exc)
                    return
                for filepath in unreferenced_cache_audio_paths(
                    self.cache_dir, removed_entries, self.cache
                ):
                    try:
                        filepath.unlink(missing_ok=True)
                    except OSError as exc:
                        logging.warning(
                            "Не удалось удалить устаревший файл кэша %s: %s",
                            filepath,
                            exc,
                        )
                self.unsaved_cache_items = 0
                self.cache_metadata_dirty = False

    def flush_cache(self):
        """Явная финальная запись новых элементов и RAM-only hit-статистики."""
        self._save_cache(force=True)

    def apply_regex_rules(self, text):
        cfg = getattr(self, "cfg", {})
        if not _config_bool(cfg.get("glossary_enabled", True), default=True):
            return text
        for rule in self.glossary_regex:
            if not isinstance(rule, dict): continue
            # Verbatim-RegEx применяется после разбиения на предложения. Его
            # замена должна пережить ru-normalizr и финальную очистку, поэтому
            # на раннем этапе подготовки абзаца такое правило не исполняем.
            if _config_bool(rule.get("verbatim"), default=False):
                continue
            pattern = rule.get("pattern") or rule.get("regex")
            repl = str(rule.get("repl") if rule.get("repl") is not None else "")
            if not pattern: continue
            
            # --- АВТО-САНИТАЙЗЕР: Безопасно чинит \1..\9 -> \g<1>..\g<9> ---
            for i in range(1, 10):
                repl = repl.replace(chr(i), f"\\g<{i}>")
            # -------------------------------------------------------------
            
            try: 
                text = re.sub(pattern, repl, text, flags=re.MULTILINE)
            except Exception as e: 
                logging.error(f"Ошибка в RegEx '{pattern}': {e}")
        return text

    def _protect_separator_lines(self, raw_text, separator_token):
        """Заменяет только отдельные строки-разделители служебным токеном.

        Разделитель должен занимать всю строку; вокруг него допускаются лишь
        пробелы и табуляции. Защита выполняется до типографики тире и
        пользовательских RegEx, поэтому ``---``/``–––`` не превращаются в
        реплику диалога и не исчезают до распознавания паузы.
        """
        if not self.separators:
            return raw_text
        alternatives = sorted(set(self.separators), key=len, reverse=True)
        if not alternatives:
            return raw_text
        pattern = r"^[ \t]*(?:" + "|".join(
            re.escape(separator) for separator in alternatives
        ) + r")[ \t]*$"
        return re.sub(pattern, separator_token, raw_text, flags=re.MULTILINE)

    def _prepare_raw_text(self, raw_text, separator_token):
        """Единая предварительная обработка для синтеза и оптимизации кэша."""
        raw_text = strip_leading_text_bom(raw_text)
        raw_text = self._protect_separator_lines(raw_text, separator_token)
        if _config_bool(
            getattr(self, "cfg", {}).get("text_is_prepared", False),
            default=False,
        ):
            # Внешний редактор ударений уже сформировал произносимую строку.
            # Никакие пользовательские RegEx и типографика здесь не нужны;
            # служебные строки-разделители уже защищены выше.
            return raw_text
        raw_text = re.sub(r'[«»“”„]', '"', raw_text)
        raw_text = normalize_dialogue_line_starts(raw_text)
        raw_text = self.apply_regex_rules(raw_text)
        # Пользовательские правила глоссария могут добавлять новые строки
        # разделителей, например ``\n***`` после заголовка. Их тоже нужно
        # превратить в токен до sentenize(), иначе такая строка ошибочно
        # проходит как пустой фрагмент вместо настроенной паузы.
        return self._protect_separator_lines(raw_text, separator_token)

    def load_glossary_file(self):
        backup_path = self.glossary_path.with_suffix(
            self.glossary_path.suffix + ".bak"
        )
        candidates = [
            candidate
            for candidate in (self.glossary_path, backup_path)
            if candidate.exists()
        ]
        if not candidates:
            return

        data = None
        for candidate in candidates:
            try:
                with open(candidate, 'r', encoding='utf-8-sig') as f:
                    loaded = json.load(f)
                data = canonicalize_glossary_data(loaded)
                if candidate == backup_path:
                    logging.warning(
                        "Глоссарий восстановлен из резервной копии %s",
                        backup_path,
                    )
                break
            except Exception as e:
                logging.error(f"Ошибка чтения файла глоссария {candidate}: {e}")
        if data is None:
            return

        self.load_glossary_data(data)

    @staticmethod
    def _glossary_word_pattern(source, *, ignore_case=False, whole_word=True):
        flags = re.IGNORECASE if ignore_case else 0
        escaped = re.escape(source)
        if whole_word:
            escaped = (
                r"(?<![а-яА-Яa-zA-Z0-9_ёЁ])" + escaped
                + r"(?![а-яА-Яa-zA-Z0-9_ёЁ])"
            )
        return re.compile(escaped, flags)

    @classmethod
    def _compile_glossary_term_rule(
        cls, source, value, *, ignore_case=False
    ):
        spec = glossary_term_spec(value)
        source_pattern = cls._glossary_word_pattern(
            source,
            ignore_case=ignore_case,
            whole_word=spec["whole_word"],
        )
        match_pattern = source_pattern
        inner_pattern = None
        if spec["verbatim"] and not spec["whole_word"]:
            # Частичная verbatim-замена защищает всё содержащее её слово.
            # Иначе ru-normalizr добавляет пробелы вокруг служебного маркера и
            # незаметно меняет границы слова. Внутри защищённого слова меняется
            # только найденная пользователем подстрока.
            word_chars = r"[а-яА-Яa-zA-Z0-9_ёЁ]"
            flags = re.IGNORECASE if ignore_case else 0
            match_pattern = re.compile(
                r"(?<![а-яА-Яa-zA-Z0-9_ёЁ])"
                + word_chars + "*" + re.escape(source) + word_chars + "*"
                + r"(?![а-яА-Яa-zA-Z0-9_ёЁ])",
                flags,
            )
            inner_pattern = re.compile(re.escape(source), flags)
        return {
            "pattern": match_pattern,
            "inner_pattern": inner_pattern,
            "replacement": spec["replacement"],
            "verbatim": spec["verbatim"],
            "ignore_case": bool(ignore_case),
        }

    def load_glossary_data(self, data):
        """Компилирует JSON-объект без обращения к диску (в том числе для preview)."""
        data = canonicalize_glossary_data(data)
        shadowed_accents = glossary_shadowed_accent_rules(data)
        if shadowed_accents:
            preview = ", ".join(
                conflict["accent"] for conflict in shadowed_accents[:5]
            )
            suffix = ", …" if len(shadowed_accents) > 5 else ""
            logging.warning(
                "В глоссарии %d правил ударения перекрыты правилами терминов "
                "и не применяются (термин имеет приоритет): %s%s",
                len(shadowed_accents),
                preview,
                suffix,
            )

        # Очищаем словари и списки перед загрузкой: редактор и песочница могут
        # многократно применять один объект в течение сеанса.
        self.glossary_ignore_case = {}
        self.glossary_strict_case = {}
        self.compiled_strict_case = []
        self.compiled_ignore_case = []

        accents_ignore_case = data.get("accents_ignore_case", [])
        if not isinstance(accents_ignore_case, (list, tuple)):
            accents_ignore_case = []
        for w in accents_ignore_case:
            if isinstance(w, str) and w.strip():
                self.glossary_ignore_case[w.replace("+", "").lower()] = w
                
        accents_strict_case = data.get("accents_strict_case", [])
        if not isinstance(accents_strict_case, (list, tuple)):
            accents_strict_case = []
        for w in accents_strict_case:
            if isinstance(w, str) and w.strip():
                self.glossary_strict_case[w.replace("+", "")] = w
                
        terms_ignore_case = data.get("terms_ignore_case", {})
        if not isinstance(terms_ignore_case, dict):
            terms_ignore_case = {}
        for k, v in terms_ignore_case.items():
            if isinstance(k, str) and k.strip():
                self.glossary_ignore_case[k.lower()] = copy.deepcopy(v)
                
        terms_strict_case = data.get("terms_strict_case", {})
        if not isinstance(terms_strict_case, dict):
            terms_strict_case = {}
        for k, v in terms_strict_case.items():
            if isinstance(k, str) and k.strip():
                self.glossary_strict_case[k] = copy.deepcopy(v)

        # Предкомпиляция терминов. Legacy-строки остаются обычными правилами;
        # расширенный JSON-объект несёт verbatim/whole_word.
        for original, replacement in self.glossary_strict_case.items():
            self.compiled_strict_case.append(
                self._compile_glossary_term_rule(
                    original, replacement, ignore_case=False
                )
            )

        for original_lower, replacement in self.glossary_ignore_case.items():
            self.compiled_ignore_case.append(
                self._compile_glossary_term_rule(
                    original_lower, replacement, ignore_case=True
                )
            )

        regex_rules = data.get("regex_rules", [])
        self.glossary_regex = (
            copy.deepcopy(regex_rules) if isinstance(regex_rules, list) else []
        )
        self.glossary_verbatim_regex = []
        for rule in self.glossary_regex:
            if not isinstance(rule, dict) or not _config_bool(
                rule.get("verbatim"), default=False
            ):
                continue
            pattern = rule.get("pattern") or rule.get("regex")
            if not pattern:
                continue
            repl = str(rule.get("repl") if rule.get("repl") is not None else "")
            for index in range(1, 10):
                repl = repl.replace(chr(index), f"\\g<{index}>")
            try:
                self.glossary_verbatim_regex.append(
                    (re.compile(pattern, re.MULTILINE), repl, pattern)
                )
            except re.error as exc:
                logging.error("Ошибка в RegEx '%s': %s", pattern, exc)

    @staticmethod
    def _case_aware_glossary_replacement(source, replacement, ignore_case):
        replacement = str(replacement)
        if not ignore_case:
            return replacement
        if source.isupper():
            return replacement.upper()
        if source.istitle() and replacement:
            return replacement[0].upper() + replacement[1:]
        return replacement

    @classmethod
    def _term_rule_replacement(cls, rule, match):
        matched = match.group(0)
        inner_pattern = rule.get("inner_pattern")
        if inner_pattern is None:
            return cls._case_aware_glossary_replacement(
                matched, rule["replacement"], rule["ignore_case"]
            )
        return inner_pattern.sub(
            lambda inner: cls._case_aware_glossary_replacement(
                inner.group(0), rule["replacement"], rule["ignore_case"]
            ),
            matched,
        )

    @staticmethod
    def _tagged_substitution(
        segments,
        pattern,
        replacement_func,
        *,
        protect_adjacent_word=False,
    ):
        """Применяет правило только к ещё не защищённым частям текста."""
        result = []
        word_character = re.compile(r"[а-яА-Яa-zA-Z0-9_ёЁ]").fullmatch
        for segment_text, is_verbatim in segments:
            if is_verbatim or not segment_text:
                result.append((segment_text, is_verbatim))
                continue
            matches = list(pattern.finditer(segment_text))
            if not matches:
                result.append((segment_text, False))
                continue

            if not protect_adjacent_word:
                position = 0
                for match in matches:
                    if match.start() > position:
                        result.append((segment_text[position:match.start()], False))
                    result.append(
                        (
                            _VerbatimText(
                                replacement_func(match),
                                segment_text[match.start():match.end()],
                            ),
                            True,
                        )
                    )
                    position = match.end()
                if position < len(segment_text):
                    result.append((segment_text[position:], False))
                continue

            # Verbatim-RegEx защищает всё содержащее совпадение слово, чтобы
            # normalizer не изменил его остаток. Несколько совпадений в одном
            # слове объединяем в один сегмент, но применяем каждую замену —
            # семантика остаётся такой же, как у re.sub().
            protected_regions = []
            for match in matches:
                protected_start = match.start()
                protected_end = match.end()
                while (
                    protected_start > 0
                    and word_character(segment_text[protected_start - 1])
                ):
                    protected_start -= 1
                while (
                    protected_end < len(segment_text)
                    and word_character(segment_text[protected_end])
                ):
                    protected_end += 1
                if protected_regions and protected_start <= protected_regions[-1][1]:
                    protected_regions[-1][1] = max(
                        protected_regions[-1][1], protected_end
                    )
                    protected_regions[-1][2].append(match)
                else:
                    protected_regions.append(
                        [protected_start, protected_end, [match]]
                    )

            position = 0
            for protected_start, protected_end, region_matches in protected_regions:
                if protected_start > position:
                    result.append((segment_text[position:protected_start], False))
                replacement_parts = []
                match_position = protected_start
                for match in region_matches:
                    replacement_parts.append(
                        segment_text[match_position:match.start()]
                    )
                    replacement_parts.append(replacement_func(match))
                    match_position = match.end()
                replacement_parts.append(
                    segment_text[match_position:protected_end]
                )
                result.append(
                    (
                        _VerbatimText(
                            "".join(replacement_parts),
                            segment_text[protected_start:protected_end],
                        ),
                        True,
                    )
                )
                position = protected_end
            if position < len(segment_text):
                result.append((segment_text[position:], False))
        return result

    def _apply_verbatim_regex_segments(self, segments):
        for pattern, replacement, source_pattern in getattr(
            self, "glossary_verbatim_regex", []
        ):
            try:
                segments = self._tagged_substitution(
                    segments,
                    pattern,
                    lambda match, repl=replacement: match.expand(repl),
                    protect_adjacent_word=True,
                )
            except Exception as exc:
                logging.error("Ошибка в verbatim RegEx '%s': %s", source_pattern, exc)
        return segments

    def apply_glossary_segments(self, text):
        """Возвращает обычные и verbatim-фрагменты без хрупкого text diff."""
        cfg = getattr(self, "cfg", {})
        if not _config_bool(cfg.get("glossary_enabled", True), default=True):
            return [(text, False)]

        segments = self._apply_verbatim_regex_segments([(text, False)])
        for rule in getattr(self, "compiled_strict_case", []):
            if not isinstance(rule, dict):
                pattern, replacement = rule[:2]
                rule = {
                    "pattern": pattern,
                    "inner_pattern": None,
                    "replacement": replacement,
                    "verbatim": False,
                    "ignore_case": False,
                }
            if rule.get("verbatim"):
                segments = self._tagged_substitution(
                    segments,
                    rule["pattern"],
                    lambda match, current=rule: self._term_rule_replacement(
                        current, match
                    ),
                )
            else:
                segments = [
                    (
                        segment_text
                        if is_verbatim
                        else rule["pattern"].sub(
                            lambda match, current=rule: self._term_rule_replacement(
                                current, match
                            ),
                            segment_text,
                        ),
                        is_verbatim,
                    )
                    for segment_text, is_verbatim in segments
                ]

        for rule in getattr(self, "compiled_ignore_case", []):
            if not isinstance(rule, dict):
                pattern, replacement = rule[:2]
                rule = {
                    "pattern": pattern,
                    "inner_pattern": None,
                    "replacement": replacement,
                    "verbatim": False,
                    "ignore_case": True,
                }
            if rule.get("verbatim"):
                segments = self._tagged_substitution(
                    segments,
                    rule["pattern"],
                    lambda match, current=rule: self._term_rule_replacement(
                        current, match
                    ),
                )
            else:
                segments = [
                    (
                        segment_text
                        if is_verbatim
                        else rule["pattern"].sub(
                            lambda match, current=rule: self._term_rule_replacement(
                                current, match
                            ),
                            segment_text,
                        ),
                        is_verbatim,
                    )
                    for segment_text, is_verbatim in segments
                ]
        return segments

    def apply_glossary(self, text):
        return "".join(
            segment for segment, _is_verbatim in self.apply_glossary_segments(text)
        )

    @staticmethod
    def _segments_to_markers(segments):
        occupied = "".join(
            str(segment)
            + (getattr(segment, "source", "") if is_verbatim else "")
            for segment, is_verbatim in segments
        )
        marker_map = {}
        rendered = []
        codepoint = 0xF0000
        for segment, is_verbatim in segments:
            if not is_verbatim:
                rendered.append(segment)
                continue
            while codepoint <= 0xFFFFD and chr(codepoint) in occupied:
                codepoint += 1
            if codepoint > 0xFFFFD:
                raise ValueError("слишком много verbatim-фрагментов в одном чанке")
            marker = chr(codepoint)
            codepoint += 1
            marker_map[marker] = str(segment)
            rendered.append(marker)
        return "".join(rendered), marker_map

    @staticmethod
    def _contextual_verbatim_text(segments):
        return "".join(
            getattr(segment, "source", str(segment))
            if is_verbatim
            else str(segment)
            for segment, is_verbatim in segments
        )

    @classmethod
    def _mark_surviving_verbatim_sources(cls, normalized, segments):
        """Заменяет пережившие normalizer исходники на безопасные маркеры.

        Поиск идёт слева направо в порядке сегментов. Это позволяет выполнить
        ru-normalizr всего один раз в обычном случае и при этом не закрывать от
        него значимый контекст вроде ``Глава IV`` или ``2 кг``.
        """
        _protected_text, marker_map = cls._segments_to_markers(segments)
        entries = []
        marker_iter = iter(marker_map)
        for segment, is_verbatim in segments:
            if not is_verbatim:
                continue
            source = getattr(segment, "source", str(segment))
            if not source:
                raise ValueError("пустой verbatim-фрагмент нельзя локализовать")
            entries.append((next(marker_iter), source))

        if not entries:
            return normalized, marker_map

        parts = []
        cursor = 0
        for marker, source in entries:
            start = normalized.find(source, cursor)
            end = start + len(source)
            if start < 0:
                match = re.search(
                    re.escape(source),
                    normalized[cursor:],
                    flags=re.IGNORECASE,
                )
                if match is None:
                    raise ValueError(
                        "ru-normalizr изменил исходный verbatim-фрагмент"
                    )
                start = cursor + match.start()
                end = cursor + match.end()
            parts.append(normalized[cursor:start])
            parts.append(marker)
            cursor = end
        parts.append(normalized[cursor:])
        rendered = "".join(parts)
        if not all(rendered.count(marker) == 1 for marker in marker_map):
            raise ValueError("не удалось однозначно расставить verbatim-маркеры")
        return rendered, marker_map

    def _normalize_glossary_segments(self, segments):
        active_normalizer = getattr(self, "_normalizer", normalizer)
        if active_normalizer is None:
            return self._segments_to_markers(segments)

        # Сначала оставляем исходные термины видимыми normalizer. Если они не
        # изменились, точные замены локализуются в результате без text diff и
        # без потери контекста соседних чисел, римских чисел и единиц измерения.
        try:
            contextual_text = self._contextual_verbatim_text(segments)
            normalized = active_normalizer.normalize(contextual_text)
            return self._mark_surviving_verbatim_sources(normalized, segments)
        except Exception as contextual_exc:
            logging.warning(
                "Verbatim применён через резервную защиту: ru-normalizr "
                "изменил исходный фрагмент, поэтому грамматический контекст "
                "рядом с ним может отличаться; text='%s'; reason=%s",
                _log_text_preview(contextual_text),
                contextual_exc,
            )

        # Если сам исходник должен был нормализоваться (например, RegEx нашёл
        # число), выполняем прежний непрозрачный проход с PUA-маркерами.
        protected_text, marker_map = self._segments_to_markers(segments)
        try:
            normalized = active_normalizer.normalize(protected_text)
            if all(normalized.count(marker) == 1 for marker in marker_map):
                return normalized, marker_map
            raise ValueError("ru-normalizr изменил служебный verbatim-маркер")
        except Exception as exc:
            logging.warning(
                "Не удалось нормализовать фразу единым проходом (%s); "
                "используется безопасная сегментная обработка.",
                exc,
            )
            fallback_segments = []
            for segment, is_verbatim in segments:
                if is_verbatim:
                    fallback_segments.append((segment, True))
                    continue
                try:
                    segment = active_normalizer.normalize(segment)
                except Exception as segment_exc:
                    logging.warning(
                        "Ошибка нормализации фрагмента '%s...': %s",
                        segment[:30],
                        segment_exc,
                    )
                fallback_segments.append((segment, False))
            return self._segments_to_markers(fallback_segments)

    @staticmethod
    def _restore_verbatim_markers(text, marker_map):
        for marker, replacement in marker_map.items():
            text = text.replace(marker, replacement)
        return text

    @staticmethod
    def _ends_with_pause_punctuation(text, marker_map):
        stripped = text.rstrip()
        if not stripped:
            return False
        replacement = marker_map.get(stripped[-1])
        if replacement is not None:
            return bool(re.search(r"[.!?…:;]$", replacement.rstrip()))
        return bool(re.search(r"[.!?…:;]$", stripped))

    @classmethod
    def normalization_context(cls, config, glossary_data=None):
        """Создаёт лёгкий процессор для песочницы без кэша, сети и папок."""
        context = object.__new__(cls)
        context.cfg = normalize_config(config)
        context._normalizer = build_ru_normalizer(context.cfg)
        raw_separators = str(context.cfg.get("separator_symbols", ""))
        if "," in raw_separators and "\n" not in raw_separators:
            raw_separators = raw_separators.replace(",", "\n")
        context.separators = [
            item.strip() for item in raw_separators.split("\n") if item.strip()
        ]
        context.glossary_ignore_case = {}
        context.glossary_strict_case = {}
        context.glossary_regex = []
        context.glossary_verbatim_regex = []
        context.load_glossary_data(glossary_data or empty_glossary_data())
        return context

    def preview_normalization(self, raw_text):
        """Нормализует предпросмотр теми же стадиями, что и синтез.

        В результат не входят паузы и аудиогруппировка, но подготовка текста,
        пользовательские RegEx, sentenize, глоссарий, ru-normalizr, фильтрация
        неподдерживаемых чанков и финальная очистка вызываются теми же методами.
        """
        separator_token = "___NORMALIZER_PREVIEW_SEPARATOR_TOKEN___"
        prepared = self._prepare_raw_text(raw_text, separator_token)
        normalized_paragraphs = []
        file_paragraphs = []
        skipped = []
        sentence_count = 0
        for paragraph in (
            value.strip() for value in prepared.split("\n") if value.strip()
        ):
            if paragraph == separator_token:
                normalized_paragraphs.append("[ПАУЗА РАЗДЕЛИТЕЛЯ]")
                # Preview показывает понятную подпись, а сохраняемый TXT обязан
                # остаться пригодным для повторного синтеза. Любой настроенный
                # разделитель имеет ту же длительность, поэтому используем
                # первый как каноническое переносимое представление паузы.
                if self.separators:
                    file_paragraphs.append(self.separators[0])
                continue
            normalized_sentences = []
            for sentence in (item.text for item in sentenize(paragraph)):
                normalized = self.process_sentence_text(sentence)
                if not contains_synthesizable_text(normalized):
                    skipped.append(
                        {"source": sentence, "normalized": normalized}
                    )
                    continue
                normalized_sentences.append(normalized)
                sentence_count += 1
            if normalized_sentences:
                normalized_paragraph = " ".join(normalized_sentences)
                normalized_paragraphs.append(normalized_paragraph)
                file_paragraph = normalized_paragraph
                if (
                    paragraph_starts_with_speech(paragraph)
                    and not paragraph_starts_with_speech(file_paragraph)
                ):
                    # Тире не произносится, но после повторной загрузки снова
                    # включает pause_speech перед этим абзацем.
                    file_paragraph = f"— {file_paragraph}"
                file_paragraphs.append(file_paragraph)
        return {
            "source": str(raw_text or ""),
            "prepared": prepared,
            "normalized_text": "\n\n".join(normalized_paragraphs),
            "normalized_text_for_file": "\n\n".join(file_paragraphs),
            "sentence_count": sentence_count,
            "skipped": skipped,
        }

    def process_sentence_text(self, text):
        """Полный цикл обработки одного предложения (сохраняет чистый исходник отдельно)"""
        if _config_bool(
            self.cfg.get("text_is_prepared", False), default=False
        ):
            # Структурное тире уже повлияло на pause_speech и снимается перед
            # API. Остальной текст проходит буквально: сохраняются апострофы,
            # плюсы ударений, цифры и пунктуация внешнего редактора.
            prepared = strip_dialogue_prefix(str(text).strip()).strip()
            for char in prepared:
                codepoint = ord(char)
                if unicodedata.category(char) == "Cc" or (
                    0xE000 <= codepoint <= 0xF8FF
                    or 0xF0000 <= codepoint <= 0xFFFFD
                    or 0x100000 <= codepoint <= 0x10FFFD
                ):
                    raise ValueError(
                        "подготовленный текст содержит служебный или "
                        "управляющий символ"
                    )
            return prepared

        # Обычно это уже выполнено для всего абзаца, но повторяем защиту здесь:
        # функция также используется напрямую при тестировании и из старых
        # интеграций. Иначе ru-normalizr успеет принять ``- 62-й`` за минус.
        text = normalize_dialogue_line_starts(text)

        # 1. Запоминаем знак препинания в конце
        term_punct = ""
        m = re.search(r'([.!?…]+)["\'»”]*$', text.strip())
        if m: term_punct = m.group(1)

        # 2. Умная замена плюсов (защита ручных ударений)
        magic_token = "___PLUS_ACCENT_TOKEN___"
        # Плюсы, окруженные пробелами, или в начале строки с пробелом -> "плюс"
        text = re.sub(r'(^|\s)\+(?=\s)', r'\g<1>плюс ', text)
        # Плюсы между цифрами -> "плюс"
        text = re.sub(r'(?<=\d)\s*\+\s*(?=\d)', ' плюс ', text)
        # Маскируем плюсы ВНУТРИ слов (з+амок)
        text = re.sub(r'(?<=[а-яА-ЯёЁa-zA-Z])\+(?=[а-яА-ЯёЁa-zA-Z])', magic_token, text)
        # Маскируем плюсы В НАЧАЛЕ слов (+аура)
        text = re.sub(r'(?<![а-яА-ЯёЁa-zA-Z0-9])\+(?=[а-яА-ЯёЁa-zA-Z])', magic_token, text)
        # Остальные плюсы меняем на слово "плюс"
        text = text.replace("+", " плюс ")
        # Возвращаем замаскированные ударения обратно
        text = text.replace(magic_token, "+")

        # 3. Авто-аббревиатуры (И.И. -> И-И, к.п.д. -> к-п-д)
        if _config_bool(self.cfg.get("auto_abbreviations", True), default=True):
            def _repl_abbr(match):
                letters = [c for c in match.group(0) if c != '.']
                return "-".join(letters)
            text = re.sub(r'\b(?:[а-яА-Яa-zA-ZёЁ]\.){2,}', _repl_abbr, text)

        # 4. Авто-сокращения (г. -> г, ур. -> ур)
        if _config_bool(self.cfg.get("auto_short_words", True), default=True):
            text = re.sub(r'\b([а-яА-ЯёЁa-zA-Z]{1,3})\.', r'\1', text)

        # 5. Глоссарий терминов и ударений
        glossary_segments = self.apply_glossary_segments(text)

        # 6. Нормализация (числа в слова). Сначала ru-normalizr видит исходные
        # verbatim-термины ради контекста, после чего их точные замены занимают
        # по одному Private Use codepoint до финальной очистки. Если исходник
        # был изменён, автоматически используется защищённый резервный проход.
        text, verbatim_markers = self._normalize_glossary_segments(
            glossary_segments
        )

        # Защитный маркер ru-normalizr не должен попадать ни в API, ни в ключ
        # кэша. В нормальном пути он относится лишь к настоящему минусу:
        # дефис реплики перед ``62-й`` заменяется на тире ещё до нормализации.
        text = text.replace("\ue001", "минус ")

        # 7. Очистка спецсимволов (не трогаем +, так как он уже для ударений)
        text = re.sub(r'[*|\\/_\#~^()\[\]{}<>"\'«»„“”]', '', text)
        text = strip_dialogue_prefix(text)
        text = re.sub(r'\s+', ' ', text).strip()

        # 8. Восстановление или добавление финальной пунктуации
        # Если в конце нет знака препинания, который дает паузу (. ! ? … : ;)
        if text and not self._ends_with_pause_punctuation(
            text, verbatim_markers
        ):
            if term_punct:
                # Возвращаем оригинальную пунктуацию (очищенную от кавычек)
                clean_punct = re.sub(r'["\'»”]', '', term_punct)
                text += clean_punct if clean_punct else "."
            else:
                # Если пунктуации не было вообще (например, заголовок), ставим точку
                text += "."

        # Восстанавливаем в самом конце: ни ru-normalizr, ни очистка кавычек и
        # спецсимволов не могут изменить ``уб+о'го`` или другую точную замену.
        return self._restore_verbatim_markers(text, verbatim_markers)

    def get_hash(self, text):
        """Возвращает совместимый ключ кэша с опциональным namespace steps.

        При выключенном steps или снятом «учитывать в кэше» формула намеренно
        совпадает со всеми предыдущими версиями приложения байт-в-байт.
        """
        steps = resolve_api_steps(self.cfg)
        if steps is not None and _config_bool(self.cfg.get("cache_include_steps", True), default=True):
            namespaced_key = f"{text}_{self.cfg['speaker']}_steps={steps}"
            return hashlib.md5(namespaced_key.encode('utf-8')).hexdigest()
        return cache_content_hash(text, self.cfg["speaker"])

    @staticmethod
    def _steps_match_cache_entry(cache_info, steps):
        """Проверяет метаданные при намеренно общем legacy-ключе.

        Если пользователь отключил разделение ключей, старые записи без
        метаданных остаются пригодны — это выбранная совместимость. Известное
        несовпадение приводит к новому запросу и замене единственной общей
        записи. При раздельных ключах другой steps уже даёт другой MD5.
        """
        cached_steps = cache_info.get("steps")
        if steps is None:
            # Выключенный параметр означает прежнее серверное поведение. Любая
            # найденная по legacy-ключу запись с явными steps была создана в
            # режиме общего ключа, поэтому не выдаём её за серверный вариант.
            return cached_steps is None
        if cached_steps is None:
            return True
        try:
            return int(cached_steps) == steps
        except (TypeError, ValueError):
            return False

    def build_api_payload(self, normalized_text):
        """Создаёт payload; выключенный steps в нём физически отсутствует."""
        payload = {
            'api_token': self.cfg["api_token"], 'text': normalized_text,
            'sample_rate': 48000, 'speaker': self.cfg["speaker"],
            'remote_id': 'python_script', 'format': 'ogg'
        }
        steps = resolve_api_steps(self.cfg)
        if steps is not None:
            payload['steps'] = steps
        return payload

    def _get_silence_file(self, duration_ms):
        """Генерирует паузу в каноническом Ogg/Opus 48 kHz mono."""
        duration_ms = max(1, int(round(duration_ms)))
        silence_dir = self.cache_dir / "silences"
        silence_dir.mkdir(parents=True, exist_ok=True)
        filepath = silence_dir / f"silence_{duration_ms}ms.ogg"

        if not filepath.exists() or _detect_ogg_audio_codec(filepath) != CACHE_AUDIO_CODEC:
            # Тот же формат, в который приводится каждый новый ответ API.
            silence_seg = AudioSegment.silent(
                duration=duration_ms,
                frame_rate=CACHE_AUDIO_SAMPLE_RATE,
            ).set_channels(CACHE_AUDIO_CHANNELS)
            # Два одновременно работающих синтеза могут запросить одну и ту же
            # паузу. Каждый кодирует в собственный файл, а os.replace публикует
            # только полностью завершённый OGG.
            temp_path = silence_dir / (
                f".{filepath.stem}.{uuid.uuid4().hex}.tmp{filepath.suffix}"
            )
            exported = None
            try:
                exported = silence_seg.export(
                    temp_path,
                    format="ogg",
                    codec=CACHE_AUDIO_FFMPEG_CODEC,
                    bitrate=CACHE_AUDIO_BITRATE,
                    parameters=[
                        "-vbr", "on",
                        "-compression_level", "10",
                        "-application", "audio",
                    ],
                )
                if hasattr(exported, "flush"):
                    exported.flush()
                if hasattr(exported, "close"):
                    exported.close()
                    exported = None
                if not temp_path.exists():
                    raise OSError(
                        f"Кодировщик не создал временный файл {temp_path.name}"
                    )
                _require_opus_audio_file(temp_path)
                os.replace(temp_path, filepath)
            finally:
                if exported is not None and hasattr(exported, "close"):
                    try:
                        exported.close()
                    except OSError:
                        pass
                temp_path.unlink(missing_ok=True)

        return filepath

    def _run_ffmpeg_concat(self, audio_files):
        """Склеивает только канонические Opus-фрагменты внутреннего кэша."""
        SESSION_TEMP_DIR.mkdir(parents=True, exist_ok=True)

        list_path = None
        temp_out = SESSION_TEMP_DIR / f"out_{uuid.uuid4().hex}.ogg"

        try:
            audio_files = _require_opus_audio_files(audio_files)
            list_path = _create_ffmpeg_concat_manifest(audio_files)

            # Временный direct-результат использует тот же кодек, что и кэш.
            cmd = [
                get_ffmpeg_path(), "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                "-ar", str(CACHE_AUDIO_SAMPLE_RATE),
                "-ac", str(CACHE_AUDIO_CHANNELS),
                "-c:a", CACHE_AUDIO_FFMPEG_CODEC,
                "-b:a", CACHE_AUDIO_BITRATE,
                "-vbr", "on", "-compression_level", "10",
                "-application", "audio",
                str(temp_out),
            ]

            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo,
            )
            if result.returncode != 0 or not temp_out.exists():
                error_text = result.stderr.decode("utf-8", errors="ignore") if result.stderr else ""
                logging.error(f"Ошибка FFmpeg при временной склейке:\n{error_text}")
                if temp_out.exists():
                    temp_out.unlink(missing_ok=True)
                return None
            _require_opus_audio_file(temp_out)
            return temp_out
        except Exception:
            logging.exception("Не удалось выполнить временную склейку FFmpeg")
            if temp_out.exists():
                temp_out.unlink(missing_ok=True)
            return None
        finally:
            if list_path is not None:
                list_path.unlink(missing_ok=True)

    def synthesize_sentence(self, normalized_text, original_text, force_new=False):
        text_hash = self.get_hash(normalized_text)
        file_name = f"{text_hash}.ogg"
        cache_file = self.cache_audio_dir / file_name
        
        steps = resolve_api_steps(self.cfg)
        use_cache = _config_bool(self.cfg.get("use_cache", True), default=True)
        cache_hit_info = None
        with self.cache_lock:
            if (
                use_cache
                and not force_new
                and text_hash in self.cache
                and cache_file.exists()
                and self._steps_match_cache_entry(self.cache[text_hash], steps)
            ):
                cache_hit_info = self.cache[text_hash]

        if cache_hit_info is not None:
            try:
                audio_codec = _canonicalize_cached_audio_if_needed(
                    cache_file,
                    cache_hit_info.get("audio_codec"),
                    publish_lock=self.cache_lock,
                )
            except Exception as exc:
                # Повреждённый/неизвестный OGG не отдаём в общий concat: новый
                # запрос ниже попробует атомарно заменить проблемную запись.
                logging.error(
                    "Не удалось привести cache-hit %s к Ogg/Opus: %s. "
                    "Фрагмент будет запрошен заново.",
                    cache_file.name,
                    exc,
                )
            else:
                with self.cache_lock:
                    cache_info = self.cache.get(text_hash)
                    if cache_info is not None:
                        cache_info["last_accessed"] = time.time()
                        cache_info["usage_count"] = int(
                            cache_info.get("usage_count", 0)
                        ) + 1
                        if audio_codec and cache_info.get("audio_codec") != audio_codec:
                            cache_info["audio_codec"] = audio_codec
                # Статистика попаданий обновляется только в RAM. Не помечаем весь
                # индекс грязным: иначе _save_cache() в конце каждого входного
                # файла заново сериализует огромный JSON даже при 100% cache hit.
                # Если в запуске появились/удалились записи, свежая статистика
                # попадёт в ту же пакетную запись. При чистом cache-hit проходе
                # диск не трогаем на каждом файле — только в конце очереди/Stop.
                self.cache_metadata_dirty = True
                return cache_file, True

        payload = self.build_api_payload(normalized_text)
        steps = payload.get('steps')
        
        for attempt in range(1, int(self.cfg["max_retries"]) + 1):
            temp_ogg = None
            prepared_ogg = None
            audio_response_received = False
            if self.is_stopped:
                return None, False
            if not self.rate_limiter.wait(lambda: self.is_stopped):
                return None, False
            if self.is_stopped:
                return None, False
            try:
                r = self.session.post(self.cfg["api_url"], json=payload, timeout=30)
                r.raise_for_status()
                audio_data = base64.b64decode(r.json()['results'][0]['audio'])
                audio_response_received = True
                
                temp_ogg = self.cache_audio_dir / f"temp_{uuid.uuid4().hex}.ogg"
                prepared_ogg = self.cache_audio_dir / f"prepared_{uuid.uuid4().hex}.ogg"
                with open(temp_ogg, "wb") as file:
                    file.write(audio_data)

                _prepare_api_audio_file(
                    temp_ogg,
                    prepared_ogg,
                    trim_silence=_config_bool(
                        self.cfg.get("auto_trim_silence", True), default=True
                    ),
                    silence_threshold=float(self.cfg["silence_threshold"]),
                )
                _require_opus_audio_file(prepared_ogg)

                # Читатели видят либо старый полный файл, либо новый полный файл —
                # но никогда частично записанный OGG.
                if use_cache:
                    with self.cache_lock:
                        os.replace(prepared_ogg, cache_file)
                    result_file = cache_file
                else:
                    # При выключенном кэше результат всё ещё нужен сборщику,
                    # но он не должен попадать ни в индекс, ни под стабильное
                    # cache-key имя. Уникальный файл живёт лишь до системной
                    # очистки temp и безопасен для параллельных direct-запусков.
                    SESSION_TEMP_DIR.mkdir(parents=True, exist_ok=True)
                    result_file = SESSION_TEMP_DIR / f"synth_{uuid.uuid4().hex}.ogg"
                    os.replace(prepared_ogg, result_file)

                if temp_ogg and temp_ogg.exists():
                    temp_ogg.unlink(missing_ok=True)
                if prepared_ogg and prepared_ogg.exists():
                    prepared_ogg.unlink(missing_ok=True)
                
                should_save_cache = False
                if use_cache:
                    with self.cache_lock:
                        now = time.time()
                        self.cache[text_hash] = {
                            "file_name": file_name, "original_text": original_text,
                            "normalized_text": normalized_text, "speaker": self.cfg["speaker"],
                            "created_at": now, "last_accessed": now, "usage_count": 1,
                            "audio_codec": CACHE_AUDIO_CODEC,
                        }
                        if steps is not None:
                            self.cache[text_hash]["steps"] = steps
                            self.cache[text_hash]["steps_in_cache_key"] = _config_bool(
                                self.cfg.get("cache_include_steps", True), default=True
                            )
                        self.unsaved_cache_items += 1
                        should_save_cache = self.unsaved_cache_items >= int(
                            self.cfg["cache_save_frequency"]
                        )

                if should_save_cache:
                    self._save_cache()
                return result_file, True
                
            except requests.exceptions.HTTPError as e:
                if temp_ogg is not None:
                    temp_ogg.unlink(missing_ok=True)
                if prepared_ogg is not None:
                    prepared_ogg.unlink(missing_ok=True)
                if self.is_stopped:
                    return None, False
                if r.status_code == 422:
                    detail = ""
                    try:
                        response_data = r.json()
                        detail = (
                            response_data.get("detail", "")
                            if isinstance(response_data, dict)
                            else response_data
                        )
                        if not isinstance(detail, str):
                            detail = json.dumps(detail, ensure_ascii=False)
                        # FastAPI validation details can include rejected input.
                        # Never let the API token appear in the local log.
                        api_token = str(self.cfg.get("api_token", ""))
                        if api_token:
                            detail = detail.replace(api_token, "[REDACTED]")
                        detail = " ".join(detail.split())[:1000]
                        if "unknown api token" in detail.lower():
                            msg = "КРИТИЧЕСКАЯ ОШИБКА: Неверный API Token!"
                            logging.error(msg)
                            self.is_stopped = True
                            if self.error_callback: self.error_callback(msg)
                            return None, False # <-- ИСПРАВЛЕНО
                        elif "unknown speaker" in detail.lower():
                            short_detail = " ".join(detail.split()[:3]).strip(',')
                            msg = f"КРИТИЧЕСКАЯ ОШИБКА: {short_detail}"
                            logging.error(msg)
                            self.is_stopped = True
                            if self.error_callback: self.error_callback(msg)
                            return None, False # <-- ИСПРАВЛЕНО
                    except (TypeError, ValueError, KeyError) as detail_error:
                        logging.debug(
                            "Не удалось разобрать detail HTTP 422: %s",
                            detail_error,
                        )
                    normalized_preview = _log_text_preview(normalized_text)
                    source_preview = _log_text_preview(original_text)
                    logging.warning(
                        "HTTP 422: сервер отклонил фрагмент без повторной "
                        "попытки; detail=%r; source=%r; normalized=%r",
                        detail or "причина не указана",
                        source_preview,
                        normalized_preview,
                    )
                    return self._get_silence_file(
                        int(self.cfg["pause_sentence"])
                    ), False
                logging.warning(
                    "HTTP ошибка (попытка %d): %s; source=%r; normalized=%r",
                    attempt,
                    e,
                    _log_text_preview(original_text),
                    _log_text_preview(normalized_text),
                )
                if attempt < int(self.cfg["max_retries"]):
                    time.sleep(2)
                else: return self._get_silence_file(int(self.cfg["pause_sentence"])), False
            except Exception as e:
                if temp_ogg is not None:
                    temp_ogg.unlink(missing_ok=True)
                if prepared_ogg is not None:
                    prepared_ogg.unlink(missing_ok=True)
                if self.is_stopped:
                    return None, False
                if audio_response_received:
                    logging.exception(
                        "Ошибка локальной подготовки ответа API; повторный "
                        "сетевой запрос не выполняется; source=%r; "
                        "normalized=%r",
                        _log_text_preview(original_text),
                        _log_text_preview(normalized_text),
                    )
                    return self._get_silence_file(
                        int(self.cfg["pause_sentence"])
                    ), False
                logging.error(
                    "Ошибка сети/API (попытка %d): %s; source=%r; normalized=%r",
                    attempt,
                    e,
                    _log_text_preview(original_text),
                    _log_text_preview(normalized_text),
                )
                if attempt < int(self.cfg["max_retries"]):
                    time.sleep(2)
                else: return self._get_silence_file(int(self.cfg["pause_sentence"])), False

    def process_raw_text(
        self,
        raw_text,
        out_filename,
        force_new=False,
        save_to_disk=True,
        progress_callback=None,
        completion_callback=None,
        encoding_callback=None,
    ):
        separator_token = "___SEPARATOR_TOKEN___"
        raw_text = self._prepare_raw_text(raw_text, separator_token)
        
        paragraphs = [p.strip() for p in raw_text.split('\n') if p.strip()]
        
        tasks = []
        prev_ended_with_colon = False
        current_full_text_clean, current_full_text_raw = [], []

        def merge_pause_with_preceding_separator(pause_ms):
            """Не ставит вторую тишину на границе после разделителя."""
            if not tasks or tasks[-1][0] != "__SILENCE__":
                return pause_ms
            separator_pause = int(tasks[-1][1])
            tasks[-1] = (
                "__SILENCE__",
                max(separator_pause, int(pause_ms)),
                0,
            )
            return 0

        for para in paragraphs:
            if para == separator_token:
                if self.cfg["synthesis_mode"] == "full" and current_full_text_clean:
                    tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))
                    current_full_text_clean, current_full_text_raw = [], []
                tasks.append(("__SILENCE__", int(self.cfg["pause_separator"]), 0))
                prev_ended_with_colon = False
                continue

            pause_before = paragraph_boundary_pause(
                self.cfg,
                para,
                previous_ended_with_colon=prev_ended_with_colon,
            )

            sentences = [s.text for s in sentenize(para)]

            processed_sentences = []
            for sent_raw in sentences:
                sent_clean = self.process_sentence_text(sent_raw)
                # После нормализации самостоятельный фрагмент из одних
                # неподдерживаемых символов для API эквивалентен пустоте.
                # Смешанный текст не чистим: это сохраняет payload и ключ кэша.
                if not contains_synthesizable_text(sent_clean):
                    logging.info(
                        "Пропущен самостоятельный фрагмент без поддерживаемого "
                        "текста; source=%r; normalized=%r",
                        _log_text_preview(sent_raw),
                        _log_text_preview(sent_clean),
                    )
                    continue
                processed_sentences.append((sent_raw, sent_clean))

            if not processed_sentences: continue

            # Двоеточие относится только к последнему реально синтезируемому
            # абзацу. Закрывающие кавычки и скобки после него не меняют смысл.
            current_ended_with_colon = paragraph_ends_with_colon(para)

            if self.cfg["synthesis_mode"] == "sentence":
                for i, (s_raw, s_clean) in enumerate(processed_sentences):
                    pb = (
                        merge_pause_with_preceding_separator(pause_before)
                        if i == 0 and tasks
                        else (int(self.cfg["pause_sentence"]) if i > 0 else 0)
                    )
                    tasks.append((s_clean, s_raw, pb))
            elif self.cfg["synthesis_mode"] == "paragraph":
                para_raw = " ".join([p[0] for p in processed_sentences])
                para_clean = " ".join([p[1] for p in processed_sentences])
                pb = (
                    merge_pause_with_preceding_separator(pause_before)
                    if tasks
                    else 0
                )
                tasks.append((para_clean, para_raw, pb))
            elif self.cfg["synthesis_mode"] == "full":
                para_raw = " ".join([p[0] for p in processed_sentences])
                para_clean = " ".join([p[1] for p in processed_sentences])

                # Внутри одного full-блока паузы определяет сама модель по
                # переводам строк. Но если перед новым блоком уже есть явная
                # тишина разделителя, выбираем максимум с требованиями текущей
                # границы вместо двух пауз подряд.
                if not current_full_text_clean:
                    merge_pause_with_preceding_separator(pause_before)
                
                current_len = sum(len(t) for t in current_full_text_clean) + len(current_full_text_clean)
                if current_full_text_clean and (current_len + len(para_clean) > SAFE_LIMIT):
                    tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))
                    tasks.append(("__SILENCE__", pause_before, 0))
                    current_full_text_clean, current_full_text_raw = [], []
                    
                current_full_text_raw.append(para_raw)
                current_full_text_clean.append(para_clean)

            prev_ended_with_colon = current_ended_with_colon

        if self.cfg["synthesis_mode"] == "full" and current_full_text_clean:
            tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))

        audio_files = []
        if not tasks:
            # Пустой текст или строка только из пунктуации не являются сетевой
            # ошибкой. Не создаём из настроек пауз «успешный» пустой аудиофайл:
            # намеренную тишину по-прежнему можно получить строкой-разделителем.
            logging.warning(
                "Синтез %s пропущен: после обработки не осталось текста или "
                "распознанных разделителей.",
                out_filename,
            )
            if save_to_disk:
                # «Нет текста» не требует resume и не должно оставлять
                # processing_statuses.json как будто произошёл сбой API.
                self._mark_output_status(Path(self.cfg["output_dir"]) / out_filename, "empty")
            if completion_callback:
                completion_callback(out_filename, "empty", None)
            return

        if int(self.cfg["pause_file_start"]) > 0:
            f = self._get_silence_file(int(self.cfg["pause_file_start"]))
            if f: audio_files.append(f)

        file_has_errors = False
        speech_task_count = 0
        successful_speech_count = 0
        total_tasks = len(tasks)

        for i, task in enumerate(tasks):
            if self.is_stopped:
                self._save_cache()
                if save_to_disk:
                    self._mark_output_status(Path(self.cfg["output_dir"]) / out_filename, "error")
                if completion_callback: completion_callback(out_filename, "error", None)
                return

            clean_text, raw_text_or_duration, pause_before = task
            
            if pause_before > 0:
                f = self._get_silence_file(pause_before)
                if f: audio_files.append(f)

            if clean_text == "__SILENCE__":
                f = self._get_silence_file(raw_text_or_duration)
                if f: audio_files.append(f)
                if progress_callback: progress_callback(i + 1, total_tasks, "[ПАУЗА РАЗДЕЛИТЕЛЯ]")
            else:
                if progress_callback: progress_callback(i + 1, total_tasks, clean_text)
                speech_task_count += 1
                filepath, success = self.synthesize_sentence(clean_text, raw_text_or_duration, force_new)
                if filepath:
                    audio_files.append(filepath)
                if success:
                    successful_speech_count += 1
                else:
                    file_has_errors = True

        # При частичном сбое тишина вместо одной фразы сохраняет порядок и
        # длительность остальных фрагментов. Но файл, в котором не удалось
        # получить вообще ни одного речевого фрагмента, вводит пользователя в
        # заблуждение — не собираем результат целиком из аварийных заглушек.
        # Чистая тишина из явных строк-разделителей имеет speech_task_count == 0
        # и поэтому остаётся поддерживаемым осознанным сценарием.
        if speech_task_count > 0 and successful_speech_count == 0:
            logging.error(
                "Синтез %s не выполнен: ни один из %d речевых фрагментов не "
                "был получен успешно; файл из одной тишины не создан.",
                out_filename,
                speech_task_count,
            )
            if save_to_disk:
                self._mark_output_status(
                    Path(self.cfg["output_dir"]) / out_filename, "error"
                )
            if completion_callback:
                completion_callback(out_filename, "error", None)
            return

        if int(self.cfg["pause_file_end"]) > 0:
            f = self._get_silence_file(int(self.cfg["pause_file_end"]))
            if f: audio_files.append(f)

        self._save_cache()

        if not audio_files:
            if save_to_disk:
                self._mark_output_status(Path(self.cfg["output_dir"]) / out_filename, "error")
            if completion_callback: completion_callback(out_filename, "error", None)
            return

        if save_to_disk:
            out_filepath = Path(self.cfg["output_dir"]) / out_filename
            if encoding_callback:
                encoding_callback(out_filename)
            t = threading.Thread(
                target=self._merge_save_and_notify,
                args=(
                    tuple(audio_files),
                    out_filepath,
                    out_filename,
                    file_has_errors,
                    completion_callback,
                ),
            )
            self.active_threads.append(t)
            try:
                t.start()
            except Exception:
                self.active_threads.remove(t)
                logging.exception(
                    "Не удалось запустить сборку аудиофайла %s", out_filename
                )
                self._mark_output_status(out_filepath, "error")
                if completion_callback:
                    completion_callback(out_filename, "error", None)
        else:
            temp_out = self._run_ffmpeg_concat(audio_files)
            if completion_callback:
                if temp_out:
                    completion_callback(out_filename, "warning" if file_has_errors else "success", str(temp_out))
                else:
                    completion_callback(out_filename, "error", None)

    def _merge_save_and_notify(self, audio_files, out_filepath, original_filename, has_errors, callback):
        def _encode():
            list_path = None
            res = None
            temp_out = None
            try:
                audio_files_verified = _require_opus_audio_files(audio_files)
                list_path = _create_ffmpeg_concat_manifest(audio_files_verified)

                cmd = [
                    get_ffmpeg_path(), "-y", "-f", "concat", "-safe", "0",
                    "-i", str(list_path),
                ]
                
                out_filepath.parent.mkdir(parents=True, exist_ok=True)
                temp_out = out_filepath.with_name(
                    f".{out_filepath.stem}.{uuid.uuid4().hex}.tmp{out_filepath.suffix}"
                )

                fmt = self.cfg["output_format"].lower()
                output_profile = _select_book_audio_profile(
                    fmt,
                    sample_rate=self.cfg.get("output_sample_rate", "auto"),
                    channels=self.cfg.get("output_channels", "auto"),
                    bitrate=self.cfg.get("output_bitrate", "128k"),
                )
                has_cover = False
                xiph_cover = None
                apply_tags = _config_bool(
                    self.cfg.get("apply_output_tags", True), default=True
                )
                cover_path = self.cfg.get("tag_cover", "") if apply_tags else ""
                if cover_path and os.path.exists(cover_path):
                    if fmt == "mp3":
                        cmd.extend(["-i", cover_path])
                        has_cover = True
                    elif fmt == "opus":
                        xiph_cover = _xiph_cover_metadata(
                            cover_path, out_filepath.name
                        )
                    elif fmt == "ogg":
                        logging.warning(
                            "Обложка пропущена для OGG %s: Ogg/Vorbis не "
                            "поддерживает JPEG/PNG attached_pic этим способом.",
                            out_filepath.name,
                        )

                # concat demuxer и файл обложки могут нести собственные теги.
                # Не переносим их неявно: ниже записываются только выбранные
                # пользователем метаданные (либо ни одного тега для direct).
                cmd.extend(["-map_metadata", "-1"])
    
                sp = float(self.cfg.get("fx_speed", 1.0))
                pt = float(self.cfg.get("fx_pitch", 1.0))
                ec = _config_bool(self.cfg.get("fx_echo", False))
                ed = int(self.cfg.get("fx_echo_delay", 300))
                ey = float(self.cfg.get("fx_echo_decay", 0.3))
    
                filters = []
                if pt != 1.0:
                    filters.append(f"asetrate={int(48000 * pt)}")
                    filters.extend(AudioEffects._atempo_filters(1 / pt))
                if sp != 1.0:
                    filters.extend(AudioEffects._atempo_filters(sp))
                if ec:
                    filters.append(f"aecho=0.8:0.8:{int(ed)}:{float(ey)}")
    
                if filters:
                    cmd.extend(["-af", ",".join(filters)])
    
                if fmt == "mp3":
                    cmd.extend([
                        "-c:a", "libmp3lame", "-b:a", output_profile["bitrate"]
                    ])
                    if has_cover:
                        # Для Windows пишем обложку как JPEG в ID3v2.3/APIC:
                        # PNG формально допустим, но Проводник/Media Player
                        # отображают его непоследовательно.
                        cmd.extend([
                            "-map", "0:a:0", "-map", "1:v:0",
                            "-c:v", "mjpeg",
                            "-id3v2_version", "3",
                            "-disposition:v:0", "attached_pic",
                            "-metadata:s:v", "title=Album cover",
                            "-metadata:s:v", "comment=Cover (front)",
                        ])
                elif fmt == "ogg":
                    cmd.extend(["-c:a", "libvorbis"])
                    if output_profile["bitrate"]:
                        cmd.extend(["-b:a", output_profile["bitrate"]])
                elif fmt == "opus":
                    cmd.extend([
                        "-c:a", "libopus", "-b:a", output_profile["bitrate"]
                    ])
                elif fmt == "wav":
                    # RIFF хранит размер чанков в 32 битах. Для длинных книг
                    # FFmpeg должен сам переключиться на RF64, иначе запись
                    # финального заголовка заканчивается ошибкой упаковки
                    # ``'L' format requires 0 <= number <= 4294967295``.
                    cmd.extend(["-c:a", "pcm_s16le", "-rf64", "auto"])

                cmd.extend([
                    "-ar", str(output_profile["sample_rate"]),
                    "-ac", str(output_profile["channels"]),
                ])
    
                if apply_tags:
                    base_name = out_filepath.stem

                    def _apply_tag_template(tmpl_key):
                        val = str(self.cfg.get(tmpl_key, ""))
                        if not val:
                            return ""
                        val = val.replace("{filename}", base_name)
                        val = val.replace("{name}", base_name)
                        val = val.replace("{title}", base_name)
                        return val.strip()

                    title = _apply_tag_template("tag_title")
                    if title:
                        cmd.extend(["-metadata", f"title={title}"])

                    artist = _apply_tag_template("tag_artist")
                    if artist:
                        cmd.extend(["-metadata", f"artist={artist}"])

                    album_artist = _apply_tag_template("tag_album_artist")
                    if album_artist:
                        cmd.extend(["-metadata", f"album_artist={album_artist}"])

                    album = _apply_tag_template("tag_album")
                    if album:
                        cmd.extend(["-metadata", f"album={album}"])

                    genre = _apply_tag_template("tag_genre")
                    if genre:
                        cmd.extend(["-metadata", f"genre={genre}"])

                    composer = _apply_tag_template("tag_composer")
                    if composer:
                        cmd.extend(["-metadata", f"composer={composer}"])

                    year = _apply_tag_template("tag_year")
                    if year:
                        cmd.extend(["-metadata", f"date={year}"])

                    if xiph_cover:
                        cmd.extend([
                            "-metadata",
                            f"METADATA_BLOCK_PICTURE={xiph_cover}",
                        ])
    
                cmd.append(str(temp_out))

                # Сохраняем финальную команду для тестов/диагностики. Это не
                # влияет на subprocess и не содержит аудиоданных.
                self._last_ffmpeg_save_command = tuple(cmd)
    
                startupinfo = None
                if platform.system() == "Windows":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
            except Exception:
                logging.exception(f"Ошибка запуска FFmpeg при сохранении {out_filepath.name}")
            finally:
                # Удаляем только манифест этой операции и строго после FFmpeg.
                # Чужие concat-файлы и общий каталог здесь не затрагиваются.
                if list_path is not None:
                    list_path.unlink(missing_ok=True)

            # --- ЗАЩИТА 1: Если во время сборки нажали «Принудительно» — СТИРАЕМ БИТЫЙ ФАЙЛ ---
            if self.is_stopped:
                if temp_out is not None:
                    temp_out.unlink(missing_ok=True)
                self._mark_output_status(out_filepath, "error")
                if callback:
                    callback(original_filename, "error", None)
                return
            # -----------------------------------------------------------------------------------

            # --- ЗАЩИТА 2: Если FFmpeg вылетел с ошибкой — СТИРАЕМ ПОЛУГОТОВЫЙ ФАЙЛ ---
            encode_succeeded = (
                res is not None
                and res.returncode == 0
                and temp_out is not None
                and temp_out.exists()
            )
            if not encode_succeeded:
                err_log = (
                    res.stderr.decode('utf-8', errors='ignore')
                    if res is not None and res.stderr
                    else "Не удалось запустить FFmpeg или получить выходной файл"
                )
                logging.error(f"Ошибка FFmpeg при сохранении {out_filepath.name}:\n{err_log}")
                if temp_out is not None:
                    temp_out.unlink(missing_ok=True)
                self._mark_output_status(out_filepath, "error")
                if callback:
                    callback(original_filename, "error", None)
                return
            # ---------------------------------------------------------------------------

            try:
                os.replace(temp_out, out_filepath)
            except OSError as exc:
                logging.error("Не удалось атомарно сохранить %s: %s", out_filepath, exc)
                temp_out.unlink(missing_ok=True)
                self._mark_output_status(out_filepath, "error")
                if callback:
                    callback(original_filename, "error", None)
                return

            # ОПТИМИЗАЦИЯ 2: Безопасное обновление статуса ТОЛЬКО В ПАМЯТИ
            has_errors_local = has_errors
            status_str = "warning" if has_errors_local else "success"
            
            self._mark_output_status(out_filepath, status_str)
                
            if not has_errors_local:
                logging.info(f"Файл {out_filepath.name} сохранен с эффектами (Скорость: {sp}x, Тон: {pt}).")
            if callback: 
                callback(original_filename, status_str, str(out_filepath.resolve()))
    
        if self.encode_semaphore:
            with self.encode_semaphore:
                _encode()
        else:
            _encode()

    def get_all_possible_hashes(self, raw_text, include_prepared=False):
        """Собирает канонические хэши содержимого для всех режимов синтеза.

        Оптимизатор кэша передаёт ``include_prepared=True``: один и тот же TXT
        пользователь мог синтезировать как исходный или как уже подготовленный.
        Поэтому при очистке нужно сохранить объединение обоих наборов. Обычный
        dry-run остаётся привязан к режиму текущего запуска.
        """
        source_text = raw_text
        hashes = set()
        speaker = self.cfg["speaker"]

        def add_hash(text):
            hashes.add(cache_content_hash(text, speaker))

        # Синтез и оптимизация обязаны видеть один и тот же текст. В частности,
        # строки ``---`` и ``–––`` защищаются до нормализации начальных тире.
        separator_token = "___SEPARATOR_TOKEN___"
        raw_text = self._prepare_raw_text(raw_text, separator_token)
        
        paragraphs = [p.strip() for p in raw_text.split('\n') if p.strip()]
        current_full_text_clean = []

        for para in paragraphs:
            if para == separator_token:
                if current_full_text_clean:
                    full_clean = "\n".join(current_full_text_clean)
                    add_hash(full_clean)
                    current_full_text_clean = []
                continue

            sentences = [s.text for s in sentenize(para)]
            processed_sentences = []
            
            for sent_raw in sentences:
                sent_clean = self.process_sentence_text(sent_raw)
                if not contains_synthesizable_text(sent_clean):
                    continue
                processed_sentences.append(sent_clean)

            if not processed_sentences: 
                continue

            # 1. Хэши предложений (SENTENCE)
            for s_clean in processed_sentences:
                add_hash(s_clean)
                
            # 2. Хэш параграфа (PARAGRAPH)
            para_clean = " ".join(processed_sentences)
            add_hash(para_clean)
            
            # 3. Накопление для режима FULL с защитой (30 000)
            current_len = sum(len(t) for t in current_full_text_clean) + len(current_full_text_clean)
            if current_full_text_clean and (current_len + len(para_clean) > SAFE_LIMIT):
                full_clean = "\n".join(current_full_text_clean)
                add_hash(full_clean)
                current_full_text_clean = []
                
            current_full_text_clean.append(para_clean)

        # Хэш последнего блока FULL
        if current_full_text_clean:
            full_clean = "\n".join(current_full_text_clean)
            add_hash(full_clean)

        if include_prepared and not _config_bool(
            self.cfg.get("text_is_prepared", False), default=False
        ):
            prepared_config = copy.deepcopy(self.cfg)
            prepared_config["text_is_prepared"] = True
            prepared_context = self.normalization_context(
                prepared_config, empty_glossary_data()
            )
            hashes.update(prepared_context.get_all_possible_hashes(source_text))

        return hashes
    
    def process_text_file(
        self,
        filepath,
        dry_run=False,
        progress_callback=None,
        completion_callback=None,
        encoding_callback=None,
    ):
        filepath = Path(filepath)
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: raw_text = f.read()
        except Exception as e:
            logging.error(f"Не удалось прочитать файл {filepath.name}: {e}")
            if completion_callback: completion_callback(filepath.name, "error", None)
            return
            
        if dry_run:
            # Dry-run не должен менять рабочую конфигурацию. Возвращаем
            # рассчитанные идентификаторы содержимого, чтобы вызывающий код
            # мог показать предварительную статистику без сетевых запросов.
            return self.get_all_possible_hashes(raw_text)

        out_filename = filepath.with_suffix(f'.{self.cfg["output_format"]}').name
        self.process_raw_text(
            raw_text,
            out_filename,
            False,
            True,
            progress_callback,
            lambda fname, status, audio: completion_callback(filepath.name, status) if completion_callback else None,
            lambda _fname: encoding_callback(filepath.name) if encoding_callback else None,
        )

        

# ================= ЭКСТРАКТОР И НАРЕЗЧИК КНИГ =================
class BookExtractor:
    """Утилита для извлечения текста из различных форматов книг (EPUB, FB2, DOCX)."""
    
    @staticmethod
    def _clean_html_text(soup, block_tags):
        """
        Умная очистка HTML: сохраняет абзацы, но игнорирует переносы строк 
        внутри inline-тегов (em, strong, b, i) и форматирования кода.
        """
        # 1. Принудительные переносы заменяем на маркер блока
        for tag in soup.find_all(['br', 'hr']):
            tag.replace_with('\n___BLOCK___\n')
            
        # 2. Ставим маркер блока ПОСЛЕ каждого блочного тега
        for tag in soup.find_all(block_tags):
            tag.insert_after('\n___BLOCK___\n')
            
        # 3. Извлекаем весь текст, заменяя ВСЕ внутренние переносы на пробелы
        raw_text = soup.get_text(separator=' ')
        
        # 4. Разбиваем текст по нашим маркерам и очищаем от лишних пробелов
        blocks = []
        for block in raw_text.split('___BLOCK___'):
            clean = re.sub(r'\s+', ' ', block).strip()
            if clean:
                blocks.append(clean)
                
        # 5. Склеиваем чистые абзацы
        return "\n".join(blocks)

    @staticmethod
    def extract_epub(filepath):
        book = epub.read_epub(str(filepath))
        chapters = []
        author = ""
        
        try:
            creators = book.get_metadata('DC', 'creator')
            if creators: author = creators[0][0]
        except (AttributeError, IndexError, TypeError) as exc:
            logging.debug("Не удалось прочитать автора EPUB: %s", exc)

        # Блочные теги для EPUB
        epub_blocks = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']

        def parse_toc(toc_list):
            items = []
            for item in toc_list:
                if isinstance(item, epub.Link):
                    items.append(item)
                elif isinstance(item, (tuple, list)):
                    for sub in item:
                        if isinstance(sub, (list, tuple)): items.extend(parse_toc(sub))
                        elif isinstance(sub, epub.Link): items.append(sub)
            return items

        links = parse_toc(book.toc)
        if links:
            for link in links:
                href = link.href.split('#')[0]
                for item in book.get_items():
                    if item.file_name == href:
                        html_content = item.get_content().decode('utf-8', errors='ignore')
                        soup = BeautifulSoup(html_content, 'html.parser')
                        text = BookExtractor._clean_html_text(soup, epub_blocks)
                        if text: chapters.append((link.title, text))
                        break
        else:
            for item_id in book.spine:
                item = book.get_item_with_id(item_id[0])
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    html_content = item.get_content().decode('utf-8', errors='ignore')
                    soup = BeautifulSoup(html_content, 'html.parser')
                    text = BookExtractor._clean_html_text(soup, epub_blocks)
                    if text: chapters.append(("Глава", text))
                    
        return chapters, author

    @staticmethod
    def extract_fb2(filepath):
        # Передаём parser байты: XML-декларация FB2 сама задаёт кодировку.
        # Принудительный UTF-8 ломал легальные старые книги в windows-1251.
        with open(filepath, 'rb') as f:
            soup = BeautifulSoup(f, 'xml')
            
        chapters = []
        author = ""
        
        try:
            a_tag = soup.find('author')
            if a_tag:
                fn = a_tag.find('first-name')
                ln = a_tag.find('last-name')
                fn_str = fn.get_text(strip=True) if fn else ""
                ln_str = ln.get_text(strip=True) if ln else ""
                author = f"{fn_str} {ln_str}".strip()
        except (AttributeError, TypeError) as exc:
            logging.debug("Не удалось прочитать автора FB2: %s", exc)

        # Блочные теги для FB2
        fb2_blocks = ['p', 'v', 'subtitle', 'text-author', 'title', 'empty-line']

        body = soup.find('body')
        if not body: 
            return [("Книга", BookExtractor._clean_html_text(soup, fb2_blocks))], author
        
        sections = body.find_all('section', recursive=False)
        if not sections: sections = body.find_all('section')
        if not sections: 
            return [("Книга", BookExtractor._clean_html_text(body, fb2_blocks))], author

        for sec in sections:
            title_tag = sec.find('title')
            title = title_tag.get_text(strip=True) if title_tag else "Глава"
            text = BookExtractor._clean_html_text(sec, fb2_blocks)
            if text: chapters.append((title, text))
            
        return chapters, author

    @staticmethod
    def extract_docx(filepath):
        doc = docx.Document(filepath)
        chapters = []
        current_title = "Вступление"
        current_text = []
        
        # DOCX не подвержен проблеме тегов, так как python-docx выдает чистый текст параграфа
        for p in doc.paragraphs:
            if p.style.name.startswith('Heading'):
                if current_text:
                    chapters.append((current_title, "\n".join(current_text)))
                    current_text = []
                current_title = p.text.strip() or "Глава"
                current_text.append(current_title)
            else:
                if p.text.strip(): current_text.append(p.text.strip())
                
        if current_text:
            chapters.append((current_title, "\n".join(current_text)))
            
        return chapters

    @staticmethod
    def split_txt_by_regex(filepath, pattern):
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            text = f.read()
            
        if not pattern:
            return [("Текст", text)]
            
        matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
        if not matches:
            return [("Текст", text)]
            
        chapters = []
        if matches[0].start() > 0:
            intro = text[:matches[0].start()].strip('\n')
            if intro.strip(): chapters.append(("Вступление", intro))
            
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            content = text[start:end].strip('\n')
            title = match.group(0).strip()
            if content.strip(): chapters.append((title, content))
            
        return chapters

    @staticmethod
    def save_chapters(chapters, out_dir, orig_filename, template, author=""):
        total = len(chapters)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        
        saved_files = []
        name_no_ext = Path(orig_filename).stem
        
        match_start = re.search(r'\{num:(\d+)\}', template)
        start_index = int(match_start.group(1)) if match_start else 1

        occupied_names = {
            path.name.casefold()
            for path in out_dir.iterdir()
            if path.is_file()
        }

        for idx, (title, content) in enumerate(chapters, 0):
            safe_title = sanitize_filename_component(
                title, fallback="Глава", max_length=50
            )
            current_num = format_sequence_number(
                start_index + idx, total, start_index=start_index
            )
            
            filename = template
            filename = re.sub(r'\{num(?::\d+)?\}', current_num, filename)
            filename = filename.replace("{name}", name_no_ext)
            filename = filename.replace("{book}", name_no_ext)
            filename = filename.replace("{title}", safe_title)
            filename = filename.replace("{author}", author if author else "Автор")
            
            filename = sanitize_filename_component(
                Path(filename).stem if filename.lower().endswith(".txt") else filename,
                fallback=f"Глава {current_num}",
                max_length=180,
            )
            base_filename = filename
            filename += ".txt"
            duplicate_index = 2
            while filename.casefold() in occupied_names:
                suffix = f" ({duplicate_index})"
                trimmed = base_filename[: max(1, 180 - len(suffix))].rstrip(". ")
                filename = f"{trimmed}{suffix}.txt"
                duplicate_index += 1
            occupied_names.add(filename.casefold())
            
            out_path = out_dir / filename
            _write_text_atomic(out_path, content)
            saved_files.append(filename)
            
        return saved_files
# ==============================================================
    
# ================= ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (GUI) =================
class TTSApp:
    """Главный класс графического интерфейса приложения."""
    STATUS_COLORS_LIGHT = {
        "info": "#003366",
        "success": "#14532D",
        "warning": "#7C2D12",
        "error": "#7F1D1D",
        "text": "#000000",
        "muted": "#525252",
    }
    STATUS_COLORS_DARK = {
        "info": "#7DD3FC",
        "success": "#86EFAC",
        "warning": "#FDBA74",
        "error": "#FCA5A5",
        "text": "#FFFFFF",
        "muted": "#D1D5DB",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Silero TTS Studio")

        # Фоновые потоки не должны обращаться к Tcl/Tk напрямую. Они складывают
        # вызовы сюда, а главный поток регулярно исполняет их из mainloop.
        self._ui_queue = queue.SimpleQueue()
        self.root.after(25, self._drain_ui_queue)
        
        self.settings_vars = {}
        self.config = self.load_settings()

        # Старые версии сохраняли вычисляемый путь глоссария в settings.json.
        # Источник истины — cache_dir; удаляем устаревший ключ, чтобы он не
        # выглядел как импортированный путь и не переезжал в новые конфиги.
        self.config.pop("glossary_path", None)

        # Общий лимитер создаём до построения вкладок: setup/load_files вызывают
        # save_settings(), и теперь им не нужна проверка частично созданного app.
        self.shared_rate_limiter = RateLimiter(
            self.config.get("api_max_requests", 15),
            self.config.get("api_time_window", 15.0),
        )

        # Динамический размер окна (70% от экрана по центру)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        w = int(screen_width * 0.7)
        h = int(screen_height * 0.7)
        x = int((screen_width - w) / 2)
        y = int((screen_height - h) / 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        # Не позволяем вручную сжать окно до состояния, в котором нижние
        # действия экспортной вкладки и кнопки тегов оказываются за границей.
        # На маленьком экране предел автоматически уменьшается до доступного.
        self.root.minsize(
            max(360, min(900, screen_width - 40)),
            max(320, min(520, screen_height - 80)),
        )
        
        self.processor = None
        self.batch_processor = None
        self.direct_processor = None
        self.processing_thread = None
        self.direct_thread = None
        self.last_direct_audio = None
        self.last_direct_audio_has_effects = False
        self._batch_hard_stop_requested = False
        self._direct_hard_stop_requested = False
        # Все операции, способные читать/менять индекс кэша, координируются
        # единым состоянием. Отдельные bool сохранены как совместимые запросы
        # через методы is_cache_*(), но источником истины является эта строка.
        self._cache_operation = None
        self._cache_optimization_running = False
        self._cache_archive_running = False
        self._cache_transcode_cancel = None
        self._cache_ui_loading = False
        self._cache_ui_loaded = False
        self._cache_state_generation = 0
        self._cache_ui_request_id = 0
        self._import_running = False
        self._import_thread = None
        # Не повторяем одно и то же предупреждение для Steps при каждом файле.
        # При смене значения ключ меняется и предупреждение снова показывается.
        self._confirmed_steps_warnings = set()
        self._is_closing = False
        self._shared_cache_dir = None
        self._shared_cache = None
        self._shared_processing_statuses = None
        self._shared_cache_lock = threading.RLock()
        self._cache_ui_filter_after_id = None
        self._settings_save_after_id = None
        self._is_dark_appearance = False
        self._appearance_check_after_id = None
        self._status_label_kinds = {}
        self._palette_widget_kinds = {}

        self._export_lock = False
        self._export_running = False
        self._export_import_thread = None
        self._export_thread = None
        
        # Переменная шрифта теперь глобальная, но без нижнего ползунка
        self.font_size_var = tk.IntVar(value=self.config.get("ui_font_size", 10))
        
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        self.tab_main = ttk.Frame(self.notebook)
        self.tab_direct = ttk.Frame(self.notebook)
        self.tab_import = ttk.Frame(self.notebook)
        self.tab_utils = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_glossary = ttk.Frame(self.notebook)
        self.tab_normalizer = ttk.Frame(self.notebook)
        self.tab_cache = ttk.Frame(self.notebook)
        self.tab_help = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_main, text="Синтез из папки")
        self.notebook.add(self.tab_direct, text="Прямой синтез")
        self.notebook.add(self.tab_import, text="Импорт книг")
        self.notebook.add(self.tab_utils, text="Экспорт и Сборка")
        self.notebook.add(self.tab_settings, text="Настройки")
        self.notebook.add(self.tab_glossary, text="Глоссарий")
        self.notebook.add(self.tab_normalizer, text="Нормализатор")
        self.notebook.add(self.tab_cache, text="Кэш")
        self.notebook.add(self.tab_help, text="Справка")
        
        self.setup_main_tab()
        self.setup_direct_tab()
        self.setup_import_tab()
        self.setup_utils_tab()
        self.setup_settings_tab()
        self.setup_glossary_tab()
        self.setup_normalizer_tab()
        self.setup_cache_tab()
        self.setup_help_tab()
        
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_change)
        # Мягкая страховка после закрытия native messagebox: если Aqua не
        # вернул локальный Tk-фокус, первый обычный клик назначает его именно
        # на нажатый виджет. focus_force не используется и потому приложение
        # не отбирает фокус у другого окна без действия пользователя.
        self.root.bind_all(
            "<ButtonPress-1>", self._restore_focus_on_app_click, add="+"
        )
        self.load_files()
        self.update_fonts()

        self.apply_theme()

        if sys.platform == "darwin":
            # Tk 9 reports the effective Aqua appearance directly. Tk 8.6 does
            # not expose that API, so the RGB fallback below keeps compatibility.
            self._appearance_check_after_id = self.root.after(1000, self._check_system_appearance)
        
        # Перехват закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        if sys.platform == "darwin":
            self._setup_mac_hotkeys()
            self._setup_mac_startup_focus()
        else:
            # Фикс буфера обмена для кириллической раскладки на Windows/Linux
            self._fix_cyrillic_clipboard()

        self.root.after(300, self._silent_pre_warm_tabs)


    def _post_to_ui(self, callback, *args, **kwargs):
        """Потокобезопасно ставит вызов Tkinter в очередь главного потока."""
        if getattr(self, "_is_closing", False):
            return
        self._ui_queue.put((callback, args, kwargs))

    def _restore_focus_on_app_click(self, event):
        """Подтверждает локальный фокус после клика и обновляет Aqua controls."""
        widget = getattr(event, "widget", None)
        if widget is None or getattr(self, "_is_closing", False):
            return
        try:
            if widget.winfo_toplevel() is not self.root:
                return
        except (AttributeError, tk.TclError):
            return

        def restore_after_click():
            if getattr(self, "_is_closing", False):
                return
            try:
                if not self.root.winfo_exists() or self.root.state() == "iconic":
                    return
                # К моменту idle штатные bindings Entry/Button/Treeview уже
                # выбрали правильный дочерний виджет. Повторный focus_set на
                # нём активирует локальный Tk-контекст, но не заменяет выбор
                # пользователя. Для клика по пустому Frame сохраняется старое
                # поле; если фокуса действительно нет, используем сам виджет.
                target = self.root.focus_get()
                try:
                    if target is not None and target.winfo_toplevel() is not self.root:
                        target = None
                except (AttributeError, tk.TclError):
                    target = None
                if target is None:
                    target = widget
                try:
                    if not target.winfo_exists():
                        target = self.root
                except (AttributeError, tk.TclError):
                    target = self.root
                target.focus_set()

                # Нативные Progressbar/Scale Aqua иногда продолжают выглядеть
                # неактивными, хотя клик уже вернул NSWindow приложению. Тот же
                # debounced redraw используется после Map/Activate и не меняет
                # выбранную тему или значения виджетов.
                if (
                    sys.platform == "darwin"
                    and not getattr(self, "_mac_window_active", False)
                ):
                    self._schedule_mac_restore_refresh()
            except tk.TclError:
                pass

        try:
            self.root.after_idle(restore_after_click)
        except tk.TclError:
            pass

    def _run_messagebox(self, dialog_function, title, message, **options):
        """Показывает системный диалог с владельцем и возвращает Tk-фокус.

        На macOS ``messagebox`` без ``parent`` создаёт нативный alert, который
        не всегда возвращает keyboard focus главному NSWindow после закрытия.
        Явный владелец делает alert оконно-модальным, а отложенный ``focus_set``
        восстанавливает именно тот Tk-виджет, в котором пользователь работал.
        ``focus_force`` намеренно не используется: он мог бы перехватить фокус,
        если пользователь переключился в другое приложение.
        """
        try:
            previous_focus = self.root.focus_get()
        except tk.TclError:
            previous_focus = None

        options.setdefault("parent", self.root)
        try:
            return dialog_function(title, message, **options)
        finally:
            self._schedule_focus_after_messagebox(previous_focus)

    def _schedule_focus_after_messagebox(self, previous_focus=None):
        """Возвращает локальный фокус после полного закрытия native dialog."""
        if getattr(self, "_is_closing", False):
            return

        def restore_focus():
            if getattr(self, "_is_closing", False):
                return
            try:
                if not self.root.winfo_exists():
                    return
                target = previous_focus
                if target is None or not target.winfo_exists():
                    target = self.root
                target.focus_set()
            except (AttributeError, tk.TclError):
                try:
                    self.root.focus_set()
                except tk.TclError:
                    pass

        try:
            self.root.after_idle(restore_focus)
        except tk.TclError:
            pass

    def _show_info(self, title, message, **options):
        return self._run_messagebox(messagebox.showinfo, title, message, **options)

    def _show_warning(self, title, message, **options):
        return self._run_messagebox(messagebox.showwarning, title, message, **options)

    def _show_error(self, title, message, **options):
        return self._run_messagebox(messagebox.showerror, title, message, **options)

    def _ask_yes_no(self, title, message, **options):
        return self._run_messagebox(messagebox.askyesno, title, message, **options)

    def _ask_yes_no_cancel(self, title, message, **options):
        return self._run_messagebox(
            messagebox.askyesnocancel, title, message, **options
        )

    def _create_synthesis_processor(self, config):
        """Создаёт процессор и делит RAM-кэш между одновременно активными вкладками."""
        cache_dir = Path(config["cache_dir"]).expanduser().resolve()
        active_processors = tuple(
            processor
            for processor in (self.batch_processor, self.direct_processor)
            if processor is not None
        )
        if active_processors and any(processor.cache_dir.resolve() != cache_dir for processor in active_processors):
            raise RuntimeError("Нельзя одновременно синтезировать с разными папками кэша")
        shared_cache = self._shared_cache if self._shared_cache_dir == cache_dir else None
        processor = TTSProcessor(
            config,
            shared_rate_limiter=self.shared_rate_limiter,
            error_callback=self.show_critical_error,
            shared_cache=shared_cache,
            shared_cache_lock=self._shared_cache_lock,
            shared_processing_statuses=(
                self._shared_processing_statuses
                if self._shared_cache_dir == cache_dir
                else None
            ),
        )
        # Первый процессор загружает индекс штатным _load_cache(); следующие
        # получают уже тот же объект dict.
        self._shared_cache_dir = cache_dir
        self._shared_cache = processor.cache
        self._shared_processing_statuses = processor.processing_statuses_ram
        return processor

    def _release_shared_cache_if_idle(self):
        """Не держит устаревшую RAM-копию после завершения обоих синтезов."""
        if self.batch_processor is None and self.direct_processor is None:
            self._shared_cache_dir = None
            self._shared_cache = None
            self._shared_processing_statuses = None

    def _drain_ui_queue(self):
        """Исполняет UI-вызовы порциями, не монополизируя Tk mainloop."""
        processed = 0
        deadline = time.monotonic() + 0.008
        queue_was_limited = False

        while processed < 200 and time.monotonic() < deadline:
            try:
                callback, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break

            try:
                callback(*args, **kwargs)
            except Exception:
                logging.exception("Ошибка отложенного UI-вызова")
            processed += 1

        if processed >= 200 or time.monotonic() >= deadline:
            queue_was_limited = True

        try:
            if self.root.winfo_exists():
                # Если producer быстрее UI, отдаём Tk возможность обработать
                # клики/перерисовку и продолжаем почти сразу следующей порцией.
                self.root.after(1 if queue_was_limited else 25, self._drain_ui_queue)
        except tk.TclError:
            pass

    def _silent_pre_warm_tabs(self):
        """Тихо завершает геометрию вкладок без чтения их данных с диска."""
        tabs_list = list(self.notebook.tabs())
        
        def _step():
            if not tabs_list:
                return
            tab_id = tabs_list.pop(0)
            try:
                widget = self.notebook.nametowidget(tab_id)
                # В частности, вкладка кэша остаётся ленивой: здесь нет вызова
                # load_cache_ui() и нет построения строк Treeview.
                widget.update_idletasks()
            except tk.TclError as exc:
                logging.debug("Не удалось прогреть вкладку %s: %s", tab_id, exc)
            if tabs_list:
                self.root.after(15, _step)

        self.root.after(100, _step)

    def _setup_mac_startup_focus(self):
        """Один раз поднимает главное окно после его первого появления на macOS."""
        if sys.platform != "darwin":
            return

        self._mac_startup_focus_done = False
        self._mac_window_active = False
        self._mac_startup_focus_after_id = None
        self._mac_startup_map_bind_id = self.root.bind("<Map>", self._on_mac_initial_map, add="+")
        # Повторный Map корневого toplevel после сворачивания не должен снова
        # перехватывать фокус, но Aqua иногда оставляет themed controls в
        # неактивном сером состоянии до следующей активации приложения.
        self._mac_restore_map_bind_id = self.root.bind(
            "<Map>", self._on_mac_restore_map, add="+"
        )
        # На Aqua активность окна и фокус — разные состояния. После возврата
        # свёрнутого приложения Tk может уже иметь state == normal, но нативные
        # Scale/Progressbar всё ещё нарисованы как элементы неактивного окна.
        # <Activate> приходит именно при возврате NSWindow в активное состояние
        # и потому дополняет (но не заменяет) <Map>.
        self._mac_restore_activate_bind_id = self.root.bind(
            "<Activate>", self._on_mac_restore_activate, add="+"
        )
        self._mac_restore_deactivate_bind_id = self.root.bind(
            "<Deactivate>", self._on_mac_restore_deactivate, add="+"
        )
        # update_idletasks() во время построения большого интерфейса иногда успевает
        # отобразить root до установки bind. after_idle служит одноразовой страховкой.
        self.root.after_idle(self._schedule_mac_startup_focus)

    def _on_mac_initial_map(self, event):
        """Игнорирует Map дочерних окон и повторные Map после сворачивания."""
        if event.widget is not self.root or self._mac_startup_focus_done:
            return
        self._schedule_mac_startup_focus()

    def _on_mac_restore_map(self, event):
        """Перерисовывает Aqua после восстановления, не отбирая фокус."""
        if event.widget is not self.root or not self._mac_startup_focus_done:
            return
        self._schedule_mac_restore_refresh()

    def _on_mac_restore_activate(self, event):
        """Повторяет redraw, когда восстановленное Aqua-окно стало активным."""
        if event.widget is not self.root:
            return
        self._mac_window_active = True
        if not self._mac_startup_focus_done:
            return
        self._schedule_mac_restore_refresh()

    def _on_mac_restore_deactivate(self, event):
        """Запоминает потерю native-активности без принудительного фокуса."""
        if event.widget is self.root:
            self._mac_window_active = False

    def _schedule_mac_restore_refresh(self):
        """Объединяет близкие Map/Activate в одну отложенную перерисовку."""
        after_id = getattr(self, "_mac_restore_refresh_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._mac_restore_refresh_after_id = self.root.after(
            25, self._refresh_mac_after_restore
        )

    def _invalidate_mac_aqua_widgets(self):
        """Просит все ttk-потомки перечитать нативное состояние Aqua."""
        # ttk::ThemeChanged — штатный механизм Tk, который рассылает
        # <<ThemeChanged>> всем потомкам. Некоторые версии Aqua не
        # инвалидируют нативный drawing state Scale/Progressbar только от
        # виртуального события, поэтому дополнительно переустанавливаем уже
        # выбранную тему: это не меняет оформление или значения виджетов, но
        # заставляет theme engine пересоздать системные controls.
        theme_reloaded = False
        try:
            style = ttk.Style(self.root)
            current_theme = style.theme_use()
            if current_theme:
                style.theme_use(current_theme)
                theme_reloaded = True
        except tk.TclError:
            pass
        if not theme_reloaded:
            try:
                self.root.tk.call("ttk::ThemeChanged")
            except tk.TclError:
                try:
                    self.root.event_generate("<<ThemeChanged>>", when="tail")
                except tk.TclError:
                    pass
        try:
            self.root.event_generate("<Expose>", when="tail")
            self.root.after_idle(self.root.update_idletasks)
        except tk.TclError:
            pass

    def _refresh_mac_after_restore(self):
        """Даёт Aqua один idle-цикл и инвалидирует отрисовку всех потомков."""
        self._mac_restore_refresh_after_id = None
        try:
            if not self.root.winfo_exists() or self.root.state() != "normal":
                return
            # На Tk 9 explicit auto заставляет NSWindow повторно применить
            # текущий appearance. На Tk 8.6 опция отсутствует и безопасно
            # пропускается, после чего Expose обновляет обычные виджеты.
            try:
                self.root.tk.call(
                    "wm", "attributes", self.root._w, "-appearance", "auto"
                )
            except tk.TclError:
                pass
            self._invalidate_mac_aqua_widgets()
            self._check_system_appearance(reschedule=False)
        except tk.TclError:
            pass

    def _schedule_mac_startup_focus(self):
        """Планирует единственную активацию после завершения отрисовки окна."""
        if self._mac_startup_focus_done:
            return
        if self._mac_startup_focus_after_id is None:
            self._mac_startup_focus_after_id = self.root.after(200, self._run_mac_startup_focus)

    def _run_mac_startup_focus(self):
        self._mac_startup_focus_after_id = None
        if self._mac_startup_focus_done:
            return
        self._mac_startup_focus_done = True
        bind_id = getattr(self, "_mac_startup_map_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<Map>", bind_id)
            except tk.TclError:
                pass
            self._mac_startup_map_bind_id = None
        self._force_mac_focus()
        self._schedule_mac_restore_refresh()

    def _force_mac_focus(self, *args):
        """Активирует приложение и поднимает окно; вызывается только при старте."""
        if sys.platform != "darwin":
            return

        # Finder-сборке иногда нужна явная активация процесса. Не вызываем
        # objc_msgSend через ctypes: на разных ABI неверная сигнатура такого
        # вызова приводит к нативному SIGSEGV, который Python поймать не может.
        # Системный `open` выполняет активацию вне процесса Tk.
        if is_frozen_mac:
            try:
                subprocess.Popen(
                    ["/usr/bin/open", "-a", str(Path(sys.executable).resolve().parents[2])],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, IndexError) as exc:
                logging.debug(f"Ошибка стартовой активации .app: {exc}")

        try:
            # Одноразово поднимаем окно над Terminal/Finder, затем снимаем
            # topmost без повторного focus_force и без вложенного root.update().
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.after(120, self._release_mac_startup_topmost)
        except tk.TclError as e:
            logging.debug(f"Не удалось поднять стартовое окно Tk: {e}")

    def _release_mac_startup_topmost(self):
        """Снимает только временный topmost, не вмешиваясь в текущий фокус."""
        try:
            if self.root.winfo_exists():
                self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _paste_clipboard_once(self, event):
        """Вставляет буфер один раз для physical и виртуального Paste event."""
        widget = getattr(event, "widget", None)
        if not isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            try:
                widget = self.root.focus_get()
            except tk.TclError:
                widget = None
        if not isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            return "break"

        try:
            clipboard_text = self.root.clipboard_get()
        except Exception:
            clipboard_text = ""
        if not clipboard_text and sys.platform == "darwin":
            try:
                clipboard_text = subprocess.check_output(
                    ["/usr/bin/pbpaste"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                clipboard_text = ""
        if not clipboard_text:
            return "break"

        # Tk сопоставляет Ctrl/Command+V с <<Paste>>, а Studio дополнительно
        # ловит физическую клавишу ради кириллической раскладки. Оба callback
        # могут прийти в одном event-loop turn. Guard живёт ровно до idle и
        # не мешает следующему самостоятельному нажатию.
        if getattr(self, "_clipboard_paste_widget", None) is widget:
            return "break"
        self._clipboard_paste_widget = widget

        def clear_paste_guard():
            if getattr(self, "_clipboard_paste_widget", None) is widget:
                self._clipboard_paste_widget = None

        try:
            self.root.after_idle(clear_paste_guard)
        except tk.TclError:
            clear_paste_guard()

        clipboard_text = normalize_clipboard_text(clipboard_text)
        try:
            if isinstance(widget, tk.Text):
                if widget.tag_ranges(tk.SEL):
                    widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                widget.insert(tk.INSERT, clipboard_text)
            else:
                try:
                    if widget.selection_present():
                        widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                except (AttributeError, tk.TclError):
                    pass
                widget.insert(tk.INSERT, clipboard_text)
        except (AttributeError, tk.TclError):
            pass
        return "break"
        
    def _fix_cyrillic_clipboard(self):
        """Универсальный и надежный обработчик горячих клавиш без дублей (Win/Linux, RU/EN)"""
    
        def get_target_widget(event):
            w = event.widget
            if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
                w = self.root.focus_get()
            return w if isinstance(w, (tk.Text, tk.Entry, ttk.Entry)) else None
    
        def smart_copy(event):
            w = get_target_widget(event)
            if not w:
                return "break"
    
            text_to_copy = ""
            try:
                if isinstance(w, tk.Text) and w.tag_ranges(tk.SEL):
                    text_to_copy = w.get(tk.SEL_FIRST, tk.SEL_LAST)
                elif isinstance(w, (tk.Entry, ttk.Entry)):
                    if hasattr(w, 'selection_present') and w.selection_present():
                        text_to_copy = w.selection_get()
                    else:
                        sel = w.selection_range()
                        if sel:
                            text_to_copy = w.get()[sel[0]:sel[1]]
            except Exception:
                pass
    
            if text_to_copy:
                self.root.clipboard_clear()
                self.root.clipboard_append(text_to_copy)
            return "break"
    
        def smart_cut(event):
            w = get_target_widget(event)
            if not w:
                return "break"
    
            text_to_copy = ""
            try:
                if isinstance(w, tk.Text) and w.tag_ranges(tk.SEL):
                    text_to_copy = w.get(tk.SEL_FIRST, tk.SEL_LAST)
                    w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                elif isinstance(w, (tk.Entry, ttk.Entry)):
                    if hasattr(w, 'selection_present') and w.selection_present():
                        text_to_copy = w.selection_get()
                        w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    else:
                        sel = w.selection_range()
                        if sel:
                            start, end = sel
                            text_to_copy = w.get()[start:end]
                            w.delete(start, end)
            except Exception:
                pass
    
            if text_to_copy:
                self.root.clipboard_clear()
                self.root.clipboard_append(text_to_copy)
            return "break"
    
        def smart_paste(event):
            return self._paste_clipboard_once(event)

        def smart_select_all(event):
            w = get_target_widget(event)
            if not w:
                return "break"
            try:
                if isinstance(w, tk.Text):
                    w.tag_add(tk.SEL, "1.0", "end-1c")
                    w.mark_set(tk.INSERT, "1.0")
                elif isinstance(w, (tk.Entry, ttk.Entry)):
                    w.selection_range(0, tk.END)
                    w.icursor(tk.END)
            except Exception:
                pass
            return "break"
    
        def smart_undo_redo(event):
            w = get_target_widget(event)
            if not w:
                return "break"
            if isinstance(w, tk.Text):
                try:
                    if event.state & 0x0001:  # Shift
                        w.edit_redo()
                    else:
                        w.edit_undo()
                except Exception:
                    pass
            return "break"
    
        def handle_global_shortcuts(event):
            # Проверяем зажатый Ctrl (маска 0x0004)
            if not (event.state & 0x0004):
                return None
    
            kc = event.keycode
            char = str(event.char)
    
            # Keycodes (Win: 67, 86, 88, 65, 90 / Linux: 54, 55, 53, 38, 52)
            # + Страховка через ASCII символы управления (\x03, \x16, \x18, \x01, \x1a)
            is_c = kc in (67, 54) or char in ('\x03', 'c', 'с')
            is_v = kc in (86, 55) or char in ('\x16', 'v', 'м')
            is_x = kc in (88, 53) or char in ('\x18', 'x', 'ч')
            is_a = kc in (65, 38) or char in ('\x01', 'a', 'ф')
            is_z = kc in (90, 52) or char in ('\x1a', 'z', 'я')
    
            if is_c:
                return smart_copy(event)
            elif is_v:
                return smart_paste(event)
            elif is_x:
                return smart_cut(event)
            elif is_a:
                return smart_select_all(event)
            elif is_z:
                return smart_undo_redo(event)
    
            return None
    
        # 1. Физические и виртуальное событие ведут в один обработчик.
        # Это сохраняет кириллические keycode-fallbacks и исключает двойную
        # вставку, когда Tk также генерирует стандартный <<Paste>>.
        for w_class in ('Text', 'Entry', 'TEntry'):
            self.root.bind_class(w_class, '<<Paste>>', smart_paste)
            self.root.bind_class(w_class, '<Control-v>', smart_paste)
            self.root.bind_class(w_class, '<Control-V>', smart_paste)
            self.root.bind_class(w_class, '<Control-c>', smart_copy)
            self.root.bind_class(w_class, '<Control-C>', smart_copy)
            self.root.bind_class(w_class, '<Control-x>', smart_cut)
            self.root.bind_class(w_class, '<Control-X>', smart_cut)
            self.root.bind_class(w_class, '<Control-a>', smart_select_all)
            self.root.bind_class(w_class, '<Control-A>', smart_select_all)
            self.root.bind_class(w_class, '<Control-z>', smart_undo_redo)
            self.root.bind_class(w_class, '<Control-Z>', smart_undo_redo)
    
        # 2. Глобальный перехватчик для русской раскладки и нетипичных клавиатур
        self.root.bind("<KeyPress>", handle_global_shortcuts, add="+")

    def get_status_color(self, status="info"):
        """Возвращает цвет статуса для текущего светлого/тёмного оформления."""
        palette = self.STATUS_COLORS_DARK if self._is_dark_appearance else self.STATUS_COLORS_LIGHT
        return palette.get(status, palette["text"])

    def _register_palette_widget(self, widget, status="text"):
        """Запоминает семантический цвет статической подписи для смены темы."""
        self._palette_widget_kinds[widget] = status
        return widget

    def _set_status_label(self, label, text, status="info"):
        """Задаёт текст и семантический цвет, чтобы пережить смену темы."""
        self._status_label_kinds[label] = status
        label.config(text=text, foreground=self.get_status_color(status))

    def _post_status_label(self, label, text, status="info"):
        """Потокобезопасный вариант _set_status_label()."""
        self._post_to_ui(self._set_status_label, label, text, status)

    def _render_export_activity_status(self):
        """Подгоняет рабочий статус под текущую ширину окна и шрифт."""
        activity = getattr(self, "_export_status_activity", None)
        label = getattr(self, "lbl_export_status", None)
        if not activity or label is None:
            return
        action, subject, status = activity
        prefix = f"{normalize_display_text(action, fallback='Обработка')}: "
        text = format_export_activity_status(action, subject)
        try:
            frame_width = int(label.master.winfo_width())
            if frame_width > 1:
                max_text_width = min(
                    max(80, int(frame_width * 0.58)),
                    max(20, frame_width - 20),
                )
                font_name = str(label.cget("font") or "TkDefaultFont")
                try:
                    label_font = tkfont.nametofont(font_name, root=self.root)
                except tk.TclError:
                    label_font = tkfont.nametofont(
                        "TkDefaultFont", root=self.root
                    )
                subject_width = max(
                    8, max_text_width - label_font.measure(prefix)
                )
                fitted_subject = middle_ellipsize_to_width(
                    subject,
                    subject_width,
                    label_font.measure,
                )
                text = prefix + fitted_subject
        except (AttributeError, RuntimeError, tk.TclError, TypeError, ValueError):
            # До первого показа окна или во время его закрытия достаточно
            # консервативного ограничения по символам.
            pass
        self._set_status_label(label, text, status)

    def _set_export_activity_status(self, action, subject, status="warning"):
        """Показывает имя без изменения title, тегов или выходного пути."""
        self._export_status_activity = (action, subject, status)
        self._render_export_activity_status()

    def _post_export_activity_status(self, action, subject, status="warning"):
        """Потокобезопасно публикует рабочий статус экспорта."""
        self._post_to_ui(
            self._set_export_activity_status, action, subject, status
        )

    def _set_export_status(self, text, status="info"):
        """Устанавливает обычный статус и отменяет адаптивное имя операции."""
        self._export_status_activity = None
        self._set_status_label(self.lbl_export_status, text, status)

    def _post_export_status(self, text, status="info"):
        """Потокобезопасный вариант _set_export_status()."""
        self._post_to_ui(self._set_export_status, text, status)

    def _detect_dark_appearance(self):
        """Безопасно определяет системную тёмную тему, не вызывая AppKit."""
        if sys.platform != "darwin":
            return False

        # Новые Aqua-сборки Tk возвращают эффективную тему окна напрямую.
        # На старом Tk 8.6 атрибут отсутствует и даёт обычный TclError.
        try:
            return bool(int(self.root.tk.call("wm", "attributes", self.root._w, "-isdark")))
        except (tk.TclError, TypeError, ValueError):
            pass

        # Fallback для Tk 8.6/9: системный семантический цвет разрешается под
        # appearance именно этого окна. Внешний процесс и AppKit не нужны.
        for color_name in (
            "systemWindowBackgroundColor",
            self.root.cget("background"),
        ):
            try:
                red, green, blue = self.root.winfo_rgb(color_name)
                # Простая яркость здесь нужна лишь для двоичной классификации,
                # а не для расчёта WCAG-контраста самих статусных цветов.
                brightness = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535
                return brightness < 0.5
            except (tk.TclError, TypeError, ValueError):
                continue
        return False

    def _check_system_appearance(self, *, reschedule=True):
        """Обновляет статусные цвета при смене темы macOS на лету."""
        if reschedule:
            self._appearance_check_after_id = None
        if self._is_closing:
            return

        is_dark = self._detect_dark_appearance()
        if is_dark != self._is_dark_appearance:
            self._is_dark_appearance = is_dark
            self._refresh_status_colors()
            # Tk 9 обычно обновляет native controls сам, но принудительная
            # рассылка нужна для уже открытых/недавно восстановленных окон.
            self._invalidate_mac_aqua_widgets()

        if reschedule:
            try:
                self._appearance_check_after_id = self.root.after(
                    1500, self._check_system_appearance
                )
            except tk.TclError:
                pass
        
    def reset_global_fx(self):
        """Сброс эффектов во вкладке Настройки к дефолтным значениям"""
        if "fx_speed" in self.settings_vars: self.settings_vars["fx_speed"].set(1.0)
        if "fx_pitch" in self.settings_vars: self.settings_vars["fx_pitch"].set(1.0)
        if "fx_echo" in self.settings_vars: self.settings_vars["fx_echo"].set(False)
        if "fx_echo_delay" in self.settings_vars: self.settings_vars["fx_echo_delay"].set(300)
        if "fx_echo_decay" in self.settings_vars: self.settings_vars["fx_echo_decay"].set(0.3)
    
        if hasattr(self, 'lbl_speed_val'): self.lbl_speed_val.config(text="1.0x")
        if hasattr(self, 'lbl_pitch_val'): self.lbl_pitch_val.config(text="1.00")
        if hasattr(self, 'lbl_delay_val'): self.lbl_delay_val.config(text="300мс")
        if hasattr(self, 'lbl_decay_val'): self.lbl_decay_val.config(text="0.3")
        
        self.save_settings()
        self._show_info("Успех", "Глобальные эффекты сброшены по умолчанию!")

    def _natural_sort_key(self, text):
        """Ключ для сортировки файлов человеком, а не машиной (Глава 2 будет перед Глава 10)"""
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(text))]

    def _sort_export_tree(self, col, reverse):
        """Натуральная сортировка дерева экспорта (Глава 2 будет перед Глава 10)"""
        if getattr(self, "_export_lock", False) or getattr(self, "_export_running", False):
            return
        # 1. Сортируем корневые элементы (группы или файлы без групп)
        roots = list(self.export_tree.get_children(""))
        roots.sort(key=lambda x: self._natural_sort_key(self.export_tree.item(x, "text")), reverse=reverse)
        for idx, item in enumerate(roots):
            self.export_tree.move(item, "", idx)
            
        # 2. Сортируем файлы внутри каждой группы
        for g_id in self.export_groups:
            children = list(self.export_tree.get_children(g_id))
            children.sort(key=lambda x: self._natural_sort_key(self.export_tree.item(x, "text")), reverse=reverse)
            for idx, item in enumerate(children):
                self.export_tree.move(item, g_id, idx)
                
        # Меняем направление для следующего клика
        self.export_tree.heading(col, command=lambda: self._sort_export_tree(col, not reverse))

    def ungroup_export_items(self):
        """Удаляет группу, но оставляет файлы (переносит их в корень)"""
        if getattr(self, "_export_lock", False) or getattr(self, "_export_running", False):
            return
        selected = self.export_tree.selection()
        groups = [i for i in selected if i in self.export_groups]
        
        if not groups:
            self._show_info("Внимание", "Выделите группу(ы) для разгруппировки.")
            return
            
        for g_id in groups:
            parent = self.export_tree.parent(g_id)
            # Переносим всех детей на уровень выше (или в корень)
            for child in self.export_tree.get_children(g_id):
                self.export_tree.move(child, parent, tk.END)
            # Удаляем саму группу
            del self.export_groups[g_id]
            self.export_tree.delete(g_id)
        self.current_selected_export_item = None
        self.export_tree.selection_remove(*self.export_tree.selection())
        self._disable_export_settings()
        self.update_total_export_duration()
    
    def _export_tree_interaction_blocked(self, tree):
        """Не даёт macOS-обходам Treeview менять замороженный экспорт."""
        return (
            tree is getattr(self, "export_tree", None)
            and (
                getattr(self, "_export_lock", False)
                or getattr(self, "_export_running", False)
            )
        )

    def _mac_multiselect(self, event, tree):
        """Атомарное выделение с гарантированной зачисткой призраков на macOS"""
        if self._export_tree_interaction_blocked(tree):
            return "break"
        try:
            tree.focus_set()
        except Exception:
            pass

        item = tree.identify_row(event.y)
        if item:
            current_sel = set(tree.selection())
            if item in current_sel:
                current_sel.remove(item)
            else:
                current_sel.add(item)
            
            # Атомарно задаем весь новый массив выделения и принудительно перерисовываем
            tree.selection_set(tuple(current_sel))
            tree.focus(item)
            tree.event_generate("<<TreeviewSelect>>")
            tree.update_idletasks()
        return "break"

    def _bind_mac_treeview_clicks(self, tree):
        """Единый macOS-патч для ttk.Treeview: фокус + fallback выбора строки."""
        if sys.platform != "darwin":
            return

        tree.bind("<ButtonPress-1>", lambda e, t=tree: self._mac_tree_button_press(e, t), add="+")
        tree.bind("<Command-ButtonPress-1>", lambda e, t=tree: self._mac_multiselect(e, t))

    def _mac_tree_button_press(self, event, tree):
        if self._export_tree_interaction_blocked(tree):
            return "break"
        try:
            tree.focus_set()
        except Exception:
            pass

        item = tree.identify_row(event.y)
        region = tree.identify_region(event.x, event.y)
        if item and region in ("tree", "cell") and not self._mac_event_has_selection_modifier(event):
            tree.after_idle(lambda t=tree, i=item: self._mac_ensure_tree_plain_click(t, i))

    def _mac_event_has_selection_modifier(self, event):
        state = getattr(event, "state", 0)
        return bool(state & (0x0001 | 0x0004 | 0x0008 | 0x0010))

    def _mac_ensure_tree_plain_click(self, tree, item):
        if self._export_tree_interaction_blocked(tree):
            return
        try:
            if not tree.winfo_exists() or not tree.exists(item):
                return
            if tuple(tree.selection()) != (item,) or tree.focus() != item:
                tree.selection_set(item)
                tree.focus(item)
                tree.event_generate("<<TreeviewSelect>>")
            else:
                tree.focus(item)
        except tk.TclError:
            pass

    def _setup_mac_hotkeys(self):
        """Обработка горячих клавиш macOS и безопасный патч первого клика"""
        # Универсальные хоткеи для работы с буфером обмена
        for widget_cls in ("Text", "Entry", "TEntry"):
            self.root.bind_class(
                widget_cls, "<<Paste>>", self._paste_clipboard_once
            )
            self.root.bind_class(widget_cls, "<Command-Key>", self._dispatch_mac_cmd)


    def _dispatch_mac_cmd(self, event):
        """Нативная обработка горячих клавиш macOS и путей Finder."""
        w = event.widget
        kc = event.keycode
        char = str(event.char).lower()

        if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
            w = self.root.focus_get()
            if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
                return

        # --- ВСТАВКА (⌘V) С АВТО-ДЕКОДИРОВАНИЕМ КИРИЛЛИЦЫ И ПУТЕЙ FINDER ---
        if kc == 9 or char in ('v', 'м', '\x16'):
            return self._paste_clipboard_once(event)

        # --- КОПИРОВАНИЕ (⌘C) ---
        elif kc == 8 or char in ('c', 'с', '\x03'):
            try:
                text_to_copy = ""
                if isinstance(w, tk.Text) and w.tag_ranges(tk.SEL):
                    text_to_copy = w.get(tk.SEL_FIRST, tk.SEL_LAST)
                elif isinstance(w, (ttk.Entry, tk.Entry)) and w.selection_present():
                    text_to_copy = w.selection_get()

                if text_to_copy:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text_to_copy)
                    try:
                        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
                        p.communicate(input=text_to_copy)
                    except (OSError, subprocess.SubprocessError) as exc:
                        logging.debug("pbcopy недоступен: %s", exc)
            except (tk.TclError, AttributeError) as exc:
                logging.debug("Ошибка копирования в буфер macOS: %s", exc)
            return "break"

        # --- ВЫРЕЗАНИЕ (⌘X) ---
        elif kc == 7 or char in ('x', 'ч', '\x18'):
            try:
                text_to_copy = ""
                if isinstance(w, tk.Text) and w.tag_ranges(tk.SEL):
                    text_to_copy = w.get(tk.SEL_FIRST, tk.SEL_LAST)
                    w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                elif isinstance(w, (ttk.Entry, tk.Entry)) and w.selection_present():
                    text_to_copy = w.selection_get()
                    w.delete(tk.SEL_FIRST, tk.SEL_LAST)

                if text_to_copy:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text_to_copy)
                    try:
                        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
                        p.communicate(input=text_to_copy)
                    except (OSError, subprocess.SubprocessError) as exc:
                        logging.debug("pbcopy недоступен: %s", exc)
            except (tk.TclError, AttributeError) as exc:
                logging.debug("Ошибка вырезания в буфер macOS: %s", exc)
            return "break"

        # --- ВЫДЕЛИТЬ ВСЁ (⌘A) ---
        elif kc == 0 or char in ('a', 'ф', '\x01'):
            try:
                if isinstance(w, tk.Text):
                    w.tag_add(tk.SEL, "1.0", "end-1c")
                    w.mark_set(tk.INSERT, "1.0")
                    w.focus_set()
                elif isinstance(w, (ttk.Entry, tk.Entry)):
                    w.select_range(0, tk.END)
                    w.icursor(tk.END)
                    w.focus_set()
            except (tk.TclError, AttributeError) as exc:
                logging.debug("Ошибка выделения текста на macOS: %s", exc)
            return "break"

        # --- ОТМЕНА И ПОВТОР (⌘Z / ⌘Shift+Z) ---
        elif kc == 6 or char in ('z', 'я', '\x1a'):
            try:
                if isinstance(w, tk.Text):
                    if event.state & 1:
                        w.edit_redo()
                    else:
                        w.edit_undo()
            except Exception as e:
                logging.debug(f"Ошибка Undo/Redo: {e}")
            return "break"

    def _get_smart_dir(self, current_path, is_file=False):
        """Возвращает единый безопасный начальный путь файлового диалога."""
        return resolve_dialog_initial_dir(current_path, file_path=is_file)

    @staticmethod
    def _write_json_atomic(path, data, *, backup=False):
        """Записывает JSON атомарно и не оставляет обрезанный рабочий файл."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_temp_path = backup_path.with_name(
            f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=4, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            if backup and path.exists():
                # Повреждённый primary не должен вытеснить последнюю рабочую
                # резервную копию. Это особенно важно после запуска, который
                # уже восстановил настройки из .bak.
                preserve_current = True
                try:
                    with open(path, "r", encoding="utf-8-sig") as current_file:
                        current_data = json.load(current_file)
                    if isinstance(data, dict) and not isinstance(current_data, dict):
                        raise ValueError("корень текущего JSON не является объектом")
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                    preserve_current = False
                    logging.warning(
                        "Повреждённый JSON %s не записан поверх резервной копии: %s",
                        path,
                        exc,
                    )
                if preserve_current:
                    # Сначала готовим резервную копию во временном имени. Если
                    # копирование прервалось, существующий .bak не повреждается.
                    shutil.copy2(path, backup_temp_path)
                    os.replace(backup_temp_path, backup_path)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
            backup_temp_path.unlink(missing_ok=True)

    def _config_dialog_initial_dir(self):
        return resolve_dialog_initial_dir(
            self.config.get("last_config_dir", ""), BASE_DIR
        )

    def _glossary_dialog_initial_dir(self):
        return resolve_dialog_initial_dir(
            self.config.get("last_glossary_dir", ""), BASE_DIR
        )

    def _remember_dialog_directory(self, key, selected_path):
        """Запоминает UI-историю отдельно от переносимых групп профиля."""
        if not selected_path:
            return
        selected = Path(selected_path).expanduser()
        directory = selected if selected.is_dir() else selected.parent
        self.config[key] = str(directory)
        self.save_settings()

    @staticmethod
    def _set_descendant_state(widget, state, *, skip=()):
        """Рекурсивно меняет состояние интерактивных ttk/tk-виджетов."""
        skip_ids = {id(item) for item in skip}
        for child in widget.winfo_children():
            if id(child) in skip_ids:
                continue
            try:
                child.configure(state=state)
            except (tk.TclError, TypeError):
                pass
            TTSApp._set_descendant_state(child, state, skip=skip)
    
    def _center_popup(self, dialog, width, height, fit_screen=False):
        """Центрирует диалог; опционально вписывает его в доступный экран."""
        self.root.update_idletasks()
        
        # Получаем истинные экранные координаты и размеры главного окна
        p_x = self.root.winfo_rootx()
        p_y = self.root.winfo_rooty()
        p_w = self.root.winfo_width()
        p_h = self.root.winfo_height()
        
        if fit_screen:
            screen_w = max(1, dialog.winfo_screenwidth())
            screen_h = max(1, dialog.winfo_screenheight())
            width = min(int(width), max(360, screen_w - 40))
            height = min(int(height), max(240, screen_h - 80))

        # Высчитываем координаты центра
        x = max(0, p_x + (p_w - width) // 2)
        y = max(0, p_y + (p_h - height) // 2)
        
        # Задаем геометрические координаты
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        
        # Проявляем окно уже строго в правильной позиции!
        dialog.deiconify()
        # transient/grab_set задают владельца и модальность. lift достаточно для
        # показа над root, но в отличие от focus_force не обесцвечивает выделение
        # в ранее активном Entry/Text при каждом открытии служебного окна.
        dialog.lift(self.root)
        
    def _create_wait_popup(self, title, message, modal=True, owner=True):
        popup = tk.Toplevel(self.root)
        popup.withdraw() # 👈 Прячем при создании
        
        popup.title(title)
        popup.resizable(False, False)
        if owner:
            popup.transient(self.root)
        if modal:
            popup.grab_set()
        
        ttk.Label(popup, text=message, font=("", 10)).pack(pady=(15, 5))
        bar = ttk.Progressbar(popup, mode='indeterminate')
        bar.pack(fill=tk.X, padx=20, pady=5)
        bar.start(10)
        
        self._center_popup(popup, 320, 100) # 👈 Проявляем в конце
        
        return popup

    @staticmethod
    def _close_popup_safely(popup):
        """Закрывает только служебное окно, предварительно снимая его grab."""
        try:
            if popup.grab_current() == popup:
                popup.grab_release()
        except tk.TclError:
            pass
        try:
            popup.destroy()
        except tk.TclError:
            pass

    def _tree_select_all(self, event):
        """Рекурсивно выделяет все элементы (включая вложенные файлы) в дереве Экспорта"""
        tree = event.widget
        if self._export_tree_interaction_blocked(tree):
            return "break"
        def get_all(item=""):
            res = []
            for c in tree.get_children(item):
                res.append(c)
                res.extend(get_all(c))
            return res
        tree.selection_set(get_all())
        return "break"
    
    def on_closing(self):
        """Безопасно останавливает работу и закрывает главное окно."""
        if self._is_closing:
            return

        # Операции, которые атомарно публикуют/удаляют индекс и аудиофайлы,
        # нельзя обрывать вместе с daemon-потоком. В отличие от синтеза у них
        # нет безопасной команды Stop, поэтому сначала просим дождаться конца.
        if self.is_cache_operation_running():
            operation = getattr(self, "_cache_operation", None)
            self._show_warning(
                "Операция с кэшем выполняется",
                "Дождитесь завершения операции «"
                f"{CACHE_OPERATION_LABELS.get(operation, operation)}» перед "
                "закрытием приложения. Окно прогресса можно скрыть, но сама "
                "операция должна завершиться.",
            )
            return
        if getattr(self, "_import_running", False):
            self._show_warning(
                "Импорт выполняется",
                "Дождитесь завершения импорта книги перед закрытием приложения, "
                "чтобы не получить неполный набор глав.",
            )
            return
        if getattr(self, "_export_lock", False):
            self._show_warning(
                "Чтение аудиофайлов выполняется",
                "Дождитесь завершения добавления аудиофайлов в проект перед "
                "закрытием приложения.",
            )
            return
        if getattr(self, "_export_running", False):
            self._show_warning(
                "Экспорт выполняется",
                "Сначала нажмите «Стоп» и дождитесь завершения текущего файла, "
                "затем закройте приложение.",
            )
            return

        glossary_dirty = bool(getattr(self, "_glossary_dirty", False))
        glossary_editor = getattr(self, "txt_glossary", None)
        if glossary_editor is not None:
            try:
                glossary_dirty = glossary_dirty or bool(
                    glossary_editor.edit_modified()
                )
            except tk.TclError:
                pass
        if glossary_dirty:
            save_glossary = self._ask_yes_no_cancel(
                "Глоссарий изменён",
                "В редакторе глоссария есть несохранённые изменения.\n\n"
                "Сохранить их перед закрытием?",
                icon="warning",
            )
            if save_glossary is None:
                return
            if save_glossary and not self.save_glossary_ui(show_popup=False):
                return
        self._is_closing = True

        appearance_after_id = getattr(self, "_appearance_check_after_id", None)
        if appearance_after_id is not None:
            try:
                self.root.after_cancel(appearance_after_id)
            except tk.TclError:
                pass
            self._appearance_check_after_id = None

        restore_after_id = getattr(self, "_mac_restore_refresh_after_id", None)
        if restore_after_id is not None:
            try:
                self.root.after_cancel(restore_after_id)
            except tk.TclError:
                pass
            self._mac_restore_refresh_after_id = None

        settings_after_id = getattr(self, "_settings_save_after_id", None)
        if settings_after_id is not None:
            try:
                self.root.after_cancel(settings_after_id)
            except tk.TclError:
                pass
            self._settings_save_after_id = None

        # 1. Если идет синтез, мягко его останавливаем и сохраняем кэш.
        # Уже завершившийся processor тоже может содержать RAM-only статистику
        # cache hit, пока finish_processing ещё не успел освободить ссылку.
        for processor in (self.batch_processor, self.direct_processor):
            if not processor:
                continue
            if not processor.is_stopped:
                processor.stop()
            # При закрытии процесса daemon-worker может не успеть завершить
            # фоновую запись, поэтому здесь единственное намеренно синхронное
            # сохранение кэша перед уничтожением Tk.
            processor.flush_cache()
            # Статусы возобновления (progress/resume) не входят в flush_cache:
            # сохраняем их тем же последним синхронным барьером, иначе после
            # закрытия приложения daemon-поток может не успеть выполнить свой
            # finally-блок.
            processor._save_processing_statuses()

        self.save_settings()

        # Не удаляем SESSION_TEMP_DIR: фоновые задачи этого экземпляра могут
        # ещё ждать FFmpeg. Каждая concat-операция удалит только собственный
        # манифест в finally, уже после завершения subprocess.run().
        self.root.destroy()

    def update_fonts(self, *args):
        """Обновление размера шрифта во всех текстовых блоках (разделители зафиксированы)"""
        size = self.font_size_var.get()
        if hasattr(self, 'direct_text'): self.direct_text.config(font=("Arial", size))
        if hasattr(self, 'txt_glossary'): self.txt_glossary.config(font=("Courier", size))
        if hasattr(self, 'normalizer_source_text'):
            self.normalizer_source_text.config(font=("Arial", size))
        if hasattr(self, 'normalizer_result_text'):
            self.normalizer_result_text.config(font=("Arial", size))
        if hasattr(self, 'help_text_widget'): self.help_text_widget.config(font=("Arial", size))
        self.config["ui_font_size"] = size
        
        if not getattr(self, '_is_updating_ui', False):
            self.save_settings()

    def reset_direct_fx(self):
        """Сброс эффектов на вкладке Прямой синтез"""
        self.dir_speed_var.set(1.0)
        self.dir_pitch_var.set(1.0)
        self.dir_echo_var.set(False)
        self.dir_echo_delay_var.set(300)
        self.dir_echo_decay_var.set(0.3)
        if hasattr(self, 'lbl_dir_speed'): self.lbl_dir_speed.config(text="1.0x")
        if hasattr(self, 'lbl_dir_pitch'): self.lbl_dir_pitch.config(text="1.00")
        if hasattr(self, 'lbl_dir_delay'): self.lbl_dir_delay.config(text="300мс")
        if hasattr(self, 'lbl_dir_decay'): self.lbl_dir_decay.config(text="0.3")

    def reset_export_fx(self):
        """Сброс эффектов на вкладке Экспорт без надписи 'мс'"""
        self.exp_speed_var.set(1.0)
        self.exp_pitch_var.set(1.0)
        self.exp_echo_var.set(False)
        self.exp_delay_var.set(300)
        self.exp_decay_var.set(0.3)
        if hasattr(self, 'lbl_exp_speed'): self.lbl_exp_speed.config(text="1.0x")
        if hasattr(self, 'lbl_exp_pitch'): self.lbl_exp_pitch.config(text="1.00")
        if hasattr(self, 'lbl_exp_delay'): self.lbl_exp_delay.config(text="300")
        if hasattr(self, 'lbl_exp_decay'): self.lbl_exp_decay.config(text="0.3")

    def _refresh_status_colors(self):
        """Перекрашивает постоянные статусы, не меняя их текущий смысл."""
        if hasattr(self, "tree"):
            self.tree.tag_configure("queued", foreground=self.get_status_color("muted"))
            self.tree.tag_configure("success", foreground=self.get_status_color("success"))
            self.tree.tag_configure("warning", foreground=self.get_status_color("warning"))
            self.tree.tag_configure("error", foreground=self.get_status_color("error"))
            self.tree.tag_configure(
                "processing",
                foreground=self.get_status_color("text"),
                font=("", 10, "bold"),
            )

        # Постоянные информационные подписи и динамические статусы хранят
        # семантический тип, поэтому при смене темы не становятся все синими.
        labels = {
            getattr(self, "lbl_cache_count", None): "info",
            getattr(self, "lbl_export_total_time", None): "info",
        }
        labels.update(self._palette_widget_kinds)
        labels.update(self._status_label_kinds)
        for label, status in labels.items():
            if not label:
                continue
            try:
                if label.winfo_exists():
                    label.config(foreground=self.get_status_color(status))
            except tk.TclError:
                pass

    def apply_theme(self, *args):
        """Включает нативную системную тему ОС и статусную палитру."""
        try:
            self.root.title("Silero TTS Studio")
        except tk.TclError:
            pass

        style = ttk.Style()
        os_name = platform.system()
        default_theme = "vista" if os_name == "Windows" else "aqua" if os_name == "Darwin" else "clam"
        try:
            style.theme_use(default_theme)
        except tk.TclError:
            try:
                style.theme_use("default")
            except tk.TclError:
                pass

        self._is_dark_appearance = self._detect_dark_appearance()
        self._refresh_status_colors()

    def full_ui_refresh(self):
        """Обновление значений UI и шрифтов"""
        self._is_updating_ui = True
        try:
            self.set_ui_from_config()
            self.update_fonts()
            self.root.update_idletasks()
        finally:
            self._is_updating_ui = False

    def ensure_dirs(self, keys=None):
        """Создаёт рабочие папки, восстанавливая недоступные значения."""
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            if not hasattr(self, "config"):
                return {}
            _, recovered = ensure_config_directories(self.config, keys=keys)
        except Exception as exc:
            logging.error("Ошибка авто-создания директорий: %s", exc)
            return {}

        for key in recovered:
            variable = (
                self.settings_vars.get(key)
                if hasattr(self, "settings_vars")
                else None
            )
            if variable is not None:
                try:
                    variable.set(self.config[key])
                except (AttributeError, tk.TclError):
                    pass
        if (
            "direct_output_dir" in recovered
            and hasattr(self, "direct_output_dir_var")
        ):
            try:
                self.direct_output_dir_var.set(
                    self.config["direct_output_dir"]
                )
            except tk.TclError:
                pass
        return recovered

    def load_settings(self, path=SETTINGS_FILE):
        """Безопасная загрузка настроек с авто-созданием папки"""
        path = Path(path)

        loaded_config = {}
        backup_path = path.with_suffix(path.suffix + ".bak")
        candidates = [candidate for candidate in (path, backup_path) if candidate.exists()]
        for candidate in candidates:
            try:
                with open(candidate, 'r', encoding='utf-8-sig') as f:
                    loaded_config = json.load(f)
                if not isinstance(loaded_config, dict):
                    raise ValueError("корень settings.json должен быть JSON-объектом")
                if candidate == backup_path:
                    logging.warning(
                        "Настройки восстановлены из резервной копии %s",
                        backup_path,
                    )
                    try:
                        # Лечим primary без ротации: исправный .bak остаётся
                        # страховкой и не будет затёрт повреждённым файлом при
                        # следующем автосохранении.
                        self._write_json_atomic(path, loaded_config)
                    except Exception as recovery_exc:
                        logging.error(
                            "Не удалось восстановить основной файл настроек %s: %s",
                            path,
                            recovery_exc,
                        )
                break
            except Exception as e:
                logging.error(f"Ошибка загрузки конфига {candidate}: {e}")
                loaded_config = {}
        cfg = normalize_config(loaded_config)
        try:
            ensure_config_directories(cfg)
        except Exception as exc:
            # Если недоступен даже portable-дефолт, не рушим построение GUI:
            # конкретная операция с диском позднее покажет штатную ошибку.
            logging.error("Не удалось подготовить рабочие папки: %s", exc)
        # Флажок direct-тегов является подтверждением только для текущего
        # сеанса. Удаляем также значение, случайно сохранённое prerelease-
        # версиями, чтобы приложение всегда стартовало в безопасном режиме.
        cfg.pop("direct_apply_tags", None)
        # Emotion пока не является поддерживаемой настройкой.
        cfg.pop("api_emotion_enabled", None)
        cfg.pop("api_emotion", None)
        return cfg


    def update_config_from_ui(self):
        for key, var in list(self.settings_vars.items()):
            if key == "direct_apply_tags":
                continue
            try:
                self.config[key] = var.get()
            except Exception:
                pass

        # На вкладке прямого синтеза есть удобный дубль поля папки. Если
        # пользователь печатал именно в нём, оно является самым свежим.
        if hasattr(self, "direct_output_dir_var"):
            direct_dir = str(self.direct_output_dir_var.get()).strip()
            if direct_dir:
                self.config["direct_output_dir"] = direct_dir

        # Параметры универсального сборщика не дублируют настройки финального
        # TTS-вывода: у внешних файлов есть собственные частота и каналы, а
        # режим ``auto`` должен сохраняться между запусками.
        for key, attr_name in (
            ("export_format", "export_fmt_var"),
            ("export_bitrate", "export_bitrate_var"),
            ("export_sample_rate", "export_sample_rate_var"),
            ("export_channels", "export_channels_var"),
        ):
            variable = getattr(self, attr_name, None)
            if variable is not None:
                try:
                    self.config[key] = str(variable.get()).strip().lower()
                except tk.TclError:
                    pass

        for key, attr_name in (
            ("export_apply_fx", "export_apply_fx_var"),
            ("export_fx_speed", "exp_speed_var"),
            ("export_fx_pitch", "exp_pitch_var"),
            ("export_fx_echo", "exp_echo_var"),
            ("export_fx_echo_delay", "exp_delay_var"),
            ("export_fx_echo_decay", "exp_decay_var"),
        ):
            variable = getattr(self, attr_name, None)
            if variable is not None:
                try:
                    self.config[key] = variable.get()
                except tk.TclError:
                    pass

        self.config.pop("direct_apply_tags", None)

        # Combobox хранит представление «Другое», а в конфиг/API требуется
        # одно целое число. UI-only ключи не должны попадать в settings.json.
        if "api_steps_choice" in self.settings_vars:
            steps_choice = self.settings_vars["api_steps_choice"].get()
            if steps_choice == "Другое":
                custom_steps = str(
                    self.settings_vars["api_steps_custom"].get()
                ).strip()
                # Пустое/недописанное поле тоже сохраняем как есть. Иначе в
                # config оставался предыдущий preset (обычно 16), и нажатие
                # «Синтезировать» неожиданно проходило валидацию.
                self.config["api_steps"] = custom_steps
            elif steps_choice:
                self.config["api_steps"] = steps_choice

        self.config.pop("api_steps_choice", None)
        self.config.pop("api_steps_custom", None)
        # Собираем разделители из всех активных полей
        if hasattr(self, 'separator_entries'):
            seps = [ent.get().strip() for ent in self.separator_entries if ent.get().strip()]
            self.config["separator_symbols"] = "\n".join(seps)

    def _validate_api_steps_ui(self, show_popup=True, confirm_large=True):
        """Проверяет steps и при необходимости просит подтвердить большой value."""
        self.update_config_from_ui()
        try:
            steps = resolve_api_steps(self.config)
        except ValueError as exc:
            logging.error(f"Некорректная настройка API steps: {exc}")
            if show_popup:
                self._show_error("Некорректный Steps", str(exc))
            return False
        if steps is not None:
            self.config["api_steps"] = steps

        warning = get_api_steps_warning(steps) if confirm_large else None
        warning_key = steps
        if warning and warning_key not in self._confirmed_steps_warnings:
            if not show_popup:
                return False
            if not self._ask_yes_no(
                "Экспериментальное значение Steps",
                f"{warning}\n\nПродолжить с этим значением?",
                icon="warning",
            ):
                return False
            self._confirmed_steps_warnings.add(warning_key)
        return True
            
    def _persist_settings_snapshot(self, snapshot, path=SETTINGS_FILE):
        """Атомарно пишет уже подготовленный снимок, не читая остальные поля UI."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, snapshot, backup=True)

    def save_settings(self, path=SETTINGS_FILE, show_popup=False):
        """Безопасное сохранение настроек с авто-созданием папки и уведомлением"""
        try:
            # Сначала забираем свежий текст из Entry, затем валидируем именно
            # его. Весь путь сохранения находится внутри одного error boundary:
            # недоступный каталог не должен ронять callback Tk.
            self.update_config_from_ui()
            self.ensure_dirs()
            path = Path(path)

            # Обычные автосохранения (пути, шрифт, закрытие окна) не должны
            # блокироваться временно недописанным полем «Другое». Строгая
            # проверка выполняется перед синтезом/оптимизацией кэша.
            self._persist_settings_snapshot(self.config, path)
        except Exception as e:
            logging.error(f"Ошибка сохранения конфига {path}: {e}")
            if show_popup:
                self._show_error("Ошибка", f"Не удалось сохранить настройки:\n{e}")
            return False

        try:
            self.shared_rate_limiter.update_limits(
                self.config.get("api_max_requests", 15),
                self.config.get("api_time_window", 15.0),
            )
        except Exception:
            # settings.json уже атомарно записан; сбой необязательного runtime-
            # обновления не должен выдавать ложную ошибку сохранения.
            logging.exception("Не удалось обновить лимитер после сохранения")

        # Файл уже успешно заменён. Ошибка необязательной перерисовки не должна
        # выдавать ложное «сохранение не удалось» и провоцировать откат памяти
        # при том, что settings.json содержит новый снимок.
        for refresh in (
            self._refresh_global_normalizer_settings_summary,
            self._refresh_normalizer_scope_status,
            self._refresh_audio_profile_summaries,
        ):
            try:
                refresh()
            except Exception:
                logging.exception(
                    "Не удалось обновить интерфейс после сохранения настроек"
                )
        try:
            self._sync_glossary_editor_cache(prompt_if_dirty=show_popup)
        except Exception:
            logging.exception("Не удалось синхронизировать редактор глоссария")
        if show_popup:
            self._show_info("Успех", "Настройки успешно сохранены!")
        return True

    def _schedule_settings_save(self, *_args, delay_ms=350):
        """Объединяет серию печати в одну запись settings.json."""
        if getattr(self, "_is_updating_ui", False) or getattr(self, "_is_closing", False):
            return
        pending = getattr(self, "_settings_save_after_id", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._settings_save_after_id = self.root.after(
            delay_ms, self._run_scheduled_settings_save
        )

    def _run_scheduled_settings_save(self):
        self._settings_save_after_id = None
        self.save_settings()

    def _sync_api_steps_widgets(self, *args, focus_custom=False):
        """Синхронизирует экспериментальные steps-виджеты без trace-записей."""
        if not hasattr(self, "api_steps_combobox"):
            return

        enabled = _config_bool(self.settings_vars["api_steps_enabled"].get())
        choice = self.settings_vars["api_steps_choice"].get()
        preset = None
        if choice != "Другое":
            try:
                preset = int(choice)
            except (TypeError, ValueError):
                preset = 16
                self.settings_vars["api_steps_choice"].set("16")

        if preset is not None:
            self.settings_vars["api_steps"].set(preset)

        self.api_steps_combobox.config(state="readonly" if enabled else tk.DISABLED)
        custom_state = tk.NORMAL if enabled and choice == "Другое" else tk.DISABLED
        self.api_steps_custom_entry.config(state=custom_state)
        if hasattr(self, "api_steps_cache_check"):
            self.api_steps_cache_check.config(
                state=tk.NORMAL if enabled else tk.DISABLED
            )
        if focus_custom and custom_state == tk.NORMAL:
            # На Tk 9 первый клик сразу после смены readonly Combobox иногда
            # лишь активирует Entry. Перенос фокуса через idle делает поле
            # готовым к вводу без focus_force и без побочных эффектов macOS.
            self.root.after_idle(self.api_steps_custom_entry.focus_set)

    def _load_api_steps_widgets_from_config(self):
        """Показывает preset либо «Другое» после загрузки/импорта конфига."""
        if not hasattr(self, "api_steps_combobox"):
            return

        raw_value = self.config.get("api_steps", 16)
        try:
            raw_text = str(raw_value).strip()
            if not re.fullmatch(r"[+-]?\d+", raw_text):
                raise ValueError
            value = int(raw_text)
        except (TypeError, ValueError):
            value = None

        if value in API_STEPS_PRESETS:
            self.settings_vars["api_steps_choice"].set(str(value))
            self.settings_vars["api_steps_custom"].set("")
            self.settings_vars["api_steps"].set(value)
        else:
            self.settings_vars["api_steps_choice"].set("Другое")
            self.settings_vars["api_steps_custom"].set(str(raw_value))
            if value is not None:
                self.settings_vars["api_steps"].set(value)

        self.settings_vars["api_steps_enabled"].set(
            _config_bool(self.config.get("api_steps_enabled", False))
        )
        self.settings_vars["cache_include_steps"].set(
            _config_bool(self.config.get("cache_include_steps", True), default=True)
        )
        self._sync_api_steps_widgets()

    def _config_group_rules(self):
        """Точные списки ключей для независимого импорта групп настроек."""
        return _config_group_rules_data()

    def set_ui_from_config(self):
        """Заполнение полей UI из self.config с полной блокировкой trace-событий"""
        self._is_updating_ui = True
        try:
            self.ensure_dirs()
    
            # 1. Загрузка динамических полей разделителей
            if hasattr(self, 'separators_container') and self.separators_container.winfo_exists():
                try:
                    for child in self.separators_container.winfo_children():
                        child.destroy()
                    self.separator_entries.clear()
                    
                    raw_seps = str(self.config.get("separator_symbols", "")).strip()
                    if not raw_seps:
                        raw_seps = DEFAULT_CONFIG["separator_symbols"]
                        self.config["separator_symbols"] = raw_seps
                        
                    raw_seps = raw_seps.replace("\\n", "\n").replace(",", "\n")
                    seps_list = [s.strip() for s in raw_seps.split("\n") if s.strip()]
                    
                    for sep in seps_list:
                        self.add_separator_row(sep)
                        
                    if not self.separator_entries:
                        for default_sep in DEFAULT_CONFIG["separator_symbols"].split("\n"):
                            self.add_separator_row(default_sep)
                except Exception as e:
                    logging.error(f"Ошибка загрузки разделителей: {e}")
    
            # 2. Переменные настроек
            for key, var in list(self.settings_vars.items()):
                if key in self.config:
                    val = self.config[key]
                    if val is not None:
                        try:
                            if isinstance(var, tk.BooleanVar):
                                var.set(_config_bool(val))
                            elif isinstance(var, tk.IntVar):
                                var.set(int(float(val)))
                            elif isinstance(var, tk.DoubleVar):
                                var.set(float(val))
                            else:
                                var.set(str(val))
                        except Exception as e:
                            try:
                                var.set(str(val))
                            except (tk.TclError, TypeError) as fallback_error:
                                logging.debug(
                                    "Не удалось восстановить UI-поле %s: %s / %s",
                                    key,
                                    e,
                                    fallback_error,
                                )

            # Представление preset/«Другое» является деталью UI и заново
            # строится из единственного сохраняемого значения api_steps.
            self._load_api_steps_widgets_from_config()
    
            # 3. Поля на вкладке "Экспорт"
            if hasattr(self, 'export_outdir_var'):
                self.export_outdir_var.set(str(self.config.get("export_dir", "")))
            if hasattr(self, 'export_fmt_var'):
                self.export_fmt_var.set(
                    str(
                        self.config.get(
                            "export_format",
                            self.config.get("output_format", "mp3"),
                        )
                    )
                )
            if hasattr(self, 'export_bitrate_var'):
                self.export_bitrate_var.set(str(self.config.get("export_bitrate", "auto")))
            if hasattr(self, "export_sample_rate_var"):
                self.export_sample_rate_var.set(
                    str(self.config.get("export_sample_rate", "auto"))
                )
            if hasattr(self, "export_channels_var"):
                self.export_channels_var.set(
                    str(self.config.get("export_channels", "auto"))
                )
            if hasattr(self, 'direct_output_dir_var'):
                try:
                    self.direct_output_dir_var.set(
                        str(self.config.get("direct_output_dir", DEFAULT_DIRECT_OUTPUT_DIR))
                    )
                except Exception:
                    pass
    
            # 4. Ползунки и текстовые подписи чисел
            try:
                sp = float(self.config.get("fx_speed", 1.0))
                pt = float(self.config.get("fx_pitch", 1.0))
                ec = _config_bool(self.config.get("fx_echo", False))
                ed = int(float(self.config.get("fx_echo_delay", 300)))
                ey = float(self.config.get("fx_echo_decay", 0.3))
    
                if hasattr(self, 'lbl_speed_val'): self.lbl_speed_val.config(text=f"{sp:.1f}x")
                if hasattr(self, 'lbl_pitch_val'): self.lbl_pitch_val.config(text=f"{pt:.2f}")
                if hasattr(self, 'lbl_delay_val'): self.lbl_delay_val.config(text=f"{ed}мс")
                if hasattr(self, 'lbl_decay_val'): self.lbl_decay_val.config(text=f"{ey:.1f}")
    
                if hasattr(self, 'dir_speed_var'):
                    self.dir_speed_var.set(sp)
                    self.dir_pitch_var.set(pt)
                    self.dir_echo_var.set(ec)
                    self.dir_echo_delay_var.set(ed)
                    self.dir_echo_decay_var.set(ey)
                    if hasattr(self, 'lbl_dir_speed'): self.lbl_dir_speed.config(text=f"{sp:.1f}x")
                    if hasattr(self, 'lbl_dir_pitch'): self.lbl_dir_pitch.config(text=f"{pt:.2f}")
                    if hasattr(self, 'lbl_dir_delay'): self.lbl_dir_delay.config(text=f"{ed}мс")
                    if hasattr(self, 'lbl_dir_decay'): self.lbl_dir_decay.config(text=f"{ey:.1f}")
    
                if hasattr(self, 'exp_speed_var'):
                    export_sp = float(self.config.get("export_fx_speed", sp))
                    export_pt = float(self.config.get("export_fx_pitch", pt))
                    export_ec = _config_bool(
                        self.config.get("export_fx_echo", ec)
                    )
                    export_ed = int(
                        float(self.config.get("export_fx_echo_delay", ed))
                    )
                    export_ey = float(
                        self.config.get("export_fx_echo_decay", ey)
                    )
                    self.exp_speed_var.set(export_sp)
                    self.exp_pitch_var.set(export_pt)
                    self.exp_echo_var.set(export_ec)
                    self.exp_delay_var.set(export_ed)
                    self.exp_decay_var.set(export_ey)
                    if hasattr(self, 'export_apply_fx_var'):
                        self.export_apply_fx_var.set(
                            _config_bool(
                                self.config.get("export_apply_fx", False)
                            )
                        )
                    if hasattr(self, 'lbl_exp_speed'): self.lbl_exp_speed.config(text=f"{export_sp:.1f}x")
                    if hasattr(self, 'lbl_exp_pitch'): self.lbl_exp_pitch.config(text=f"{export_pt:.2f}")
                    if hasattr(self, 'lbl_exp_delay'): self.lbl_exp_delay.config(text=f"{export_ed}")
                    if hasattr(self, 'lbl_exp_decay'): self.lbl_exp_decay.config(text=f"{export_ey:.1f}")
            except Exception as e:
                logging.error(f"Ошибка ползунков: {e}")
    
            # 5. Шрифт и тема
            if hasattr(self, 'font_size_var') and "ui_font_size" in self.config:
                try:
                    self.font_size_var.set(int(self.config["ui_font_size"]))
                except (tk.TclError, TypeError, ValueError) as exc:
                    logging.debug("Не удалось применить размер шрифта: %s", exc)
    
        finally:
            self._is_updating_ui = False
        if (
            hasattr(self, "normalizer_preview_vars")
            and self.normalizer_preview_vars
        ):
            self._load_normalizer_preview_from_config()
        self._refresh_global_normalizer_settings_summary()
        self._refresh_audio_profile_summaries()

    def import_config(self):
        filepath = filedialog.askopenfilename(
            initialdir=self._config_dialog_initial_dir(),
            filetypes=[("JSON files", "*.json")],
        )
        if not filepath: return
        self._remember_dialog_directory("last_config_dir", filepath)

        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                imported_data = json.load(f)
            if not isinstance(imported_data, dict): raise ValueError()
        except Exception as e:
            self._show_error("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Импорт настроек")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Какие настройки из файла применить?").pack(pady=10, padx=20)
        
        vars_dict = {
            "api": (tk.BooleanVar(value=True), "API и Лимиты (без токена)"),
            "folders": (tk.BooleanVar(value=True), "Пути к папкам"),
            "pauses": (tk.BooleanVar(value=True), "Паузы и Разделители"),
            "cache": (tk.BooleanVar(value=True), "Настройки Кэша"),
            "normalizer": (
                tk.BooleanVar(value=True),
                "Нормализатор и применение глоссария",
            ),
            "effects": (tk.BooleanVar(value=True), "Эффекты (Скорость, Тон)"),
            "tags": (
                tk.BooleanVar(value=True),
                "Вывод, аудиопрофили и Теги ID3",
            ),
            "workspace": (
                tk.BooleanVar(value=True),
                "Параметры вкладок (прямой синтез и импорт книг)",
            ),
        }
        
        for key, (var, text) in vars_dict.items():
            ttk.Checkbutton(dialog, text=text, variable=var).pack(anchor=tk.W, padx=30, pady=2)

        include_api_token_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dialog,
            text="Импортировать API Token (секрет)",
            variable=include_api_token_var,
        ).pack(anchor=tk.W, padx=50, pady=(0, 4))
            
        def do_import():
            selected_groups = [
                group_key
                for group_key, (var, _) in vars_dict.items()
                if var.get()
            ]
            try:
                candidate_config = normalize_config(
                    merge_config_values(
                        self.config,
                        imported_data,
                        selected_groups,
                        include_api_token=include_api_token_var.get(),
                    )
                )
                ensure_config_directories(candidate_config)
                resolved_steps = resolve_api_steps(candidate_config)
                if resolved_steps is not None:
                    candidate_config["api_steps"] = resolved_steps
                # Сначала фиксируем весь снимок на диске. При ошибке текущие
                # config и UI остаются без изменений, а диалог можно повторить.
                self._persist_settings_snapshot(candidate_config)
            except ValueError as exc:
                self._show_error(
                    "Некорректные настройки",
                    str(exc),
                    parent=dialog,
                )
                return
            except Exception as exc:
                logging.error("Не удалось сохранить импорт настроек: %s", exc)
                self._show_error(
                    "Ошибка сохранения",
                    f"Импорт не применён:\n{exc}",
                    parent=dialog,
                )
                return

            self.config = candidate_config
            try:
                self.shared_rate_limiter.update_limits(
                    self.config.get("api_max_requests", 15),
                    self.config.get("api_time_window", 15.0),
                )
            except Exception:
                logging.exception(
                    "Настройки импортированы, но runtime-лимитер не обновлён"
                )
            dialog.destroy()
            self.full_ui_refresh()
            self._sync_glossary_editor_cache(prompt_if_dirty=True)
            self.load_files()
            self._show_info("Успех", "Выбранные настройки успешно применены!")
            
        ttk.Button(dialog, text="Импортировать", command=do_import).pack(pady=15)
        self._center_popup(dialog, 470, 350, fit_screen=True)

    def reset_config(self):
        """Сброс рабочих настроек с сохранением темы оформления и размера шрифта"""
        if self._ask_yes_no("Сбросить настройки", "Сбросить рабочие настройки (паузы, лимиты, пути) к значениям по умолчанию?"):
            try:
                # 1. Сохраняем текущие визуальные предпочтения пользователя
                current_font_size = self.config.get("ui_font_size", 10)

                # 2. Загружаем дефолты и восстанавливаем визуал
                candidate_config = normalize_config(copy.deepcopy(DEFAULT_CONFIG))
                candidate_config["ui_font_size"] = current_font_size
                ensure_config_directories(candidate_config)

                # 3. Записываем снимок до изменения работающего приложения.
                # Если диск недоступен, прежние config и UI остаются целыми.
                self._persist_settings_snapshot(candidate_config)
                self.config = candidate_config

                # 4. Полный проход (перетасовка) для идеально чистой перерисоки
                self.full_ui_refresh()
                self._sync_glossary_editor_cache(prompt_if_dirty=True)
                self.load_files()

                self._show_info("Успех", "Все рабочие настройки сброшены!\n(Тема и размер шрифта сохранены).")
            except Exception as e:
                logging.error(f"Ошибка сброса настроек: {e}")
                self._show_error("Ошибка", f"Не удалось сбросить настройки:\n{e}")

    # --- Вкладка "Синтез из папки" ---
    def setup_main_tab(self):
        list_frame = ttk.Frame(self.tab_main, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        # --- ИЗМЕНЕНИЕ: Системно-зависимая подсказка ---
        ctrl_key = "Command (⌘)" if sys.platform == "darwin" else "Ctrl"
        self._register_palette_widget(
            ttk.Label(
                list_frame,
                text=f"💡 Вы можете выделять несколько строк мышкой с зажатым {ctrl_key} или Shift",
                font=("", 8, "italic"),
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        ).pack(anchor=tk.W, pady=(0, 5))
        # -----------------------------------------------
        
        # ДОБАВЛЕНО: selectmode="extended" для множественного выделения
        self.tree = ttk.Treeview(list_frame, columns=("status", "filename"), show="headings", selectmode="extended")
        if sys.platform == "darwin":
            self._bind_mac_treeview_clicks(self.tree)
        self.tree.heading("status", text="Статус")
        self.tree.heading("filename", text="Имя файла")
        self.tree.column("status", width=120, anchor=tk.CENTER)
        self.tree.column("filename", width=600)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree.tag_configure('queued', foreground=self.get_status_color("muted"))
        self.tree.tag_configure('processing', foreground=self.get_status_color("text"), font=('', 10, 'bold'))
        self.tree.tag_configure('success', foreground=self.get_status_color("success"))
        self.tree.tag_configure('warning', foreground=self.get_status_color("warning"))
        self.tree.tag_configure('error', foreground=self.get_status_color("error"))

        prog_frame = ttk.Frame(self.tab_main, padding=10)
        prog_frame.pack(fill=tk.X)
        
        self.lbl_current_text = ttk.Label(prog_frame, text="Ожидание...", font=('', 10, 'italic'), foreground=self.get_status_color("info"), width=110)
        self.lbl_current_text.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        self._status_label_kinds[self.lbl_current_text] = "info"
        
        ttk.Label(prog_frame, text="Файл:").grid(row=1, column=0, sticky=tk.W)
        self.file_progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=600, mode='determinate')
        self.file_progress.grid(row=1, column=1, padx=10, pady=2)
        self.lbl_file_pct = ttk.Label(prog_frame, text="0%")
        self.lbl_file_pct.grid(row=1, column=2)
        
        ttk.Label(prog_frame, text="Общий:").grid(row=2, column=0, sticky=tk.W)
        self.total_progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=600, mode='determinate')
        self.total_progress.grid(row=2, column=1, padx=10, pady=2)
        self.lbl_total_pct = ttk.Label(prog_frame, text="0/0")
        self.lbl_total_pct.grid(row=2, column=2)

        self.settings_vars["skip_existing"] = tk.BooleanVar(value=self.config.get("skip_existing", True))
        ttk.Checkbutton(prog_frame, text="Пропускать готовые", variable=self.settings_vars["skip_existing"]).grid(row=3, column=0, sticky=tk.W, pady=(5,0))

        self.auto_scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(prog_frame, text="Авто-прокрутка", variable=self.auto_scroll_var).grid(row=3, column=1, sticky=tk.W, pady=(5,0), padx=10)

        self.btn_go_current = ttk.Button(prog_frame, text="📍 К текущему файлу", command=self.scroll_to_current, state=tk.DISABLED)
        self.btn_go_current.grid(row=3, column=2, sticky=tk.W, pady=(5,0))

        self.batch_prepared_text_var = tk.BooleanVar(value=False)
        self.chk_batch_prepared_text = ttk.Checkbutton(
            prog_frame,
            text=(
                "Текст уже подготовлен: не применять RegEx, глоссарий и "
                "нормализацию (только этот запуск)"
            ),
            variable=self.batch_prepared_text_var,
        )
        self.chk_batch_prepared_text.grid(
            row=4, column=0, columnspan=3, sticky=tk.W, pady=(5, 0)
        )

        # --- ОБНОВЛЕННЫЙ БЛОК КНОПОК ---
        btn_frame = ttk.Frame(self.tab_main, padding=10)
        btn_frame.pack(fill=tk.X)
        
        self.btn_start_all = ttk.Button(btn_frame, text="▶ Старт (Все)", command=lambda: self.start_processing(only_selected=False))
        self.btn_start_all.pack(side=tk.LEFT, padx=2)
        
        self.btn_start_sel = ttk.Button(btn_frame, text="▶ Старт (Выбранные)", command=lambda: self.start_processing(only_selected=True))
        self.btn_start_sel.pack(side=tk.LEFT, padx=2)
        
        self.btn_stop = ttk.Button(btn_frame, text="⏹ Стоп", command=self.stop_processing, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        
        self.btn_hard_stop = ttk.Button(btn_frame, text="☠️ Принудительно", command=self.hard_stop_processing, state=tk.DISABLED)
        self.btn_hard_stop.pack(side=tk.LEFT, padx=2)
        
        self.btn_refresh = ttk.Button(btn_frame, text="🔄 Обновить папку", command=self.load_files)
        self.btn_refresh.pack(side=tk.RIGHT, padx=2)
        
        self.btn_remove_sel = ttk.Button(btn_frame, text="🗑 Удалить из списка", command=self.remove_selected_from_queue)
        self.btn_remove_sel.pack(side=tk.RIGHT, padx=2)

    # --- Вкладка "Прямой синтез" ---
    def setup_direct_tab(self):
        frame = ttk.Frame(self.tab_direct, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        header_frame = ttk.Frame(frame)
        header_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(header_frame, text="Вставьте текст для синтеза:").pack(side=tk.LEFT)
        
        font_cb = ttk.Combobox(header_frame, textvariable=self.font_size_var, values=[10, 12, 14, 16, 18, 20, 24], state="readonly", width=5)
        font_cb.pack(side=tk.RIGHT)
        ttk.Label(header_frame, text="Шрифт:").pack(side=tk.RIGHT, padx=5)
        font_cb.bind("<<ComboboxSelected>>", lambda e: self.root.after(10, self.update_fonts))

        self.direct_text = tk.Text(frame, wrap=tk.WORD, height=7, undo=True, maxundo=50)
        self.direct_text.pack(fill=tk.BOTH, expand=True, pady=5)
        
        ctrl_frame = ttk.Frame(frame)
        ctrl_frame.pack(fill=tk.X, pady=(5, 0))
        
        ttk.Label(ctrl_frame, text="Файл:").pack(side=tk.LEFT)
        self.settings_vars["direct_filename"] = tk.StringVar(value=self.config.get("direct_filename", "direct_output.mp3"))
        ttk.Entry(ctrl_frame, textvariable=self.settings_vars["direct_filename"], width=18).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_save"] = tk.BooleanVar(value=self.config.get("direct_save", True))
        ttk.Checkbutton(ctrl_frame, text="Сохранить", variable=self.settings_vars["direct_save"]).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_force"] = tk.BooleanVar(value=self.config.get("direct_force", False))
        ttk.Checkbutton(ctrl_frame, text="Игнорировать кэш", variable=self.settings_vars["direct_force"]).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_autoplay"] = tk.BooleanVar(value=self.config.get("direct_autoplay", True))
        ttk.Checkbutton(ctrl_frame, text="Авто-воспроизведение", variable=self.settings_vars["direct_autoplay"]).pack(side=tk.LEFT, padx=5)

        direct_path_frame = ttk.Frame(frame)
        direct_path_frame.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(direct_path_frame, text="Папка:").pack(side=tk.LEFT)
        # Это отдельная ссылка на то же значение конфига. Нельзя класть её в
        # settings_vars: позднее вкладка «Настройки → Папки» создаёт второй
        # Entry с тем же ключом и перезаписала бы объект StringVar в словаре.
        self.direct_output_dir_var = tk.StringVar(
            value=self.config.get("direct_output_dir", DEFAULT_DIRECT_OUTPUT_DIR)
        )
        ttk.Entry(
            direct_path_frame,
            textvariable=self.direct_output_dir_var,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        def choose_direct_output_dir():
            current = self.direct_output_dir_var.get()
            selected = filedialog.askdirectory(initialdir=self._get_smart_dir(current))
            if selected:
                self.direct_output_dir_var.set(selected)
                self.config["direct_output_dir"] = selected
                settings_var = self.settings_vars.get("direct_output_dir")
                if settings_var is not None:
                    settings_var.set(selected)
                self.save_settings()

        ttk.Button(
            direct_path_frame,
            text="📁",
            width=3,
            command=choose_direct_output_dir,
        ).pack(side=tk.RIGHT)

        direct_options_frame = ttk.Frame(frame)
        direct_options_frame.pack(fill=tk.X, pady=(0, 5))
        self.settings_vars["direct_apply_tags"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            direct_options_frame,
            text="Применить теги из настроек",
            variable=self.settings_vars["direct_apply_tags"],
        ).pack(side=tk.LEFT, padx=5)
        self.direct_prepared_text_var = tk.BooleanVar(value=False)
        self.chk_direct_prepared_text = ttk.Checkbutton(
            direct_options_frame,
            text="Текст уже подготовлен (только этот запуск)",
            variable=self.direct_prepared_text_var,
        )
        self.chk_direct_prepared_text.pack(side=tk.RIGHT, padx=5)
        self._register_palette_widget(
            ttk.Label(
                direct_options_frame,
                text="(по умолчанию direct_output сохраняется без книжных тегов)",
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        ).pack(side=tk.LEFT, padx=5)
        
        # --- МИНИ-ПАНЕЛЬ ЭФФЕКТОВ ДЛЯ ПРЯМОГО СИНТЕЗА ---
        fx_frame = ttk.LabelFrame(frame, text="Локальные эффекты (только для этой вкладки)", padding=5)
        fx_frame.pack(fill=tk.X, pady=5)
        
        self.dir_speed_var = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        self.dir_pitch_var = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        self.dir_echo_var = tk.BooleanVar(value=self.config.get("fx_echo", False))
        self.dir_echo_delay_var = tk.IntVar(value=self.config.get("fx_echo_delay", 300))
        self.dir_echo_decay_var = tk.DoubleVar(value=self.config.get("fx_echo_decay", 0.3))
        
        # Ряд 1: Скорость и Тон
        top_fx = ttk.Frame(fx_frame)
        top_fx.pack(fill=tk.X, pady=2)
        
        ttk.Label(top_fx, text="Скорость:").pack(side=tk.LEFT, padx=5)
        self.lbl_dir_speed = ttk.Label(top_fx, text=f"{self.dir_speed_var.get():.1f}x", width=4)
        self.lbl_dir_speed.pack(side=tk.LEFT)
        scale_dir_speed = ttk.Scale(top_fx, from_=0.5, to_=3.0, variable=self.dir_speed_var, command=lambda v: self.lbl_dir_speed.config(text=f"{float(v):.1f}x"))
        scale_dir_speed.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(top_fx, text="Тон:").pack(side=tk.LEFT, padx=5)
        self.lbl_dir_pitch = ttk.Label(top_fx, text=f"{self.dir_pitch_var.get():.2f}", width=4)
        self.lbl_dir_pitch.pack(side=tk.LEFT)
        scale_dir_pitch = ttk.Scale(top_fx, from_=0.5, to_=2.0, variable=self.dir_pitch_var, command=lambda v: self.lbl_dir_pitch.config(text=f"{float(v):.2f}"))
        scale_dir_pitch.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Ряд 2: Эхо (Чекбокс + Ползунки)
        mid_fx = ttk.Frame(fx_frame)
        mid_fx.pack(fill=tk.X, pady=2)
        
        ttk.Checkbutton(mid_fx, text="Эхо", variable=self.dir_echo_var).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(mid_fx, text="Задержка:").pack(side=tk.LEFT, padx=(10, 2))
        self.lbl_dir_delay = ttk.Label(mid_fx, text=f"{self.dir_echo_delay_var.get()}мс", width=6)
        self.lbl_dir_delay.pack(side=tk.LEFT)
        scale_dir_delay = ttk.Scale(mid_fx, from_=50, to_=1000, variable=self.dir_echo_delay_var, command=lambda v: self.lbl_dir_delay.config(text=f"{int(float(v))}мс"))
        scale_dir_delay.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(mid_fx, text="Сила:").pack(side=tk.LEFT, padx=(10, 2))
        self.lbl_dir_decay = ttk.Label(mid_fx, text=f"{self.dir_echo_decay_var.get():.1f}", width=4)
        self.lbl_dir_decay.pack(side=tk.LEFT)
        scale_dir_decay = ttk.Scale(mid_fx, from_=0.1, to_=0.8, variable=self.dir_echo_decay_var, command=lambda v: self.lbl_dir_decay.config(text=f"{float(v):.1f}"))
        scale_dir_decay.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Ряд 3: Кнопки действий прямого синтеза
        bot_fx = ttk.Frame(fx_frame)
        bot_fx.pack(fill=tk.X, pady=2)
        
        def apply_to_global():
            self.settings_vars["fx_speed"].set(self.dir_speed_var.get())
            self.settings_vars["fx_pitch"].set(self.dir_pitch_var.get())
            self.settings_vars["fx_echo"].set(self.dir_echo_var.get())
            self.settings_vars["fx_echo_delay"].set(self.dir_echo_delay_var.get())
            self.settings_vars["fx_echo_decay"].set(self.dir_echo_decay_var.get())
            self.save_settings()
            self._show_info("Успех", "Эффекты сохранены в глобальные настройки!")
            
        ttk.Button(bot_fx, text="💾 Сделать глобальными", command=apply_to_global).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bot_fx, text="🔄 Сбросить эффекты", command=self.reset_direct_fx).pack(side=tk.RIGHT, padx=5)
        # ------------------------------------------------
        
        self.lbl_direct_status = ttk.Label(frame, text="", foreground=self.get_status_color("info"))
        self.lbl_direct_status.pack(anchor=tk.W)
        self._status_label_kinds[self.lbl_direct_status] = "info"
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=5)
        self.btn_direct_start = ttk.Button(btn_frame, text="▶ Синтезировать", command=self.start_direct_processing)
        self.btn_direct_start.pack(side=tk.LEFT)
        
        self.btn_direct_stop = ttk.Button(btn_frame, text="⏹ Стоп", command=self.stop_direct_processing, state=tk.DISABLED)
        self.btn_direct_stop.pack(side=tk.LEFT, padx=5)
        
        self.btn_direct_hard_stop = ttk.Button(btn_frame, text="☠️ Принудительно", command=self.hard_stop_direct_processing, state=tk.DISABLED)
        self.btn_direct_hard_stop.pack(side=tk.LEFT, padx=5)
        
        # --- ОБНОВЛЕННЫЕ КНОПКИ ПЛЕЕРА ---
        self.btn_direct_play = ttk.Button(btn_frame, text="🔊 Слушать", command=self.play_last_audio, state=tk.DISABLED)
        self.btn_direct_play.pack(side=tk.LEFT, padx=(10, 2))
        
        self.btn_direct_stop_audio = ttk.Button(btn_frame, text="🔇", width=3, command=self.stop_audio_playback)
        self.btn_direct_stop_audio.pack(side=tk.LEFT, padx=2)

    def stop_audio_playback(self):
        """Принудительно останавливает текущее воспроизведение аудио"""
        if platform.system() == "Windows":
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except RuntimeError as exc:
                logging.debug("Не удалось остановить winsound: %s", exc)
        else:
            if hasattr(self, 'current_playback_process') and self.current_playback_process:
                try:
                    self.current_playback_process.terminate()
                except OSError as exc:
                    logging.debug("Не удалось остановить аудиопроцесс: %s", exc)
                self.current_playback_process = None

    def play_audio_segment(self, audio_segment):
        """Проигрывает аудиосегмент с защитой от наложения (останавливает предыдущий)"""
        self.stop_audio_playback()

        def _play():
            # У каждого запуска собственный WAV. Иначе старый worker мог
            # одновременно читать файл, который новый worker уже перезаписывает.
            SESSION_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = SESSION_TEMP_DIR / f"playback_{uuid.uuid4().hex}.wav"

            try:
                exported = audio_segment.export(str(temp_path), format="wav")
                if hasattr(exported, "flush"):
                    exported.flush()
                if hasattr(exported, "close"):
                    exported.close()

                if platform.system() == "Windows":
                    # SND_ASYNC позволяет UI не зависать, пока играет звук.
                    # winsound продолжает читать файл после возврата, поэтому
                    # session-temp оставляем операционной системе.
                    winsound.PlaySound(str(temp_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                elif platform.system() == "Darwin":
                    self.current_playback_process = subprocess.Popen(["afplay", str(temp_path)])
                    self.current_playback_process.wait()
                else:
                    self.current_playback_process = subprocess.Popen(["aplay", str(temp_path)])
                    self.current_playback_process.wait()
            except Exception as e:
                logging.error(f"Ошибка воспроизведения: {e}")
            finally:
                # На Unix subprocess уже завершился и файл никому не нужен.
                if platform.system() != "Windows":
                    temp_path.unlink(missing_ok=True)

        # Этот метод может быть вызван как из Tk-потока, так и после фоновой
        # подготовки эффектов. subprocess не трогает Tk, поэтому запускаем
        # воспроизведение сразу в текущем worker либо создаём worker из UI.
        if threading.current_thread() is threading.main_thread():
            threading.Thread(target=_play, daemon=True).start()
        else:
            _play()

    def play_audio_file(self, filepath):
        if not os.path.exists(filepath): return
        try:
            seg = _load_audio_segment(filepath)
            self.play_audio_segment(seg)
        except Exception as e:
            logging.error(f"Ошибка чтения файла для плеера: {e}")

    def play_last_audio(self):
        if not self.last_direct_audio or not os.path.exists(self.last_direct_audio):
            return

        # Декодирование и эффекты могут занимать секунды. Не блокируем Tk mainloop,
        # но все значения Tk-переменных читаем заранее в главном потоке.
        audio_path = self.last_direct_audio
        already_processed = self.last_direct_audio_has_effects
        effect_settings = (
            self.dir_speed_var.get(),
            self.dir_pitch_var.get(),
            self.dir_echo_var.get(),
            self.dir_echo_delay_var.get(),
            self.dir_echo_decay_var.get(),
        )

        def _prepare_and_play():
            try:
                segment = _load_audio_segment(audio_path)
                if not already_processed:
                    segment = AudioEffects.apply_effects(
                        segment,
                        speed=effect_settings[0],
                        pitch=effect_settings[1],
                        echo=effect_settings[2],
                        echo_delay=effect_settings[3],
                        echo_decay=effect_settings[4],
                    )
                self.play_audio_segment(segment)
            except Exception:
                logging.exception("Ошибка подготовки последнего аудио к воспроизведению")

        threading.Thread(target=_prepare_and_play, daemon=True).start()

    # --- Вкладка "Импорт книг" ---
    def setup_import_tab(self):
        frame = ttk.Frame(self.tab_import, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        if not IMPORT_LIBS_AVAILABLE:
            self._register_palette_widget(
                ttk.Label(
                    frame,
                    text="⚠️ Для работы импорта установите библиотеки:\npip install EbookLib beautifulsoup4 python-docx lxml",
                    foreground=self.get_status_color("error"),
                ),
                "error",
            ).pack(pady=10)
            return

        # Выбор файла
        file_frame = ttk.LabelFrame(frame, text="Исходный файл (EPUB, FB2, DOCX, TXT)", padding=10)
        file_frame.pack(fill=tk.X, pady=5)
        
        self.import_filepath_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.import_filepath_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        def choose_import_file():
            init_dir = self._get_smart_dir(self.import_filepath_var.get(), is_file=True)
            res = filedialog.askopenfilename(initialdir=init_dir, filetypes=[("Книги", "*.epub *.fb2 *.docx *.txt")])
            if res: self.import_filepath_var.set(res)
            
        ttk.Button(file_frame, text="Выбрать файл", command=choose_import_file).pack(side=tk.LEFT)

        # Настройки нарезки
        split_frame = ttk.LabelFrame(frame, text="Настройки нарезки и сохранения", padding=10)
        split_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(split_frame, text="Папка для сохранения (.txt):").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_outdir"] = tk.StringVar(value=self.config.get("import_outdir", "input_texts"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_outdir"], width=40).grid(row=0, column=1, padx=5)
        
        def choose_import_dir():
            init_dir = self._get_smart_dir(self.settings_vars["import_outdir"].get())
            res = filedialog.askdirectory(initialdir=init_dir)
            if res: self.settings_vars["import_outdir"].set(res)
            
        ttk.Button(split_frame, text="📁", width=3, command=choose_import_dir).grid(row=0, column=2)
        
        ttk.Label(split_frame, text="Шаблон имени файла:\nДоступно: {num} или {num:0}, {name}, {title}, {author}").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_template"] = tk.StringVar(value=self.config.get("import_template", "{num} - {name} - {title}"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_template"], width=40).grid(row=1, column=1, padx=5)
        
        ttk.Label(split_frame, text="RegEx для TXT (нарезка по главам):\nПример: ^Глава \\d+").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_regex"] = tk.StringVar(value=self.config.get("import_regex", r"^Глава \d+"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_regex"], width=40).grid(row=2, column=1, padx=5)
        
        self.settings_vars["import_single_file"] = tk.BooleanVar(value=self.config.get("import_single_file", False))
        ttk.Checkbutton(split_frame, text="Не делить на главы (сохранить как один файл)", variable=self.settings_vars["import_single_file"]).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=5)
        
        # Кнопка и статус
        self.lbl_import_status = ttk.Label(frame, text="", foreground=self.get_status_color("info"))
        self.lbl_import_status.pack(pady=5)
        self._status_label_kinds[self.lbl_import_status] = "info"
        
        self.btn_import_start = ttk.Button(frame, text="⚡ Извлечь и Нарезать", command=self.start_import)
        self.btn_import_start.pack(pady=5)

    def start_import(self):
        if getattr(self, "_import_running", False):
            return
        self.save_settings()
        filepath = self.import_filepath_var.get().strip()
        source_path = Path(filepath).expanduser() if filepath else None
        if source_path is None or not source_path.is_file():
            self._show_error("Ошибка", "Выберите существующий файл!")
            return
            
        # save_settings() уже проверил свежий Entry и при необходимости
        # заменил недоступный каталог на portable-дефолт. Берём итог из config,
        # а не прежнее значение Tk-переменной.
        out_dir = str(self.config.get("import_outdir", "")).strip()
        template = self.settings_vars["import_template"].get().strip()
        regex_pattern = self.settings_vars["import_regex"].get()
        single_file = self.settings_vars["import_single_file"].get()
        if not out_dir:
            self._show_error("Ошибка", "Укажите папку для сохранения глав.")
            return
        if not template:
            self._show_error("Ошибка", "Шаблон имени файла не может быть пустым.")
            return
        if source_path.suffix.lower() not in {".epub", ".fb2", ".docx", ".txt"}:
            self._show_error(
                "Ошибка", "Поддерживаются только EPUB, FB2, DOCX и TXT."
            )
            return
        filepath = str(source_path)
        
        self.btn_import_start.config(state=tk.DISABLED)
        self._import_running = True
        self._set_status_label(self.lbl_import_status, "Анализ и извлечение текста...", "text")
        
        def run():
            try:
                ext = Path(filepath).suffix.lower()
                chapters = []
                author = ""
                
                if single_file:
                    if ext == ".epub": 
                        ch, author = BookExtractor.extract_epub(filepath)
                        chapters = [("Книга", "\n\n".join([c[1] for c in ch]))]
                    elif ext == ".fb2": 
                        ch, author = BookExtractor.extract_fb2(filepath)
                        chapters = [("Книга", "\n\n".join([c[1] for c in ch]))]
                    elif ext == ".docx": 
                        chapters = [("Книга", "\n\n".join([c[1] for c in BookExtractor.extract_docx(filepath)]))]
                    elif ext == ".txt": 
                        with open(filepath, 'r', encoding='utf-8-sig') as f: chapters = [("Книга", f.read())]
                else:
                    if ext == ".epub": chapters, author = BookExtractor.extract_epub(filepath)
                    elif ext == ".fb2": chapters, author = BookExtractor.extract_fb2(filepath)
                    elif ext == ".docx": chapters = BookExtractor.extract_docx(filepath)
                    elif ext == ".txt": chapters = BookExtractor.split_txt_by_regex(filepath, regex_pattern)
                
                if not chapters:
                    raise ValueError("Не удалось найти текст или главы в файле.")
                    
                self._post_status_label(
                    self.lbl_import_status,
                    f"Найдено глав: {len(chapters)}. Сохранение...",
                    "warning",
                )
                
                # Передаем автора в сохранение
                saved_files = BookExtractor.save_chapters(chapters, out_dir, filepath, template, author=author)
                
                msg = f"Успешно извлечено и сохранено файлов: {len(saved_files)}\nПапка: {out_dir}"
                self._post_status_label(self.lbl_import_status, "Готово!", "success")
                self._post_to_ui(self._show_info, "Успех", msg)
                self._post_to_ui(self.load_files)
                
            except Exception as e:
                logging.error(f"Ошибка импорта: {e}")
                error_message = f"Не удалось обработать файл:\n{e}"
                self._post_status_label(self.lbl_import_status, "Ошибка!", "error")
                self._post_to_ui(self._show_error, "Ошибка", error_message)
            finally:
                def finish_import_ui():
                    self._import_running = False
                    self._import_thread = None
                    self.btn_import_start.config(state=tk.NORMAL)
                self._post_to_ui(finish_import_ui)

        self._import_thread = threading.Thread(target=run, daemon=True)
        try:
            self._import_thread.start()
        except Exception as exc:
            self._import_thread = None
            self._import_running = False
            self.btn_import_start.config(state=tk.NORMAL)
            self._set_status_label(self.lbl_import_status, "Ошибка запуска.", "error")
            logging.exception("Не удалось запустить поток импорта книги")
            self._show_error(
                "Ошибка", f"Не удалось запустить импорт:\n{exc}"
            )

    # ================= Вкладка "Экспорт и Сборка" =================
    def setup_utils_tab(self):
        frame = ttk.Frame(self.tab_utils, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        self.export_groups = {} 
        self.export_files = {}
        self.group_counter = 0

        # === 1. ПРОГРЕСС-БАР (Пакуем в самый низ, чтобы не пропадал) ===
        prog_frame = ttk.Frame(frame)
        prog_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(3, 0))
        self.lbl_export_status = ttk.Label(
            prog_frame,
            text="Ожидание...",
            anchor=tk.W,
            foreground=self.get_status_color("info"),
        )
        self.lbl_export_status.pack(side=tk.LEFT)
        self._status_label_kinds[self.lbl_export_status] = "info"
        self._export_status_activity = None
        self.export_progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=400, mode='determinate')
        self.export_progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))
        prog_frame.bind(
            "<Configure>",
            lambda _event: self._render_export_activity_status(),
            add="+",
        )

        # === 2. ПАНЕЛЬ ЭКСПОРТА ===
        self.export_frame = ttk.LabelFrame(frame, text="Экспорт", padding=4)
        self.export_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(3, 2))
        export_frame = self.export_frame
        
        row1 = ttk.Frame(export_frame)
        row1.pack(fill=tk.X, pady=1)
        ttk.Label(row1, text="Папка:").pack(side=tk.LEFT, padx=5)
        
        self.export_outdir_var = tk.StringVar(value=self.config.get("export_dir", ""))
        def choose_exp_dir():
            init_dir = self._get_smart_dir(self.export_outdir_var.get())
            res = filedialog.askdirectory(initialdir=init_dir)
            if res: 
                self.export_outdir_var.set(res)
                self.config["export_dir"] = res
                self.save_settings()
                
        self.btn_export_dir = ttk.Button(row1, text="📁", width=3, command=choose_exp_dir)
        self.btn_export_dir.pack(side=tk.RIGHT, padx=5)
        self.ent_export_dir = ttk.Entry(row1, textvariable=self.export_outdir_var)
        self.ent_export_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        row2 = ttk.Frame(export_frame)
        row2.pack(fill=tk.X, pady=1)
        ttk.Label(row2, text="Формат:").pack(side=tk.LEFT, padx=(5, 2))
        self.export_fmt_var = tk.StringVar(
            value=self.config.get("export_format", self.config.get("output_format", "mp3"))
        )
        self.cb_export_fmt = ttk.Combobox(
            row2,
            textvariable=self.export_fmt_var,
            values=["mp3", "wav", "ogg", "opus"],
            width=5,
            state="readonly",
        )
        self.cb_export_fmt.pack(side=tk.LEFT)
        
        ttk.Label(row2, text="Битрейт:").pack(side=tk.LEFT, padx=(10, 2))
        self.export_bitrate_var = tk.StringVar(
            value=self.config.get("export_bitrate", "auto")
        )
        self.cb_export_bitrate = ttk.Combobox(
            row2,
            textvariable=self.export_bitrate_var,
            values=["auto", "32k", "48k", "64k", "96k", "128k", "192k", "256k", "320k"],
            width=6,
            state="readonly",
        )
        self.cb_export_bitrate.pack(side=tk.LEFT)

        ttk.Label(row2, text="Частота:").pack(side=tk.LEFT, padx=(10, 2))
        self.export_sample_rate_var = tk.StringVar(
            value=self.config.get("export_sample_rate", "auto")
        )
        self.cb_export_sample_rate = ttk.Combobox(
            row2,
            textvariable=self.export_sample_rate_var,
            values=[
                "auto", "8000", "12000", "16000", "22050", "24000",
                "32000", "44100", "48000", "88200", "96000",
            ],
            width=7,
            state="readonly",
        )
        self.cb_export_sample_rate.pack(side=tk.LEFT)

        ttk.Label(row2, text="Каналы:").pack(side=tk.LEFT, padx=(10, 2))
        self.export_channels_var = tk.StringVar(
            value=self.config.get("export_channels", "auto")
        )
        self.cb_export_channels = ttk.Combobox(
            row2,
            textvariable=self.export_channels_var,
            values=["auto", "mono", "stereo"],
            width=7,
            state="readonly",
        )
        self.cb_export_channels.pack(side=tk.LEFT)

        self.btn_audio_profiles_export = ttk.Button(
            row2,
            text="🎛 Аудиопрофили…",
            command=lambda: self.open_audio_profiles_dialog("export"),
        )
        self.btn_audio_profiles_export.pack(side=tk.RIGHT, padx=(10, 5))

        self.export_fmt_var.trace_add(
            "write", lambda *_args: self._sync_export_mode_controls()
        )
        
        self.export_apply_fx_var = tk.BooleanVar(
            value=_config_bool(self.config.get("export_apply_fx", False))
        )

        # Режим обновления тегов отключает параметры сборки, поэтому
        # сам флаг размещается ниже этих параметров, в строке действий.
        self.export_tags_only_var = tk.BooleanVar(value=False)

        row3 = ttk.Frame(export_frame)
        row3.pack(fill=tk.X, pady=1)

        self.chk_export_fx = ttk.Checkbutton(
            row3,
            text="Наложить эффекты",
            variable=self.export_apply_fx_var,
        )
        self.chk_export_fx.pack(side=tk.LEFT, padx=(5, 8))
        
        ttk.Label(row3, text="Скор:").pack(side=tk.LEFT, padx=1)
        self.exp_speed_var = tk.DoubleVar(
            value=self.config.get("export_fx_speed", self.config.get("fx_speed", 1.0))
        )
        self.lbl_exp_speed = ttk.Label(row3, text=f"{self.exp_speed_var.get():.1f}x", width=4)
        self.lbl_exp_speed.pack(side=tk.LEFT)
        self.scale_exp_speed = ttk.Scale(
            row3,
            from_=0.5,
            to=3.0,
            variable=self.exp_speed_var,
            command=lambda value: self.lbl_exp_speed.config(
                text=f"{float(value):.1f}x"
            ),
        )
        self.scale_exp_speed.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=2
        )
        
        ttk.Label(row3, text="Тон:").pack(side=tk.LEFT, padx=(4, 1))
        self.exp_pitch_var = tk.DoubleVar(
            value=self.config.get("export_fx_pitch", self.config.get("fx_pitch", 1.0))
        )
        self.lbl_exp_pitch = ttk.Label(row3, text=f"{self.exp_pitch_var.get():.2f}", width=4)
        self.lbl_exp_pitch.pack(side=tk.LEFT)
        self.scale_exp_pitch = ttk.Scale(
            row3,
            from_=0.5,
            to=2.0,
            variable=self.exp_pitch_var,
            command=lambda value: self.lbl_exp_pitch.config(
                text=f"{float(value):.2f}"
            ),
        )
        self.scale_exp_pitch.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=2
        )
        
        self.exp_echo_var = tk.BooleanVar(
            value=self.config.get("export_fx_echo", self.config.get("fx_echo", False))
        )
        self.chk_exp_echo = ttk.Checkbutton(
            row3, text="Эхо", variable=self.exp_echo_var
        )
        self.chk_exp_echo.pack(side=tk.LEFT, padx=(4, 2))
        
        ttk.Label(row3, text="Зад:").pack(side=tk.LEFT, padx=1)
        self.exp_delay_var = tk.IntVar(
            value=self.config.get(
                "export_fx_echo_delay", self.config.get("fx_echo_delay", 300)
            )
        )
        self.lbl_exp_delay = ttk.Label(row3, text=f"{self.exp_delay_var.get()}", width=4)
        self.lbl_exp_delay.pack(side=tk.LEFT)
        self.scale_exp_delay = ttk.Scale(
            row3,
            from_=50,
            to=1000,
            variable=self.exp_delay_var,
            command=lambda value: self.lbl_exp_delay.config(
                text=f"{int(float(value))}"
            ),
        )
        self.scale_exp_delay.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=2
        )
        
        ttk.Label(row3, text="Сил:").pack(side=tk.LEFT, padx=(4, 1))
        self.exp_decay_var = tk.DoubleVar(
            value=self.config.get(
                "export_fx_echo_decay", self.config.get("fx_echo_decay", 0.3)
            )
        )
        self.lbl_exp_decay = ttk.Label(row3, text=f"{self.exp_decay_var.get():.1f}", width=3)
        self.lbl_exp_decay.pack(side=tk.LEFT)
        self.scale_exp_decay = ttk.Scale(
            row3,
            from_=0.1,
            to=0.8,
            variable=self.exp_decay_var,
            command=lambda value: self.lbl_exp_decay.config(
                text=f"{float(value):.1f}"
            ),
        )
        self.scale_exp_decay.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=2
        )
        self.btn_reset_export_fx = ttk.Button(
            row3, text="🔄 Сброс", command=self.reset_export_fx
        )
        self.btn_reset_export_fx.pack(side=tk.RIGHT, padx=2)
        self.export_fx_value_controls = (
            self.scale_exp_speed,
            self.scale_exp_pitch,
            self.chk_exp_echo,
            self.scale_exp_delay,
            self.scale_exp_decay,
            self.btn_reset_export_fx,
        )

        # Сводка получает собственную строку: длинное имя/расшифровка больше не
        # сжимает ни флажок тегов, ни кнопки запуска.
        profile_summary_row = ttk.Frame(export_frame)
        profile_summary_row.pack(fill=tk.X, pady=(0, 1))
        self.lbl_export_audio_profile_summary = self._register_palette_widget(
            ttk.Label(
                profile_summary_row,
                text="",
                foreground=self.get_status_color("muted"),
                font=("", 9),
                anchor=tk.W,
                justify=tk.LEFT,
            ),
            "muted",
        )
        self.lbl_export_audio_profile_summary.pack(
            fill=tk.X, expand=True, padx=5
        )

        def resize_profile_summary(event):
            try:
                self.lbl_export_audio_profile_summary.configure(
                    wraplength=max(320, event.width - 10)
                )
            except tk.TclError:
                pass

        profile_summary_row.bind("<Configure>", resize_profile_summary, add="+")

        # Команды сборки вынесены в отдельную строку: параметры профиля и
        # эффекты остаются доступными даже при небольшой ширине окна.
        row4 = ttk.Frame(export_frame)
        row4.pack(fill=tk.X, pady=(1, 0))

        export_actions = ttk.Frame(row4)
        export_actions.pack(side=tk.RIGHT)
        self.btn_export_start = ttk.Button(
            export_actions,
            text="🚀 Начать Сборку",
            command=self.start_export_process,
        )
        self.btn_export_start.pack(side=tk.LEFT, padx=(0, 5))
        self.btn_export_stop = ttk.Button(
            export_actions,
            text="⏹ Стоп",
            command=self.stop_export_process,
            state=tk.DISABLED,
        )
        self.btn_export_stop.pack(side=tk.LEFT)

        self.chk_export_tags_only = ttk.Checkbutton(
            row4,
            text="Только обновить теги в исходных файлах",
            variable=self.export_tags_only_var,
        )
        self.chk_export_tags_only.pack(side=tk.LEFT, padx=5)

        # Блокировка UI при включении режима "только теги".
        self.export_tags_only_var.trace_add(
            "write", lambda *_args: self._sync_export_mode_controls()
        )

        for variable in (
            self.export_fmt_var,
            self.export_bitrate_var,
            self.export_sample_rate_var,
            self.export_channels_var,
            self.export_apply_fx_var,
            self.exp_speed_var,
            self.exp_pitch_var,
            self.exp_echo_var,
            self.exp_delay_var,
            self.exp_decay_var,
        ):
            variable.trace_add(
                "write", lambda *_args: self._refresh_audio_profile_summaries()
            )
        self._refresh_audio_profile_summaries()

        # === 3. ПАНЕЛЬ КНОПОК (В ДВА РЯДА) ===
        self.export_mid_frame = ttk.Frame(frame)
        self.export_mid_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(3, 2))

        mid_row1 = ttk.Frame(self.export_mid_frame)
        mid_row1.pack(fill=tk.X, pady=1)
        self.btn_export_add_group = ttk.Button(mid_row1, text="📁 Добавить группу", command=self.add_export_group)
        self.btn_export_add_group.pack(side=tk.LEFT, padx=1)
        self.btn_export_add_folder = ttk.Button(mid_row1, text="📂 Добавить папку", command=self.add_export_folder)
        self.btn_export_add_folder.pack(side=tk.LEFT, padx=1)
        self.btn_export_add_files = ttk.Button(mid_row1, text="🎵 Добавить аудио", command=self.add_export_files)
        self.btn_export_add_files.pack(side=tk.LEFT, padx=1)
        self.btn_export_group_selected = ttk.Button(
            mid_row1,
            text="🔗 Объединить",
            command=self.merge_selected_export_items,
        )
        self.btn_export_group_selected.pack(side=tk.LEFT, padx=(10, 1))
        # ИСПРАВЛЕНИЕ: Разгруппировать перенесено в первый ряд!
        self.btn_export_ungroup = ttk.Button(mid_row1, text="📤 Разгруппировать", command=self.ungroup_export_items)
        self.btn_export_ungroup.pack(side=tk.LEFT, padx=1)
        
        mid_row2 = ttk.Frame(self.export_mid_frame)
        mid_row2.pack(fill=tk.X, pady=1)
        self.btn_export_remove = ttk.Button(mid_row2, text="➖ Удалить", command=self.remove_export_items)
        self.btn_export_remove.pack(side=tk.LEFT, padx=1)
        self.btn_export_down = ttk.Button(mid_row2, text="⬇", width=3, command=lambda: self.move_export_item(1))
        self.btn_export_down.pack(side=tk.LEFT, padx=1)
        self.btn_export_up = ttk.Button(mid_row2, text="⬆", width=3, command=lambda: self.move_export_item(-1))
        self.btn_export_up.pack(side=tk.LEFT, padx=1)
        self.btn_export_auto_split = ttk.Button(mid_row2, text="⏱ Авто-разбивка", command=self.auto_split_export)
        self.btn_export_auto_split.pack(side=tk.LEFT, padx=(10, 1))
        self.btn_export_clear = ttk.Button(
            mid_row2,
            text="🧹 Очистить всё",
            command=self.clear_export_project,
        )
        self.btn_export_clear.pack(side=tk.RIGHT, padx=1)

        # === 4. ВЕРХНЯЯ ПАНЕЛЬ С ДЕРЕВОМ ===
        # Изменены веса, чтобы дерево занимало больше места (weight=4 vs weight=1)
        self.export_top_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        self.export_top_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        top_pane = self.export_top_pane
        
        tree_frame = ttk.Frame(top_pane)
        top_pane.add(tree_frame, weight=4)
        
        # ДОБАВЛЕНО ОБЩЕЕ ВРЕМЯ
        lbl_tree_header = ttk.Frame(tree_frame)
        lbl_tree_header.pack(fill=tk.X)
        ttk.Label(lbl_tree_header, text="Группы и файлы:").pack(side=tk.LEFT)
        self.lbl_export_total_time = ttk.Label(lbl_tree_header, text="Общее время: 00:00", font=("", 9, "bold"), foreground=self.get_status_color("info"))
        self.lbl_export_total_time.pack(side=tk.RIGHT, padx=5)

        self.export_tree = ttk.Treeview(tree_frame, columns=("duration",), selectmode="extended", height=5)
        if sys.platform == "darwin":
            self._bind_mac_treeview_clicks(self.export_tree)
        self.export_tree.bind("<Control-a>", self._tree_select_all)
        self.export_tree.bind("<Command-a>", self._tree_select_all)
        
        self.export_tree.heading("#0", text="Имя ↕", command=lambda: self._sort_export_tree("#0", False))
        self.export_tree.heading("duration", text="Длительность")
        self.export_tree.column("duration", width=100, anchor=tk.CENTER, stretch=False)
        self.export_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.export_tree.yview)
        self.export_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.export_tree.bind("<<TreeviewSelect>>", self.on_export_tree_select)
        
        self.group_settings_frame = ttk.LabelFrame(top_pane, text="Настройки", padding=5)
        top_pane.add(self.group_settings_frame, weight=1)
        
        tmpl_frame = ttk.Frame(self.group_settings_frame)
        tmpl_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))
        ttk.Label(tmpl_frame, text="Шаблон группы:").pack(side=tk.LEFT)
        self.settings_vars["default_group_name"] = tk.StringVar(value=self.config.get("default_group_name", "Том {num}"))
        self.settings_vars["default_group_name"].trace_add(
            "write", self._schedule_settings_save
        )
        ttk.Entry(tmpl_frame, textvariable=self.settings_vars["default_group_name"]).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.grp_notebook = ttk.Notebook(self.group_settings_frame)
        self.grp_notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        
        self.grp_tab_basic = ttk.Frame(self.grp_notebook, padding=5)
        self.grp_tab_tags = ttk.Frame(self.grp_notebook, padding=5)
        self.grp_notebook.add(self.grp_tab_basic, text="Основные")
        self.grp_notebook.add(self.grp_tab_tags, text="Теги")

        # Все основные поля помещаются в пять компактных строк. ttk-only
        # разметка не создаёт непрозрачного Canvas, который на Aqua/Tk 9 мог
        # выглядеть белой полосой на фоне нативной вкладки.
        self.grp_basic_content = ttk.Frame(self.grp_tab_basic)
        self.grp_basic_content.pack(fill=tk.BOTH, expand=True)
        self.grp_basic_content.columnconfigure(1, weight=1)
        self.grp_basic_content.columnconfigure(3, weight=1)

        self.lbl_grp_name = ttk.Label(
            self.grp_basic_content, text="Имя группы / Название трека:"
        )
        self.lbl_grp_name.grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        self.grp_name_var = tk.StringVar()
        self.grp_name_var.trace_add("write", self.save_export_item_settings)
        ttk.Entry(
            self.grp_basic_content, textvariable=self.grp_name_var
        ).grid(row=0, column=1, columnspan=3, sticky=tk.EW, pady=2)
        
        self.grp_merge_var = tk.BooleanVar()
        self.grp_merge_var.trace_add("write", self.save_export_item_settings)
        self.grp_merge_var.trace_add(
            "write", lambda *_args: self._sync_group_subfolder_state()
        )
        flags_row = ttk.Frame(self.grp_basic_content)
        flags_row.grid(
            row=1, column=0, columnspan=4, sticky=tk.W, pady=2
        )
        self.chk_merge = ttk.Checkbutton(
            flags_row,
            text="Склеить файлы в один трек",
            variable=self.grp_merge_var,
        )
        self.chk_merge.pack(side=tk.LEFT)
        
        self.grp_subfolder_var = tk.BooleanVar(value=True)
        self.grp_subfolder_var.trace_add("write", self.save_export_item_settings)
        self.chk_subfolder = ttk.Checkbutton(
            flags_row,
            text="Сохранять в подпапку",
            variable=self.grp_subfolder_var,
        )
        self.chk_subfolder.pack(side=tk.LEFT, padx=(6, 0))
        self.lbl_subfolder_hint = self._register_palette_widget(
            ttk.Label(
                self.grp_basic_content,
                text="Доступно только когда файлы группы не склеиваются.",
                foreground=self.get_status_color("muted"),
                font=("", 8, "italic"),
                wraplength=390,
            ),
            "muted",
        )
        self.lbl_subfolder_hint.grid(
            row=2, column=0, columnspan=4, sticky=tk.W, pady=(0, 2)
        )
        
        ttk.Label(
            self.grp_basic_content, text="Пауза между файлами (мс):"
        ).grid(row=3, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        self.grp_pause_var = tk.IntVar()
        self.grp_pause_var.trace_add("write", self.save_export_item_settings)
        self.ent_pause = ttk.Entry(self.grp_basic_content, textvariable=self.grp_pause_var, width=10)
        self.ent_pause.grid(row=3, column=1, sticky=tk.W, pady=2)
        
        self.btn_mass_apply_basic = ttk.Button(self.grp_basic_content, text="⚙️ Применить ко всем группам...", command=self.open_mass_apply_dialog)
        self.btn_mass_apply_basic.grid(
            row=4, column=0, columnspan=4, sticky=tk.W, pady=(4, 2)
        )
        
        # -- Вкладка: Теги --
        tag_grid = ttk.Frame(self.grp_tab_tags)
        tag_grid.pack(fill=tk.X, pady=5)
        
        def add_grp_tag(parent, label, var_name, r, c):
            ttk.Label(parent, text=label).grid(row=r, column=c, sticky=tk.W, pady=2, padx=2)
            var = tk.StringVar()
            var.trace_add("write", self.save_export_item_settings)
            setattr(self, var_name, var)
            ttk.Entry(parent, textvariable=var).grid(row=r, column=c+1, sticky="ew", pady=2, padx=5)

        add_grp_tag(tag_grid, "Исполнитель:", "grp_artist_var", 0, 0)
        add_grp_tag(tag_grid, "Исп. альбома:", "grp_album_artist_var", 1, 0)
        add_grp_tag(tag_grid, "Альбом:", "grp_album_var", 2, 0)
        
        add_grp_tag(tag_grid, "Жанр:", "grp_genre_var", 0, 2)
        add_grp_tag(tag_grid, "Композитор:", "grp_composer_var", 1, 2)
        add_grp_tag(tag_grid, "Год:", "grp_year_var", 2, 2)
        
        tag_grid.columnconfigure(1, weight=1)
        tag_grid.columnconfigure(3, weight=1)
        
        ttk.Label(self.grp_tab_tags, text="Обложка (путь к jpg/png):").pack(anchor=tk.W, pady=(2, 0))
        cov_frame = ttk.Frame(self.grp_tab_tags)
        cov_frame.pack(fill=tk.X)
        self.grp_cover_var = tk.StringVar()
        self.grp_cover_var.trace_add("write", self.save_export_item_settings)
        ttk.Entry(cov_frame, textvariable=self.grp_cover_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        def choose_grp_cov():
            init_dir = self._get_smart_dir(self.grp_cover_var.get(), is_file=True)
            res = filedialog.askopenfilename(initialdir=init_dir, filetypes=[("Images", "*.jpg *.jpeg *.png")])
            if res: self.grp_cover_var.set(res)
        ttk.Button(cov_frame, text="📁", width=3, command=choose_grp_cov).pack(side=tk.RIGHT)
        
        ttk.Separator(self.grp_tab_tags, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)
        
        btn_grid = ttk.Frame(self.grp_tab_tags)
        btn_grid.pack(fill=tk.X, pady=2)
        btn_grid.columnconfigure(0, weight=1)
        btn_grid.columnconfigure(1, weight=1)

        self.btn_apply_to_group_files = ttk.Button(btn_grid, text="⬇ К файлам группы", command=lambda: self.apply_tags_mass("group_files"))
        self.btn_apply_to_group_files.grid(row=0, column=0, sticky="ew", padx=1, pady=1)

        self.btn_apply_to_parent = ttk.Button(btn_grid, text="⬆ В род. группу", command=lambda: self.apply_tags_mass("parent_group"))
        self.btn_apply_to_parent.grid(row=0, column=1, sticky="ew", padx=1, pady=1)

        self.btn_apply_to_selected = ttk.Button(btn_grid, text="☑ К выделенным", command=lambda: self.apply_tags_mass("selected"))
        self.btn_apply_to_selected.grid(row=1, column=0, sticky="ew", padx=1, pady=1)

        self.btn_apply_to_all = ttk.Button(btn_grid, text="🔄 Ко всем элементам", command=lambda: self.apply_tags_mass("all"))
        self.btn_apply_to_all.grid(row=1, column=1, sticky="ew", padx=1, pady=1)
        
        self.current_selected_export_item = None
        self._disable_export_settings()
        
    # --- Логика интерфейса Сборщика ---
    def _sync_export_mode_controls(self):
        """Восстанавливает специальные состояния полей режима экспорта."""
        if getattr(self, "_export_running", False):
            return
        tags_only = bool(self.export_tags_only_var.get())
        normal_state = tk.DISABLED if tags_only else tk.NORMAL
        combo_state = tk.DISABLED if tags_only else "readonly"
        self.btn_export_dir.configure(state=normal_state)
        self.ent_export_dir.configure(state=normal_state)
        self.cb_export_fmt.configure(state=combo_state)
        fmt = str(self.export_fmt_var.get()).strip().lower()
        bitrate_state = (
            tk.DISABLED if tags_only or fmt == "wav" else "readonly"
        )
        self.cb_export_bitrate.configure(state=bitrate_state)
        self.cb_export_sample_rate.configure(state=combo_state)
        self.cb_export_channels.configure(state=combo_state)
        self.chk_export_fx.configure(state=normal_state)
        for control in getattr(self, "export_fx_value_controls", ()):
            control.configure(state=normal_state)
        audio_profiles_button = getattr(self, "btn_audio_profiles_export", None)
        if audio_profiles_button is not None:
            audio_profiles_button.configure(state=normal_state)
        self.btn_export_start.configure(state=tk.NORMAL)
        self.btn_export_stop.configure(state=tk.DISABLED)
        self.chk_export_tags_only.configure(state=tk.NORMAL)

    def _sync_group_subfolder_state(self):
        """Блокирует подпапку для склеенной группы и поясняет причину."""
        if not hasattr(self, "chk_subfolder"):
            return
        item = getattr(self, "current_selected_export_item", None)
        editable_group = bool(item and item in self.export_groups)
        enabled = (
            editable_group
            and not bool(self.grp_merge_var.get())
            and not getattr(self, "_export_running", False)
            and not getattr(self, "_export_lock", False)
        )
        self.chk_subfolder.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        if hasattr(self, "lbl_subfolder_hint"):
            if not editable_group:
                hint = "Настройка доступна только для группы."
            elif bool(self.grp_merge_var.get()):
                hint = "Недоступно: файлы группы склеиваются в один трек."
            else:
                hint = "Отдельные файлы будут сохранены в папку с именем группы."
            self.lbl_subfolder_hint.configure(text=hint)

    def _disable_export_settings(self):
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            self._set_descendant_state(tab, tk.DISABLED)
                
        # Явно отключаем вложенные элементы
        try:
            self.ent_pause.configure(state=tk.DISABLED)
            self.btn_mass_apply_basic.configure(state=tk.DISABLED) # ИСПРАВЛЕНО
            self.btn_apply_to_group_files.configure(state=tk.DISABLED)
            self.btn_apply_to_parent.configure(state=tk.DISABLED)
            self.btn_apply_to_selected.configure(state=tk.DISABLED)
            self.btn_apply_to_all.configure(state=tk.DISABLED)
        except (AttributeError, tk.TclError):
            pass

    def _enable_export_settings(self, item):
        is_group = item in self.export_groups
        parent = self.export_tree.parent(item)
        is_group_file = (not is_group) and bool(parent) # Файл внутри группы
        
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            self._set_descendant_state(tab, tk.NORMAL)
                
        # Явно включаем общие элементы
        try:
            self.ent_pause.configure(state=tk.NORMAL)
            self.btn_apply_to_selected.configure(state=tk.NORMAL)
            self.btn_apply_to_all.configure(state=tk.NORMAL)
        except (AttributeError, tk.TclError):
            pass
        
        if not is_group:
            self.chk_merge.configure(state=tk.DISABLED)
            self.chk_subfolder.configure(state=tk.DISABLED)
            self.ent_pause.configure(state=tk.DISABLED)
            self.btn_mass_apply_basic.configure(state=tk.DISABLED) # ИСПРАВЛЕНО: Блокируем массовое применение для одиночного файла
            self.lbl_grp_name.config(text="Название трека (Title):")
            self.group_settings_frame.config(text="Настройки файла")
            
            # Логика кнопок для файла
            self.btn_apply_to_group_files.configure(state=tk.DISABLED)
            if is_group_file:
                self.btn_apply_to_parent.configure(state=tk.NORMAL)
            else:
                self.btn_apply_to_parent.configure(state=tk.DISABLED) # Файл в корне
        else:
            self.btn_mass_apply_basic.configure(state=tk.NORMAL) # ИСПРАВЛЕНО: Включаем для группы
            self.lbl_grp_name.config(text="Имя группы (имя файла/папки):")
            self.group_settings_frame.config(text="Настройки группы")
            
            # Логика кнопок для группы
            self.btn_apply_to_group_files.configure(state=tk.NORMAL)
            self.btn_apply_to_parent.configure(state=tk.DISABLED)
        self._sync_group_subfolder_state()

    def on_export_tree_select(self, event):
        if (
            getattr(self, "_export_lock", False)
            or getattr(self, "_export_running", False)
        ):
            return
        selected = self.export_tree.selection()
        if not selected:
            self.current_selected_export_item = None
            self._disable_export_settings()
            return
            
        item = selected[0]
        is_group = item in self.export_groups
        settings = self.export_groups.get(item) if is_group else self.export_files.get(item)
        if settings is None:
            self.current_selected_export_item = None
            self._disable_export_settings()
            return

        self._is_updating_ui = True
        try:
            self.current_selected_export_item = item

            # Передаём сам item, чтобы функция различила файл, группу и корень.
            self._enable_export_settings(item)

            self.grp_name_var.set(settings.get("name" if is_group else "title", ""))
            self.grp_artist_var.set(settings.get("artist", ""))
            self.grp_album_var.set(settings.get("album", ""))
            self.grp_album_artist_var.set(settings.get("album_artist", ""))
            self.grp_genre_var.set(settings.get("genre", ""))
            self.grp_composer_var.set(settings.get("composer", ""))
            self.grp_year_var.set(settings.get("year", ""))
            self.grp_cover_var.set(settings.get("cover", ""))

            if is_group:
                self.grp_merge_var.set(settings.get("merge", True))
                self.grp_subfolder_var.set(settings.get("subfolder", True))
                self.grp_pause_var.set(settings.get("pause", 1000))
        finally:
            self._is_updating_ui = False
        self._sync_group_subfolder_state()

    def save_export_item_settings(self, *args):
        if (
            getattr(self, '_is_updating_ui', False)
            or getattr(self, '_export_running', False)
            or getattr(self, '_export_lock', False)
        ):
            return
        item = self.current_selected_export_item
        if not item or not self.export_tree.exists(item): return
        
        is_group = item in self.export_groups
        target_dict = self.export_groups if is_group else self.export_files
        
        if is_group:
            target_dict[item]["name"] = self.grp_name_var.get()
            target_dict[item]["merge"] = self.grp_merge_var.get()
            target_dict[item]["subfolder"] = self.grp_subfolder_var.get()
            try:
                target_dict[item]["pause"] = self.grp_pause_var.get()
            except tk.TclError: pass
        else:
            target_dict[item]["title"] = self.grp_name_var.get()
            
        target_dict[item]["artist"] = self.grp_artist_var.get()
        target_dict[item]["album"] = self.grp_album_var.get()
        target_dict[item]["album_artist"] = self.grp_album_artist_var.get()
        target_dict[item]["genre"] = self.grp_genre_var.get()
        target_dict[item]["composer"] = self.grp_composer_var.get()
        target_dict[item]["year"] = self.grp_year_var.get()
        target_dict[item]["cover"] = self.grp_cover_var.get()
        
        self.export_tree.item(item, text=self.grp_name_var.get())

    def apply_pause_mass(self):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        try:
            val = self.grp_pause_var.get()
        except tk.TclError:
            return # Игнорируем, если введено не число
            
        # Сохраняем как дефолтное значение
        self.config["default_group_pause"] = val
        self.save_settings()
        
        # Применяем ко всем существующим группам
        for g_id in self.export_groups:
            self.export_groups[g_id]["pause"] = val
            
        self._show_info("Успех", f"Пауза {val} мс применена ко всем группам и сохранена как значение по умолчанию!")

    def update_total_export_duration(self):
        """Пересчитывает и отображает общее время всех файлов в дереве экспорта"""
        total_sec = 0.0
        # Считаем только файлы, чтобы избежать двойного счета групп
        for f_id, f_data in self.export_files.items():
            if self.export_tree.exists(f_id):
                dur_str = self.export_tree.item(f_id, "values")[0]
                parts = dur_str.split(':')
                if len(parts) == 3: total_sec += int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                elif len(parts) == 2: total_sec += int(parts[0])*60 + int(parts[1])
        
        self.lbl_export_total_time.config(text=f"Общее время: {self.format_duration(total_sec)}")


    def open_mass_apply_dialog(self):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        if not self.current_selected_export_item or self.current_selected_export_item not in self.export_groups:
            self._show_warning("Внимание", "Выберите группу-эталон, настройки которой вы хотите применить к остальным.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.withdraw() # 👈 1. Мгновенно прячем окно от глаз!
        
        dialog.title("Массовое применение")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Какие настройки текущей группы\nприменить ко всем остальным группам?", justify=tk.CENTER).pack(pady=10)
        
        var_merge = tk.BooleanVar(value=True)
        var_subfolder = tk.BooleanVar(value=True)
        var_pause = tk.BooleanVar(value=True)
        var_save_default = tk.BooleanVar(value=False)
        
        ttk.Checkbutton(dialog, text="Склеить файлы в один трек", variable=var_merge).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="Сохранять в подпапку", variable=var_subfolder).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="Пауза между файлами", variable=var_pause).pack(anchor=tk.W, padx=30, pady=2)
        
        ttk.Separator(dialog, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10, padx=10)
        ttk.Checkbutton(dialog, text="Сохранить эти значения по умолчанию", variable=var_save_default).pack(anchor=tk.W, padx=30, pady=2)
        
        def apply_changes():
            val_merge = self.grp_merge_var.get()
            val_subfolder = self.grp_subfolder_var.get()
            try:
                val_pause = self.grp_pause_var.get()
            except tk.TclError:
                val_pause = 1000
            
            for g_id, g_data in self.export_groups.items():
                if var_merge.get(): g_data["merge"] = val_merge
                if var_subfolder.get(): g_data["subfolder"] = val_subfolder
                if var_pause.get(): g_data["pause"] = val_pause
                
            if var_save_default.get():
                if var_pause.get(): self.config["default_group_pause"] = val_pause
                self.save_settings()
                
            dialog.destroy()
            self._show_info("Успех", "Настройки успешно применены ко всем группам!")
            
        ttk.Button(dialog, text="Применить", command=apply_changes).pack(pady=15)
        
        # 👈 2. Проявляем окно уже готовым и отцентрированным в самом конце!
        self._center_popup(dialog, 350, 250)

    def apply_tags_mass(self, scope="group_files"):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        if not self.current_selected_export_item: return
        
        artist = self.grp_artist_var.get()
        album_artist = self.grp_album_artist_var.get()
        album = self.grp_album_var.get()
        genre = self.grp_genre_var.get()
        composer = self.grp_composer_var.get()
        year = self.grp_year_var.get()
        cover = self.grp_cover_var.get()
        
        item = self.current_selected_export_item
        is_group = item in self.export_groups
        parent_g_id = item if is_group else self.export_tree.parent(item)
        
        def apply_to_item(i_id):
            target = self.export_groups if i_id in self.export_groups else self.export_files
            target[i_id]["artist"] = artist
            target[i_id]["album"] = album
            target[i_id]["album_artist"] = album_artist
            target[i_id]["genre"] = genre
            target[i_id]["composer"] = composer
            target[i_id]["year"] = year
            target[i_id]["cover"] = cover

        if scope == "selected":
            selected = self.export_tree.selection()
            for sel_id in selected:
                apply_to_item(sel_id)
            self._show_info("Успех", "Теги применены ко всем выделенным элементам!")
            
        elif scope == "parent_group" and not is_group:
            if parent_g_id in self.export_groups:
                apply_to_item(parent_g_id)
            self._show_info("Успех", "Теги скопированы в родительскую группу!")
            
        elif scope == "group_files":
            if parent_g_id:
                for f_id in self.export_tree.get_children(parent_g_id):
                    apply_to_item(f_id)
            self._show_info("Успех", "Теги применены ко всем файлам в группе!")
            
        elif scope == "all":
            # 1. Применяем к группам и их файлам
            for g_id in self.export_groups:
                apply_to_item(g_id)
                for f_id in self.export_tree.get_children(g_id):
                    apply_to_item(f_id)
            # 2. Применяем к одиночным файлам в корне
            for f_id in self.export_tree.get_children(""):
                if f_id in self.export_files:
                    apply_to_item(f_id)
            self._show_info("Успех", "Теги применены абсолютно ко всем группам и файлам!")

    def move_export_item(self, direction):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        selected = self.export_tree.selection()
        if not selected: return
        ordered = list(selected)
        if direction > 0:
            ordered.reverse()
        affected_groups = set()
        for item in ordered:
            parent = self.export_tree.parent(item)
            idx = self.export_tree.index(item)
            siblings = self.export_tree.get_children(parent)
            new_index = max(0, min(len(siblings) - 1, idx + direction))
            self.export_tree.move(item, parent, new_index)
            if parent in self.export_groups:
                affected_groups.add(parent)
        for group_id in affected_groups:
            self.update_group_duration(group_id)

    def merge_selected_export_items(self):
        """Объединяет группы и/или файлы без изменения аудио на диске."""
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        selected_items = tuple(self.export_tree.selection())
        if not selected_items:
            self._show_warning(
                "Внимание",
                "Выделите группы или аудиофайлы для объединения.",
            )
            return

        plan = plan_export_merge(
            self.export_tree.get_children(""),
            {
                group_id: self.export_tree.get_children(group_id)
                for group_id in self.export_groups
            },
            selected_items,
            self.export_groups,
            self.export_files,
        )
        target_group = plan["target_group"]
        source_groups = plan["source_groups"]
        files_to_move = plan["file_ids"]

        selected_groups = tuple(
            group_id
            for group_id in (target_group, *source_groups)
            if group_id is not None
        )
        unknown_children = [
            child_id
            for group_id in selected_groups
            for child_id in self.export_tree.get_children(group_id)
            if child_id not in self.export_files
        ]
        if unknown_children:
            self._show_error(
                "Не удалось объединить",
                "Структура списка временно рассинхронизирована. Обновите "
                "список или перезапустите приложение; слияние не выполнялось.",
            )
            logging.error(
                "Слияние экспорта отменено: неизвестные дочерние узлы %s",
                unknown_children,
            )
            return

        roots = tuple(self.export_tree.get_children(""))
        root_positions = {item_id: index for index, item_id in enumerate(roots)}
        contributing_positions = [
            root_positions[group_id]
            for group_id in selected_groups
            if group_id in root_positions
        ]
        for file_id in files_to_move:
            parent_id = self.export_tree.parent(file_id)
            root_id = parent_id or file_id
            if root_id in root_positions:
                contributing_positions.append(root_positions[root_id])

        file_needs_move = any(
            self.export_tree.exists(file_id)
            and self.export_tree.parent(file_id) != target_group
            for file_id in files_to_move
        )
        if target_group is not None and not source_groups and not file_needs_move:
            self._show_info(
                "Объединять нечего",
                (
                    "В выбранной группе нет аудиофайлов."
                    if not files_to_move
                    else (
                        "Выбрана только одна готовая группа — объединять нечего."
                        if selected_items == (target_group,)
                        else "Все выбранные файлы уже находятся в этой группе."
                    )
                ),
            )
            return
        if target_group is None and not files_to_move:
            self._show_warning(
                "Пусто",
                "В выбранных элементах нет аудиофайлов.",
            )
            return

        if source_groups:
            target_name = self.export_groups.get(target_group, {}).get(
                "name", "целевая группа"
            )
            if not self._ask_yes_no(
                "Объединить группы",
                f"Сохранить имя, теги и настройки группы «{target_name}» "
                f"и перенести в неё выбранные файлы?\n\n"
                f"Остальные выбранные группы ({len(source_groups)}) "
                "будут удалены из проекта. Порядок файлов будет взят из "
                "текущего дерева, а итоговая группа займёт место первого "
                "выбранного источника. Файлы на диске не изменятся.",
                icon="warning",
            ):
                return

        if target_group is None:
            target_group = self.add_export_group()
            if target_group is None:
                return
        if contributing_positions:
            self.export_tree.move(target_group, "", min(contributing_positions))

        affected_groups = set()
        for file_id in files_to_move:
            if not self.export_tree.exists(file_id):
                continue
            previous_parent = self.export_tree.parent(file_id)
            if previous_parent in self.export_groups and previous_parent != target_group:
                affected_groups.add(previous_parent)
            self.export_tree.move(file_id, target_group, tk.END)

        for group_id in source_groups:
            if not self.export_tree.exists(group_id):
                self.export_groups.pop(group_id, None)
                continue
            if self.export_tree.get_children(group_id):
                logging.warning(
                    "Группа %s не удалена после слияния: остались неизвестные дочерние узлы",
                    group_id,
                )
                continue
            self.export_groups.pop(group_id, None)
            self.export_tree.delete(group_id)

        for group_id in affected_groups:
            if group_id != target_group and self.export_tree.exists(group_id):
                self.update_group_duration(group_id)

        self.update_group_duration(target_group)
        self.current_selected_export_item = None
        self.export_tree.selection_set(target_group)
        self.export_tree.focus(target_group)
        self.on_export_tree_select(None)

    def group_selected_into_new(self):
        """Совместимое имя прежней команды для внешних вызовов."""
        return self.merge_selected_export_items()
    
    def add_export_group(self, name=None):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return None
        g_id = f"group_{uuid.uuid4().hex[:8]}"
        
        if not name:
            template = self.settings_vars["default_group_name"].get()
            g_name = next_sequence_name(
                template,
                (group.get("name", "") for group in self.export_groups.values()),
            )
        else:
            g_name = name
            
        self.export_groups[g_id] = {
            "name": g_name, "merge": True, "subfolder": True, 
            "pause": self.config.get("default_group_pause", 1000),
            "artist": "", "album": "","album_artist": "", "genre": "", "composer": "", "year": "", "cover": ""
        }
        self.export_tree.insert("", tk.END, iid=g_id, text=g_name, open=True)
        self.export_tree.selection_set(g_id)
        return g_id

    def _set_export_ui_state(self, state):
        """Блокирует редактирование дерева во время фонового чтения файлов."""
        if hasattr(self, 'export_mid_frame'):
            self._set_descendant_state(self.export_mid_frame, state)
        if hasattr(self, "export_tree"):
            try:
                self.export_tree.state(("disabled",) if state == tk.DISABLED else ("!disabled",))
            except tk.TclError:
                pass
        if hasattr(self, "group_settings_frame"):
            self._set_descendant_state(self.group_settings_frame, state)
        if hasattr(self, "btn_export_start"):
            self.btn_export_start.configure(state=state)
        if state != tk.DISABLED:
            self._sync_export_mode_controls()
            selected = self.export_tree.selection() if hasattr(self, "export_tree") else ()
            if selected:
                self.on_export_tree_select(None)
            else:
                self.current_selected_export_item = None
                self._disable_export_settings()

    def _set_export_running_state(self, running):
        """Замораживает весь изменяемый проект до завершения сборки."""
        self._export_running = bool(running)
        state = tk.DISABLED if running else tk.NORMAL
        self._set_descendant_state(self.export_mid_frame, state)
        self._set_descendant_state(
            self.export_frame,
            state,
            skip=(self.btn_export_stop,) if running else (),
        )
        self._set_descendant_state(self.group_settings_frame, state)
        try:
            self.export_tree.state(("disabled",) if running else ("!disabled",))
        except tk.TclError:
            pass

        if running:
            self.current_selected_export_item = None
            self._disable_export_settings()
            self.btn_export_start.configure(state=tk.DISABLED)
            self.btn_export_stop.configure(state=tk.NORMAL)
        else:
            self.btn_export_stop.configure(state=tk.DISABLED)
            self._sync_export_mode_controls()
            selected = self.export_tree.selection()
            if selected:
                self.on_export_tree_select(None)
            else:
                self.current_selected_export_item = None
                self._disable_export_settings()

    def add_export_files(self, files=None, target_group=None):
        if getattr(self, '_export_lock', False) or getattr(self, '_export_running', False):
            return
        
        if files is None:
            # Берём последний зафиксированный путь
            last_dir = self.config.get("last_browse_dir", "")
            init_dir = resolve_dialog_initial_dir(last_dir, BASE_DIR)
            
            files = filedialog.askopenfilenames(
                initialdir=init_dir,
                filetypes=[("Audio Files", "*.mp3 *.wav *.ogg *.opus")]
            )
        if not files: return

        # Запоминаем папку первого выбранного файла
        chosen_dir = str(Path(files[0]).parent)
        self.config["last_browse_dir"] = chosen_dir

        if not self.export_outdir_var.get().strip():
            self.export_outdir_var.set(chosen_dir)
            self.config["export_dir"] = chosen_dir
            
        self.save_settings()

        self._export_lock = True
        self._set_export_ui_state(tk.DISABLED)
        
        try:
            if target_group is None:
                selected = self.export_tree.selection()
                if selected:
                    sel_item = selected[0]
                    if sel_item in self.export_groups:
                        target_group = sel_item # Если выделена группа -> добавляем в эту группу
                    else:
                        target_group = self.export_tree.parent(sel_item) # Если выделен файл -> берем его родителя (если файл в корне, вернет "")
                else:
                    target_group = ""  # Ничего не выделено -> добавляем в корень!
            
            existing_paths = set()
            for child in self.export_tree.get_children(target_group):
                if child in self.export_files:
                    existing_paths.add(self.export_files[child]["path"])

            # Убираем уже добавленные и повторно выбранные пути заранее. Тогда
            # worker всегда видит точный total_files и гарантированно публикует
            # последнюю неполную пачку, даже если исходный список заканчивался
            # существующим элементом.
            files = unique_new_file_paths(files, existing_paths)

            self._set_export_status(
                "Чтение тегов и извлечение обложек…", "warning"
            )
            
            # === ФОНОВЫЙ ПОТОК ===
            def run_import():
                try:
                    added_count = 0
                    total_files = len(files)
                    batch_data = [] # Накопитель для пакетной отрисовки
                    
                    for i, f in enumerate(files):
                        meta = self.get_audio_metadata(f)
                        f_id = f"file_{uuid.uuid4().hex[:8]}"
                        batch_data.append((f_id, f, meta))
                        added_count += 1
                        
                        # Отправляем пачку в UI каждые 10 файлов ИЛИ если это последний файл
                        if len(batch_data) >= 10 or (i + 1) == total_files:
                            # ИСПРАВЛЕНИЕ: Передаем аргументы явно, чтобы избежать проблемы замыканий Python
                            def update_ui(batch, curr):
                                for fid, fp, m in batch:
                                    self.export_files[fid] = {
                                        "path": fp, "title": m["title"], "artist": m["artist"],
                                        "album": m["album"], "album_artist": m["album_artist"],
                                        "genre": m["genre"], "composer": m["composer"],
                                        "year": m["year"], "cover": m["cover"]
                                    }
                                    self.export_tree.insert(target_group, tk.END, iid=fid, text=m["title"], values=(self.format_duration(m["duration"]),))
                                
                                self._set_export_status(
                                    f"Добавлено {curr}/{total_files}…", "warning"
                                )
                            
                            self._post_to_ui(update_ui, batch_data.copy(), i + 1)
                            batch_data.clear() # Очищаем накопитель для следующей пачки
                        
                    # Финализация
                    def finish_ui():
                        if target_group != "":
                            self.update_group_duration(target_group)
                        
                        self.update_total_export_duration()
                        
                        if added_count == 0:
                            self._set_export_status(
                                "Файлы уже присутствуют.", "info"
                            )
                        else:
                            self._set_export_status("Ожидание...", "info")
                            
                        self._export_lock = False
                        self._export_import_thread = None
                        self._set_export_ui_state(tk.NORMAL)
                        
                    self._post_to_ui(finish_ui)
                    
                except Exception as e:
                    logging.error(f"Ошибка при добавлении файлов: {e}")
                    def fail_ui():
                        self._set_export_status(
                            "Ошибка при добавлении!", "error"
                        )
                        self._export_lock = False
                        self._export_import_thread = None
                        self._set_export_ui_state(tk.NORMAL)
                    self._post_to_ui(fail_ui)

            self._export_import_thread = threading.Thread(
                target=run_import, daemon=True
            )
            self._export_import_thread.start()
            
        except Exception as e:
            logging.error(f"Ошибка инициализации импорта: {e}")
            self._export_import_thread = None
            self._export_lock = False
            self._set_export_ui_state(tk.NORMAL)

    def add_export_folder(self):
        if getattr(self, '_export_lock', False) or getattr(self, '_export_running', False):
             self._show_warning("Занято", "Дождитесь окончания предыдущего импорта файлов.")
             return
             
        # Берём последний зафиксированный путь
        last_dir = self.config.get("last_browse_dir", "")
        init_dir = resolve_dialog_initial_dir(last_dir, BASE_DIR)
        
        folder = filedialog.askdirectory(initialdir=init_dir)
        if not folder: return
        
        # Запоминаем родительскую папку выбранной директории
        self.config["last_browse_dir"] = str(Path(folder).parent)
        self.save_settings()
        
        create_group = self._ask_yes_no("Добавление папки", f"Создать отдельную группу для папки '{Path(folder).name}'?\n\nДа - создать группу\nНет - добавить файлы в корень (или текущую группу)")
        
        files = sorted(
            [
                str(path)
                for path in Path(folder).glob("*.*")
                if path.suffix.lower() in {".mp3", ".wav", ".ogg", ".opus"}
            ],
            key=self._natural_sort_key,
        )
        if not files:
            self._show_info("Пусто", "В папке нет аудиофайлов.")
            return
            
        target_group = self.add_export_group(name=Path(folder).name) if create_group else None
        self.add_export_files(files=files, target_group=target_group)

    def get_audio_metadata(self, filepath):
        """Читает длительность, теги и извлекает обложку через ffprobe/ffmpeg"""
        cmd = [get_ffprobe_path(), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", filepath]
        try:
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            # ИСПРАВЛЕНИЕ: Декодируем байты в строку для надежного парсинга JSON
            out = subprocess.check_output(cmd, startupinfo=startupinfo).decode('utf-8', errors='ignore')
            data = json.loads(out)
            
            # MP3 обычно показывает теги на уровне контейнера, тогда как Ogg
            # Vorbis/Opus FFmpeg возвращает VorbisComment/OpusTags у аудио-
            # потока. Читаем оба места, иначе при повторном импорте Opus в UI
            # визуально пропадали Album и другие уже записанные поля.
            tags = {
                k.lower(): v
                for k, v in data.get("format", {}).get("tags", {}).items()
            }
            for stream in data.get("streams", []):
                if stream.get("codec_type") != "audio":
                    continue
                for key, value in stream.get("tags", {}).items():
                    tags.setdefault(key.lower(), value)
            duration = float(data.get("format", {}).get("duration", 0.0))
            
            cover_path = ""
            # Ищем видеопоток (в аудиофайлах это обложка)
            has_cover = any(s.get("codec_type") == "video" for s in data.get("streams", []))
            if has_cover:
                covers_dir = SESSION_TEMP_DIR / "covers"
                covers_dir.mkdir(parents=True, exist_ok=True)
                cover_file = covers_dir / f"cover_{uuid.uuid4().hex}.jpg"
                
                # Извлекаем 1 кадр обложки
                ffmpeg_cmd = [get_ffmpeg_path(), "-y", "-i", filepath, "-an", "-vframes", "1", str(cover_file)]
                cover_result = subprocess.run(
                    ffmpeg_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo,
                )
                
                if cover_result.returncode == 0 and cover_file.exists():
                    cover_path = str(cover_file.resolve())
                else:
                    cover_file.unlink(missing_ok=True)

            # ИСПРАВЛЕНИЕ: Расширенный поиск ключей тегов (включая сырые ID3)
            title = tags.get("title", tags.get("tit2", Path(filepath).stem))
            artist = tags.get("artist", tags.get("tpe1", ""))
            album = tags.get("album", tags.get("talb", ""))
            album_artist = tags.get("album_artist", tags.get("albumartist", tags.get("tpe2", "")))
            genre = tags.get("genre", tags.get("tcon", ""))
            composer = tags.get("composer", tags.get("tcom", ""))
            year = tags.get("date", tags.get("year", tags.get("tyer", tags.get("tdrc", ""))))

            return {
                "duration": duration,
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "genre": genre,
                "composer": composer,
                "year": year,
                "cover": cover_path
            }
        except Exception as e:
            logging.error(f"Ошибка чтения метаданных {filepath}: {e}")
            return {"duration": 0.0, "title": Path(filepath).stem, "artist": "","album_artist":"","genre":"", "album": "", "composer": "", "year": "", "cover": ""}

    def format_duration(self, seconds):
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def update_group_duration(self, group_id):
        total_sec = 0.0
        for child in self.export_tree.get_children(group_id):
            dur_str = self.export_tree.item(child, "values")[0]
            # Простой парсинг обратно в секунды для суммы
            parts = dur_str.split(':')
            if len(parts) == 3: total_sec += int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
            elif len(parts) == 2: total_sec += int(parts[0])*60 + int(parts[1])
        self.export_tree.item(group_id, values=(self.format_duration(total_sec),))
        self.update_total_export_duration()

    def remove_export_items(self):
        """Безопасное удаление элементов с защитой от фантомных дочерних узлов"""
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        selected = self.export_tree.selection()
        groups_to_update = set()

        for item in selected:
            # 1. Защита: Если элемент уже был удален ранее (вместе со своей родитеской группой)
            if not self.export_tree.exists(item):
                if item in self.export_groups: del self.export_groups[item]
                if item in self.export_files: del self.export_files[item]
                continue

            # 2. Если это ГРУППА
            if item in self.export_groups:
                # Удаляем из памяти словаря все входящие в нее файлы
                for child in self.export_tree.get_children(item):
                    if child in self.export_files:
                        del self.export_files[child]
                del self.export_groups[item]
                self.export_tree.delete(item)

            # 3. Если это ОДИНОЧНЫЙ ФАЙЛ
            elif item in self.export_files:
                parent = self.export_tree.parent(item)
                if parent: groups_to_update.add(parent)
                del self.export_files[item]
                self.export_tree.delete(item)

        # Пересчитываем длительность для оставшихся групп и общее время
        for g in groups_to_update:
            if self.export_tree.exists(g): 
                self.update_group_duration(g)
                
        self.update_total_export_duration()
        self.current_selected_export_item = None
        self._disable_export_settings()

    def clear_export_project(self):
        """Очищает только текущий список, никогда не удаляя аудио с диска."""
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return

        root_items = tuple(self.export_tree.get_children(""))
        file_count = len(self.export_files)
        group_count = len(self.export_groups)
        if not root_items and not file_count and not group_count:
            self._show_info("Пусто", "Список экспорта уже пуст.")
            return

        if not self._ask_yes_no(
            "Очистить весь список",
            f"Убрать из проекта все группы ({group_count}) "
            f"и аудиофайлы ({file_count})?\n\n"
            "Сами файлы на диске не будут удалены.",
            icon="warning",
        ):
            return

        for item_id in root_items:
            if self.export_tree.exists(item_id):
                self.export_tree.delete(item_id)
        self.export_groups.clear()
        self.export_files.clear()
        self.group_counter = 0
        self.current_selected_export_item = None
        self._disable_export_settings()
        self.update_total_export_duration()
        self.export_progress["value"] = 0
        self._set_export_status("Ожидание...", "info")

    def auto_split_export(self):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return

        root_items = tuple(self.export_tree.get_children(""))
        group_children = {
            group_id: tuple(self.export_tree.get_children(group_id))
            for group_id in self.export_groups
        }
        all_files = ordered_export_file_ids(
            root_items, group_children, self.export_files.keys()
        )
                
        if not all_files:
            self._show_info("Пусто", "Сначала добавьте аудиофайлы.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.withdraw() # 👈 Прячем при создании
        dialog.title("Авто-разбивка")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Максимальная длительность группы (минут):").pack(pady=(10, 0))
        limit_var = tk.IntVar(value=60)
        ttk.Entry(dialog, textvariable=limit_var, justify=tk.CENTER).pack(pady=5)
        
        ttk.Label(dialog, text="Шаблон имени группы (доступно {num} или {num:0}):").pack(pady=(10, 0))
        template_var = tk.StringVar(value=self.settings_vars["default_group_name"].get())
        ttk.Entry(dialog, textvariable=template_var, justify=tk.CENTER).pack(fill=tk.X, padx=20, pady=5)
        
        def do_split():
            try:
                limit_minutes = limit_var.get()
                if limit_minutes <= 0:
                    raise ValueError
                limit_sec = limit_minutes * 60
            except (tk.TclError, TypeError, ValueError):
                self._show_error("Ошибка", "Введите положительное число минут!")
                return
            
            template = template_var.get().strip()
            if not template:
                self._show_error("Ошибка", "Шаблон имени группы не может быть пустым.")
                return
            self.settings_vars["default_group_name"].set(template)
            self.save_settings()
            dialog.destroy()
            
            file_durs = {}
            for f_id in all_files:
                dur_str = self.export_tree.item(f_id, "values")[0]
                parts = dur_str.split(':')
                sec = 0
                if len(parts) == 3: sec = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                elif len(parts) == 2: sec = int(parts[0])*60 + int(parts[1])
                file_durs[f_id] = sec
            
            groups_data = split_export_file_ids(all_files, file_durs, limit_sec)
                
            if not groups_data: return
            
            # Сначала строим новую структуру в памяти. Дерево изменяем только
            # после успешного расчёта, чтобы ошибка шаблона/длительности не
            # оставила проект наполовину перегруппированным.
            planned_groups = []
            
            # Определяем стартовый номер
            match_start = re.search(r'\{num:(\d+)\}', template)
            start_index = int(match_start.group(1)) if match_start else 1
            
            for idx, group_files in enumerate(groups_data, 0):
                current_num = format_sequence_number(
                    start_index + idx,
                    len(groups_data),
                    start_index=start_index,
                )
                g_name = re.sub(r'\{num(?::\d+)?\}', current_num, template)
                planned_groups.append((g_name, group_files))

            for item in self.export_tree.get_children():
                self.export_tree.delete(item)
            self.export_groups.clear()
            self.current_selected_export_item = None

            for g_name, group_files in planned_groups:
                g_id = self.add_export_group(name=g_name)
                for f_id in group_files:
                    title = self.export_files[f_id]["title"]
                    self.export_tree.insert(g_id, tk.END, iid=f_id, text=title, values=(self.format_duration(file_durs[f_id]),))
                self.update_group_duration(g_id)
            self.export_tree.selection_remove(*self.export_tree.selection())
            self._disable_export_settings()
            
        ttk.Button(dialog, text="Разбить", command=do_split).pack(pady=10)
        
        self._center_popup(dialog, 350, 220)
        
    # --- Процесс Экспорта ---
    def stop_export_process(self):
        self.is_export_stopped = True
        self.btn_export_stop.config(state=tk.DISABLED)
        self._set_export_status("Остановка…", "warning")

    def _finish_export_process_ui(self, outcome):
        """Возвращает UI сборщика в устойчивое состояние после worker-потока.

        При штатной пользовательской остановке worker сперва ставит в UI-очередь
        модальное сообщение и лишь затем этот callback, поэтому сброс виден
        после закрытия сообщения. Успех, частичный успех и реальная ошибка
        сохраняют свои финальные статус и прогресс для диагностики.
        """
        self._export_thread = None
        self._set_export_running_state(False)
        if outcome == "stopped":
            self.export_progress.configure(value=0)
            self._set_export_status("Ожидание...", "info")
        self.is_export_stopped = False

    def _update_file_tags_inplace(self, fp, f_tags, cov, title_for_status):
        """Обновляет теги файла прямо в его исходной папке на диске без конвертации"""
        source_path = Path(fp)
        # Тестовые/внешние вызовы могут использовать helper без полностью
        # построенного Tk-приложения. В реальном worker очередь всегда есть.
        if hasattr(self, "_ui_queue"):
            self._post_export_activity_status(
                "Теги",
                title_for_status or source_path.name,
                "warning",
            )
        elif hasattr(self, "_post_status_label"):
            self._post_status_label(
                self.lbl_export_status,
                format_export_activity_status(
                    "Теги", title_for_status or source_path.name
                ),
                "warning",
            )

        if not source_path.is_file():
            logging.error("Исходный аудиофайл для тегирования не найден: %s", fp)
            return False
        temp_file = source_path.with_name(
            f".{source_path.stem}.{uuid.uuid4().hex}.tmp{source_path.suffix}"
        )
        cmd = [get_ffmpeg_path(), "-y", "-i", str(source_path)]

        ext = source_path.suffix.lower()
        cover_supported = ext == ".mp3"
        xiph_cover = (
            _xiph_cover_metadata(cov, source_path.name)
            if ext == ".opus"
            else None
        )
        if cov and os.path.isfile(cov) and cover_supported:
            # Явные map исключают перенос старой обложки вместе с новой.
            cmd.extend([
                "-i", str(cov), "-map", "0:a:0", "-map", "1:v:0",
                "-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3",
                "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
            ])
        else:
            # Входной виртуальный attached_pic у Xiph-файла является
            # представлением METADATA_BLOCK_PICTURE. При новой обложке явно
            # копируем лишь аудио, чтобы старый picture block не задублировался.
            if ext == ".opus":
                # FFmpeg exposes METADATA_BLOCK_PICTURE as a virtual video
                # stream which the Opus muxer cannot copy back. Always select
                # only the real audio stream, then write the new comment below.
                cmd.extend(["-map", "0:a:0", "-c:a", "copy"])
            else:
                cmd.extend(["-map", "0", "-c", "copy"])
            if (
                cov
                and os.path.isfile(cov)
                and not cover_supported
                and ext != ".opus"
            ):
                logging.warning(
                    "Обложка пропущена при обновлении тегов %s: формат %s "
                    "не поддерживает attached picture этим способом.",
                    source_path.name,
                    ext or "без расширения",
                )

        # Не наследуем format metadata вслепую; ниже записывается снимок из UI.
        cmd.extend(["-map_metadata", "-1"])

        if f_tags:
            for k, v in f_tags.items():
                if v: cmd.extend(["-metadata", f"{k}={v}"])
        if xiph_cover:
            cmd.extend([
                "-metadata", f"METADATA_BLOCK_PICTURE={xiph_cover}"
            ])

        cmd.append(str(temp_file))

        startupinfo = None
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo
            )
            if res.returncode == 0 and temp_file.exists():
                os.replace(temp_file, source_path)
                return True

            err_log = (
                res.stderr.decode('utf-8', errors='ignore')
                if res.stderr
                else "Неизвестная ошибка"
            )
            logging.error(f"Ошибка FFmpeg при обновлении тегов {fp}:\n{err_log}")
            return False
        except Exception:
            logging.exception("Ошибка запуска FFmpeg при обновлении тегов %s", fp)
            return False
        finally:
            temp_file.unlink(missing_ok=True)
    
    def start_export_process(self):
        if getattr(self, "_export_running", False) or getattr(self, "_export_lock", False):
            return
        items = self.export_tree.get_children()
        if not items:
            self._show_warning("Пусто", "Список пуст. Добавьте аудиофайлы или группы.")
            return
            
        # Проверяем, есть ли вообще файлы (в корне или в группах)
        has_any_files = False
        for item in items:
            if item in self.export_files:
                has_any_files = True
                break
            elif item in self.export_groups and len(self.export_tree.get_children(item)) > 0:
                has_any_files = True
                break

        if not has_any_files:
            self._show_warning("Нет файлов", "Добавьте аудиофайлы перед началом сборки.")
            return
        
        # ИСПРАВЛЕНИЕ: Если включено "Только теги", игнорируем пустую папку!
        tags_only = self.export_tags_only_var.get()
        out_dir_str = self.export_outdir_var.get().strip()
        
        if not tags_only and not out_dir_str:
            self._show_warning("Папка не выбрана", "Пожалуйста, укажите папку для сохранения результатов экспорта.")
            chosen = filedialog.askdirectory(
                initialdir=resolve_dialog_initial_dir(
                    self.config.get("export_dir", ""),
                    self.config.get("last_browse_dir", ""),
                    BASE_DIR,
                )
            )
            if chosen:
                self.export_outdir_var.set(chosen)
                out_dir_str = chosen
                self.config["export_dir"] = chosen
                self.save_settings()
            else:
                return

        out_dir = Path(out_dir_str).expanduser() if out_dir_str else None
        if out_dir and not tags_only:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logging.error("Не удалось создать папку экспорта %s: %s", out_dir, exc)
                self._show_error(
                    "Ошибка папки",
                    f"Не удалось создать папку экспорта:\n{out_dir}\n\n{exc}",
                )
                return

        # Все источники должны существовать до заморозки UI. Иначе сборка
        # могла закончиться частичным результатом лишь потому, что один файл
        # был перемещён после добавления в дерево.
        missing_sources = [
            data.get("path", "")
            for data in self.export_files.values()
            if not data.get("path") or not Path(data["path"]).is_file()
        ]
        if missing_sources:
            preview = "\n".join(str(path) for path in missing_sources[:5])
            suffix = "\n…" if len(missing_sources) > 5 else ""
            self._show_error(
                "Нет исходных файлов",
                f"Не найдено файлов: {len(missing_sources)}\n\n{preview}{suffix}",
            )
            return

        # Все значения Tk и структура дерева снимаются до запуска потока.
        # В фоне используются только обычные Python-объекты.
        apply_fx = bool(self.export_apply_fx_var.get())
        if apply_fx and not tags_only:
            sp = float(self.exp_speed_var.get())
            pt = float(self.exp_pitch_var.get())
            ec = bool(self.exp_echo_var.get())
            ed = int(self.exp_delay_var.get())
            ey = float(self.exp_decay_var.get())
        else:
            sp, pt, ec, ed, ey = 1.0, 1.0, False, 300, 0.3

        fmt = self.export_fmt_var.get().strip().lower()
        bitrate = self.export_bitrate_var.get().strip().lower()
        sample_rate = self.export_sample_rate_var.get().strip().lower()
        channels = self.export_channels_var.get().strip().lower()
        if fmt not in {"mp3", "wav", "ogg", "opus"}:
            self._show_error("Ошибка", "Выбран неподдерживаемый формат экспорта.")
            return
        if bitrate != "auto" and not re.fullmatch(r"[1-9]\d*k", bitrate):
            self._show_error("Ошибка", "Некорректный битрейт экспорта.")
            return
        if sample_rate != "auto" and sample_rate not in {
            "8000", "12000", "16000", "22050", "24000", "32000",
            "44100", "48000", "88200", "96000"
        }:
            self._show_error("Ошибка", "Некорректная частота экспорта.")
            return
        if channels not in {"auto", "mono", "stereo"}:
            self._show_error("Ошибка", "Некорректный режим каналов экспорта.")
            return
        group_children = {
            group_id: tuple(self.export_tree.get_children(group_id))
            for group_id in self.export_groups
        }
        export_groups = {
            group_id: settings.copy()
            for group_id, settings in self.export_groups.items()
        }
        export_files = {
            file_id: settings.copy()
            for file_id, settings in self.export_files.items()
        }

        if not tags_only:
            planned_outputs = []
            for item_id in items:
                if item_id in export_groups:
                    group = export_groups[item_id]
                    group_name = sanitize_filename_component(
                        group.get("name"), fallback="Группа"
                    )
                    children = group_children.get(item_id, ())
                    # Пустая группа не создаёт файл в worker, значит и
                    # preflight не должен резервировать для неё выходное имя.
                    if not children:
                        continue
                    if group.get("merge"):
                        planned_outputs.append(out_dir / f"{group_name}.{fmt}")
                    else:
                        target = (
                            out_dir / group_name
                            if effective_group_subfolder(
                                group.get("merge"), group.get("subfolder")
                            )
                            else out_dir
                        )
                        for file_id in children:
                            source = export_files[file_id]
                            name = sanitize_filename_component(
                                source.get("title"),
                                fallback=Path(source["path"]).stem,
                            )
                            planned_outputs.append(target / f"{name}.{fmt}")
                elif item_id in export_files:
                    source = export_files[item_id]
                    name = sanitize_filename_component(
                        source.get("title"), fallback=Path(source["path"]).stem
                    )
                    planned_outputs.append(out_dir / f"{name}.{fmt}")

            duplicates = duplicate_paths(planned_outputs)
            if duplicates:
                preview = "\n".join(duplicates[:5])
                self._show_error(
                    "Совпадающие имена",
                    "Несколько элементов будут записаны в один файл. "
                    "Измените названия:\n\n" + preview,
                )
                return

        self.save_settings()
        self._set_export_running_state(True)
        self.export_progress['value'] = 0
        self.is_export_stopped = False
        
        def run_export():
            outcome = "running"
            try:
                total_files = len(export_files)
                processed_files = 0
                failed_files = 0
                
                def build_tags(settings_dict):
                    t = {}
                    if settings_dict.get("title"): t["title"] = settings_dict["title"]
                    if settings_dict.get("artist"): t["artist"] = settings_dict["artist"]
                    if settings_dict.get("album"): t["album"] = settings_dict["album"]
                    if settings_dict.get("album_artist"): t["album_artist"] = settings_dict["album_artist"]
                    if settings_dict.get("genre"): t["genre"] = settings_dict["genre"]
                    if settings_dict.get("composer"): t["composer"] = settings_dict["composer"]
                    if settings_dict.get("year"): t["date"] = settings_dict["year"]
                    return t

                # =========================================================================
                # РЕЖИМ 1: ТОЛЬКО ТЕГИ (Обновление прямо в исходных папках файлов)
                # =========================================================================
                if tags_only:
                    for item_id in items:
                        if self.is_export_stopped: break

                        # ЭТО ГРУППА
                        if item_id in export_groups:
                            g_set = export_groups[item_id]
                            g_tags = build_tags(g_set)
                            files = group_children.get(item_id, ())

                            for f_id in files:
                                if self.is_export_stopped: break
                                f_set = export_files[f_id]
                                fp = f_set["path"] # Взяли ТОЧНЫЙ исходный путь файла!

                                f_tags = build_tags(f_set)
                                for k in ["artist", "album", "album_artist", "genre", "composer", "date"]:
                                    if k not in f_tags and g_tags.get(k):
                                        f_tags[k] = g_tags[k]
                                cov = f_set.get("cover") or g_set.get("cover")

                                if not self._update_file_tags_inplace(
                                    fp, f_tags, cov, f_set.get("title", "")
                                ):
                                    failed_files += 1

                                processed_files += 1
                                pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                                self._post_to_ui(self.export_progress.config, value=pct)

                        # ЭТО ОДИНОЧНЫЙ ФАЙЛ
                        elif item_id in export_files:
                            f_set = export_files[item_id]
                            fp = f_set["path"] # Взяли ТОЧНЫЙ исходный путь файла!

                            f_tags = build_tags(f_set)
                            cov = f_set.get("cover")

                            if not self._update_file_tags_inplace(
                                fp, f_tags, cov, f_set.get("title", "")
                            ):
                                failed_files += 1

                            processed_files += 1
                            pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                            self._post_to_ui(self.export_progress.config, value=pct)

                # =========================================================================
                # РЕЖИМ 2: ПОЛНЫЙ ЭКСПОРТ (Конвертация с сохранением в Папку Экспорта)
                # =========================================================================
                else:
                    for item_idx, item_id in enumerate(items):
                        if self.is_export_stopped: break
                        
                        if item_id in export_groups:
                            g_id = item_id
                            g_set = export_groups[g_id]
                            g_name = g_set["name"]
                            safe_group_name = sanitize_filename_component(
                                g_name, fallback="Группа"
                            )
                            files = group_children.get(g_id, ())
                            if not files: continue
                            
                            if g_set["merge"]:
                                pause_ms = g_set["pause"]

                                first_f_set = export_files.get(files[0], {})
                                for key in ["artist", "album", "album_artist", "genre", "composer", "year", "cover"]:
                                    if not g_set.get(key) and first_f_set.get(key):
                                        g_set[key] = first_f_set[key]

                                merge_sources = []
                                for f_id in files:
                                    if self.is_export_stopped:
                                        break
                                    merge_sources.append(export_files[f_id]["path"])

                                if self.is_export_stopped:
                                    break
                                self._post_export_activity_status(
                                    "Склейка",
                                    g_name,
                                    "warning",
                                )
                                g_set["title"] = g_name
                                tags = build_tags(g_set)
                                out_file = out_dir / f"{safe_group_name}.{fmt}"
                                _export_merged_audio_ffmpeg(
                                    merge_sources,
                                    out_file,
                                    output_format=fmt,
                                    bitrate=None if bitrate == "auto" else bitrate,
                                    bitrate_mode=bitrate,
                                    sample_rate=sample_rate,
                                    channels=channels,
                                    pause_ms=pause_ms,
                                    speed=sp if apply_fx else 1.0,
                                    pitch=pt if apply_fx else 1.0,
                                    echo=ec if apply_fx else False,
                                    echo_delay=ed,
                                    echo_decay=ey,
                                    tags=tags,
                                    cover=g_set.get("cover"),
                                    cancelled=lambda: self.is_export_stopped,
                                )
                                processed_files += len(files)
                                pct = (
                                    int((processed_files / total_files) * 100)
                                    if total_files > 0
                                    else 0
                                )
                                self._post_to_ui(
                                    self.export_progress.config, value=pct
                                )
                                
                            else:
                                target_dir = (
                                    out_dir / safe_group_name
                                    if effective_group_subfolder(
                                        g_set.get("merge"), g_set.get("subfolder")
                                    )
                                    else out_dir
                                )
                                target_dir.mkdir(parents=True, exist_ok=True)
                                
                                for i, f_id in enumerate(files):
                                    if self.is_export_stopped: break
                                    
                                    f_set = export_files[f_id]
                                    fp = f_set["path"]
                                    
                                    f_tags = build_tags(f_set)
                                    g_tags = build_tags(g_set)
                                    for k in ["artist", "album", "album_artist", "genre", "composer", "date"]:
                                        if k not in f_tags and g_tags.get(k):
                                            f_tags[k] = g_tags[k]
                                            
                                    cov = f_set.get("cover") or g_set.get("cover")
                                    
                                    self._post_export_activity_status(
                                        "Экспорт",
                                        f_set["title"],
                                        "warning",
                                    )
                                    safe_name = sanitize_filename_component(
                                        f_set["title"], fallback=Path(fp).stem
                                    )
                                    out_file = target_dir / f"{safe_name}.{fmt}"
                                    # Один вход всё равно проходит через общий
                                    # FFmpeg-граф: так выбранные частота,
                                    # каналы, битрейт и эффекты одинаково
                                    # применяются и к склейке, и к отдельным
                                    # файлам без промежуточного PCM/WAV.
                                    _export_single_audio_ffmpeg(
                                        fp,
                                        out_file,
                                        output_format=fmt,
                                        bitrate=None if bitrate == "auto" else bitrate,
                                        bitrate_mode=bitrate,
                                        sample_rate=sample_rate,
                                        channels=channels,
                                        speed=sp if apply_fx else 1.0,
                                        pitch=pt if apply_fx else 1.0,
                                        echo=ec if apply_fx else False,
                                        echo_delay=ed,
                                        echo_decay=ey,
                                        tags=f_tags,
                                        cover=cov,
                                        cancelled=lambda: self.is_export_stopped,
                                    )

                                    processed_files += 1
                                    pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                                    self._post_to_ui(self.export_progress.config, value=pct)

                        elif item_id in export_files:
                            f_id = item_id
                            f_set = export_files[f_id]
                            fp = f_set["path"]
                            
                            f_tags = build_tags(f_set)
                            cov = f_set.get("cover")
                            
                            self._post_export_activity_status(
                                "Экспорт",
                                f_set["title"],
                                "warning",
                            )
                            safe_name = sanitize_filename_component(
                                f_set["title"], fallback=Path(fp).stem
                            )
                            out_file = out_dir / f"{safe_name}.{fmt}"
                            _export_single_audio_ffmpeg(
                                fp,
                                out_file,
                                output_format=fmt,
                                bitrate=None if bitrate == "auto" else bitrate,
                                bitrate_mode=bitrate,
                                sample_rate=sample_rate,
                                channels=channels,
                                speed=sp if apply_fx else 1.0,
                                pitch=pt if apply_fx else 1.0,
                                echo=ec if apply_fx else False,
                                echo_delay=ed,
                                echo_decay=ey,
                                tags=f_tags,
                                cover=cov,
                                cancelled=lambda: self.is_export_stopped,
                            )

                            processed_files += 1
                            pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                            self._post_to_ui(self.export_progress.config, value=pct)

                if self.is_export_stopped:
                    outcome = "stopped"
                    self._post_export_status("Сборка прервана!", "warning")
                    self._post_to_ui(self._show_warning, "Остановлено", "Процесс сборки был прерван пользователем.")
                else:
                    if failed_files:
                        outcome = "warning"
                        msg = (
                            f"Обработка завершена с ошибками: {failed_files}.\n"
                            "Подробности записаны в журнал."
                        )
                        self._post_export_status(
                            "Готово с ошибками.", "warning"
                        )
                        self._post_to_ui(self._show_warning, "Готово", msg)
                    else:
                        outcome = "success"
                        msg = "Теги успешно обновлены в исходных файлах!" if tags_only else f"Сборка успешно завершена!\nСохранено в: {out_dir}"
                        self._post_export_status("Готово!", "success")
                        self._post_to_ui(self._show_info, "Успех", msg)
                
            except InterruptedError:
                # Отмена потоковой FFmpeg-склейки является штатным исходом:
                # helper уже завершил дочерний процесс и удалил временный файл.
                outcome = "stopped"
                self.is_export_stopped = True
                self._post_export_status("Сборка прервана!", "warning")
                self._post_to_ui(
                    self._show_warning,
                    "Остановлено",
                    "Процесс сборки был прерван пользователем.",
                )
            except Exception as e:
                outcome = "error"
                logging.exception("Ошибка сборки")
                error_message = f"Произошла ошибка при сборке:\n{e}"
                self._post_export_status("Ошибка!", "error")
                self._post_to_ui(self._show_error, "Ошибка", error_message)
            finally:
                self._post_to_ui(self._finish_export_process_ui, outcome)

        self._export_thread = threading.Thread(target=run_export, daemon=True)
        try:
            self._export_thread.start()
        except Exception as exc:
            self._export_thread = None
            self._set_export_running_state(False)
            self._set_export_status("Ошибка запуска сборки.", "error")
            logging.exception("Не удалось запустить поток экспорта")
            self._show_error(
                "Ошибка", f"Не удалось запустить сборку:\n{exc}"
            )

    def add_separator_row(self, initial_value=""):
        """Добавляет новую строчку с ttk.Entry для разделителя с прямой записью текста"""
        if not hasattr(self, 'separators_container') or not self.separators_container.winfo_exists():
            return

        row_frame = ttk.Frame(self.separators_container)
        row_frame.pack(fill=tk.X, pady=2)

        # Прямое создание инпута без временного StringVar (защита от сборщика мусора)
        ent = ttk.Entry(row_frame)
        ent.insert(0, str(initial_value)) # 👈 Прямая вставка значения в память поля!
        ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        btn_del = ttk.Button(row_frame, text="❌", width=3, command=lambda: self.remove_separator_row(row_frame, ent))
        btn_del.pack(side=tk.RIGHT)

        self.separator_entries.append(ent)

    def remove_separator_row(self, row_frame, entry_widget):
        """Удаляет строчку разделителя"""
        if entry_widget in self.separator_entries:
            self.separator_entries.remove(entry_widget)
        row_frame.destroy()

    
    # --- Вкладка "Настройки" ---
    def setup_settings_tab(self):
        btn_frame = ttk.Frame(self.tab_settings, padding=10)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        def save_settings_from_button():
            # Сохранение конфига не запускает API: большое значение здесь лишь
            # валидируем, а подтверждение показываем непосредственно перед API.
            if self._validate_api_steps_ui(confirm_large=False):
                self.save_settings(show_popup=True)

        ttk.Button(btn_frame, text="💾 Сохранить", command=save_settings_from_button).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Загрузить конфиг", command=self.import_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📤 Экспорт конфига", command=self.export_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Сбросить", command=self.reset_config).pack(side=tk.LEFT, padx=5)
        
        set_notebook = ttk.Notebook(self.tab_settings)
        set_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tab_settings.pack_propagate(False)
        
        tab_api = ttk.Frame(set_notebook, padding=10)
        tab_folders = ttk.Frame(set_notebook, padding=10)
        tab_pauses = ttk.Frame(set_notebook, padding=10)
        tab_cache = ttk.Frame(set_notebook, padding=10)
        tab_normalization = ttk.Frame(set_notebook, padding=10)
        tab_effects = ttk.Frame(set_notebook, padding=10)
        tab_output = ttk.Frame(set_notebook, padding=10)
        
        set_notebook.add(tab_api, text="API и Лимиты")
        set_notebook.add(tab_folders, text="Папки")
        set_notebook.add(tab_pauses, text="Паузы и Разделители")
        set_notebook.add(tab_cache, text="Обработка и Кэш")
        set_notebook.add(tab_normalization, text="Нормализация")
        set_notebook.add(tab_effects, text="Эффекты (Постобработка)")
        set_notebook.add(tab_output, text="Вывод и Теги")

        def add_entry(parent, label, key, row, vtype=tk.StringVar):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            var = vtype(value=self.config.get(key, ""))
            self.settings_vars[key] = var
            ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2, padx=5)

        def add_combobox(parent, label, key, row, values):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            var = tk.StringVar(value=self.config.get(key, values[0]))
            self.settings_vars[key] = var
            cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly")
            cb.grid(row=row, column=1, sticky="ew", pady=2, padx=5)

        def add_dir_entry(parent, label, key, row, is_file=False):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            # Папка direct уже показана на одноимённой вкладке. Оба Entry
            # должны использовать один StringVar, а не две рассинхронизируемые
            # копии одного значения.
            if key == "direct_output_dir" and hasattr(self, "direct_output_dir_var"):
                var = self.direct_output_dir_var
            else:
                var = tk.StringVar(value=self.config.get(key, ""))
            self.settings_vars[key] = var
            f = ttk.Frame(parent)
            f.grid(row=row, column=1, sticky="ew", pady=2, padx=5)
            ttk.Entry(f, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
            def cmd():
                init_dir = self._get_smart_dir(var.get(), is_file=is_file)
                res = filedialog.askopenfilename(initialdir=init_dir) if is_file else filedialog.askdirectory(initialdir=init_dir)
                if res: var.set(res)
            ttk.Button(f, text="📁", width=3, command=cmd).pack(side=tk.RIGHT, padx=2)

        def add_check(parent, label, key, row):
            var = tk.BooleanVar(value=self.config.get(key, False))
            self.settings_vars[key] = var
            ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=2, padx=5)

        # 1. API
        add_entry(tab_api, "API Token:", "api_token", 0)
        add_entry(tab_api, "API URL:", "api_url", 1)
        add_entry(tab_api, "Спикер (Голос):", "speaker", 2)

        steps_frame = ttk.LabelFrame(
            tab_api,
            text="Экспериментально: скорость / качество синтеза (steps)",
            padding=8,
        )
        steps_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 4), padx=5)
        steps_frame.columnconfigure(1, weight=1)

        self.settings_vars["api_steps_enabled"] = tk.BooleanVar(
            value=_config_bool(self.config.get("api_steps_enabled", False))
        )
        ttk.Checkbutton(
            steps_frame,
            text="Передавать параметр steps в Silero API",
            variable=self.settings_vars["api_steps_enabled"],
            command=self._sync_api_steps_widgets,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W)

        initial_steps_raw = self.config.get("api_steps", 16)
        initial_steps = initial_steps_raw
        try:
            initial_text = str(initial_steps).strip()
            if not re.fullmatch(r"[+-]?\d+", initial_text):
                raise ValueError
            initial_steps = int(initial_text)
        except (TypeError, ValueError):
            # Не маскируем ошибочный импорт значением 16: пользователь увидит
            # исходное значение в поле «Другое» и явную ошибку при сохранении.
            initial_steps = 0
        initial_choice = str(initial_steps) if initial_steps in API_STEPS_PRESETS else "Другое"

        ttk.Label(steps_frame, text="Steps:").grid(row=1, column=0, sticky=tk.W, pady=(5, 2))
        self.settings_vars["api_steps_choice"] = tk.StringVar(value=initial_choice)
        self.settings_vars["api_steps"] = tk.IntVar(value=initial_steps)
        self.api_steps_combobox = ttk.Combobox(
            steps_frame,
            textvariable=self.settings_vars["api_steps_choice"],
            values=[str(value) for value in API_STEPS_PRESETS] + ["Другое"],
            state="readonly",
            width=12,
        )
        self.api_steps_combobox.grid(row=1, column=1, sticky=tk.W, pady=(5, 2))
        self.api_steps_combobox.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._sync_api_steps_widgets(focus_custom=True),
        )

        ttk.Label(steps_frame, text=f"Своё значение (целое {API_STEPS_MIN}–{API_STEPS_MAX}):").grid(
            row=2, column=0, sticky=tk.W, pady=2
        )
        self.settings_vars["api_steps_custom"] = tk.StringVar(
            value="" if initial_choice != "Другое" else str(initial_steps_raw)
        )
        self.api_steps_custom_entry = ttk.Entry(
            steps_frame,
            textvariable=self.settings_vars["api_steps_custom"],
            width=14,
        )
        self.api_steps_custom_entry.grid(row=2, column=1, sticky=tk.W, pady=2)

        self.settings_vars["cache_include_steps"] = tk.BooleanVar(
            value=_config_bool(self.config.get("cache_include_steps", True), default=True)
        )
        self.api_steps_cache_check = ttk.Checkbutton(
            steps_frame,
            text="Учитывать steps в ключе кэша (рекомендуется)",
            variable=self.settings_vars["cache_include_steps"],
        )
        self.api_steps_cache_check.grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=(3, 0)
        )

        def enforce_steps_cache_key(*_args):
            if (
                _config_bool(self.settings_vars["api_steps_enabled"].get())
                and not _config_bool(self.settings_vars["cache_include_steps"].get())
            ):
                if not self._ask_yes_no(
                    "Общий кэш Steps",
                    "Без учёта Steps разные значения качества используют один "
                    "и тот же ключ и могут заменять друг друга. Оставить общий "
                    "legacy-кэш?",
                    icon="warning",
                ):
                    self.settings_vars["cache_include_steps"].set(True)

        self.api_steps_cache_check.configure(command=enforce_steps_cache_key)

        self._register_palette_widget(
            ttk.Label(
                steps_frame,
                text=(
                    "Выключено = параметр не отправляется (серверное значение по умолчанию и старый кэш). "
                    "16 — значение API по умолчанию; 8 — быстрее, немного хуже; "
                    "4 — ещё быстрее, но заметно хуже; 12 — промежуточный вариант. "
                    "Выше 16 будет предупреждение, допустимый диапазон сейчас 1–72."
                ),
                wraplength=700,
                justify=tk.LEFT,
                font=("", 9, "italic"),
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self._sync_api_steps_widgets()

        ttk.Separator(tab_api, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)
        add_entry(tab_api, "Макс. запросов API:", "api_max_requests", 5, tk.IntVar)
        add_entry(tab_api, "Окно времени (сек):", "api_time_window", 6, tk.DoubleVar)
        add_entry(tab_api, "Кол-во попыток при ошибке:", "max_retries", 7, tk.IntVar)
        ttk.Separator(tab_api, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=2, sticky="ew", pady=10)
        add_entry(tab_api, "Параллельных сборок FFmpeg (0 = Макс.):", "max_parallel_encodes", 9, tk.IntVar)

        # 2. Папки
        add_dir_entry(tab_folders, "Папка с текстами:", "input_dir", 0)
        add_dir_entry(tab_folders, "Папка для аудио:", "output_dir", 1)
        add_dir_entry(tab_folders, "Папка для кэша:", "cache_dir", 2)
        self._register_palette_widget(
            ttk.Label(
                tab_folders,
                text=(
                    "Папка прямого синтеза задаётся на вкладке «Прямой синтез» "
                    "и сохраняется вместе с остальными путями."
                ),
                foreground=self.get_status_color("muted"),
                wraplength=680,
                justify=tk.LEFT,
            ),
            "muted",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(10, 2), padx=5)

        # 3. Паузы
        add_entry(tab_pauses, "Начало файла (мс):", "pause_file_start", 0, tk.IntVar)
        add_entry(tab_pauses, "Конец файла (мс):", "pause_file_end", 1, tk.IntVar)
        add_entry(tab_pauses, "Между предложениями (мс):", "pause_sentence", 2, tk.IntVar)
        add_entry(tab_pauses, "Между абзацами (мс):", "pause_paragraph", 3, tk.IntVar)
        add_entry(tab_pauses, "Перед репликой/мыслью (мс):", "pause_speech", 4, tk.IntVar)
        add_entry(tab_pauses, "После двоеточия (мс):", "pause_colon", 5, tk.IntVar)
        add_entry(tab_pauses, "Пауза разделителя (мс):", "pause_separator", 6, tk.IntVar)
        ttk.Separator(tab_pauses, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)
        # Заголовок и кнопка добавления
        seps_header_frame = ttk.Frame(tab_pauses)
        seps_header_frame.grid(row=8, column=0, sticky=tk.NW, pady=2, padx=5)

        ttk.Label(seps_header_frame, text="Символы-разделители:").pack(anchor=tk.W)
        ttk.Button(seps_header_frame, text="➕ Добавить", command=lambda: self.add_separator_row()).pack(anchor=tk.W, pady=5)

        # Динамический контейнер под строчки ttk.Entry
        self.separators_container = ttk.Frame(tab_pauses)
        self.separators_container.grid(row=8, column=1, sticky="ew", pady=2, padx=5)
        self.separator_entries = []

        # 4. Кэш
        add_check(tab_cache, "Авто-обрезка тишины от Silero", "auto_trim_silence", 0)
        add_entry(tab_cache, "Порог тишины (dBFS):", "silence_threshold", 1, tk.DoubleVar)
        ttk.Separator(tab_cache, orient=tk.HORIZONTAL).grid(row=2, column=0, columnspan=2, sticky="ew", pady=10)
        add_check(tab_cache, "Включить кэширование", "use_cache", 3)
        add_entry(tab_cache, "Сохранять кэш на диск каждые (фраз):", "cache_save_frequency", 4, tk.IntVar)
        add_check(tab_cache, "Ограничить количество записей (LRU):", "enable_cache_lru", 5)
        add_entry(tab_cache, "Макс. записей в кэше:", "cache_max_entries", 6, tk.IntVar)
        add_check(tab_cache, "Удалять старые записи по времени (TTL):", "enable_cache_ttl", 7)
        add_entry(tab_cache, "Время жизни кэша (часов):", "cache_ttl_hours", 8, tk.DoubleVar)

        # 5. Нормализация. Полный редактор остаётся в песочнице, а здесь всегда
        # виден именно сохранённый глобальный снимок, а не локальные пробы.
        global_normalizer_frame = ttk.LabelFrame(
            tab_normalization,
            text="Сохранено глобально",
            padding=10,
        )
        global_normalizer_frame.grid(
            row=0, column=0, columnspan=2, sticky=tk.EW, padx=5, pady=(0, 10)
        )
        global_normalizer_frame.columnconfigure(0, weight=1)
        self.lbl_settings_normalizer_profile = ttk.Label(
            global_normalizer_frame,
            text="",
            font=("", 10, "bold"),
        )
        self.lbl_settings_normalizer_profile.grid(row=0, column=0, sticky=tk.W)
        self.lbl_settings_normalizer_details = self._register_palette_widget(
            ttk.Label(
                global_normalizer_frame,
                text="",
                wraplength=680,
                justify=tk.LEFT,
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        )
        self.lbl_settings_normalizer_details.grid(
            row=1, column=0, sticky=tk.EW, pady=(4, 8)
        )
        ttk.Button(
            global_normalizer_frame,
            text="Открыть вкладку «Нормализатор»",
            command=self._open_normalizer_tab_from_settings,
        ).grid(row=2, column=0, sticky=tk.W)

        self._register_palette_widget(
            ttk.Label(
                tab_normalization,
                text=(
                    "Параметры во вкладке «Нормализатор» сначала локальны. "
                    "Глобальными они становятся только после кнопки "
                    "«Применить глобально»."
                ),
                wraplength=700,
                justify=tk.LEFT,
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=5, pady=(0, 10))

        studio_normalizer_frame = ttk.LabelFrame(
            tab_normalization,
            text="Предобработка Studio (сохраняется кнопкой «Сохранить» ниже)",
            padding=8,
        )
        studio_normalizer_frame.grid(
            row=2, column=0, columnspan=2, sticky=tk.EW, padx=5
        )
        add_check(
            studio_normalizer_frame,
            "Авто-исправление аббревиатур (И.И. → И-И)",
            "auto_abbreviations",
            0,
        )
        add_check(
            studio_normalizer_frame,
            "Авто-сокращения (г., ул., ур. → г, ул, ур)",
            "auto_short_words",
            1,
        )
        self._refresh_global_normalizer_settings_summary()

        # 6. Эффекты
        self._register_palette_widget(
            ttk.Label(
                tab_effects,
                text=(
                    "Текущие эффекты книги и Прямого синтеза применяются ПОСЛЕ "
                    "генерации (без затрат API). Применение аудиопрофиля к книге "
                    "заменяет эти значения; последующая ручная правка не меняет "
                    "сохранённый профиль. У вкладки «Экспорт и сборка» свои эффекты."
                ),
                font=("", 9, "italic"),
                foreground=self.get_status_color("muted"),
                wraplength=700,
                justify=tk.LEFT,
            ),
            "muted",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        
        # Скорость
        ttk.Label(tab_effects, text="Скорость (Tempo):").grid(row=1, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_speed"] = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        self.lbl_speed_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_speed'].get():.1f}x", width=5)
        self.lbl_speed_val.grid(row=1, column=2, sticky=tk.W)
        scale_speed = ttk.Scale(tab_effects, from_=0.5, to_=3.0, variable=self.settings_vars["fx_speed"], command=lambda v: self.lbl_speed_val.config(text=f"{float(v):.1f}x"))
        scale_speed.grid(row=1, column=1, sticky=tk.EW, padx=10)
        
        # Тон
        ttk.Label(tab_effects, text="Тон (Pitch):").grid(row=2, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_pitch"] = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        self.lbl_pitch_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_pitch'].get():.2f}", width=5)
        self.lbl_pitch_val.grid(row=2, column=2, sticky=tk.W)
        scale_pitch = ttk.Scale(tab_effects, from_=0.5, to_=2.0, variable=self.settings_vars["fx_pitch"], command=lambda v: self.lbl_pitch_val.config(text=f"{float(v):.2f}"))
        scale_pitch.grid(row=2, column=1, sticky=tk.EW, padx=10)
        
        ttk.Separator(tab_effects, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        
        # Эхо
        add_check(tab_effects, "Включить Эхо (Reverb/Delay)", "fx_echo", 4)
        
        ttk.Label(tab_effects, text="Задержка эхо (мс):").grid(row=5, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_echo_delay"] = tk.IntVar(value=self.config.get("fx_echo_delay", 300))
        self.lbl_delay_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_echo_delay'].get()}мс", width=6)
        self.lbl_delay_val.grid(row=5, column=2, sticky=tk.W)
        scale_delay = ttk.Scale(tab_effects, from_=50, to_=1000, variable=self.settings_vars["fx_echo_delay"], command=lambda v: self.lbl_delay_val.config(text=f"{int(float(v))}мс"))
        scale_delay.grid(row=5, column=1, sticky=tk.EW, padx=10)

        ttk.Label(tab_effects, text="Сила эхо (Decay):").grid(row=6, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_echo_decay"] = tk.DoubleVar(value=self.config.get("fx_echo_decay", 0.3))
        self.lbl_decay_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_echo_decay'].get():.1f}", width=5)
        self.lbl_decay_val.grid(row=6, column=2, sticky=tk.W)
        scale_decay = ttk.Scale(tab_effects, from_=0.1, to_=0.8, variable=self.settings_vars["fx_echo_decay"], command=lambda v: self.lbl_decay_val.config(text=f"{float(v):.1f}"))
        scale_decay.grid(row=6, column=1, sticky=tk.EW, padx=10)

        ttk.Separator(tab_effects, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=3, sticky="ew", pady=10)
        
        # Кнопка сброса глобальных эффектов в Настройках:
        ttk.Button(tab_effects, text="🔄 Сбросить эффекты по умолчанию", command=self.reset_global_fx).grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=10, padx=5)

        tab_effects.columnconfigure(1, weight=1)

        # 7. Вывод и Теги
        add_combobox(tab_output, "Режим синтеза:", "synthesis_mode", 0, ["sentence", "paragraph", "full"])
        add_combobox(
            tab_output,
            "Формат аудио:",
            "output_format",
            1,
            ["mp3", "wav", "ogg", "opus"],
        )
        add_combobox(
            tab_output,
            "Битрейт (для сжатых форматов):",
            "output_bitrate",
            2,
            ["auto", "32k", "48k", "64k", "96k", "128k", "192k", "256k", "320k"],
        )
        add_combobox(
            tab_output,
            "Частота финального файла:",
            "output_sample_rate",
            3,
            list(AUDIO_PROFILE_SAMPLE_RATES),
        )
        add_combobox(
            tab_output,
            "Каналы финального файла:",
            "output_channels",
            4,
            list(AUDIO_PROFILE_CHANNELS),
        )
        ttk.Button(
            tab_output,
            text="🎛 Управление аудиопрофилями…",
            command=lambda: self.open_audio_profiles_dialog("book"),
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, padx=5, pady=(6, 2))
        self.lbl_book_audio_profile_summary = self._register_palette_widget(
            ttk.Label(
                tab_output,
                text="",
                wraplength=700,
                justify=tk.LEFT,
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        )
        self.lbl_book_audio_profile_summary.grid(
            row=6, column=0, columnspan=2, sticky=tk.EW, padx=5, pady=(3, 2)
        )
        self._register_palette_widget(
            ttk.Label(
                tab_output,
                text=(
                    "Профиль копирует параметры в эту область и не остаётся "
                    "связанным с полями. Поэтому ниже показано текущее совпадение, "
                    "а не скрытый «активный профиль»."
                ),
                wraplength=700,
                justify=tk.LEFT,
                foreground=self.get_status_color("muted"),
                font=("", 9, "italic"),
            ),
            "muted",
        ).grid(row=7, column=0, columnspan=2, sticky=tk.EW, padx=5, pady=(2, 0))
        ttk.Separator(tab_output, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(tab_output, text="Теги (для mp3/ogg/opus):").grid(row=9, column=0, columnspan=2, sticky=tk.W, padx=5)

        tags_frame = ttk.Frame(tab_output)
        tags_frame.grid(row=10, column=0, columnspan=2, sticky="ew", padx=5)

        def add_tag_grid(parent, label, key, r, c, is_file=False):
            ttk.Label(parent, text=label).grid(row=r, column=c, sticky=tk.W, pady=2, padx=5)
            var = tk.StringVar(value=self.config.get(key, ""))
            self.settings_vars[key] = var
            if is_file:
                f = ttk.Frame(parent)
                f.grid(row=r, column=c+1, sticky="ew", pady=2, padx=5)
                ttk.Entry(f, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
                def cmd():
                    init_dir = self._get_smart_dir(var.get(), is_file=True)
                    res = filedialog.askopenfilename(initialdir=init_dir, filetypes=[("Images", "*.jpg *.jpeg *.png")])
                    if res: var.set(res)
                ttk.Button(f, text="📁", width=3, command=cmd).pack(side=tk.RIGHT, padx=2)
            else:
                ttk.Entry(parent, textvariable=var).grid(row=r, column=c+1, sticky="ew", pady=2, padx=5)

        add_tag_grid(tags_frame, "Название ({filename}):", "tag_title", 0, 0)
        add_tag_grid(tags_frame, "Исполнитель:", "tag_artist", 1, 0)
        add_tag_grid(tags_frame, "Исполн. альбома:", "tag_album_artist", 2, 0)
        add_tag_grid(tags_frame, "Альбом:", "tag_album", 3, 0)
        
        add_tag_grid(tags_frame, "Жанр:", "tag_genre", 0, 2)
        add_tag_grid(tags_frame, "Композитор:", "tag_composer", 1, 2)
        add_tag_grid(tags_frame, "Год:", "tag_year", 2, 2)
        add_tag_grid(tags_frame, "Обложка:", "tag_cover", 3, 2, is_file=True)
        
        tags_frame.columnconfigure(1, weight=1)
        tags_frame.columnconfigure(3, weight=1)

        for variable in (
            self.settings_vars["output_format"],
            self.settings_vars["output_bitrate"],
            self.settings_vars["output_sample_rate"],
            self.settings_vars["output_channels"],
            self.settings_vars["fx_speed"],
            self.settings_vars["fx_pitch"],
            self.settings_vars["fx_echo"],
            self.settings_vars["fx_echo_delay"],
            self.settings_vars["fx_echo_decay"],
        ):
            variable.trace_add(
                "write", lambda *_args: self._refresh_audio_profile_summaries()
            )
        self._refresh_audio_profile_summaries()

        for tab in (
            tab_api,
            tab_folders,
            tab_pauses,
            tab_cache,
            tab_normalization,
            tab_effects,
            tab_output,
        ):
            tab.columnconfigure(1, weight=1)

        self.set_ui_from_config()

    # --- Вкладка "Глоссарий" ---
    def setup_glossary_tab(self):
        frame = ttk.Frame(self.tab_glossary, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        self._glossary_loaded_path = None
        self._glossary_dirty = False
        self._glossary_ui_loading = False
        
        add_frame = ttk.LabelFrame(frame, text="Добавить правило", padding=10)
        add_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.glos_type = tk.StringVar(value="accent")
        ttk.Radiobutton(add_frame, text="Ударение (+)", variable=self.glos_type, value="accent", command=self.toggle_glos_fields).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(add_frame, text="Замена термина", variable=self.glos_type, value="term", command=self.toggle_glos_fields).grid(row=0, column=1, sticky=tk.W)
        ttk.Radiobutton(add_frame, text="RegEx (Паттерн)", variable=self.glos_type, value="regex", command=self.toggle_glos_fields).grid(row=0, column=2, sticky=tk.W)
        
        self.glos_strict = tk.BooleanVar(value=False)
        self.chk_glos_strict = ttk.Checkbutton(
            add_frame,
            text="Чувствительно к регистру",
            variable=self.glos_strict,
        )
        self.chk_glos_strict.grid(row=0, column=3, padx=15, sticky=tk.W)

        self.glos_verbatim = tk.BooleanVar(value=False)
        self.chk_glos_verbatim = ttk.Checkbutton(
            add_frame,
            text="Не нормализовать замену (verbatim)",
            variable=self.glos_verbatim,
        )
        self.chk_glos_verbatim.grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(5, 0)
        )

        self.glos_whole_word = tk.BooleanVar(value=True)
        self.chk_glos_whole_word = ttk.Checkbutton(
            add_frame,
            text="Только слово целиком",
            variable=self.glos_whole_word,
        )
        self.chk_glos_whole_word.grid(
            row=1, column=2, columnspan=2, sticky=tk.W, pady=(5, 0)
        )
        
        # Поля раположены СТРОГО друг под другом
        self.lbl_glos_word1 = ttk.Label(add_frame, text="Слово (с '+' или исходное):")
        self.lbl_glos_word1.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(5,0))
        self.glos_word1 = tk.StringVar()
        ttk.Entry(add_frame, textvariable=self.glos_word1).grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
        
        self.lbl_glos_word2 = ttk.Label(add_frame, text="Замена:")
        self.glos_word2 = tk.StringVar()
        self.ent_glos_word2 = ttk.Entry(add_frame, textvariable=self.glos_word2)
        
        ttk.Button(add_frame, text="➕ Добавить в JSON", command=self.add_glossary_rule).grid(row=6, column=0, columnspan=4, pady=10)
        
        self.toggle_glos_fields()

        # 1. Сначала пакуем нижние кнопки и прибиваем их ко дну!
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))
        ttk.Button(btn_frame, text="💾 Сохранить файл", command=self.save_glossary_ui).pack(side=tk.LEFT)
        ttk.Button(
            btn_frame,
            text="🔄 Перезагрузить",
            command=self.reload_glossary_ui,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame,
            text="🗑 Удалить правила…",
            command=self.open_glossary_delete_dialog,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Импорт", command=self.import_glossary).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="📤 Экспорт", command=self.export_glossary).pack(side=tk.RIGHT, padx=5)

        # 2. Затем пакуем панель с выбором шрифта
        lbl_frame = ttk.Frame(frame)
        lbl_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(lbl_frame, text="Редактор glossary.json:").pack(side=tk.LEFT)
        font_cb = ttk.Combobox(lbl_frame, textvariable=self.font_size_var, values=[10, 12, 14, 16, 18, 20, 24], state="readonly", width=5)
        font_cb.pack(side=tk.RIGHT)
        ttk.Label(lbl_frame, text="Шрифт:").pack(side=tk.RIGHT, padx=5)
        font_cb.bind("<<ComboboxSelected>>", lambda e: self.root.after(10, self.update_fonts))

        # 3. В самом конце пакуем текстовое поле, чтобы оно сжималось/растягивалось
        self.txt_glossary = tk.Text(frame, wrap=tk.WORD, font=("Courier", self.font_size_var.get()), undo=True, maxundo=50)
        self.txt_glossary.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)
        self.txt_glossary.bind("<<Modified>>", self._on_glossary_modified)
        self.txt_glossary.edit_modified(False)

        self.load_glossary_ui()

    def toggle_glos_fields(self):
        gtype = self.glos_type.get()
        if gtype == "term":
            self.lbl_glos_word1.config(text="Исходное слово:")
            self.lbl_glos_word2.grid(row=4, column=0, columnspan=4, sticky=tk.W)
            self.ent_glos_word2.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
            self.chk_glos_strict.configure(state=tk.NORMAL)
            self.chk_glos_verbatim.configure(state=tk.NORMAL)
            self.chk_glos_whole_word.configure(state=tk.NORMAL)
        elif gtype == "regex":
            self.lbl_glos_word1.config(text="Регулярное выражение:")
            self.lbl_glos_word2.grid(row=4, column=0, columnspan=4, sticky=tk.W)
            self.ent_glos_word2.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
            self.chk_glos_strict.configure(state=tk.DISABLED)
            self.chk_glos_verbatim.configure(state=tk.NORMAL)
            self.chk_glos_whole_word.configure(state=tk.DISABLED)
        else:
            self.lbl_glos_word1.config(text="Слово (с '+' или исходное):")
            self.lbl_glos_word2.grid_remove()
            self.ent_glos_word2.grid_remove()
            self.chk_glos_strict.configure(state=tk.NORMAL)
            self.chk_glos_verbatim.configure(state=tk.DISABLED)
            self.chk_glos_whole_word.configure(state=tk.DISABLED)

    def add_glossary_rule(self):
        w1 = self.glos_word1.get().strip()
        w2 = self.glos_word2.get() # Разрешаем пустоту
        strict = self.glos_strict.get()
        verbatim = self.glos_verbatim.get()
        whole_word = self.glos_whole_word.get()
        gtype = self.glos_type.get()

        if not w1: return

        try:
            content = self.txt_glossary.get(1.0, tk.END).strip()
            data = canonicalize_glossary_data(
                json.loads(content) if content else empty_glossary_data()
            )
            previous_shadowed = {
                conflict["key"] for conflict in glossary_shadowed_accent_rules(data)
            }

            if gtype == "accent":
                if strict: data.setdefault("accents_strict_case", []).append(w1)
                else: data.setdefault("accents_ignore_case", []).append(w1)
            elif gtype == "term":
                value = make_glossary_term_value(
                    w2,
                    verbatim=verbatim,
                    whole_word=whole_word,
                )
                if strict: data.setdefault("terms_strict_case", {})[w1] = value
                else: data.setdefault("terms_ignore_case", {})[w1] = value
            elif gtype == "regex":
                rule = {"pattern": w1, "repl": w2}
                if verbatim:
                    rule["verbatim"] = True
                data.setdefault("regex_rules", []).append(rule)

            self._replace_glossary_editor_data(data)
            self.glos_word1.set("")
            self.glos_word2.set("")
            current_shadowed = {
                conflict["key"] for conflict in glossary_shadowed_accent_rules(data)
            }
            if current_shadowed - previous_shadowed:
                self._show_warning(
                    "Конфликт правил",
                    "Для одного исходного слова одновременно заданы ударение "
                    "и термин. Оба правила сохранены, но термин имеет приоритет, "
                    "поэтому совпадающее ударение применяться не будет.",
                )
        except Exception as e:
            self._show_error("Ошибка", f"Не удалось обновить JSON: {e}")

    def _mark_glossary_editor_dirty(self):
        """Единообразно помечает JSON и инвалидирует preview."""
        self._glossary_dirty = True
        if not hasattr(self, "normalizer_source_text"):
            return
        glossary_switch = getattr(self, "normalizer_preview_glossary_var", None)
        if glossary_switch is not None and not glossary_switch.get():
            return
        self._schedule_normalizer_preview()

    def _replace_glossary_editor_data(self, data):
        """Заменяет JSON в редакторе и синхронно помечает его изменённым."""
        normalized = canonicalize_glossary_data(data)
        self._glossary_ui_loading = True
        try:
            try:
                self.txt_glossary.edit_separator()
            except tk.TclError:
                pass
            self.txt_glossary.delete(1.0, tk.END)
            self.txt_glossary.insert(
                tk.END, json.dumps(normalized, indent=4, ensure_ascii=False)
            )
            try:
                self.txt_glossary.edit_separator()
                self.txt_glossary.edit_modified(False)
            except tk.TclError:
                pass
        finally:
            self._glossary_ui_loading = False
        self._mark_glossary_editor_dirty()

    def _on_glossary_modified(self, _event=None):
        """Инвалидирует preview, если используемый им JSON был изменён."""
        try:
            if not self.txt_glossary.edit_modified():
                return
            self.txt_glossary.edit_modified(False)
        except (AttributeError, tk.TclError):
            return
        if getattr(self, "_glossary_ui_loading", False):
            return
        self._mark_glossary_editor_dirty()

    def _configured_glossary_path(self):
        cache_dir = Path(self.config.get("cache_dir", "cache_audio")).expanduser()
        return cache_dir.absolute() / "glossary.json"

    def _sync_glossary_editor_cache(self, *, prompt_if_dirty=False):
        """Переключает редактор на glossary.json выбранного cache_dir."""
        if not hasattr(self, "txt_glossary"):
            return True
        target_path = self._configured_glossary_path()
        loaded_path = getattr(self, "_glossary_loaded_path", None)
        if loaded_path is None or Path(loaded_path) == target_path:
            return True
        if getattr(self, "_glossary_dirty", False):
            if not prompt_if_dirty or not self._ask_yes_no(
                "Папка кэша изменена",
                "В редакторе есть несохранённые изменения старого глоссария. "
                "Перезагрузить glossary.json из новой папки кэша и отбросить "
                "эти изменения?",
                icon="warning",
            ):
                logging.warning(
                    "Редактор глоссария оставлен на %s после смены cache_dir на %s",
                    loaded_path,
                    target_path.parent,
                )
                if hasattr(self, "lbl_normalizer_preview_status"):
                    self._set_status_label(
                        self.lbl_normalizer_preview_status,
                        "Папка кэша изменена: перезагрузите глоссарий перед preview.",
                        "warning",
                    )
                if hasattr(self, "normalizer_source_text"):
                    # Инвалидация происходит сразу, до debounce: старый
                    # результат больше нельзя сохранить как относящийся к
                    # новой папке кэша.
                    self._schedule_normalizer_preview(delay_ms=10)
                return False
        return bool(self.load_glossary_ui())

    def reload_glossary_ui(self):
        """Перечитывает файл, не отбрасывая правки без подтверждения."""
        try:
            editor_modified = bool(self.txt_glossary.edit_modified())
        except (AttributeError, tk.TclError):
            editor_modified = False
        if (
            getattr(self, "_glossary_dirty", False) or editor_modified
        ) and not self._ask_yes_no(
            "Перезагрузить глоссарий",
            "Отбросить несохранённые изменения и перечитать glossary.json с диска?",
            icon="warning",
        ):
            return False
        return bool(self.load_glossary_ui())

    def load_glossary_ui(self):
        cache_dir = self._configured_glossary_path().parent
        path = cache_dir / "glossary.json"
        backup_path = path.with_suffix(path.suffix + ".bak")

        data = None
        recovered_from_backup = False
        candidates = []
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            candidates = [
                candidate
                for candidate in (path, backup_path)
                if candidate.exists()
            ]
            for candidate in candidates:
                try:
                    with open(candidate, 'r', encoding='utf-8-sig') as f:
                        candidate_data = json.load(f)
                    data = canonicalize_glossary_data(candidate_data)
                    recovered_from_backup = candidate == backup_path
                    break
                except Exception as exc:
                    logging.error(
                        "Ошибка чтения глоссария %s: %s", candidate, exc
                    )
        except OSError as exc:
            logging.error("Недоступна папка глоссария %s: %s", cache_dir, exc)
            self._show_error(
                "Ошибка папки кэша",
                f"Не удалось открыть редактор глоссария:\n{cache_dir}\n\n{exc}",
            )
            return False

        if data is None and candidates:
            self._show_error(
                "Ошибка глоссария",
                "Не удалось прочитать ни основной glossary.json, ни его "
                "резервную копию. Текст в редакторе оставлен без изменений; "
                "подробности записаны в журнал.",
            )
            return False
        if data is None:
            data = empty_glossary_data()

        self._glossary_ui_loading = True
        try:
            self.txt_glossary.delete(1.0, tk.END)
            self.txt_glossary.insert(
                tk.END, json.dumps(data, indent=4, ensure_ascii=False)
            )
            self.txt_glossary.edit_modified(False)
        finally:
            self._glossary_ui_loading = False
        self._glossary_loaded_path = path
        self._glossary_dirty = False

        if recovered_from_backup:
            logging.warning(
                "Редактор глоссария загрузил резервную копию %s",
                backup_path,
            )
            try:
                self._write_json_atomic(path, data)
            except Exception as recovery_exc:
                logging.error(
                    "Не удалось восстановить основной глоссарий %s: %s",
                    path,
                    recovery_exc,
                )

        if hasattr(self, "normalizer_source_text"):
            glossary_switch = getattr(
                self, "normalizer_preview_glossary_var", None
            )
            if glossary_switch is None or glossary_switch.get():
                self._schedule_normalizer_preview()
        return True

    def save_glossary_ui(self, show_popup=True):
        try:
            path = self._configured_glossary_path()
            loaded_path = getattr(self, "_glossary_loaded_path", None)
            if loaded_path is not None and Path(loaded_path) != path:
                raise ValueError(
                    "папка кэша изменилась; сначала нажмите «Перезагрузить», "
                    "чтобы не записать старый глоссарий в новый кэш"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            content = self.txt_glossary.get(1.0, tk.END).strip()
            parsed = canonicalize_glossary_data(json.loads(content))
            self._write_json_atomic(path, parsed, backup=True)
            self._glossary_loaded_path = path
            self._glossary_dirty = False
            self.txt_glossary.edit_modified(False)
            if show_popup:
                shadowed = glossary_shadowed_accent_rules(parsed)
                if shadowed:
                    self._show_warning(
                        "Сохранено с предупреждением",
                        f"Глоссарий сохранён. Правил ударения, которые не "
                        f"применяются из-за приоритета совпадающего термина: "
                        f"{len(shadowed)}.",
                    )
                else:
                    self._show_info("Успех", "Глоссарий сохранён!")
            return True
        except Exception as e:
            self._show_error("Ошибка JSON", f"Неверный формат JSON:\n{e}")
            return False

    def open_glossary_delete_dialog(self):
        """Открывает транзакционный поиск и удаление правил глоссария."""
        try:
            content = self.txt_glossary.get(1.0, tk.END).strip()
            parsed = canonicalize_glossary_data(
                json.loads(content) if content else empty_glossary_data()
            )
            records = glossary_rule_records(parsed)
        except Exception as exc:
            self._show_error(
                "Ошибка JSON",
                "Исправьте ошибки в редакторе перед удалением:\n"
                f"{exc}",
            )
            return

        if not records:
            self._show_info("Пусто", "В глоссарии нет правил для удаления.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Удаление правил глоссария")
        dialog.transient(self.root)
        dialog.grab_set()
        filter_after_id = None

        def close_dialog():
            nonlocal filter_after_id
            if filter_after_id is not None:
                try:
                    dialog.after_cancel(filter_after_id)
                except tk.TclError:
                    pass
                filter_after_id = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Escape>", lambda _event: close_dialog())

        ttk.Label(
            dialog,
            text=(
                "Отметьте правила в нужных секциях. Изменения сначала попадут "
                "в JSON-редактор; запись на диск выполняет «Сохранить файл»."
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=12, pady=(10, 6))

        search_frame = ttk.Frame(dialog)
        search_frame.pack(fill=tk.X, padx=12, pady=(0, 6))
        ttk.Label(search_frame, text="Поиск:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        group_titles = {
            "accents": "Ударения",
            "terms": "Термины",
            "regex": "RegEx",
        }
        records_by_group = {
            group: tuple(record for record in records if record["group"] == group)
            for group in group_titles
        }
        record_by_iid = {
            f"glossary_rule_{index}": record
            for index, record in enumerate(records)
        }
        iid_by_key = {
            record["key"]: iid for iid, record in record_by_iid.items()
        }
        selected_keys = set()
        group_widgets = {}

        for group, title in group_titles.items():
            tab = ttk.Frame(notebook, padding=4)
            tab.rowconfigure(0, weight=1)
            tab.columnconfigure(0, weight=1)
            notebook.add(tab, text=title)

            tree = ttk.Treeview(
                tab,
                columns=("selected", "kind", "source", "replacement", "flags"),
                show="headings",
                selectmode="extended",
            )
            tree.heading("selected", text="✓")
            tree.heading("kind", text="Тип")
            tree.heading("source", text="Исходное / паттерн")
            tree.heading("replacement", text="Замена")
            tree.heading("flags", text="Флаги")
            tree.column("selected", width=46, minwidth=46, stretch=False, anchor=tk.CENTER)
            tree.column("kind", width=90, minwidth=70, stretch=False)
            tree.column("source", width=250, minwidth=130)
            tree.column("replacement", width=250, minwidth=130)
            tree.column("flags", width=190, minwidth=120)
            tree.grid(row=0, column=0, sticky=tk.NSEW)

            y_scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=tree.yview)
            y_scroll.grid(row=0, column=1, sticky=tk.NS)
            x_scroll = ttk.Scrollbar(tab, orient=tk.HORIZONTAL, command=tree.xview)
            x_scroll.grid(row=1, column=0, sticky=tk.EW)
            tree.configure(
                yscrollcommand=y_scroll.set,
                xscrollcommand=x_scroll.set,
            )
            group_widgets[group] = {"tab": tab, "tree": tree}

        selection_frame = ttk.Frame(dialog)
        selection_frame.pack(fill=tk.X, padx=12, pady=(0, 6))
        status_var = tk.StringVar()
        status_label = ttk.Label(selection_frame, textvariable=status_var)
        status_label.pack(side=tk.RIGHT)

        action_frame = ttk.Frame(dialog)
        action_frame.pack(fill=tk.X, padx=12, pady=(0, 10))

        def current_group():
            selected_tab = notebook.select()
            for group, widgets in group_widgets.items():
                if str(widgets["tab"]) == selected_tab:
                    return group
            return "accents"

        def matching_records(group):
            query = search_var.get().strip().casefold()
            group_records = records_by_group[group]
            if not query:
                return group_records
            return tuple(
                record for record in group_records if query in record["search_text"]
            )

        def update_status():
            visible_count = len(matching_records(current_group()))
            status_var.set(
                f"Выбрано: {len(selected_keys)} из {len(records)}; "
                f"показано в разделе: {visible_count}"
            )
            delete_button.configure(
                state=tk.NORMAL if selected_keys else tk.DISABLED
            )

        def render_group(group):
            widgets = group_widgets[group]
            tree = widgets["tree"]
            children = tree.get_children("")
            if children:
                tree.delete(*children)
            for record in matching_records(group):
                iid = iid_by_key[record["key"]]
                tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        "☑" if record["key"] in selected_keys else "☐",
                        record["kind"],
                        record["source"],
                        record["replacement"],
                        record["flags"],
                    ),
                )
            selected_in_group = sum(
                record["key"] in selected_keys
                for record in records_by_group[group]
            )
            notebook.tab(
                widgets["tab"],
                text=f"{group_titles[group]} ({selected_in_group}/{len(records_by_group[group])})",
            )

        def render_all():
            nonlocal filter_after_id
            filter_after_id = None
            for group in group_titles:
                render_group(group)
            update_status()

        def schedule_filter(*_args):
            nonlocal filter_after_id
            if filter_after_id is not None:
                try:
                    dialog.after_cancel(filter_after_id)
                except tk.TclError:
                    pass
            filter_after_id = dialog.after(120, render_all)

        def toggle_iids(group, iids):
            keys = [
                record_by_iid[iid]["key"]
                for iid in iids
                if iid in record_by_iid
            ]
            if not keys:
                return
            if all(key in selected_keys for key in keys):
                selected_keys.difference_update(keys)
            else:
                selected_keys.update(keys)
            render_group(group)
            update_status()

        def on_tree_click(event, group):
            tree = group_widgets[group]["tree"]
            if tree.identify_region(event.x, event.y) != "cell":
                return None
            if tree.identify_column(event.x) != "#1":
                return None
            iid = tree.identify_row(event.y)
            if iid:
                toggle_iids(group, (iid,))
            return "break"

        def on_tree_space(_event, group):
            tree = group_widgets[group]["tree"]
            iids = tree.selection()
            if not iids and tree.focus():
                iids = (tree.focus(),)
            toggle_iids(group, iids)
            return "break"

        for group, widgets in group_widgets.items():
            widgets["tree"].bind(
                "<Button-1>",
                lambda event, selected_group=group: on_tree_click(
                    event, selected_group
                ),
            )
            widgets["tree"].bind(
                "<space>",
                lambda event, selected_group=group: on_tree_space(
                    event, selected_group
                ),
            )

        def select_visible():
            group = current_group()
            selected_keys.update(record["key"] for record in matching_records(group))
            render_group(group)
            update_status()

        def select_section():
            group = current_group()
            selected_keys.update(
                record["key"] for record in records_by_group[group]
            )
            render_group(group)
            update_status()

        def deselect_section():
            group = current_group()
            selected_keys.difference_update(
                record["key"] for record in records_by_group[group]
            )
            render_group(group)
            update_status()

        ttk.Button(
            selection_frame,
            text="Выбрать результаты поиска",
            command=select_visible,
        ).pack(side=tk.LEFT)
        ttk.Button(
            selection_frame,
            text="Выбрать все в разделе",
            command=select_section,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            selection_frame,
            text="Снять всё в разделе",
            command=deselect_section,
        ).pack(side=tk.LEFT)

        def apply_selected_deletion():
            if not selected_keys:
                return
            if not self._ask_yes_no(
                "Удалить правила",
                f"Удалить выбранные правила ({len(selected_keys)}) "
                "из JSON-редактора?\n\n"
                "Файл на диске изменится только после кнопки «Сохранить файл».",
                icon="warning",
                parent=dialog,
            ):
                return
            updated, removed = remove_glossary_rules(parsed, selected_keys)
            self._replace_glossary_editor_data(updated)
            close_dialog()
            self._show_info(
                "Глоссарий изменён",
                f"Из редактора удалено правил: {removed}.\n"
                "Для записи на диск нажмите «Сохранить файл».",
            )

        def apply_full_clear():
            if not self._ask_yes_no(
                "Очистить весь глоссарий",
                f"Убрать все правила ({len(records)}) из JSON-редактора?\n\n"
                "Неизвестные служебные поля JSON будут сохранены. "
                "Файл на диске изменится только после кнопки «Сохранить файл».",
                icon="warning",
                parent=dialog,
            ):
                return
            updated, removed = clear_glossary_rules(parsed)
            self._replace_glossary_editor_data(updated)
            close_dialog()
            self._show_info(
                "Глоссарий очищен",
                f"Из редактора удалено правил: {removed}.\n"
                "Для записи на диск нажмите «Сохранить файл».",
            )

        ttk.Button(
            action_frame,
            text="🔥 Очистить весь глоссарий…",
            command=apply_full_clear,
        ).pack(side=tk.LEFT)
        ttk.Button(
            action_frame,
            text="Отмена",
            command=close_dialog,
        ).pack(side=tk.RIGHT)
        delete_button = ttk.Button(
            action_frame,
            text="🗑 Удалить выбранные…",
            command=apply_selected_deletion,
            state=tk.DISABLED,
        )
        delete_button.pack(side=tk.RIGHT, padx=5)

        search_var.trace_add("write", schedule_filter)
        notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _event: update_status(),
            add="+",
        )
        render_all()
        self._center_popup(dialog, 940, 560, fit_screen=True)
        search_entry.focus_set()

    # --- Вкладка "Нормализатор" (v1.5) ---
    def setup_normalizer_tab(self):
        self._normalizer_preview_after_id = None
        self._normalizer_preview_generation = 0
        self._normalizer_last_preview_result = None
        self._normalizer_ui_loading = True
        self.normalizer_preview_vars = {}

        frame = ttk.Frame(self.tab_normalizer, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        toolbar.columnconfigure(7, weight=1)

        ttk.Label(toolbar, text="Профиль:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 4)
        )
        self.normalizer_profile_choice = tk.StringVar(
            value="TTS (для озвучки)"
        )
        self.normalizer_profile_combobox = ttk.Combobox(
            toolbar,
            textvariable=self.normalizer_profile_choice,
            values=(
                "TTS (для озвучки)",
                "Safe (бережный)",
                "Пользовательский",
            ),
            state="readonly",
            width=22,
        )
        self.normalizer_profile_combobox.grid(row=0, column=1, sticky=tk.W)
        self.normalizer_profile_combobox.bind(
            "<<ComboboxSelected>>", self._on_normalizer_profile_selected
        )

        self.normalizer_preview_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="ru-normalizr",
            variable=self.normalizer_preview_enabled_var,
            command=self._normalizer_option_changed,
        ).grid(row=0, column=2, sticky=tk.W, padx=(12, 4))

        self.normalizer_preview_glossary_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="Глоссарий",
            variable=self.normalizer_preview_glossary_var,
            command=self._normalizer_option_changed,
        ).grid(row=0, column=3, sticky=tk.W, padx=4)

        self.normalizer_preview_auto_abbr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="Автоаббревиатуры Studio",
            variable=self.normalizer_preview_auto_abbr_var,
            command=self._normalizer_option_changed,
        ).grid(row=0, column=4, sticky=tk.W, padx=4)

        self.normalizer_preview_short_words_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="Короткие сокращения Studio",
            variable=self.normalizer_preview_short_words_var,
            command=self._normalizer_option_changed,
        ).grid(row=0, column=5, sticky=tk.W, padx=4)

        actions = ttk.Frame(toolbar)
        actions.grid(row=1, column=0, columnspan=8, sticky=tk.EW, pady=(6, 0))
        ttk.Button(
            actions,
            text="↩ Вернуть глобальные",
            command=self._load_normalizer_preview_from_config,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="📂 Импорт профиля",
            command=self.import_normalizer_profile,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            actions,
            text="📤 Экспорт профиля",
            command=self.export_normalizer_profile,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="✓ Применить глобально",
            command=self.apply_normalizer_preview_globally,
        ).pack(side=tk.RIGHT)
        self.lbl_normalizer_scope_status = ttk.Label(
            toolbar,
            text="",
            anchor=tk.W,
            wraplength=900,
            foreground=self.get_status_color("info"),
        )
        self.lbl_normalizer_scope_status.grid(
            row=2, column=0, columnspan=8, sticky=tk.EW, pady=(5, 0)
        )
        self._status_label_kinds[self.lbl_normalizer_scope_status] = "info"

        main_pane = ttk.Panedwindow(frame, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True)

        # Text по умолчанию запрашивает ширину около 80 символов. Для двух
        # редакторов это незаметно перетягивало sash вправо и обрезало вторую
        # колонку настроек на небольшом экране. Задаём компактный initial size
        # и отдаём больше свободной ширины панели параметров; sash по-прежнему
        # можно перетащить вручную.
        text_pane = ttk.Frame(main_pane, width=440)
        settings_pane = ttk.Frame(
            main_pane, width=560, padding=(8, 0, 0, 0)
        )
        main_pane.add(text_pane, weight=2)
        main_pane.add(settings_pane, weight=3)

        editor_pane = ttk.Panedwindow(text_pane, orient=tk.VERTICAL)

        source_frame = ttk.LabelFrame(
            editor_pane, text="Исходный текст (можно редактировать)", padding=5
        )
        result_frame = ttk.LabelFrame(
            editor_pane, text="Результат полного pipeline синтеза", padding=5
        )
        editor_pane.add(source_frame, weight=1)
        editor_pane.add(result_frame, weight=1)

        source_scroll = ttk.Scrollbar(source_frame, orient=tk.VERTICAL)
        source_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.normalizer_source_text = tk.Text(
            source_frame,
            wrap=tk.WORD,
            width=44,
            undo=True,
            maxundo=50,
            font=("Arial", self.font_size_var.get()),
            yscrollcommand=source_scroll.set,
        )
        self.normalizer_source_text.pack(fill=tk.BOTH, expand=True)
        source_scroll.configure(command=self.normalizer_source_text.yview)
        self.normalizer_source_text.bind(
            "<<Modified>>", self._on_normalizer_source_modified
        )
        self.normalizer_source_text.bind(
            "<Control-Return>", lambda _event: self.run_normalizer_preview()
        )
        self.normalizer_source_text.bind(
            "<Command-Return>", lambda _event: self.run_normalizer_preview()
        )

        result_scroll = ttk.Scrollbar(result_frame, orient=tk.VERTICAL)
        result_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.normalizer_result_text = tk.Text(
            result_frame,
            wrap=tk.WORD,
            width=44,
            state=tk.DISABLED,
            font=("Arial", self.font_size_var.get()),
            yscrollcommand=result_scroll.set,
        )
        self.normalizer_result_text.pack(fill=tk.BOTH, expand=True)
        result_scroll.configure(command=self.normalizer_result_text.yview)

        # Нижнюю панель резервируем до expand-панели редакторов. Иначе Tk при
        # небольшой высоте отдаёт Panedwindow всю полость и визуально обрезает
        # кнопки, хотя они присутствуют в дереве виджетов.
        preview_controls = ttk.Frame(text_pane)
        preview_controls.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        preview_actions = ttk.Frame(preview_controls)
        preview_actions.pack(fill=tk.X)
        ttk.Button(
            preview_actions,
            text="▶ Нормализовать (Ctrl/⌘+Enter)",
            command=self.run_normalizer_preview,
        ).pack(side=tk.LEFT)
        self.btn_save_normalized_text = ttk.Button(
            preview_actions,
            text="💾 Сохранить TXT…",
            command=self.save_normalized_text_to_file,
            state=tk.DISABLED,
        )
        self.btn_save_normalized_text.pack(side=tk.LEFT, padx=(6, 0))
        preview_status_row = ttk.Frame(preview_controls)
        preview_status_row.pack(fill=tk.X, pady=(4, 0))
        self.normalizer_font_combobox = ttk.Combobox(
            preview_status_row,
            textvariable=self.font_size_var,
            values=(10, 12, 14, 16, 18, 20, 24),
            state="readonly",
            width=5,
        )
        self.normalizer_font_combobox.pack(side=tk.RIGHT)
        ttk.Label(preview_status_row, text="Шрифт:").pack(
            side=tk.RIGHT, padx=(12, 5)
        )
        self.normalizer_font_combobox.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.root.after(10, self.update_fonts),
        )
        self.lbl_normalizer_preview_status = ttk.Label(
            preview_status_row,
            text="Введите несколько предложений для предпросмотра.",
            anchor=tk.W,
            wraplength=360,
            foreground=self.get_status_color("info"),
        )
        self.lbl_normalizer_preview_status.pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        preview_status_row.bind(
            "<Configure>",
            lambda event: self.lbl_normalizer_preview_status.configure(
                wraplength=max(180, event.width - 125)
            ),
            add="+",
        )
        self._status_label_kinds[self.lbl_normalizer_preview_status] = "info"
        editor_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        settings_tabs = ttk.Notebook(settings_pane)
        settings_tabs.pack(fill=tk.BOTH, expand=True)
        stages_tab = ttk.Frame(settings_tabs, padding=8)
        advanced_tab = ttk.Frame(settings_tabs, padding=8)
        settings_tabs.add(stages_tab, text="Этапы")
        settings_tabs.add(advanced_tab, text="Расширенные")

        stage_labels = (
            ("enable_caps_normalization", "Нормализация CAPS"),
            ("enable_first_word_decap", "Регистр первого слова"),
            ("remove_links", "Удалять цифровые сноски"),
            ("enable_url_normalization", "Произносить URL"),
            ("enable_year_normalization", "Годы и десятилетия"),
            ("enable_roman_normalization", "Римские числа"),
            ("enable_dates_time_normalization", "Даты и время"),
            ("enable_numeral_normalization", "Числа и дроби"),
            ("enable_abbreviation_expansion", "Сокращения"),
            (
                "enable_contextual_abbreviation_expansion",
                "Контекстные сокращения",
            ),
            ("enable_years_ago_expansion", "Сокращение «л. н.»"),
            ("enable_initials_expansion", "Инициалы"),
            (
                "enable_letter_abbreviation_expansion",
                "Буквенные аббревиатуры",
            ),
            ("enable_dictionary_normalization", "Словари ru-normalizr"),
            ("enable_latinization", "Латиница → кириллица"),
            (
                "enable_latinization_stress_marks",
                "Ударения при латинизации",
            ),
        )
        for index, (key, label) in enumerate(stage_labels):
            variable = tk.BooleanVar()
            self.normalizer_preview_vars[key] = variable
            ttk.Checkbutton(
                stages_tab,
                text=label,
                variable=variable,
                command=self._normalizer_option_changed,
            ).grid(
                row=index % 8,
                column=index // 8,
                sticky=tk.W,
                padx=(0, 12),
                pady=3,
            )
        stages_tab.columnconfigure(0, weight=1)
        stages_tab.columnconfigure(1, weight=1)

        advanced_tab.columnconfigure(1, weight=1)

        def add_preview_combo(row, label, key, values):
            ttk.Label(advanced_tab, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 6), pady=3
            )
            variable = tk.StringVar()
            self.normalizer_preview_vars[key] = variable
            widget = ttk.Combobox(
                advanced_tab,
                textvariable=variable,
                values=values,
                state="readonly",
            )
            widget.grid(row=row, column=1, sticky=tk.EW, pady=3)
            widget.bind(
                "<<ComboboxSelected>>",
                lambda _event: self._normalizer_option_changed(),
            )

        add_preview_combo(
            0, "Гласные в инициалах:", "initials_vowel_mode", ("single", "double")
        )
        add_preview_combo(
            1,
            "Паузы между инициалами:",
            "initials_pause_mode",
            ("preserve", "comma"),
        )
        add_preview_combo(
            2,
            "Латинизация:",
            "latinization_backend",
            ("ipa", "dictionary"),
        )

        interval_frame = ttk.Frame(advanced_tab)
        interval_frame.grid(row=3, column=1, sticky=tk.EW, pady=3)
        self.normalizer_ignore_start_var = tk.StringVar()
        self.normalizer_ignore_end_var = tk.StringVar()
        ttk.Entry(
            interval_frame,
            textvariable=self.normalizer_ignore_start_var,
            width=8,
        ).pack(side=tk.LEFT)
        ttk.Label(interval_frame, text=" … ").pack(side=tk.LEFT)
        ttk.Entry(
            interval_frame,
            textvariable=self.normalizer_ignore_end_var,
            width=8,
        ).pack(side=tk.LEFT)
        ttk.Label(advanced_tab, text="Не удалять номера сносок:").grid(
            row=3, column=0, sticky=tk.W, padx=(0, 6), pady=3
        )

        def add_preview_entry(row, label, key):
            ttk.Label(advanced_tab, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 6), pady=3
            )
            variable = tk.StringVar()
            self.normalizer_preview_vars[key] = variable
            entry = ttk.Entry(advanced_tab, textvariable=variable)
            entry.grid(row=row, column=1, sticky=tk.EW, pady=3)
            entry.bind("<KeyRelease>", lambda _event: self._normalizer_option_changed())
            return entry

        add_preview_entry(4, "Файл словаря латинизации:", "latin_dictionary_filename")
        add_preview_entry(5, "Включить только .dic (через запятую):", "dictionary_include_files")
        add_preview_entry(6, "Исключить .dic (через запятую):", "dictionary_exclude_files")

        ttk.Label(
            advanced_tab,
            text="Каталог словарей (локальный путь):",
        ).grid(row=7, column=0, sticky=tk.W, padx=(0, 6), pady=3)
        dictionary_path_frame = ttk.Frame(advanced_tab)
        dictionary_path_frame.grid(row=7, column=1, sticky=tk.EW, pady=3)
        dictionary_path_frame.columnconfigure(0, weight=1)
        dictionary_path_var = tk.StringVar()
        self.normalizer_preview_vars["dictionaries_path"] = dictionary_path_var
        dictionary_path_entry = ttk.Entry(
            dictionary_path_frame, textvariable=dictionary_path_var
        )
        dictionary_path_entry.grid(row=0, column=0, sticky=tk.EW)
        dictionary_path_entry.bind(
            "<KeyRelease>", lambda _event: self._normalizer_option_changed()
        )

        def choose_dictionary_path():
            selected = filedialog.askdirectory(
                initialdir=resolve_dialog_initial_dir(
                    dictionary_path_var.get(), BASE_DIR
                )
            )
            if selected:
                dictionary_path_var.set(selected)
                self._normalizer_option_changed()

        ttk.Button(
            dictionary_path_frame,
            text="📁",
            width=3,
            command=choose_dictionary_path,
        ).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(
            advanced_tab,
            text=(
                "Локальный путь применяется глобально, но не включается в "
                "экспортируемый профиль."
            ),
            wraplength=360,
            foreground=self.get_status_color("muted"),
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        for variable in (
            self.normalizer_ignore_start_var,
            self.normalizer_ignore_end_var,
        ):
            variable.trace_add(
                "write", lambda *_args: self._normalizer_option_changed()
            )

        self._load_normalizer_preview_from_config()
        self._normalizer_ui_loading = False

    def _open_normalizer_tab_from_settings(self):
        self.notebook.select(self.tab_normalizer)
        if hasattr(self, "normalizer_source_text"):
            self.root.after_idle(self.normalizer_source_text.focus_set)

    def _refresh_global_normalizer_settings_summary(self):
        summary = describe_normalizer_settings(self.config)
        if hasattr(self, "lbl_settings_normalizer_profile"):
            self.lbl_settings_normalizer_profile.configure(
                text=f"Глобальный профиль: {summary['profile']}"
            )
        if hasattr(self, "lbl_settings_normalizer_details"):
            self.lbl_settings_normalizer_details.configure(
                text=summary["details"]
            )

    def _refresh_normalizer_scope_status(self):
        if not hasattr(self, "lbl_normalizer_scope_status"):
            return
        summary = describe_normalizer_settings(self.config)
        try:
            local = normalizer_settings_snapshot(
                self._collect_normalizer_preview_config()
            )
            saved = normalizer_settings_snapshot(self.config)
            matches = local == saved
        except Exception:
            matches = False
        if matches:
            text = (
                f"Глобально: {summary['profile']} • локальные настройки "
                "совпадают с сохранёнными"
            )
            status = "success"
        else:
            text = (
                f"Глобально: {summary['profile']} • есть неприменённые "
                "локальные изменения"
            )
            status = "warning"
        self._set_status_label(self.lbl_normalizer_scope_status, text, status)

    def _normalizer_option_changed(self, *_args):
        if getattr(self, "_normalizer_ui_loading", False):
            return
        self.normalizer_profile_choice.set("Пользовательский")
        self._refresh_normalizer_scope_status()
        self._schedule_normalizer_preview()

    def _on_normalizer_profile_selected(self, _event=None):
        if getattr(self, "_normalizer_ui_loading", False):
            return
        choice = self.normalizer_profile_choice.get()
        mode = {
            "TTS (для озвучки)": "tts",
            "Safe (бережный)": "safe",
        }.get(choice)
        if mode is None:
            return
        config = {
            "normalizer_enabled": True,
            "normalizer_mode": mode,
            "normalizer_options": normalizer_mode_defaults(mode),
            "glossary_enabled": True,
            "auto_abbreviations": True,
            "auto_short_words": True,
        }
        self._set_normalizer_preview_config(config, selected_choice=choice)
        self._schedule_normalizer_preview(delay_ms=10)

    def _set_normalizer_preview_config(self, config, selected_choice=None):
        normalized = normalize_config(config)
        mode = normalized.get("normalizer_mode", "tts")
        options = resolved_normalizer_options(normalized)
        self._normalizer_ui_loading = True
        try:
            self._normalizer_preview_mode = mode
            self.normalizer_preview_enabled_var.set(
                _config_bool(normalized.get("normalizer_enabled", True), default=True)
            )
            self.normalizer_preview_glossary_var.set(
                _config_bool(normalized.get("glossary_enabled", True), default=True)
            )
            self.normalizer_preview_auto_abbr_var.set(
                _config_bool(normalized.get("auto_abbreviations", True), default=True)
            )
            self.normalizer_preview_short_words_var.set(
                _config_bool(normalized.get("auto_short_words", True), default=True)
            )
            for key in NORMALIZER_BOOLEAN_OPTIONS:
                self.normalizer_preview_vars[key].set(bool(options[key]))
            for key in NORMALIZER_ENUM_OPTIONS:
                self.normalizer_preview_vars[key].set(str(options[key]))
            interval = options["remove_links_ignore_interval"]
            self.normalizer_ignore_start_var.set(str(interval[0]))
            self.normalizer_ignore_end_var.set(str(interval[1]))
            self.normalizer_preview_vars["latin_dictionary_filename"].set(
                options["latin_dictionary_filename"]
            )
            for key in NORMALIZER_SEQUENCE_OPTIONS:
                self.normalizer_preview_vars[key].set(", ".join(options[key]))
            self.normalizer_preview_vars["dictionaries_path"].set(
                options["dictionaries_path"]
            )

            if selected_choice is None:
                is_builtin = (
                    options == normalizer_mode_defaults(mode)
                    and self.normalizer_preview_enabled_var.get()
                    and self.normalizer_preview_glossary_var.get()
                    and self.normalizer_preview_auto_abbr_var.get()
                    and self.normalizer_preview_short_words_var.get()
                )
                selected_choice = (
                    "TTS (для озвучки)"
                    if is_builtin and mode == "tts"
                    else (
                        "Safe (бережный)"
                        if is_builtin and mode == "safe"
                        else "Пользовательский"
                    )
                )
            self.normalizer_profile_choice.set(selected_choice)
        finally:
            self._normalizer_ui_loading = False
        self._refresh_normalizer_scope_status()

    def _load_normalizer_preview_from_config(self):
        self._set_normalizer_preview_config(self.config)
        self._schedule_normalizer_preview(delay_ms=10)

    def _collect_normalizer_preview_config(self):
        raw_options = {}
        for key in NORMALIZER_BOOLEAN_OPTIONS:
            raw_options[key] = self.normalizer_preview_vars[key].get()
        for key in NORMALIZER_ENUM_OPTIONS:
            raw_options[key] = self.normalizer_preview_vars[key].get()
        raw_options["remove_links_ignore_interval"] = [
            self.normalizer_ignore_start_var.get(),
            self.normalizer_ignore_end_var.get(),
        ]
        raw_options["latin_dictionary_filename"] = (
            self.normalizer_preview_vars["latin_dictionary_filename"].get()
        )
        for key in NORMALIZER_SEQUENCE_OPTIONS:
            raw_options[key] = self.normalizer_preview_vars[key].get()
        raw_options["dictionaries_path"] = self.normalizer_preview_vars[
            "dictionaries_path"
        ].get()

        mode = getattr(self, "_normalizer_preview_mode", "tts")
        options = normalize_normalizer_options(raw_options, mode, full=True)
        config = dict(self.config)
        config.update(
            {
                "normalizer_enabled": self.normalizer_preview_enabled_var.get(),
                "normalizer_mode": mode,
                "normalizer_options": options,
                "glossary_enabled": self.normalizer_preview_glossary_var.get(),
                "auto_abbreviations": self.normalizer_preview_auto_abbr_var.get(),
                "auto_short_words": self.normalizer_preview_short_words_var.get(),
            }
        )
        return config

    def _on_normalizer_source_modified(self, _event=None):
        if not self.normalizer_source_text.edit_modified():
            return
        self.normalizer_source_text.edit_modified(False)
        self._schedule_normalizer_preview()

    def _schedule_normalizer_preview(self, *_args, delay_ms=350):
        if not hasattr(self, "normalizer_source_text"):
            return
        # Изменённый источник или параметр немедленно делает предыдущий preview
        # устаревшим. Инвалидация до debounce также не даёт позднему worker
        # снова включить Save для результата, рассчитанного по старым данным.
        self._normalizer_preview_generation += 1
        self._normalizer_last_preview_result = None
        if hasattr(self, "btn_save_normalized_text"):
            self.btn_save_normalized_text.configure(state=tk.DISABLED)
        pending = getattr(self, "_normalizer_preview_after_id", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._normalizer_preview_after_id = self.root.after(
            delay_ms, self.run_normalizer_preview
        )

    def _normalizer_glossary_snapshot(self, enabled):
        if not enabled:
            return empty_glossary_data()
        loaded_path = getattr(self, "_glossary_loaded_path", None)
        configured_path = self._configured_glossary_path()
        if loaded_path is not None and Path(loaded_path) != configured_path:
            raise ValueError(
                "папка кэша изменилась; перезагрузите редактор глоссария"
            )
        content = self.txt_glossary.get(1.0, tk.END).strip()
        data = json.loads(content) if content else empty_glossary_data()
        return canonicalize_glossary_data(data)

    def run_normalizer_preview(self):
        pending = getattr(self, "_normalizer_preview_after_id", None)
        self._normalizer_preview_after_id = None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                # При вызове самим after() callback уже снят из очереди Tcl.
                pass
        self._normalizer_last_preview_result = None
        self.btn_save_normalized_text.configure(state=tk.DISABLED)
        source = self.normalizer_source_text.get(1.0, tk.END).rstrip("\n")
        self._normalizer_preview_generation += 1
        generation = self._normalizer_preview_generation
        if not source:
            self._finish_normalizer_preview(
                generation,
                {
                    "normalized_text": "",
                    "normalized_text_for_file": "",
                    "sentence_count": 0,
                    "skipped": [],
                },
                None,
            )
            return "break"
        try:
            config = self._collect_normalizer_preview_config()
            glossary = self._normalizer_glossary_snapshot(
                config.get("glossary_enabled", True)
            )
        except Exception as exc:
            self._set_status_label(
                self.lbl_normalizer_preview_status,
                f"Ошибка настроек/глоссария: {exc}",
                "error",
            )
            return "break"

        self._set_status_label(
            self.lbl_normalizer_preview_status,
            "Нормализация...",
            "warning",
        )

        def worker():
            try:
                context = TTSProcessor.normalization_context(config, glossary)
                result = context.preview_normalization(source)
                error = None
            except Exception as exc:
                result = None
                error = exc
            self._post_to_ui(
                self._finish_normalizer_preview,
                generation,
                result,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()
        return "break"

    def _finish_normalizer_preview(self, generation, result, error):
        if generation != self._normalizer_preview_generation:
            return
        self.normalizer_result_text.configure(state=tk.NORMAL)
        self.normalizer_result_text.delete(1.0, tk.END)
        if error is None and result is not None:
            self.normalizer_result_text.insert(
                tk.END, result.get("normalized_text", "")
            )
        self.normalizer_result_text.configure(state=tk.DISABLED)
        if error is not None:
            logging.error("Ошибка предпросмотра нормализации: %s", error)
            self._set_status_label(
                self.lbl_normalizer_preview_status,
                f"Ошибка: {error}",
                "error",
            )
            return
        self._normalizer_last_preview_result = copy.deepcopy(result)
        savable_text = result.get("normalized_text_for_file", "")
        self.btn_save_normalized_text.configure(
            state=tk.NORMAL if savable_text.strip() else tk.DISABLED
        )
        skipped = len(result.get("skipped", []))
        status = f"Готово: предложений {result.get('sentence_count', 0)}"
        if skipped:
            status += f", пропущено неподдерживаемых фрагментов: {skipped}"
        self._set_status_label(
            self.lbl_normalizer_preview_status,
            status,
            "warning" if skipped else "success",
        )

    def save_normalized_text_to_file(self):
        """Сохраняет завершённый preview как повторно используемый UTF-8 TXT."""
        result = getattr(self, "_normalizer_last_preview_result", None)
        text = (
            result.get("normalized_text_for_file", "")
            if isinstance(result, dict)
            else ""
        )
        if not text.strip():
            self._show_warning(
                "Нет результата",
                "Сначала нормализуйте непустой текст.",
            )
            return
        skipped = result.get("skipped", []) if isinstance(result, dict) else []
        if skipped and not self._ask_yes_no(
            "Есть пропущенные фрагменты",
            f"При нормализации пропущено фрагментов без поддерживаемого "
            f"текста: {len(skipped)}. В сохранённом TXT их не будет.\n\n"
            "Сохранить неполный результат?",
            icon="warning",
        ):
            return
        filepath = filedialog.asksaveasfilename(
            initialdir=resolve_dialog_initial_dir(
                self.config.get("last_normalizer_text_dir"),
                self.config.get("input_dir"),
                BASE_DIR,
            ),
            defaultextension=".txt",
            filetypes=[("Текст UTF-8", "*.txt"), ("Все файлы", "*.*")],
            initialfile="normalized_text.txt",
        )
        if not filepath:
            return
        try:
            _write_text_atomic(filepath, text)
            self.config["last_normalizer_text_dir"] = str(Path(filepath).parent)
            self.save_settings()
            self._show_info(
                "Текст сохранён",
                f"Нормализованный UTF-8 TXT без BOM сохранён в:\n{filepath}\n\n"
                "Для повторного синтеза включите «Текст уже подготовлен». "
                "Строки пауз записаны каноническим разделителем и используют "
                "текущие настройки пауз.",
            )
        except Exception as exc:
            self._show_error(
                "Ошибка",
                f"Не удалось сохранить нормализованный текст:\n{exc}",
            )

    def apply_normalizer_preview_globally(self):
        try:
            local = self._collect_normalizer_preview_config()
        except Exception as exc:
            self._show_error("Некорректный профиль", str(exc))
            return
        candidate_config = copy.deepcopy(self.config)
        for key in (
            "normalizer_enabled",
            "normalizer_mode",
            "normalizer_options",
            "glossary_enabled",
            "auto_abbreviations",
            "auto_short_words",
        ):
            candidate_config[key] = copy.deepcopy(local[key])
        try:
            self._persist_settings_snapshot(candidate_config)
        except Exception as exc:
            logging.error("Не удалось применить профиль нормализатора: %s", exc)
            self._show_error(
                "Ошибка сохранения",
                f"Профиль не применён:\n{exc}",
            )
            return

        self.config = candidate_config
        previous_update_state = getattr(self, "_is_updating_ui", False)
        self._is_updating_ui = True
        try:
            for key in ("auto_abbreviations", "auto_short_words"):
                variable = self.settings_vars.get(key)
                if variable is not None:
                    variable.set(self.config[key])
        finally:
            self._is_updating_ui = previous_update_state
        self._refresh_global_normalizer_settings_summary()
        self._refresh_normalizer_scope_status()
        self._show_info(
            "Профиль применён",
            "Настройки нормализатора сохранены глобально и будут "
            "использоваться новым синтезом.",
        )

    def export_normalizer_profile(self):
        try:
            config = self._collect_normalizer_preview_config()
            profile = normalizer_profile_from_config(
                config,
                name=self.normalizer_profile_choice.get(),
            )
        except Exception as exc:
            self._show_error("Некорректный профиль", str(exc))
            return
        filepath = filedialog.asksaveasfilename(
            initialdir=self._config_dialog_initial_dir(),
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile="normalizer_profile.json",
        )
        if not filepath:
            return
        try:
            self._write_json_atomic(filepath, profile)
            self._remember_dialog_directory("last_config_dir", filepath)
            self._show_info(
                "Профиль экспортирован",
                f"Файл сохранён:\n{filepath}\n\n"
                "Локальный путь к каталогу словарей не переносится.",
            )
        except Exception as exc:
            self._show_error("Ошибка", f"Не удалось экспортировать профиль:\n{exc}")

    def import_normalizer_profile(self):
        filepath = filedialog.askopenfilename(
            initialdir=self._config_dialog_initial_dir(),
            filetypes=[("JSON files", "*.json")],
        )
        if not filepath:
            return
        try:
            with open(filepath, "r", encoding="utf-8-sig") as file:
                profile = normalize_normalizer_profile(json.load(file))
            local = apply_normalizer_profile(self.config, profile)
            self._set_normalizer_preview_config(
                local, selected_choice="Пользовательский"
            )
            self._remember_dialog_directory("last_config_dir", filepath)
            self._schedule_normalizer_preview(delay_ms=10)
        except Exception as exc:
            self._show_error("Ошибка", f"Не удалось импортировать профиль:\n{exc}")

    # --- Переносимые аудиопрофили (v1.5) ---
    def _audio_profile_from_current_ui(self, target, name="Новый профиль"):
        if target == "export":
            return make_audio_profile(
                name,
                output_format=self.export_fmt_var.get(),
                bitrate=self.export_bitrate_var.get(),
                sample_rate=self.export_sample_rate_var.get(),
                channels=self.export_channels_var.get(),
                effects_enabled=self.export_apply_fx_var.get(),
                speed=self.exp_speed_var.get(),
                pitch=self.exp_pitch_var.get(),
                echo=self.exp_echo_var.get(),
                echo_delay=self.exp_delay_var.get(),
                echo_decay=self.exp_decay_var.get(),
            )
        if target != "book":
            raise ValueError("неизвестная область аудиопрофиля")

        def setting(key, default):
            variable = self.settings_vars.get(key)
            return variable.get() if variable is not None else self.config.get(key, default)

        speed = float(setting("fx_speed", 1.0))
        pitch = float(setting("fx_pitch", 1.0))
        echo = _config_bool(setting("fx_echo", False))
        effects_enabled = echo or speed != 1.0 or pitch != 1.0
        return make_audio_profile(
            name,
            output_format=setting("output_format", "mp3"),
            bitrate=setting("output_bitrate", "128k"),
            sample_rate=setting("output_sample_rate", "auto"),
            channels=setting("output_channels", "auto"),
            effects_enabled=effects_enabled,
            speed=speed,
            pitch=pitch,
            echo=echo,
            echo_delay=setting("fx_echo_delay", 300),
            echo_decay=setting("fx_echo_decay", 0.3),
        )

    def _current_audio_profile_summary(self, target):
        profile = self._audio_profile_from_current_ui(target, "Текущие параметры")
        matching_name = matching_audio_profile_name(
            profile, self.config.get("audio_profiles", [])
        )
        prefix = (
            f"Совпадает с профилем «{matching_name}»"
            if matching_name
            else "Пользовательские параметры"
        )
        return f"{prefix}: {describe_audio_profile(profile)}"

    def _refresh_audio_profile_summaries(self):
        for target, label_name in (
            ("book", "lbl_book_audio_profile_summary"),
            ("export", "lbl_export_audio_profile_summary"),
        ):
            label = getattr(self, label_name, None)
            if label is None:
                continue
            try:
                label.configure(text=self._current_audio_profile_summary(target))
            except (AttributeError, KeyError, tk.TclError, TypeError, ValueError):
                # Во время поэтапного построения/загрузки UI не все Tk-переменные
                # уже существуют. Финальный set_ui_from_config обновит подпись.
                label.configure(text="Параметры профиля пока недоступны.")

    def _apply_audio_profile_to_ui(self, profile, target, *, save=True):
        values = audio_profile_config_values(profile, target)
        if target == "book":
            _select_book_audio_profile(
                values["output_format"],
                sample_rate=values["output_sample_rate"],
                channels=values["output_channels"],
                bitrate=values["output_bitrate"],
            )
            self.config.update(copy.deepcopy(values))
            for key, value in values.items():
                variable = self.settings_vars.get(key)
                if variable is not None:
                    variable.set(value)
            if hasattr(self, "dir_speed_var"):
                self.dir_speed_var.set(values["fx_speed"])
                self.dir_pitch_var.set(values["fx_pitch"])
                self.dir_echo_var.set(values["fx_echo"])
                self.dir_echo_delay_var.set(values["fx_echo_delay"])
                self.dir_echo_decay_var.set(values["fx_echo_decay"])
                self.lbl_dir_speed.configure(text=f"{values['fx_speed']:.1f}x")
                self.lbl_dir_pitch.configure(text=f"{values['fx_pitch']:.2f}")
                self.lbl_dir_delay.configure(text=f"{values['fx_echo_delay']}мс")
                self.lbl_dir_decay.configure(text=f"{values['fx_echo_decay']:.1f}")
            if hasattr(self, "lbl_speed_val"):
                self.lbl_speed_val.configure(text=f"{values['fx_speed']:.1f}x")
                self.lbl_pitch_val.configure(text=f"{values['fx_pitch']:.2f}")
                self.lbl_delay_val.configure(text=f"{values['fx_echo_delay']}мс")
                self.lbl_decay_val.configure(text=f"{values['fx_echo_decay']:.1f}")
        elif target == "export":
            self.config.update(copy.deepcopy(values))
            self.export_fmt_var.set(values["export_format"])
            self.export_bitrate_var.set(values["export_bitrate"])
            self.export_sample_rate_var.set(values["export_sample_rate"])
            self.export_channels_var.set(values["export_channels"])
            self.export_apply_fx_var.set(values["export_apply_fx"])
            self.exp_speed_var.set(values["export_fx_speed"])
            self.exp_pitch_var.set(values["export_fx_pitch"])
            self.exp_echo_var.set(values["export_fx_echo"])
            self.exp_delay_var.set(values["export_fx_echo_delay"])
            self.exp_decay_var.set(values["export_fx_echo_decay"])
            self.lbl_exp_speed.configure(text=f"{values['export_fx_speed']:.1f}x")
            self.lbl_exp_pitch.configure(text=f"{values['export_fx_pitch']:.2f}")
            self.lbl_exp_delay.configure(text=str(values["export_fx_echo_delay"]))
            self.lbl_exp_decay.configure(text=f"{values['export_fx_echo_decay']:.1f}")
            self._sync_export_mode_controls()
        else:
            raise ValueError("неизвестная область аудиопрофиля")
        if save:
            self.save_settings()
        self._refresh_audio_profile_summaries()

    def _audio_profile_dialog_initial_dir(self):
        return resolve_dialog_initial_dir(
            self.config.get("last_audio_profile_dir"),
            self.config.get("last_config_dir"),
            BASE_DIR,
        )

    def _remember_audio_profile_directory(self, path):
        candidate = Path(path).expanduser()
        directory = candidate if candidate.is_dir() else candidate.parent
        self.config["last_audio_profile_dir"] = str(directory)

    def open_audio_profiles_dialog(self, preferred_target="export"):
        """Открывает отдельный менеджер, не раздувая компактные панели вкладок."""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Аудиопрофили")
        dialog.transient(self.root)
        dialog.grab_set()

        staged_profiles = []
        for stored_profile in self.config.get("audio_profiles", []):
            try:
                staged_profiles.append(normalize_audio_profile(stored_profile))
            except (TypeError, ValueError):
                continue
        staged_last_dir = {
            "value": str(self.config.get("last_audio_profile_dir", ""))
        }
        staged_dirty = {"value": False}

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        # Footer резервируется первым: на низком экране expand-панель редактора
        # не может вытеснить подсказку, кнопки целей, сохранение или отмену.
        footer = ttk.LabelFrame(outer, text="Применение", padding=7)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        pane = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        list_frame = ttk.LabelFrame(pane, text="Профили", padding=6)
        editor = ttk.LabelFrame(pane, text="Параметры", padding=8)
        pane.add(list_frame, weight=2)
        pane.add(editor, weight=3)

        profile_list = tk.Listbox(list_frame, exportselection=False)
        profile_scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=profile_list.yview
        )
        profile_list.configure(yscrollcommand=profile_scroll.set)
        profile_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        profile_list.pack(fill=tk.BOTH, expand=True)

        name_var = tk.StringVar(value="Новый профиль")
        format_var = tk.StringVar(value="opus")
        bitrate_var = tk.StringVar(value="48k")
        rate_var = tk.StringVar(value="48000")
        channels_var = tk.StringVar(value="mono")
        effects_enabled_var = tk.BooleanVar(value=False)
        speed_var = tk.StringVar(value="1.0")
        pitch_var = tk.StringVar(value="1.0")
        echo_var = tk.BooleanVar(value=False)
        delay_var = tk.StringVar(value="300")
        decay_var = tk.StringVar(value="0.3")

        editor.columnconfigure(1, weight=1)

        def add_combo(row, label, variable, values):
            ttk.Label(editor, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 8), pady=2
            )
            widget = ttk.Combobox(
                editor,
                textvariable=variable,
                values=values,
                state="readonly",
            )
            widget.grid(row=row, column=1, sticky=tk.EW, pady=2)
            return widget

        ttk.Label(editor, text="Название:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8), pady=2
        )
        name_entry = ttk.Entry(editor, textvariable=name_var)
        name_entry.grid(row=0, column=1, sticky=tk.EW, pady=2)
        add_combo(1, "Формат:", format_var, AUDIO_PROFILE_FORMATS)
        bitrate_combo = add_combo(
            2, "Битрейт:", bitrate_var, AUDIO_PROFILE_BITRATES
        )
        add_combo(3, "Частота:", rate_var, AUDIO_PROFILE_SAMPLE_RATES)
        add_combo(4, "Каналы:", channels_var, AUDIO_PROFILE_CHANNELS)
        ttk.Separator(editor, orient=tk.HORIZONTAL).grid(
            row=5, column=0, columnspan=2, sticky=tk.EW, pady=8
        )
        ttk.Checkbutton(
            editor,
            text="Включить эффекты в профиле",
            variable=effects_enabled_var,
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=2)

        def add_entry(row, label, variable):
            ttk.Label(editor, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 8), pady=2
            )
            ttk.Entry(editor, textvariable=variable).grid(
                row=row, column=1, sticky=tk.EW, pady=2
            )

        add_entry(7, "Скорость (0.5–3.0):", speed_var)
        add_entry(8, "Тон (0.5–2.0):", pitch_var)
        ttk.Checkbutton(editor, text="Эхо", variable=echo_var).grid(
            row=9, column=0, columnspan=2, sticky=tk.W, pady=2
        )
        add_entry(10, "Задержка эхо (50–1000 мс):", delay_var)
        add_entry(11, "Сила эхо (0.1–0.8):", decay_var)

        entries = []
        selected_custom_index = {"value": None}
        delete_button_ref = {"widget": None}
        manager_status_ref = {"widget": None}
        editor_state = {
            "loading": False,
            "dirty": False,
            "selection_key": None,
        }
        selection_guard = {"value": False}

        def set_manager_status(text, status="info"):
            widget = manager_status_ref["widget"]
            if widget is not None:
                self._set_status_label(widget, text, status)

        def update_transaction_status():
            if editor_state["dirty"]:
                set_manager_status(
                    "Есть несохранённые изменения в полях профиля.",
                    "warning",
                )
            elif staged_dirty["value"]:
                set_manager_status(
                    "Библиотека профилей изменена и ещё не записана.",
                    "warning",
                )
            else:
                set_manager_status(
                    "Изменений нет. Выберите цель либо отредактируйте список профилей.",
                    "info",
                )

        def current_editor_profile():
            return make_audio_profile(
                name_var.get(),
                output_format=format_var.get(),
                bitrate=bitrate_var.get(),
                sample_rate=rate_var.get(),
                channels=channels_var.get(),
                effects_enabled=effects_enabled_var.get(),
                speed=speed_var.get(),
                pitch=pitch_var.get(),
                echo=echo_var.get(),
                echo_delay=delay_var.get(),
                echo_decay=decay_var.get(),
            )

        def load_editor(profile, custom_index=None, selection_key=None):
            profile = normalize_audio_profile(profile)
            audio = profile["audio"]
            effects = audio["effects"]
            editor_state["loading"] = True
            try:
                name_var.set(profile["name"])
                format_var.set(audio["format"])
                bitrate_var.set(audio["bitrate"])
                rate_var.set(audio["sample_rate"])
                channels_var.set(audio["channels"])
                effects_enabled_var.set(effects["enabled"])
                speed_var.set(str(effects["speed"]))
                pitch_var.set(str(effects["pitch"]))
                echo_var.set(effects["echo"])
                delay_var.set(str(effects["echo_delay"]))
                decay_var.set(str(effects["echo_decay"]))
            finally:
                editor_state["loading"] = False
            editor_state["dirty"] = False
            editor_state["selection_key"] = selection_key
            selected_custom_index["value"] = custom_index
            delete_button = delete_button_ref["widget"]
            if delete_button is not None:
                delete_button.configure(
                    state=tk.NORMAL if custom_index is not None else tk.DISABLED
                )
            bitrate_combo.configure(
                state=tk.DISABLED if audio["format"] == "wav" else "readonly"
            )
            update_transaction_status()

        def entry_key(kind, index, profile):
            return (
                ("custom", index)
                if kind == "custom"
                else ("builtin", profile["name"])
            )

        def refresh_list(select_custom=None, select_key=None):
            entries.clear()
            profile_list.delete(0, tk.END)
            for profile in builtin_audio_profiles():
                entries.append(("builtin", None, profile))
                profile_list.insert(tk.END, f"★ {profile['name']}")
            for index, profile in enumerate(staged_profiles):
                try:
                    profile = normalize_audio_profile(profile)
                except ValueError:
                    continue
                entries.append(("custom", index, profile))
                profile_list.insert(tk.END, f"● {profile['name']}")
            target_index = 0
            if select_key is None and select_custom is not None:
                select_key = ("custom", select_custom)
            if select_key is not None:
                for position, (kind, index, profile) in enumerate(entries):
                    if entry_key(kind, index, profile) == select_key:
                        target_index = position
                        break
            if entries:
                selection_guard["value"] = True
                try:
                    profile_list.selection_clear(0, tk.END)
                    profile_list.selection_set(target_index)
                    profile_list.see(target_index)
                finally:
                    selection_guard["value"] = False
                kind, index, profile = entries[target_index]
                load_editor(
                    profile,
                    index if kind == "custom" else None,
                    entry_key(kind, index, profile),
                )

        def restore_editor_selection():
            selected_key = editor_state["selection_key"]
            selection_guard["value"] = True
            try:
                profile_list.selection_clear(0, tk.END)
                for position, (kind, index, profile) in enumerate(entries):
                    if entry_key(kind, index, profile) == selected_key:
                        profile_list.selection_set(position)
                        profile_list.see(position)
                        break
            finally:
                selection_guard["value"] = False

        def on_select(_event=None):
            if selection_guard["value"]:
                return
            selection = profile_list.curselection()
            if not selection:
                return
            kind, index, profile = entries[selection[0]]
            target_key = entry_key(kind, index, profile)
            if target_key == editor_state["selection_key"]:
                return
            if not resolve_editor_before_navigation("переключением профиля"):
                restore_editor_selection()
                return
            refresh_list(select_key=target_key)

        profile_list.bind("<<ListboxSelect>>", on_select)

        def load_current(target):
            if not resolve_editor_before_navigation(
                "загрузкой текущих параметров"
            ):
                return
            try:
                refresh_list(select_key=editor_state["selection_key"])
                load_editor(
                    self._audio_profile_from_current_ui(
                        target,
                        "Текущий экспорт" if target == "export" else "Текущая книга",
                    ),
                    selection_key=None,
                )
                selection_guard["value"] = True
                try:
                    profile_list.selection_clear(0, tk.END)
                finally:
                    selection_guard["value"] = False
            except Exception as exc:
                self._show_error("Ошибка профиля", str(exc), parent=dialog)

        manager_status_ref["widget"] = ttk.Label(
            list_frame,
            text="",
            wraplength=285,
            justify=tk.LEFT,
            anchor=tk.W,
            foreground=self.get_status_color("info"),
        )
        manager_status_ref["widget"].pack(fill=tk.X, pady=(6, 0))
        self._status_label_kinds[manager_status_ref["widget"]] = "info"

        list_actions = ttk.Frame(list_frame)
        list_actions.pack(fill=tk.X, pady=(6, 0))
        def add_custom():
            if not resolve_editor_before_navigation("созданием нового профиля"):
                return
            draft = make_audio_profile(
                unique_audio_profile_name("Новый профиль", staged_profiles),
                output_format="opus",
                bitrate="48k",
                sample_rate="48000",
                channels="mono",
            )
            refresh_list(select_key=editor_state["selection_key"])
            selection_guard["value"] = True
            try:
                profile_list.selection_clear(0, tk.END)
            finally:
                selection_guard["value"] = False
            load_editor(draft, selection_key=None)
            # Новый черновик ещё не входит даже во временный список: Отмена
            # действительно отбрасывает его, а сохранение/применение сначала
            # добавят профиль в staged-копию.
            editor_state["dirty"] = True
            name_entry.focus_set()
            name_entry.selection_range(0, tk.END)
            update_transaction_status()

        ttk.Button(
            list_actions,
            text="➕ Добавить",
            command=add_custom,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        delete_button_ref["widget"] = ttk.Button(
            list_actions,
            text="🗑 Удалить",
            state=tk.DISABLED,
        )
        delete_button_ref["widget"].pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )

        source_actions = ttk.Frame(list_frame)
        source_actions.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            source_actions,
            text="Из экспорта",
            command=lambda: load_current("export"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            source_actions,
            text="Из книги",
            command=lambda: load_current("book"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        def stage_current_editor(*, refresh=True):
            try:
                profile = current_editor_profile()
            except ValueError as exc:
                self._show_error(
                    "Некорректный профиль", str(exc), parent=dialog
                )
                return None
            index = selected_custom_index["value"]
            if index is None:
                profile["name"] = unique_audio_profile_name(
                    profile["name"], staged_profiles
                )
            elif audio_profile_name_conflict(
                profile["name"], staged_profiles, exclude_index=index
            ):
                self._show_error(
                    "Совпадающее имя",
                    "Такое имя уже занято встроенным или пользовательским "
                    "профилем. Выберите другое название.",
                    parent=dialog,
                )
                return None
            if index is None or index >= len(staged_profiles):
                staged_profiles.append(profile)
                index = len(staged_profiles) - 1
            else:
                staged_profiles[index] = profile
            staged_dirty["value"] = True
            editor_state["dirty"] = False
            editor_state["selection_key"] = ("custom", index)
            selected_custom_index["value"] = index
            if name_var.get() != profile["name"]:
                editor_state["loading"] = True
                try:
                    name_var.set(profile["name"])
                finally:
                    editor_state["loading"] = False
            if refresh:
                refresh_list(select_custom=index)
                update_transaction_status()
            return index, profile

        def save_custom():
            stage_current_editor()

        def resolve_editor_before_navigation(action):
            """Не даёт переключению молча стереть незаписанные поля."""
            if not editor_state["dirty"]:
                return True
            answer = self._run_messagebox(
                messagebox.askyesnocancel,
                "Несохранённые поля профиля",
                "Поля текущего профиля изменены, но ещё не добавлены во "
                f"временный список перед {action}.\n\n"
                "Да — сохранить профиль; Нет — отбросить правки; "
                "Отмена — остаться в редакторе.",
                parent=dialog,
                icon="warning",
            )
            if answer is None:
                return False
            if answer:
                return stage_current_editor(refresh=False) is not None
            editor_state["dirty"] = False
            update_transaction_status()
            return True

        def delete_custom():
            index = selected_custom_index["value"]
            if index is None or index >= len(staged_profiles):
                return
            if not self._ask_yes_no(
                "Удалить профиль",
                f"Удалить пользовательский профиль «{staged_profiles[index].get('name', '')}»?",
                icon="warning",
                parent=dialog,
            ):
                return
            del staged_profiles[index]
            staged_dirty["value"] = True
            refresh_list()
            update_transaction_status()

        delete_button_ref["widget"].configure(command=delete_custom)

        def staged_initial_dir():
            return resolve_dialog_initial_dir(
                staged_last_dir["value"],
                self.config.get("last_config_dir"),
                BASE_DIR,
            )

        def export_profile():
            try:
                profile = current_editor_profile()
            except ValueError as exc:
                self._show_error(
                    "Некорректный профиль", str(exc), parent=dialog
                )
                return
            filepath = filedialog.asksaveasfilename(
                parent=dialog,
                initialdir=staged_initial_dir(),
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                initialfile="audio_profile.json",
            )
            if not filepath:
                return
            try:
                self._write_json_atomic(filepath, profile)
                staged_last_dir["value"] = str(Path(filepath).expanduser().parent)
            except Exception as exc:
                self._show_error(
                    "Ошибка",
                    f"Не удалось экспортировать профиль:\n{exc}",
                    parent=dialog,
                )

        def import_profile():
            if not resolve_editor_before_navigation("импортом профиля"):
                return
            # Если открыта несохранённая копия «Из экспорта/Из книги» и
            # пользователь отменит файловый диалог, не подменяем её первым
            # встроенным профилем только ради перерисовки списка.
            if editor_state["selection_key"] is not None:
                refresh_list(select_key=editor_state["selection_key"])
            filepath = filedialog.askopenfilename(
                parent=dialog,
                initialdir=staged_initial_dir(),
                filetypes=[("JSON files", "*.json")],
            )
            if not filepath:
                return
            try:
                with open(filepath, "r", encoding="utf-8-sig") as profile_file:
                    profile = normalize_audio_profile(
                        json.load(profile_file), require_envelope=True
                    )
                conflict = next(
                    (
                        index
                        for index, existing in enumerate(staged_profiles)
                        if str(existing.get("name", "")).casefold()
                        == profile["name"].casefold()
                    ),
                    None,
                )
                builtin_conflict = any(
                    item["name"].casefold() == profile["name"].casefold()
                    for item in builtin_audio_profiles()
                )
                if conflict is not None and self._ask_yes_no(
                    "Совпадающее имя",
                    "Пользовательский профиль с таким именем уже есть. Заменить его?",
                    icon="warning",
                    parent=dialog,
                ):
                    staged_profiles[conflict] = profile
                    index = conflict
                else:
                    if conflict is not None or builtin_conflict:
                        profile["name"] = unique_audio_profile_name(
                            profile["name"], staged_profiles
                        )
                    staged_profiles.append(profile)
                    index = len(staged_profiles) - 1
                staged_last_dir["value"] = str(Path(filepath).expanduser().parent)
                staged_dirty["value"] = True
                refresh_list(select_custom=index)
                update_transaction_status()
            except Exception as exc:
                self._show_error(
                    "Ошибка",
                    f"Не удалось импортировать профиль:\n{exc}",
                    parent=dialog,
                )

        editor_actions = ttk.Frame(editor)
        editor_actions.grid(
            row=12, column=0, columnspan=2, sticky=tk.EW, pady=(6, 0)
        )
        ttk.Button(
            editor_actions, text="💾 Сохранить профиль", command=save_custom
        ).pack(side=tk.LEFT)
        ttk.Button(
            editor_actions, text="📂 Импорт", command=import_profile
        ).pack(side=tk.RIGHT)
        ttk.Button(
            editor_actions, text="📤 Экспорт", command=export_profile
        ).pack(side=tk.RIGHT, padx=4)

        def mark_editor_dirty(*_args):
            if editor_state["loading"]:
                return
            editor_state["dirty"] = True
            update_transaction_status()

        for editor_variable in (
            name_var,
            format_var,
            bitrate_var,
            rate_var,
            channels_var,
            effects_enabled_var,
            speed_var,
            pitch_var,
            echo_var,
            delay_var,
            decay_var,
        ):
            editor_variable.trace_add("write", mark_editor_dirty)

        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=0)
        footer_hint = self._register_palette_widget(
            ttk.Label(
                footer,
                text=(
                    "Профиль — шаблон: применение заменит параметры только "
                    "выбранной области и сохранит текущие поля со списком.\n"
                    "«Сохранить и закрыть» записывает библиотеку; «Отмена» "
                    "отбрасывает добавление, изменение, удаление и импорт."
                ),
                foreground=self.get_status_color("muted"),
                wraplength=700,
                justify=tk.LEFT,
                anchor=tk.W,
            ),
            "muted",
        )
        footer_hint.grid(row=0, column=0, columnspan=2, sticky=tk.EW)

        apply_buttons = ttk.Frame(footer)
        apply_buttons.grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(6, 0)
        )
        close_buttons = ttk.Frame(footer)
        close_buttons.grid(
            row=2, column=0, columnspan=2, sticky=tk.E, pady=(5, 0)
        )

        def resize_footer_hint(event):
            try:
                footer_hint.configure(wraplength=max(300, event.width - 18))
            except tk.TclError:
                pass

        footer.bind("<Configure>", resize_footer_hint, add="+")

        def close_dialog():
            self._status_label_kinds.pop(manager_status_ref["widget"], None)
            self._palette_widget_kinds.pop(footer_hint, None)
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except tk.TclError:
                pass

        def commit_dialog(target=None):
            profile = None
            if target is not None:
                if target == "book" and self.is_synthesis_running():
                    self._show_warning(
                        "Синтез уже выполняется",
                        "Профиль книги можно применить после завершения или остановки синтеза.",
                        parent=dialog,
                    )
                    return
                if target == "export" and (
                    getattr(self, "_export_running", False)
                    or getattr(self, "_export_lock", False)
                ):
                    self._show_warning(
                        "Сборка уже выполняется",
                        "Профиль экспорта можно применить после завершения или остановки сборки.",
                        parent=dialog,
                    )
                    return
                try:
                    profile = current_editor_profile()
                    audio = normalize_audio_profile(profile)["audio"]
                    if target == "book":
                        _select_book_audio_profile(
                            audio["format"],
                            sample_rate=audio["sample_rate"],
                            channels=audio["channels"],
                            bitrate=audio["bitrate"],
                        )
                    else:
                        # Без входных файлов проверяем именно явные требования.
                        # Для auto-каналов берём stereo, чтобы не отвергать
                        # допустимый профиль OGG до анализа реальных источников.
                        _select_merge_audio_profile(
                            (),
                            audio["format"],
                            sample_rate=audio["sample_rate"],
                            channels=(
                                "stereo"
                                if audio["channels"] == "auto"
                                else audio["channels"]
                            ),
                            bitrate=audio["bitrate"],
                        )
                except Exception as exc:
                    self._show_error(
                        "Профиль несовместим", str(exc), parent=dialog
                    )
                    return

            # Сохранение и кнопки применения принимают текущие поля редактора.
            # Поэтому профиль не может примениться в одной версии, а
            # сохраниться в библиотеке в другой.
            staged_editor_index = None
            if editor_state["dirty"]:
                staged_result = stage_current_editor(refresh=False)
                if staged_result is None:
                    return
                staged_editor_index, staged_profile = staged_result
                if target is not None:
                    profile = staged_profile
                update_transaction_status()

            candidate_config = copy.deepcopy(self.config)
            candidate_config["audio_profiles"] = copy.deepcopy(staged_profiles)
            if staged_last_dir["value"]:
                candidate_config["last_audio_profile_dir"] = staged_last_dir["value"]
            if profile is not None:
                candidate_config.update(
                    audio_profile_config_values(profile, target)
                )
            try:
                # Пишем подготовленный снимок без update_config_from_ui():
                # менеджер не должен попутно сохранять недописанные поля из
                # других вкладок Настроек.
                self._persist_settings_snapshot(candidate_config)
            except Exception as exc:
                self._show_error(
                    "Ошибка сохранения",
                    f"Изменения не применены:\n{exc}",
                    parent=dialog,
                )
                if staged_editor_index is not None:
                    refresh_list(select_custom=staged_editor_index)
                    update_transaction_status()
                return

            self.config = candidate_config
            ui_error = None
            if profile is not None:
                previous_update_state = getattr(self, "_is_updating_ui", False)
                self._is_updating_ui = True
                try:
                    self._apply_audio_profile_to_ui(profile, target, save=False)
                except Exception as exc:
                    ui_error = exc
                    logging.exception(
                        "Настройки аудиопрофиля записаны, но UI не обновлён"
                    )
                finally:
                    self._is_updating_ui = previous_update_state
            else:
                self._refresh_audio_profile_summaries()

            close_dialog()
            if ui_error is not None:
                self._show_warning(
                    "Профиль сохранён",
                    "settings.json обновлён, но интерфейс не удалось полностью "
                    f"перерисовать: {ui_error}\nПерезапустите приложение.",
                )
                return
            if profile is not None:
                target_name = (
                    "вкладке «Экспорт и сборка»"
                    if target == "export"
                    else "сборке книги и прямому синтезу"
                )
                self._show_info(
                    "Профиль применён",
                    f"Параметры профиля «{profile['name']}» скопированы к "
                    f"{target_name}. Профиль не остаётся связанным с полями.",
                )

        ttk.Button(
            apply_buttons,
            text="К сборке книги",
            command=lambda: commit_dialog("book"),
        ).pack(side=tk.LEFT)
        ttk.Button(
            apply_buttons,
            text="К экспорту",
            command=lambda: commit_dialog("export"),
        ).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(
            close_buttons,
            text="Отмена",
            command=close_dialog,
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            close_buttons,
            text="Сохранить и закрыть",
            command=commit_dialog,
        ).pack(side=tk.LEFT)

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Escape>", lambda _event: close_dialog())

        format_var.trace_add(
            "write",
            lambda *_args: bitrate_combo.configure(
                state=tk.DISABLED if format_var.get() == "wav" else "readonly"
            ),
        )
        refresh_list()
        if preferred_target in {"book", "export"}:
            load_current(preferred_target)
        update_transaction_status()
        self._center_popup(dialog, 780, 560, fit_screen=True)

# --- Вкладка "Кэш" ---
    def setup_cache_tab(self):
        # Нижняя панель пакуется первой и занимает две строки. В Tk 9 системные
        # кнопки выше/шире, поэтому прежняя единственная строка обрезалась на
        # небольших окнах, хотя в Tk 8.6 выглядела нормально.
        controls = ttk.Frame(self.tab_cache, padding=(10, 5))
        controls.pack(side=tk.BOTTOM, fill=tk.X)
        primary_controls = ttk.Frame(controls)
        primary_controls.pack(fill=tk.X)
        secondary_controls = ttk.Frame(controls)
        secondary_controls.pack(fill=tk.X, pady=(4, 0))

        self.btn_cache_delete = ttk.Button(
            primary_controls,
            text="🗑 Удалить выбранные",
            command=self.delete_selected_cache,
        )
        self.btn_cache_delete.pack(side=tk.LEFT)
        self.btn_cache_optimize = ttk.Button(
            primary_controls,
            text="🧹 Оптимизировать кэш",
            command=self.optimize_cache,
        )
        self.btn_cache_optimize.pack(side=tk.LEFT, padx=5)
        self.btn_cache_clear = ttk.Button(
            primary_controls,
            text="🔥 Очистить ВСЁ",
            command=self.clear_entire_cache,
        )
        self.btn_cache_clear.pack(side=tk.LEFT, padx=5)
        self.btn_cache_refresh = ttk.Button(
            primary_controls, text="🔄 Обновить", command=self.load_cache_ui
        )
        self.btn_cache_refresh.pack(side=tk.LEFT, padx=5)

        self.btn_cache_transcode = ttk.Button(
            secondary_controls,
            text="🎧 Перекодировать Vorbis → Opus",
            command=self.transcode_cache_to_opus,
        )
        self.btn_cache_transcode.pack(side=tk.LEFT)

        self.del_after_zip = tk.BooleanVar(value=False)
        self.chk_cache_delete_after_zip = ttk.Checkbutton(
            secondary_controls,
            text="Очистить кэш после архивации",
            variable=self.del_after_zip,
        )
        self.chk_cache_delete_after_zip.pack(side=tk.RIGHT, padx=(10, 0))
        self.btn_cache_archive = ttk.Button(
            secondary_controls,
            text="📦 Создать ZIP архив",
            command=self.archive_cache,
        )
        self.btn_cache_archive.pack(side=tk.RIGHT)

        frame = ttk.Frame(self.tab_cache, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        search_frame = ttk.Frame(frame)
        search_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(search_frame, text="Поиск по тексту:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._schedule_cache_filter)
        ttk.Entry(search_frame, textvariable=self.search_var, width=50).pack(side=tk.LEFT, padx=5)
        
        self.lbl_cache_count = ttk.Label(search_frame, text="Всего записей: 0", foreground=self.get_status_color("info"))
        self.lbl_cache_count.pack(side=tk.RIGHT, padx=5)
        
        # --- ИЗМЕНЕНИЕ: Системно-зависимая подсказка ---
        ctrl_key = "Command (⌘)" if sys.platform == "darwin" else "Ctrl"
        self._register_palette_widget(
            ttk.Label(
                frame,
                text=f"💡 Вы можете выделять несколько строк мышкой с зажатым {ctrl_key} или Shift",
                font=("", 8, "italic"),
                foreground=self.get_status_color("muted"),
            ),
            "muted",
        ).pack(anchor=tk.W, pady=(0, 5))
        # -----------------------------------------------

        columns = ("hash", "text", "speaker", "uses")
        # selectmode="extended" разрешает выделение множества строк
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.cache_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        if sys.platform == "darwin":
            self._bind_mac_treeview_clicks(self.cache_tree)
        self.cache_tree.heading("hash", text="Хэш")
        self.cache_tree.heading("text", text="Текст")
        self.cache_tree.heading("speaker", text="Спикер")
        self.cache_tree.heading("uses", text="Использований")
        
        self.cache_tree.column("hash", width=80, stretch=False)
        self.cache_tree.column("text", width=500)
        self.cache_tree.column("speaker", width=80, stretch=False)
        self.cache_tree.column("uses", width=100, stretch=False, anchor=tk.CENTER)
        
        self.cache_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.cache_tree.yview
        )
        self.cache_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.cache_tree.bind("<Double-1>", self.on_cache_double_click)
        self._update_cache_controls()

    def on_cache_double_click(self, event):
        if self.is_cache_operation_running():
            return
        selected = self.cache_tree.selection()
        if not selected:
            return
        hash_key = selected[0]
        data = self.cache_data.get(hash_key)
        if not data:
            return

        top = tk.Toplevel(self.root)
        top.withdraw()
        top.title(f"Детали кэша: {hash_key}")
        top.transient(self.root)

        # Tk 9/Python 3.13 использует более высокие системные ttk-кнопки, чем
        # Tk 8.6. Нижнюю панель фиксируем, а содержимое делаем прокручиваемым.
        button_bar = ttk.Frame(top, padding=(10, 5, 10, 10))
        button_bar.pack(side=tk.BOTTOM, fill=tk.X)

        body_host = ttk.Frame(top)
        body_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        body_canvas = tk.Canvas(body_host, highlightthickness=0, borderwidth=0)
        body_scrollbar = ttk.Scrollbar(
            body_host, orient=tk.VERTICAL, command=body_canvas.yview
        )
        body_canvas.configure(yscrollcommand=body_scrollbar.set)
        body_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        body_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(body_canvas)
        body_window = body_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind(
            "<Configure>",
            lambda _event: body_canvas.configure(scrollregion=body_canvas.bbox("all")),
        )
        body_canvas.bind(
            "<Configure>",
            lambda event: body_canvas.itemconfigure(body_window, width=event.width),
        )

        ttk.Label(body, text="Исходный текст:").pack(
            anchor=tk.W, padx=10, pady=(10, 0)
        )
        t1 = tk.Text(
            body, height=4, wrap=tk.WORD, font=("Arial", self.font_size_var.get())
        )
        t1.pack(fill=tk.X, padx=10)
        t1.insert(tk.END, data.get("original_text", ""))

        ttk.Label(
            body, text="Нормализованный текст (отправлен в API):"
        ).pack(anchor=tk.W, padx=10, pady=(10, 0))
        t2 = tk.Text(
            body, height=4, wrap=tk.WORD, font=("Arial", self.font_size_var.get())
        )
        t2.pack(fill=tk.X, padx=10)
        t2.insert(tk.END, data.get("normalized_text", ""))

        info = f"Спикер: {data.get('speaker', '')}\n"
        # В краткую таблицу steps намеренно не добавляем. В подробной карточке
        # он полезен для диагностики происхождения конкретного аудиофрагмента.
        if "steps" in data:
            cache_mode = (
                "отдельный ключ"
                if _config_bool(
                    data.get("steps_in_cache_key", True), default=True
                )
                else "общий legacy-ключ"
            )
            info += f"Steps API: {data.get('steps')} ({cache_mode})\n"
        else:
            info += "Steps API: значение сервера / запись старого формата\n"
        info += f"Использований: {data.get('usage_count', 0)}\n"
        info += (
            "Создано: "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('created_at', 0)))}\n"
        )
        ttk.Label(body, text=info, justify=tk.LEFT).pack(
            anchor=tk.W, padx=10, pady=5
        )

        fx_frame = ttk.LabelFrame(
            body, text="Локальные эффекты (только для тестирования)", padding=5
        )
        fx_frame.pack(fill=tk.X, padx=10, pady=5)

        cache_speed_var = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        cache_pitch_var = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        cache_echo_var = tk.BooleanVar(value=self.config.get("fx_echo", False))
        cache_echo_delay_var = tk.IntVar(
            value=self.config.get("fx_echo_delay", 300)
        )
        cache_echo_decay_var = tk.DoubleVar(
            value=self.config.get("fx_echo_decay", 0.3)
        )

        top_fx = ttk.Frame(fx_frame)
        top_fx.pack(fill=tk.X, pady=2)
        ttk.Label(top_fx, text="Скорость:").pack(side=tk.LEFT, padx=5)
        lbl_spd = ttk.Label(top_fx, text=f"{cache_speed_var.get():.1f}x", width=4)
        lbl_spd.pack(side=tk.LEFT)
        ttk.Scale(
            top_fx,
            from_=0.5,
            to_=3.0,
            variable=cache_speed_var,
            command=lambda value: lbl_spd.config(text=f"{float(value):.1f}x"),
        ).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(top_fx, text="Тон:").pack(side=tk.LEFT, padx=5)
        lbl_pch = ttk.Label(top_fx, text=f"{cache_pitch_var.get():.2f}", width=4)
        lbl_pch.pack(side=tk.LEFT)
        ttk.Scale(
            top_fx,
            from_=0.5,
            to_=2.0,
            variable=cache_pitch_var,
            command=lambda value: lbl_pch.config(text=f"{float(value):.2f}"),
        ).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        mid_fx = ttk.Frame(fx_frame)
        mid_fx.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(mid_fx, text="Эхо", variable=cache_echo_var).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Label(mid_fx, text="Задержка:").pack(side=tk.LEFT, padx=(10, 2))
        lbl_delay = ttk.Label(
            mid_fx, text=f"{cache_echo_delay_var.get()}мс", width=6
        )
        lbl_delay.pack(side=tk.LEFT)
        ttk.Scale(
            mid_fx,
            from_=50,
            to_=1000,
            variable=cache_echo_delay_var,
            command=lambda value: lbl_delay.config(text=f"{int(float(value))}мс"),
        ).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(mid_fx, text="Сила:").pack(side=tk.LEFT, padx=(10, 2))
        lbl_decay = ttk.Label(
            mid_fx, text=f"{cache_echo_decay_var.get():.1f}", width=4
        )
        lbl_decay.pack(side=tk.LEFT)
        ttk.Scale(
            mid_fx,
            from_=0.1,
            to_=0.8,
            variable=cache_echo_decay_var,
            command=lambda value: lbl_decay.config(text=f"{float(value):.1f}"),
        ).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        def apply_cache_to_global():
            self.settings_vars["fx_speed"].set(cache_speed_var.get())
            self.settings_vars["fx_pitch"].set(cache_pitch_var.get())
            self.settings_vars["fx_echo"].set(cache_echo_var.get())
            self.settings_vars["fx_echo_delay"].set(cache_echo_delay_var.get())
            self.settings_vars["fx_echo_decay"].set(cache_echo_decay_var.get())
            self.save_settings()
            self._show_info("Успех", "Эффекты сохранены в глобальные настройки!")

        filepath = resolve_cache_audio_path(
            self.config.get("cache_dir", "cache_audio"),
            data.get("file_name", ""),
        )

        def play_cache_audio():
            if filepath is None or not filepath.is_file():
                self._show_warning(
                    "Файл не найден",
                    "Аудиофайл этой записи отсутствует или имеет небезопасный путь.",
                )
                return
            effect_settings = (
                cache_speed_var.get(),
                cache_pitch_var.get(),
                cache_echo_var.get(),
                cache_echo_delay_var.get(),
                cache_echo_decay_var.get(),
            )

            def prepare_and_play():
                try:
                    segment = _load_audio_segment(filepath)
                    processed_segment = AudioEffects.apply_effects(
                        segment,
                        speed=effect_settings[0],
                        pitch=effect_settings[1],
                        echo=effect_settings[2],
                        echo_delay=effect_settings[3],
                        echo_decay=effect_settings[4],
                    )
                    self.play_audio_segment(processed_segment)
                except Exception:
                    logging.exception("Ошибка подготовки файла кэша к воспроизведению")

            threading.Thread(target=prepare_and_play, daemon=True).start()

        ttk.Button(button_bar, text="🔊 Слушать", command=play_cache_audio).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(
            button_bar, text="🔇", width=3, command=self.stop_audio_playback
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            button_bar,
            text="💾 Сделать глобальными",
            command=apply_cache_to_global,
        ).pack(side=tk.RIGHT, padx=5)
        ttk.Button(button_bar, text="Закрыть", command=top.destroy).pack(
            side=tk.RIGHT
        )

        top.update_idletasks()
        requested_height = max(500, top.winfo_reqheight() + 10)
        self._center_popup(top, 750, requested_height, fit_screen=True)

    def _set_cache_operation(self, operation):
        """Меняет единое состояние I/O-операции и доступность контролов."""
        self._cache_operation = operation
        self._cache_optimization_running = operation == "optimize"
        self._cache_archive_running = operation == "archive"
        self._cache_ui_loading = operation == "load"
        self._update_cache_controls()

    def _update_cache_controls(self):
        """Блокирует только конфликтующие контролы вкладки кэша."""
        busy = self.is_cache_operation_running()
        state = tk.DISABLED if busy else tk.NORMAL
        for name in (
            "btn_cache_delete",
            "btn_cache_optimize",
            "btn_cache_clear",
            "btn_cache_refresh",
            "btn_cache_transcode",
            "btn_cache_archive",
            "chk_cache_delete_after_zip",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    pass
        if hasattr(self, "cache_tree"):
            try:
                self.cache_tree.configure(
                    selectmode="none" if busy else "extended"
                )
            except tk.TclError:
                pass

    def _begin_cache_operation(self, operation, *, title=None, message=None):
        """Атомарно резервирует операцию; при конфликте показывает причину."""
        current = getattr(self, "_cache_operation", None)
        if current is not None:
            self._show_info(
                "Кэш занят",
                "Дождитесь завершения операции: "
                f"{CACHE_OPERATION_LABELS.get(current, current)}.",
            )
            return None
        self._set_cache_operation(operation)
        if title and message:
            popup = self._create_wait_popup(
                title, message, modal=False, owner=False
            )
            popup.protocol("WM_DELETE_WINDOW", popup.withdraw)
            return popup
        return True

    def _end_cache_operation(self, expected_operation):
        if getattr(self, "_cache_operation", None) == expected_operation:
            self._set_cache_operation(None)

    def _warn_if_cache_busy_for_synthesis(self):
        """Не запускает синтез рядом с чтением или изменением индекса."""
        operation = getattr(self, "_cache_operation", None)
        if operation is None:
            return False
        self._show_warning(
            "Кэш занят",
            "Дождитесь завершения операции: "
            f"{CACHE_OPERATION_LABELS.get(operation, operation)}.",
        )
        return True

    def _cache_generation_changed(self):
        """Инвалидирует все уже запущенные снимки таблицы."""
        self._cache_state_generation += 1
        self._cache_ui_request_id += 1

    def load_cache_ui(self):
        if self._begin_cache_operation("load") is None:
            return
        pending_filter = getattr(self, "_cache_ui_filter_after_id", None)
        if pending_filter is not None:
            try:
                self.root.after_cancel(pending_filter)
            except tk.TclError:
                pass
            self._cache_ui_filter_after_id = None

        cache_path = (
            Path(self.config.get("cache_dir", "cache_audio"))
            / "sentence_cache.json"
        )
        request_id = self._cache_ui_request_id = self._cache_ui_request_id + 1
        generation = self._cache_state_generation
        self.lbl_cache_count.config(text="Чтение индекса...")

        def read_cache():
            loaded_cache, source_path, errors = read_cache_index_with_backup(
                cache_path
            )
            for path, exc in errors:
                logging.error("Ошибка загрузки кэша %s в UI: %s", path, exc)
            error = None
            warning = None
            if source_path is None and errors:
                details = "\n".join(
                    f"{path.name}: {exc}" for path, exc in errors
                )
                error = (
                    "Основной индекс и резервная копия повреждены.\n\n"
                    f"{details}\n\n"
                    "Исправьте файлы или удалите их для пересоздания."
                )
            elif source_path is not None and source_path != cache_path:
                warning = (
                    f"Основной индекс недоступен; показана резервная копия "
                    f"{source_path.name}."
                )
            self._post_to_ui(
                self._finish_cache_ui_load,
                request_id,
                generation,
                loaded_cache,
                error,
                warning,
            )

        try:
            threading.Thread(target=read_cache, daemon=True).start()
        except Exception:
            self._end_cache_operation("load")
            raise

    def _finish_cache_ui_load(
        self,
        request_id,
        generation,
        loaded_cache,
        error=None,
        warning=None,
    ):
        self._end_cache_operation("load")
        if (
            request_id != self._cache_ui_request_id
            or generation != self._cache_state_generation
        ):
            return
        self._cache_ui_loaded = True
        self.cache_data = loaded_cache
        self.lbl_cache_count.config(text=f"Всего записей: {len(self.cache_data)}")
        self.filter_cache()
        if error:
            self._show_error("Ошибка кэша", error)
        elif warning:
            self._show_warning("Восстановление кэша", warning)

    def _schedule_cache_filter(self, *_args):
        """Объединяет быстрый набор в поиске в одну перерисовку Treeview."""
        pending_filter = getattr(self, "_cache_ui_filter_after_id", None)
        if pending_filter is not None:
            try:
                self.root.after_cancel(pending_filter)
            except tk.TclError:
                pass
        self._cache_ui_filter_after_id = self.root.after(150, self.filter_cache)

    def filter_cache(self):
        self._cache_ui_filter_after_id = None
        if not hasattr(self, 'cache_data'):
            self.cache_data = {}
        query = self.search_var.get().lower()
        old_items = self.cache_tree.get_children()
        if old_items:
            self.cache_tree.delete(*old_items)
            
        count = 0
        for h, data in self.cache_data.items():
            if not isinstance(h, str) or not isinstance(data, dict):
                continue
            text = str(data.get("original_text", ""))
            if query in text.lower() or query in h.lower():
                # --- ИСПРАВЛЕНИЕ: Убираем переносы строк для красивого отображения в таблице ---
                display_text = text.replace("\n", " ↵ ")
                # -------------------------------------------------------------------------------
                self.cache_tree.insert(
                    "",
                    tk.END,
                    iid=h,
                    values=(
                        h[:8] + "...",
                        display_text,
                        data.get("speaker", ""),
                        data.get("usage_count", 0),
                    ),
                )
                count += 1
                if count >= 1000: break

    def delete_selected_cache(self):
        if self.is_synthesis_running():
            self._show_warning("Занято", "Нельзя удалять записи из кэша во время активного синтеза!")
            return
        selected = self.cache_tree.selection()
        if not selected:
            return
        if not self._ask_yes_no(
            "Удаление",
            f"Удалить выбранные записи ({len(selected)} шт.) из кэша?",
        ):
            return
        popup = self._begin_cache_operation(
            "delete",
            title="Удаление из кэша",
            message="Обновление индекса и удаление аудиофайлов...",
        )
        if popup is None:
            return

        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        cache_path = cache_dir / "sentence_cache.json"
        selected_keys = tuple(selected)
        generation = self._cache_state_generation

        def run_delete():
            error = None
            updated_cache = None
            try:
                current_cache, source_path, errors = read_cache_index_with_backup(
                    cache_path
                )
                for path, exc in errors:
                    logging.error(
                        "Ошибка чтения кэша перед удалением %s: %s", path, exc
                    )
                if source_path is None and errors:
                    details = "; ".join(
                        f"{path.name}: {exc}" for path, exc in errors
                    )
                    raise RuntimeError(
                        "основной индекс и резервная копия повреждены: "
                        f"{details}"
                    )

                updated_cache = dict(current_cache)
                removed_entries = []
                for hash_key in selected_keys:
                    cache_info = updated_cache.pop(hash_key, None)
                    if cache_info is not None:
                        removed_entries.append(cache_info)
                files_to_delete = unreferenced_cache_audio_paths(
                    cache_dir, removed_entries, updated_cache
                )
                # Сначала публикуем индекс. При сбое записи аудио остаётся
                # доступно, а индекс никогда не указывает на удалённый файл.
                write_cache_index_atomic(cache_dir, updated_cache)
                for filepath in files_to_delete:
                    try:
                        filepath.unlink(missing_ok=True)
                    except OSError as exc:
                        # Сиротский файл безопаснее отсутствующего файла по
                        # живой ссылке; сообщаем, но не откатываем индекс.
                        logging.warning(
                            "Не удалось удалить сиротский файл кэша %s: %s",
                            filepath,
                            exc,
                        )
            except Exception as exc:
                logging.error("Не удалось удалить выбранные записи кэша: %s", exc)
                error = f"Не удалось обновить кэш:\n{exc}"
            self._post_to_ui(
                self._finish_cache_delete,
                popup,
                generation,
                updated_cache,
                selected_keys,
                error,
            )

        try:
            threading.Thread(target=run_delete, daemon=True).start()
        except Exception:
            self._end_cache_operation("delete")
            self._close_popup_safely(popup)
            raise

    def _finish_cache_delete(
        self, popup, generation, updated_cache, selected, error=None
    ):
        self._end_cache_operation("delete")
        self._close_popup_safely(popup)
        if error:
            self._show_error("Ошибка", error)
            return
        if updated_cache is None:
            self._cache_ui_loaded = False
            return
        if generation != self._cache_state_generation:
            # Защитный fallback: при неожиданной смене поколения не публикуем
            # устаревший снимок, а оставляем таблицу для ручного «Обновить».
            self._cache_ui_loaded = False
            return
        self._cache_generation_changed()
        self.cache_data = updated_cache
        for hash_key in selected:
            if self.cache_tree.exists(hash_key):
                self.cache_tree.delete(hash_key)
        self.lbl_cache_count.config(text=f"Всего записей: {len(self.cache_data)}")
        self._show_info("Успех", "Записи удалены.")

    def is_synthesis_running(self):
        """Проверяет, запущен ли основной или прямой синтез в данный момент"""
        batch_active = bool(self.processing_thread and self.processing_thread.is_alive())
        direct_active = bool(self.direct_thread and self.direct_thread.is_alive())
        return batch_active or direct_active

    def is_cache_optimization_running(self):
        return getattr(self, "_cache_operation", None) == "optimize"

    def is_cache_archive_running(self):
        return getattr(self, "_cache_operation", None) == "archive"

    def is_cache_operation_running(self):
        return getattr(self, "_cache_operation", None) is not None

    def _finish_cache_optimization(
        self, popup, cache_snapshot=None, error=None
    ):
        self._end_cache_operation("optimize")
        self._close_popup_safely(popup)
        # В отличие от обычного перелистывания вкладок, оптимизация — явная
        # операция пользователя. Если таблица уже была загружена, обновляем её,
        # чтобы в Treeview не оставались удалённые «призрачные» записи. Первый
        # вход во вкладку всё ещё остаётся ленивым.
        if cache_snapshot is not None:
            self._cache_generation_changed()
        if (
            cache_snapshot is not None
            and getattr(self, "_cache_ui_loaded", False)
            and hasattr(self, "cache_data")
        ):
            self.cache_data = cache_snapshot
            self.lbl_cache_count.config(
                text=f"Всего записей: {len(self.cache_data)}"
            )
            self.filter_cache()
        if error:
            self._show_error("Ошибка", error)

    @staticmethod
    def _format_byte_count(value):
        """Форматирует байты для итогов долгих операций с кэшем."""
        value = int(value or 0)
        sign = "-" if value < 0 else ""
        value = abs(value)
        units = ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ")
        number = float(value)
        for unit in units:
            if number < 1024 or unit == units[-1]:
                formatted = (
                    f"{number:.0f} {unit}"
                    if unit == "Б"
                    else f"{number:.2f} {unit}"
                )
                return sign + formatted
            number /= 1024

    def _finish_cache_transcode(self, popup, stats, error=None):
        self._cache_transcode_cancel = None
        self._end_cache_operation("transcode")
        self._close_popup_safely(popup)
        if error:
            self._show_error("Ошибка", error)
            return

        converted = int(stats.get("converted", 0))
        already_opus = int(stats.get("already_opus", 0))
        missing = int(stats.get("missing", 0))
        failed = int(stats.get("failed", 0))
        cancelled = bool(stats.get("cancelled"))
        logical_saved = int(stats.get("old_size", 0)) - int(
            stats.get("new_size", 0)
        )
        allocated_saved = int(stats.get("old_allocated", 0)) - int(
            stats.get("new_allocated", 0)
        )
        title = "Перекодирование остановлено" if cancelled else "Перекодирование завершено"
        lines = [
            f"Перекодировано Vorbis → Opus: {converted}",
            f"Уже были Opus: {already_opus}",
            f"Отсутствовали на диске: {missing}",
            f"Ошибок: {failed}",
            f"Экономия логического размера: {self._format_byte_count(logical_saved)}",
            f"Экономия занятого места: {self._format_byte_count(allocated_saved)}",
        ]
        if cancelled:
            lines.append(
                "Операцию можно запустить снова: уже готовые Opus-файлы будут пропущены."
            )
        self._show_info(title, "\n".join(lines))

    def transcode_cache_to_opus(self):
        """Явно и возобновляемо мигрирует старый Vorbis-кэш в Ogg/Opus."""
        if self.is_cache_operation_running():
            self._show_warning(
                "Кэш занят", "Дождитесь завершения текущей операции с кэшем."
            )
            return
        if self.is_synthesis_running():
            self._show_warning(
                "Занято",
                "Нельзя перекодировать кэш во время активного синтеза.",
            )
            return

        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        cache_path = cache_dir / "sentence_cache.json"
        if not cache_path.exists() and not cache_path.with_suffix(
            cache_path.suffix + ".bak"
        ).exists():
            self._show_info("Пусто", "Индекс кэша не найден.")
            return

        if not self._ask_yes_no(
            "Перекодирование кэша в Opus",
            "Старые фрагменты Ogg/Vorbis будут по одному перекодированы в "
            f"Ogg/Opus {CACHE_AUDIO_BITRATE}, 48 кГц mono.\n\n"
            "Имена файлов и хэши не меняются, поэтому книга останется полностью "
            "доступна для пересборки. Каждый файл сначала создаётся рядом во "
            "временном имени и только затем атомарно заменяет оригинал.\n\n"
            "Важно: Vorbis и Opus — lossy-кодеки, поэтому миграция необратима и "
            "может немного снизить качество уже сжатого старого аудио. Для "
            "новых ответов API приложение использует Opus сразу.\n\n"
            "Продолжить?",
            icon="warning",
        ):
            return

        popup = self._begin_cache_operation(
            "transcode",
            title="Vorbis → Opus",
            message="Подготовка списка кэша...",
        )
        if popup is None:
            return

        cancel_event = threading.Event()
        self._cache_transcode_cancel = cancel_event

        def request_transcode_cancel():
            if cancel_event.is_set():
                return
            cancel_event.set()

        progress_label = None
        progressbar = None
        cancel_button = None
        try:
            labels = popup.winfo_children()
            if labels:
                progress_label = labels[0]
            bars = [
                child for child in popup.winfo_children()
                if isinstance(child, ttk.Progressbar)
            ]
            if bars:
                progressbar = bars[0]
                progressbar.stop()
                progressbar.configure(mode="determinate", maximum=1, value=0)
            cancel_button = ttk.Button(
                popup,
                text="Остановить после текущего файла",
                command=request_transcode_cancel,
            )
            cancel_button.pack(pady=(0, 10))
            self._center_popup(popup, 470, 140)
        except tk.TclError:
            pass
        # Красная кнопка окна означает ту же штатную остановку, а не скрывает
        # единственный доступный пользователю индикатор долгой миграции.
        popup.protocol("WM_DELETE_WINDOW", request_transcode_cancel)

        def update_progress(done, total, stats):
            converted = stats.get("converted", 0)
            already_opus = stats.get("already_opus", 0)
            failed = stats.get("failed", 0)
            if progress_label is not None:
                progress_label.configure(
                    text=(
                        f"Проверено {done:,} из {total:,}\n"
                        f"Перекодировано: {converted:,}; уже Opus: "
                        f"{already_opus:,}; ошибок: {failed:,}"
                    ).replace(",", " ")
                )
            if progressbar is not None:
                progressbar.configure(maximum=max(1, total), value=done)
            if cancel_button is not None and cancel_event.is_set():
                cancel_button.configure(state=tk.DISABLED, text="Остановка...")

        def run_transcode():
            stats = {}
            error = None
            try:
                cache_data, source_path, errors = read_cache_index_with_backup(
                    cache_path
                )
                for path, exc in errors:
                    logging.error(
                        "Ошибка чтения индекса перед миграцией %s: %s", path, exc
                    )
                if source_path is None and errors:
                    details = "; ".join(
                        f"{path.name}: {exc}" for path, exc in errors
                    )
                    raise RuntimeError(
                        "основной индекс и резервная копия повреждены: " + details
                    )

                def report_progress(done, total, current_stats):
                    self._post_to_ui(
                        update_progress,
                        done,
                        total,
                        dict(current_stats),
                    )

                def save_checkpoint(current_cache):
                    # Сами OGG уже являются возобновляемыми checkpoint. Огромный
                    # JSON публикуем лишь один раз при завершении/штатной отмене,
                    # иначе индекс на сотни МБ превратится в дисковый bottleneck.
                    if progress_label is not None:
                        self._post_to_ui(
                            progress_label.configure,
                            text="Сохранение обновлённого индекса кэша...",
                        )
                    write_cache_index_atomic(cache_dir, current_cache)

                max_workers = int(
                    self.config.get("max_parallel_encodes", 0) or 0
                ) or None

                stats = transcode_cache_entries_to_opus(
                    cache_dir,
                    cache_data,
                    cancel_event=cancel_event,
                    max_workers=max_workers,
                    progress_callback=report_progress,
                    checkpoint_callback=save_checkpoint,
                )
                for filepath, details in stats.get("errors", []):
                    logging.error(
                        "Не удалось перекодировать %s: %s", filepath, details
                    )

                if stats.get("index_changed"):
                    shared_dir = getattr(self, "_shared_cache_dir", None)
                    if (
                        shared_dir is not None
                        and Path(shared_dir).resolve() == cache_dir.resolve()
                        and getattr(self, "_shared_cache", None) is not None
                    ):
                        with self._shared_cache_lock:
                            self._shared_cache.clear()
                            self._shared_cache.update(cache_data)
                    if (
                        getattr(self, "_cache_ui_loaded", False)
                        and hasattr(self, "cache_data")
                    ):
                        self._post_to_ui(setattr, self, "cache_data", cache_data)
                if getattr(self, "_cache_ui_loaded", False):
                    self._post_to_ui(self._cache_generation_changed)
            except Exception as exc:
                logging.exception("Ошибка массового перекодирования кэша")
                error = f"Не удалось перекодировать кэш:\n{exc}"
            self._post_to_ui(
                self._finish_cache_transcode, popup, stats, error
            )

        try:
            threading.Thread(target=run_transcode, daemon=True).start()
        except Exception:
            self._cache_transcode_cancel = None
            self._end_cache_operation("transcode")
            self._close_popup_safely(popup)
            raise

    @staticmethod
    def _default_cache_variant_policy(current_steps, include_steps, stats):
        """Выбирает безопасную политику и сообщает, нужен ли диалог."""
        has_step_variants = bool(stats.get("steps", 0))
        if not has_step_variants:
            return CACHE_VARIANT_KEEP_ALL, False
        if current_steps is None:
            return CACHE_VARIANT_KEEP_LEGACY, True
        if include_steps:
            return CACHE_VARIANT_KEEP_LEGACY_CURRENT, True
        # В общем ключе legacy и Steps физически не могут сосуществовать для
        # одной фразы. Безопасный default ничего дополнительно не удаляет.
        return CACHE_VARIANT_KEEP_ALL, True

    def _ask_cache_variant_policy(
        self, stats, current_steps, include_steps, default_policy
    ):
        """Показывает интеллектуальный выбор вариантов качества кэша."""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Варианты Steps при оптимизации")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        result = {"value": None}
        selected = tk.StringVar(value=default_policy)
        values_text = ", ".join(
            f"{value}: {count}"
            for value, count in sorted(
                stats.get("steps_by_value", {}).items(), key=lambda item: str(item[0])
            )
        ) or "нет"

        ttk.Label(
            dialog,
            text=(
                "В кэше найдены разные варианты качества.\n"
                "Выберите, какие из актуальных по текстам записей сохранить:"
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=16, pady=(14, 8))
        ttk.Label(
            dialog,
            text=(
                f"Всего записей: {stats.get('total', 0)}\n"
                f"Обычный/legacy-кэш: {stats.get('legacy', 0)}\n"
                f"Записи с явным Steps: {stats.get('steps', 0)} ({values_text})"
            ),
            justify=tk.LEFT,
            foreground=self.get_status_color("muted"),
        ).pack(anchor=tk.W, padx=16, pady=(0, 10))

        choices = []
        if current_steps is None:
            choices = [
                (
                    CACHE_VARIANT_KEEP_LEGACY,
                    "Оставить только обычный/legacy-кэш (рекомендуется)",
                ),
                (CACHE_VARIANT_KEEP_ALL, "Сохранить все варианты Steps"),
            ]
        elif include_steps:
            choices = [
                (
                    CACHE_VARIANT_KEEP_LEGACY_CURRENT,
                    f"Сохранить legacy и текущий Steps = {current_steps} (рекомендуется)",
                ),
                (CACHE_VARIANT_KEEP_ALL, "Сохранить все варианты качества"),
                (
                    CACHE_VARIANT_KEEP_CURRENT,
                    f"Оставить только текущий Steps = {current_steps}",
                ),
            ]
        else:
            choices = [
                (
                    CACHE_VARIANT_KEEP_ALL,
                    "Сохранить текущий общий кэш (рекомендуется)",
                ),
                (
                    CACHE_VARIANT_KEEP_CURRENT,
                    f"Удалить несовместимые варианты и оставить Steps = {current_steps}",
                ),
            ]

        options = ttk.LabelFrame(dialog, text="Политика вариантов", padding=10)
        options.pack(fill=tk.X, padx=16, pady=(0, 10))
        for value, label in choices:
            ttk.Radiobutton(
                options, text=label, value=value, variable=selected
            ).pack(anchor=tk.W, pady=3)

        if not include_steps and current_steps is not None:
            ttk.Label(
                dialog,
                text=(
                    "Учёт Steps в ключе выключен: для одной фразы общий legacy-ключ "
                    "может хранить только один вариант."
                ),
                wraplength=560,
                justify=tk.LEFT,
                foreground=self.get_status_color("warning"),
            ).pack(anchor=tk.W, padx=16, pady=(0, 10))

        buttons = ttk.Frame(dialog)
        buttons.pack(fill=tk.X, padx=16, pady=(0, 14))

        def finish(value):
            result["value"] = value
            self._close_popup_safely(dialog)

        ttk.Button(
            buttons, text="Продолжить", command=lambda: finish(selected.get())
        ).pack(side=tk.RIGHT)
        ttk.Button(
            buttons, text="Отмена", command=lambda: finish(None)
        ).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        dialog.grab_set()
        self._center_popup(dialog, 620, 360, fit_screen=True)
        self.root.wait_window(dialog)
        return result["value"]

    def _choose_cache_variant_policy(self, processor_config):
        """Читает индекс в worker и спрашивает только при наличии Steps."""
        cache_dir = Path(processor_config.get("cache_dir", "cache_audio"))
        cache_path = cache_dir / "sentence_cache.json"
        current_steps = resolve_api_steps(processor_config)
        include_steps = _config_bool(
            processor_config.get("cache_include_steps", True), default=True
        )
        outcome = {}

        popup = self._create_wait_popup(
            "Анализ кэша",
            "Проверка вариантов Steps...",
            modal=False,
            owner=False,
        )
        # Закрытие крестиком только скрывает индикатор. Иначе wait_window мог
        # завершиться раньше worker и принять ещё не вычисленную статистику за
        # пустой кэш.
        popup.protocol("WM_DELETE_WINDOW", popup.withdraw)

        def read_preview():
            cache_preview, source_path, errors = read_cache_index_with_backup(
                cache_path
            )
            for path, exc in errors:
                logging.error(
                    "Не удалось проанализировать варианты Steps в %s: %s",
                    path,
                    exc,
                )
            if source_path is None and errors:
                details = "\n".join(
                    f"{path.name}: {exc}" for path, exc in errors
                )
                outcome["error"] = (
                    f"Не удалось прочитать индекс кэша:\n{details}\n\n"
                    "Оптимизация отменена, чтобы не удалить записи вслепую."
                )
                return
            outcome["stats"] = analyze_cache_step_variants(cache_preview)

        reader = threading.Thread(target=read_preview, daemon=True)
        try:
            reader.start()
        except Exception:
            self._close_popup_safely(popup)
            raise

        def poll_reader():
            if reader.is_alive():
                self.root.after(25, poll_reader)
                return
            self._close_popup_safely(popup)

        poll_reader()
        self.root.wait_window(popup)
        if "error" in outcome:
            self._show_error("Ошибка кэша", outcome["error"])
            return None

        stats = outcome.get("stats", analyze_cache_step_variants({}))
        default_policy, needs_choice = self._default_cache_variant_policy(
            current_steps, include_steps, stats
        )
        if not needs_choice:
            return default_policy
        return self._ask_cache_variant_policy(
            stats, current_steps, include_steps, default_policy
        )

    def optimize_cache(self):
        if self.is_cache_operation_running():
            self._show_info(
                "Кэш занят",
                "Дождитесь завершения текущей операции с кэшем.",
            )
            return
        if self.is_synthesis_running():
            self._show_warning("Занято", "Нельзя оптимизировать кэш во время активного синтеза!")
            return
            
        # Оптимизация не делает API-запросов, поэтому большое steps здесь не
        # требует отдельного подтверждения скорости/качества.
        if not self._validate_api_steps_ui(confirm_large=False):
            return
        self.save_settings()
        processor_config = self.config.copy()
        input_dir = Path(processor_config["input_dir"])
        if self._begin_cache_operation("analyze") is None:
            return
        try:
            variant_policy = self._choose_cache_variant_policy(processor_config)
        finally:
            self._end_cache_operation("analyze")
        if variant_policy is None:
            return

        if not self._ask_yes_no(
            "Оптимизация",
            "Скрипт просканирует папку с текстами и удалит из кэша "
            "аудиофрагменты, которых нет в текущих текстах.\n\n"
            "Будут учтены фразы для всех трёх режимов: по предложениям, "
            "абзацам и целиком. Выбранная политика вариантов Steps будет "
            "применена только к оставшимся актуальным фразам.\n\n"
            "Продолжить?",
        ):
            return

        # Оптимизация работает в фоне и не требует блокировать всё приложение.
        # Без grab_set пользователь может перейти на другие вкладки, свернуть
        # или закрыть только это окно; главное окно при этом не скрывается.
        popup = self._begin_cache_operation(
            "optimize",
            title="Оптимизация кэша",
            message=(
                "Сканирование текстов и проверка кэша...\n"
                "Можно продолжать работать в других вкладках."
            ),
        )
        if popup is None:
            return
        popup.protocol(
            "WM_DELETE_WINDOW",
            lambda: (
                popup.withdraw()
                if self.is_cache_optimization_running()
                else self._close_popup_safely(popup)
            ),
        )
        
        def run_opt():
            processor = None
            cache_snapshot = None
            operation_error = None
            try:
                # TTSProcessor загружает sentence_cache.json в __init__.
                # Для больших индексов это заметная операция, поэтому она
                # должна выполняться внутри worker, а не до показа popup в Tk.
                processor = TTSProcessor(processor_config)
                txt_files = list(input_dir.glob("*.txt"))
                
                if not txt_files:
                    self._post_to_ui(
                        self._show_warning,
                        "Отмена",
                        f"В папке '{input_dir}' не найдено текстовых файлов (.txt).\n"
                        "Оптимизация отменена, чтобы защитить кэш.",
                    )
                    return

                if not processor.cache:
                    self._post_to_ui(self._show_info, "Информация", "Кэш пуст.")
                    return

                # Текст проверяется по единому хэшу normalized_text + speaker.
                # Namespace Steps применяется отдельно как политика вариантов.
                current_steps = resolve_api_steps(processor_config)

                required_hashes = set()
                errors_occurred = False
                
                for f in txt_files:
                    try:
                        with open(f, 'r', encoding='utf-8-sig') as file: raw_text = file.read()
                        file_hashes = processor.get_all_possible_hashes(
                            raw_text, include_prepared=True
                        )
                        required_hashes.update(file_hashes)
                    except Exception as e:
                        logging.error(f"Ошибка при сканировании {f.name}: {e}")
                        errors_occurred = True

                if errors_occurred:
                    raise RuntimeError(
                        "Не удалось прочитать или обработать один или несколько "
                        "TXT-файлов. Удаление отменено, чтобы не потерять "
                        "относящийся к ним кэш. Подробности записаны в лог."
                    )
                if not required_hashes:
                    raise RuntimeError(
                        "Не удалось извлечь ни одного хэша из текстов. "
                        "Оптимизация отменена."
                    )

                keys_to_delete = []
                deleted_stale_count = 0
                deleted_variant_count = 0
                for cache_key, cache_info in processor.cache.items():
                    is_current_variant = cache_entry_matches_required_text(
                        cache_info, required_hashes
                    )
                    variant_allowed = should_keep_cache_variant(
                        cache_info, variant_policy, current_steps
                    )
                    keep_entry = is_current_variant and variant_allowed
                    if not keep_entry:
                        keys_to_delete.append(cache_key)
                        if not variant_allowed:
                            deleted_variant_count += 1
                        else:
                            deleted_stale_count += 1
                
                if keys_to_delete:
                    removed_entries = [
                        processor.cache[key]
                        for key in keys_to_delete
                        if key in processor.cache
                    ]
                    updated_cache = {
                        key: value
                        for key, value in processor.cache.items()
                        if key not in set(keys_to_delete)
                    }
                    files_to_delete = unreferenced_cache_audio_paths(
                        processor.cache_dir,
                        removed_entries,
                        updated_cache,
                    )
                    # Сначала публикуется новый индекс. Если процесс завершится
                    # аварийно позже, максимум останутся безопасные сиротские
                    # OGG, но ни одна живая ссылка не станет битой.
                    write_cache_index_atomic(processor.cache_dir, updated_cache)
                    processor.cache.clear()
                    processor.cache.update(updated_cache)
                    for filepath in files_to_delete:
                        try:
                            filepath.unlink(missing_ok=True)
                        except OSError as exc:
                            logging.warning(
                                "Не удалось удалить сиротский файл %s: %s",
                                filepath,
                                exc,
                            )
                    msg = (
                        "Оптимизация завершена.\n"
                        f"Удалено устаревших по текстам: {deleted_stale_count}\n"
                        f"Удалено по выбранной политике Steps: {deleted_variant_count}\n"
                        f"Всего удалено: {len(keys_to_delete)}"
                    )
                    self._post_to_ui(self._show_info, "Успех", msg)
                else:
                    self._post_to_ui(self._show_info, "Готово", "Оптимизация завершена. Лишних записей не найдено.")
                cache_snapshot = dict(processor.cache)
            except Exception as exc:
                logging.exception("Ошибка оптимизации кэша")
                operation_error = f"Не удалось оптимизировать кэш:\n{exc}"
            finally:
                # Снимок создаётся в worker, чтобы UI не перечитывал большой
                # JSON. Уже открытая таблица обновится без «призраков», но
                # никогда не загружавшаяся вкладка останется ленивой.
                if cache_snapshot is None and processor is not None:
                    cache_snapshot = dict(processor.cache)
                self._post_to_ui(
                    self._finish_cache_optimization,
                    popup,
                    cache_snapshot,
                    operation_error,
                )

        try:
            threading.Thread(target=run_opt, daemon=True).start()
        except Exception:
            self._end_cache_operation("optimize")
            self._close_popup_safely(popup)
            raise

    def clear_entire_cache(self):
        """Полная очистка кэша"""
        if self.is_cache_operation_running():
            self._show_warning("Кэш занят", "Дождитесь завершения текущей операции с кэшем.")
            return
        if self.is_synthesis_running():
            self._show_warning("Занято", "Нельзя полностью очищать кэш во время активного синтеза!")
            return
        if not self._ask_yes_no(
            "🔥 КРИТИЧЕСКОЕ ДЕЙСТВИЕ",
            "Вы уверены, что хотите полностью очистить синтезированный кэш?\n"
            "Аудиофрагменты, паузы и индекс будут удалены безвозвратно.\n"
            "Файл glossary.json будет сохранён.",
            icon="warning",
        ):
            return

        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        popup = self._begin_cache_operation(
            "clear",
            title="Очистка кэша",
            message="Удаление аудиофрагментов, пауз и индекса...",
        )
        if popup is None:
            return

        def run_clear():
            error = None
            try:
                clear_cache_storage(cache_dir)
            except Exception as exc:
                logging.exception("Не удалось полностью очистить кэш")
                error = f"Не удалось очистить кэш:\n{exc}"
            self._post_to_ui(self._finish_cache_clear, popup, error)

        try:
            threading.Thread(target=run_clear, daemon=True).start()
        except Exception:
            self._end_cache_operation("clear")
            self._close_popup_safely(popup)
            raise

    def _finish_cache_clear(self, popup, error=None):
        self._end_cache_operation("clear")
        self._close_popup_safely(popup)
        if error:
            self._show_error("Ошибка", error)
            return
        self._cache_generation_changed()
        self._clear_cache_view()
        self._show_info(
            "Готово", "Синтезированный кэш очищен. Глоссарий сохранён."
        )

    def archive_cache(self):
        if self.is_cache_operation_running():
            self._show_warning("Кэш занят", "Дождитесь завершения текущей операции с кэшем.")
            return
        if self.is_synthesis_running():
            self._show_warning(
                "Занято",
                "Нельзя архивировать кэш во время активного синтеза: индекс "
                "и аудиофайлы могут измениться в процессе копирования.",
            )
            return
        cache_dir = Path(self.config.get("cache_dir", "cache_audio")).resolve()
        if not cache_dir.is_dir():
            self._show_info("Пусто", "Папка кэша не существует.")
            return
            
        out_zip = filedialog.asksaveasfilename(
            initialdir=resolve_dialog_initial_dir(cache_dir, BASE_DIR),
            defaultextension=".zip",
            filetypes=[("ZIP Archive", "*.zip")],
            initialfile="cache_audio_backup.zip",
        )
        if not out_zip:
            return
        out_zip = str(Path(out_zip).with_suffix(".zip").resolve())
        try:
            Path(out_zip).relative_to(cache_dir)
        except ValueError:
            pass
        else:
            self._show_error(
                "Некорректный путь",
                "ZIP-архив нельзя сохранять внутрь самой папки кэша.",
            )
            return

        delete_after_zip = _config_bool(self.del_after_zip.get())
        
        # Создаем окно ожидания
        popup = self._begin_cache_operation(
            "archive",
            title="Архивация",
            message="Создание ZIP-архива кэша...\nПожалуйста, подождите.",
        )
        if popup is None:
            return
        popup.protocol("WM_DELETE_WINDOW", popup.withdraw)
        
        def run_zip():
            error_message = None
            try:
                archive_path = create_zip_archive_atomic(cache_dir, out_zip)
                if delete_after_zip:
                    clear_cache_storage(cache_dir)
                self._post_to_ui(
                    self._finish_cache_archive,
                    popup,
                    archive_path,
                    delete_after_zip,
                    None,
                )
            except Exception as exc:
                logging.exception("Не удалось создать архив кэша")
                error_message = f"Не удалось создать архив:\n{exc}"
                self._post_to_ui(
                    self._finish_cache_archive,
                    popup,
                    None,
                    False,
                    error_message,
                )

        try:
            threading.Thread(target=run_zip, daemon=True).start()
        except Exception:
            self._end_cache_operation("archive")
            self._close_popup_safely(popup)
            raise

    def _finish_cache_archive(
        self, popup, archive_path=None, cache_cleared=False, error=None
    ):
        self._end_cache_operation("archive")
        self._close_popup_safely(popup)
        if cache_cleared:
            self._cache_generation_changed()
            self._clear_cache_view()
        if error:
            self._show_error("Ошибка", error)
        else:
            self._show_info("Успех", f"Архив создан:\n{archive_path}")

    def _clear_cache_view(self):
        """Очищает только RAM/Treeview, не перечитывая индекс кэша с диска."""
        self.cache_data = {}
        old_items = self.cache_tree.get_children()
        if old_items:
            self.cache_tree.delete(*old_items)
        self.lbl_cache_count.config(text="Всего записей: 0")
        self._cache_ui_loaded = True

    # --- Вкладка "Справка" ---
    def setup_help_tab(self):
        ctrl_frame = ttk.Frame(self.tab_help)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
        font_cb = ttk.Combobox(ctrl_frame, textvariable=self.font_size_var, values=[10, 12, 14, 16, 18, 20, 24], state="readonly", width=5)
        font_cb.pack(side=tk.RIGHT)
        ttk.Label(ctrl_frame, text="Шрифт:").pack(side=tk.RIGHT, padx=5)
        font_cb.bind("<<ComboboxSelected>>", lambda e: self.root.after(10, self.update_fonts))

        # --- ИСПРАВЛЕНИЕ СКРОЛЛБАРА ---
        text_frame = ttk.Frame(self.tab_help)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Сначала пакуем скроллбар
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Затем пакуем текст
        self.help_text_widget = tk.Text(text_frame, wrap=tk.WORD, font=("Arial", self.font_size_var.get()))
        self.help_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.help_text_widget.config(yscrollcommand=scrollbar.set)
        scrollbar.config(command=self.help_text_widget.yview)
        # ------------------------------
        
        help_text = r"""Добро пожаловать в Silero TTS Studio v{APP_PUBLIC_VERSION}!

Это профессиональная рабочая среда для генерации аудиокниг, подкастов и озвучки текста с помощью нейросети Silero. Программа разработана с акцентом на бережное отношение к API-лимитам, быстрый RAM-индекс кэша, гибридную постобработку звука и автоматизацию сборки.

====================================================================
🚀 1. БЫСТРЫЙ СТАРТ
====================================================================
1. Перейдите во вкладку "Настройки" -> "API и Лимиты" и введите ваш API Token.
   Экспериментальный Steps можно оставить выключенным: тогда параметр не
   отправляется и сервер использует значение по умолчанию 16.
2. Во вкладке "Импорт книг" выберите файл книги (EPUB, FB2, DOCX, TXT) и нажмите "Извлечь и Нарезать". Скрипт автоматически разобьет книгу на главы и сохранит их в папку input_texts.
3. Перейдите во вкладку "Синтез из папки" и нажмите "▶ Старт (Все)".
4. Готовые аудиофайлы появятся в папке output_audio.
5. В процессе работы список файлов автоматически прокручивается. Вы всегда можете вернуться к активному файлу кнопкой "📍 К текущему файлу".

====================================================================
🧰 РАБОТА БЕЗ API-КЛЮЧА
====================================================================
API Token нужен только для создания новых речевых фрагментов. Без ключа доступны
локальные операции: импорт и извлечение EPUB/FB2/DOCX, RegEx-нарезка TXT по
главам с шаблонами {author} и {num:N}, проверка нормализации в песочнице,
редактирование глоссария, сохранение подготовленного TXT, а также группировка,
разгруппировка, склейка, конвертация, эффекты и теги уже существующих MP3/WAV/
OGG/Opus. Заполненный кэш можно просматривать и обслуживать без новых API-
запросов. Обычный запуск синтеза из папки сейчас всё равно проверяет наличие
токена перед стартом, даже если предполагается полный cache-hit; это не приводит
к запросам, но для полностью офлайн-сценария используйте локальные инструменты
или сохранённый подготовленный текст.

====================================================================
🎙 2. РЕЖИМЫ СИНТЕЗА (SYNTHESIS MODES)
====================================================================
В "Настройках" -> "Вывод и Теги" доступно 3 режима генерации:

• По предложениям (sentence) — [РЕКОМЕНДУЕТСЯ]:
  Текст разбивается на отдельные предложения.
  - Плюсы: Максимальная точность кэширования. Если вы измените или опечатаетесь в одном предложении книги на 500 страниц, программа переозвучит ТОЛЬКО это одно предложение, взяв остальные 99.9% из кэша!
  - Гибкость: Позволяет высчитывать индивидуальные паузы между фразами, перед прямой речью/мыслью в кавычках и после двоеточий.

• По абзацам (paragraph):
  Каждый абзац текста отправляется в API целиком.
  - Плюсы: Более естественный и связный темп речи внутри одного абзаца.
  - Особенности: Кэш привязывается к целому абзацу. При изменении хотя бы одного символа переозвучивается весь абзац.

• Большими блоками (full):
  Программа склеивает весь текст главы в гигантские аудиоблоки.
  - Плюсы: Минимальное количество запросов к API и самая монолитная интонация речи.
  - Защита: Включает встроенную защиту от переполнения (SAFE_LIMIT = 30 000 символов). Если глава слишком большая, программа автоматически разрывает блок по границам абзацев и вставляет паузу.

====================================================================
⚡ 3. ПРЯМОЙ СИНТЕЗ (ЛАБОРАТОРИЯ ТЕСТОВ)
====================================================================
Вкладка "Прямой синтез" предназначена для быстрой озвучки произвольного текста:

• Поле ввода: Вставьте любой фрагмент текста и нажмите "▶ Синтезировать".
  Пустой ввод или строка только из пунктуации не создают фиктивный файл из
  стартовой/финальной паузы: будет показан статус «Нет текста». Намеренную
  тишину можно создать отдельной строкой-разделителем.
• Имя файла: Назовите итоговый трек (по умолчанию direct_output.mp3).
• Папка: По умолчанию тестовый файл сохраняется отдельно, в direct_audio. Путь можно изменить в единственном поле на самой вкладке; в "Настройки" -> "Папки" остаётся только пояснение без дублирующего поля.
• Чекбоксы управления:
  - [x] Сохранить: Сохраняет аудиофайл на диск в выбранную папку прямого синтеза.
  - [x] Игнорировать кэш: Принудительно генерирует речь заново через API.
  - [x] Авто-воспроизведение: Мгновенно проигрывает результат через системный плеер.
  - [ ] Применить теги из настроек: По умолчанию выключено, поэтому тестовый direct_output не получает книжные теги и обложку.
  - [ ] Текст уже подготовлен: Для заранее нормализованного TXT или текста из внешнего редактора. Флажок не сохраняется, перед запуском требует подтверждения и автоматически снимается после завершения.
• Умный плеер: Кнопка "🔊 Слушать" мгновенно прерывает старый трек при повторном нажатии (никакой звуковой каши). Кнопка "🔇" принудительно останавливает любой звук.
• Эффекты: Индивидуальные ползунки скорости, тона и эхо. Нажатие кнопки "💾 Сделать глобальными" мгновенно применяет эти эффекты ко всем настройкам программы.

====================================================================
⏱ 4. ТОНКАЯ НАСТРОЙКА ПАУЗ И РАЗДЕЛИТЕЛЕЙ
====================================================================
Во вкладке "Настройки" -> "Паузы и Разделители" вы можете настроить идеальный ритм повествования (длительность указывается в миллисекундах, 1000 мс = 1 сек):

• Пауза в начале / конце файла: Задает тишину на старте и в самом финише трека (удобно для плееров).
• Между предложениями: Базовая пауза между обычными предложениями внутри абзаца.
• Между абзацами: Пауза при переходе на новую строку текста.
• Перед диалогом / мыслью в кавычках: Автоматически УВЕЛИЧИВАЕТ паузу перед абзацем, если он начинается с тире (—) либо открывающей одинарной/двойной кавычки. Поэтому отдельная строка «Мысль персонажа» получает тот же ритм, что и реплика.
• После двоеточия: Увеличивает паузу перед следующим абзацем, если предыдущий заканчивался на двоеточие (:), в том числе перед закрывающей кавычкой или скобкой.
• Без задвоения: Если на одной границе одновременно подходят пауза между абзацами, перед репликой, после двоеточия или пауза строки-разделителя, программа вставляет одну наибольшую паузу, а не складывает их.
  Точные значения между абзацами применяются в режимах sentence/paragraph. В full абзацы внутри одного API-блока разделены переводами строк, и их ритм выбирает модель; числовая пауза используется на разделителях и защитных разрывах SAFE_LIMIT.
• Символы-разделители (☆☆☆, ***, ###, ---):
  Управление разделителями осуществляется через динамические поля (добавление строчки кнопкой "➕ Добавить", удаление — "❌"). Когда программа встречает указанный символ в тексте, она полностью вырезает его и вставляет на его место чистую тишину заданной длины ("Пауза разделителя"). Разделитель срабатывает только на отдельной строке: кроме него там допустимы лишь пробелы и табуляции. Строки --- и ––– защищаются до нормализации тире, поэтому не превращаются в начало диалога.
  В прогрессе такая строка явно показывается как [ПАУЗА РАЗДЕЛИТЕЛЯ], чтобы намеренная тишина не выглядела зависанием.
  Два и более разделителя подряд не схлопываются: каждый добавляет полную настроенную паузу. С правилом максимума объединяется только структурная пауза между последним разделителем и следующим обычным абзацем.
• Минус перед числом: Обычный дефис сохраняется и слитно, и через пробел (-5, - 5), чтобы ru-normalizr произнёс его как «минус». Unicode-минус (−5, − 5) приводится к обычному дефису. Короткое и длинное тире в начале строки остаются маркерами реплики; дефис перед порядковым числительным в начале отдельной строки (- 62-й ранг) также считается началом реплики, а не отрицательным числом.

====================================================================
⚙️ 5. ПОЛНЫЙ ЦИКЛ ОБРАБОТКИ И НОРМАЛИЗАЦИИ ТЕКСТА
====================================================================
Чтобы нейросеть правильно озвучила текст, программа бережно обрабатывает каждую фразу строго в следующем порядке:

0. UTF-8 BOM: Служебный BOM в самом начале TXT бесшумно удаляется до RegEx и вычисления ключа кэша, поэтому шаблоны с ^ работают одинаково для UTF-8 с BOM и без него. Внутренние U+FEFF не удаляются как обычный текст.
1. Математика и плюсы: Математические плюсы (1 + 1) и одиночные плюсы заменяются на слово "плюс". Плюсы внутри и в начале слов (з+амок, +аура) маскируются для защиты ручных ударений.
2. Обычные RegEx-правила: Применяются шаблоны замены из Глоссария ДО разбивки на предложения. Verbatim-RegEx выполняются позднее внутри одного предложения.
3. Сегментация: Текст разбивается на предложения с помощью библиотеки Razdel.
4. Авто-аббревиатуры: Превращает "И.И.", "к.п.д." в "И-И", "к-п-д", чтобы нейросеть произносила их по буквам.
5. Авто-сокращения: Убирает точки у слов из 1-3 букв ("г.", "ул.", "ур."), чтобы диктор не делал фальшивую паузу посреди фразы.
6. Глоссарий терминов и ударений: Заменяются слова, применяются verbatim-RegEx и расставляются плюсы ударений.
7. Нормализация (ru-normalizr): Преобразует числа, даты и числительные в пропись ("10" -> "десять"). Точные verbatim-замены восстанавливаются после normalizer и очистки.
8. Очистка: Удаляются только уже предусмотренные лишние кавычки, скобки и спецсимволы. Дополнительная агрессивная очистка всей начальной пунктуации не выполняется: это сохраняет знаки чисел, формулы и ручные ударения.
9. Защита пунктуации: Если после обработки у предложения пропала финальная точка, программа возвращает её, гарантируя правильную интонацию, после чего фраза уходит в API.
   После нормализации самостоятельный чанк, в котором нет ни русских, ни
   ASCII-букв, пропускается: EnhancedTTS считает состоящий только из
   неподдерживаемых символов фрагмент пустым и возвращает HTTP 422
   «Your text is empty!».
   Unicode внутри поддерживаемой фразы не очищается, поэтому её payload и ключ
   кэша не меняются. Ожидаемый пропуск пишется в журнал на уровне INFO, а
   предупреждения API содержат короткие source и normalized для диагностики.

====================================================================
🧪 5.1. ПЕСОЧНИЦА И ПРОФИЛИ НОРМАЛИЗАТОРА
====================================================================
Вкладка "Нормализатор" запускает тот же текстовый pipeline без API-запроса:

• Слева вводится редактируемый пример, ниже показывается реальный результат.
• Справа доступны профили TTS/Safe и публичные стадии ru-normalizr 0.3.x.
• Текстовая часть изначально компактнее панели параметров; границу можно
  перетянуть вручную. Размер шрифта исходника и результата выбирается рядом с
  кнопками preview и сохраняется как общая настройка текстовых вкладок.
• ru-normalizr, Глоссарий и preprocessing Studio можно временно отключать,
  чтобы найти ошибочное правило. Эти изменения локальны до кнопки
  "Применить глобально"; "Вернуть глобальные" отменяет эксперимент.
• Строка под кнопками всегда показывает сохранённый глобальный профиль и
  отдельно отмечает неприменённые локальные изменения. Тот же глобальный
  снимок расшифрован в "Настройки" -> "Нормализация"; оттуда можно открыть
  песочницу, не дублируя все расширенные поля.
• Профиль переносится отдельным JSON без API Token, папок проекта и локального
  абсолютного пути к каталогу словарей. Импорт сохраняет локальный путь
  получателя; полный снимок публичных опций строго проверяется без тихой
  подмены опечаток значениями TTS/Safe.
• Ctrl/⌘+Enter запускает проверку сразу; обычное редактирование обновляет
  результат с небольшой задержкой и не позволяет старому фоновому результату
  перезаписать более новый.
• "Сохранить TXT…" закреплена в постоянно видимой нижней панели. До успешного непустого preview кнопка видна, но отключена; изменение текста, профиля или JSON глоссария инвалидирует старый результат. TXT записывается в UTF-8 без BOM. Видимая строка [ПАУЗА РАЗДЕЛИТЕЛЯ] заменяется первым активным разделителем. Если для абзаца нужна пауза перед репликой, в файле сохраняется структурный префикс "— "; перед API он снимается. При наличии пропущенных неподдерживаемых фрагментов перед сохранением показывается предупреждение: в TXT этих фрагментов не будет.
• Перед обычным пакетным или прямым синтезом несохранённый глоссарий предлагается записать, чтобы preview и TTSProcessor использовали одни правила. Редактор привязан к glossary.json текущего cache_dir: после смены кэша старый JSON нельзя записать поверх нового, а устаревший preview нельзя сохранить. "Текст уже подготовлен" обходит эту проверку вместе с глоссарием.
• Для повторного запуска включите флажок "Текст уже подготовлен" в "Синтезе из папки" или "Прямом синтезе". Он не пишется в settings.json, автоматически снимается после завершения и для подтверждённого запуска обходит RegEx, глоссарий, автообработку Studio, ru-normalizr и финальную очистку. Разбиение на чанки, SAFE_LIMIT и расстановка пауз продолжают работать.
• TXT хранит текст и структурные маркеры, но не профиль нормализатора, synthesis_mode или числовые паузы. Для эквивалентных пауз используйте тот же режим синтеза и те же настройки пауз.

====================================================================
📖 6. ИМПОРТ И НАРЕЗКА КНИГ (EPUB, FB2, DOCX, TXT)
====================================================================
Вкладка "Импорт книг" позволяет мгновенно подготовить любую электронную книгу к озвучке:

• Поддерживаемые форматы: .epub, .fb2, .docx, .txt.
• Авто-структурирование: Скрипт рекурсивно читает оглавление (TOC) книги и режет её точно по главам.
• Автоматическое извлечение автора: Извлекает метаданные автора из файлов EPUB и FB2.
• Умная очистка HTML (EPUB/FB2): Парсер корректно обрабатывает теги <br/> и не разрывает абзацы из-за внутреннего форматирования (жирный, курсив, ссылки), выдавая идеально чистый текст.
• Нарезка TXT по RegEx: Вы можете нарезать обычный текстовый файл по шаблону заголовков (например: ^Глава \d+ или ^Часть [I-V]+).
• Шаблоны имен файлов: Используйте переменные {name}, {title}, {author} и {num}. 
  Синтаксис {num:0} или {num:15} позволяет начать нумерацию с нужного вам числа (с нуля, с 15 и т.д.).
• Сохранение в один файл: Галочка позволяет извлечь весь текст книги в один монолитный txt-файл.

====================================================================
📚 7. ГЛОССАРИЙ: УДАРЕНИЯ, ТЕРМИНЫ И REGEX
====================================================================
Вкладка "Глоссарий" редактирует файл glossary.json и устраняет ошибки произношения:

• Ударения (+): Добавьте слово с плюсом перед гласной (например: з+амок, зам+ок). Программа сама поймет оригинал ("замок") и будет применять правило только к нему.
• Замена терминов: Позволяет заменить сокращение на полное слово (например: "ОС" -> "операционная система").
• Режимы регистра: "Чувствительно к регистру" (Strict Case) заменяет только точные совпадения. Без галочки (Ignore Case) программа умна: если исходник "Ос", замена станет "Операционная система".
• RegEx (Паттерны): Мощные регулярные выражения. Например, замена всех римских цифр или очистка сносок [1], (прим. ред.).
• Verbatim: Галочка "Не нормализовать замену" вставляет результат после
  ru-normalizr и очистки, поэтому убого -> уб+о'го сохраняет плюс и апостроф
  точно. По умолчанию термин совпадает как целое слово; поиск внутри слова
  включается отдельной галочкой. Флаг доступен и RegEx, но такой RegEx работает
  внутри одного уже выделенного предложения, а не через несколько строк.
  Несколько последовательных совпадений в одном защищённом слове заменяются все.
• Контекст: Пока исходный термин сам переживает normalizer, он остаётся видим
  библиотеке (Глава IV -> Гл+ава четвёртая). Если normalizer изменил сам
  исходник, используется резервная непрозрачная защита и в лог пишется WARNING:
  рядом с таким правилом грамматическое согласование может отличаться.
• Управление шрифтом: В правом верхнем углу расположен выпадающий список размера шрифта (10–24) для комфортного чтения редактора.
• Объединение глоссариев: Импорт добавляет выбранные ударения, термины и RegEx.
  При конфликте личное правило сохраняется по умолчанию; замену импортируемым
  нужно выбрать явно. После merge показывается статистика, порядок не теряется.
• Удаление правил: Кнопка "Удалить правила…" открывает менеджер с поиском и
  вкладками ударений, терминов и RegEx. "Выбрать результаты поиска" отмечает
  только показанные фильтром строки текущего раздела, а "Выбрать все в разделе" —
  включая скрытые поиском. "Снять всё в разделе" снимает выбор и со
  скрытых строк текущего раздела; "Очистить весь глоссарий…" очищает известные
  секции правил. Правки сначала попадают в JSON-редактор
  и записываются только кнопкой "Сохранить файл"; служебные поля будущих версий
  не стираются.
• Перекрывающиеся правила: Если одному слову соответствуют ударение и термин
  в том же режиме регистра, совместимый приоритет остаётся у термина. Оба
  правила хранятся, а неактивное ударение отмечается в менеджере и журнале.
• Выборочный импорт/экспорт: При сохранении или загрузке Настроек и Глоссария появляются удобные окна с галочками — вы сами решаете, какие именно группы параметров (папки, нормализатор, эффекты, API) перенести в другой проект.
• Смена папки кэша: Чистый редактор автоматически загружает glossary.json из
  новой папки. Несохранённые изменения требуют явного решения; ошибка доступа
  показывается штатно и не очищает текущий текст редактора.
• Безопасная перезагрузка: Ручное перечитывание файла требует подтверждения,
  если JSON менялся. Повреждённый primary не скрывает исправный .bak; если обе
  копии невалидны, текущий текст редактора остаётся без изменений.

====================================================================
🎛 8. АУДИОЭФФЕКТЫ И ПОСТОБРАБОТКА (FFMPEG)
====================================================================
Вы можете менять звучание голоса БЕЗ повторных запросов к API и БЕЗ траты лимитов! Эффекты накладываются локально через FFmpeg:

• Скорость (Tempo): Ускорение или замедление речи от 0.5x до 3.0x без изменения высоты голоса.
• Тон (Pitch): Изменение высоты голоса (сделать голос басистее или выше).
• Эхо (Reverb/Delay): Настройка задержки (мс) и затухания (decay). Идеально для мыслей персонажей, воспоминаний или фэнтези-существ.
• Естественное сжатие пауз: При изменении скорости речи паузы между предложениями пропорционально адаптируются, сохраняя живой ритм.

Эффекты можно настраивать во вкладках "Прямой синтез", "Кэш", "Экспорт и Сборка", а также глобально в "Настройках".

• Аудиопрофили: Кнопка во вкладке экспорта и в "Настройки" -> "Вывод и
  Теги" открывает отдельный менеджер формата, битрейта, частоты, каналов и
  эффектов. Встроенные профили можно использовать как основу; пользовательские
  сохраняются по имени и импортируются/экспортируются отдельным JSON.
• Безопасное редактирование: "Добавить", "Сохранить профиль", "Удалить" и
  импорт сначала меняют временную копию списка. "Сохранить и закрыть" записывает
  её, а "Отмена", Escape или закрытие окна отбрасывают изменения. Текущие поля
  автоматически сохраняются в ту же временную копию при commit или применении;
  перед сменой профиля можно сохранить их, отбросить либо остаться в редакторе.
  Кнопки применения и закрытия находятся в отдельных рядах и не вытесняют друг
  друга. "К сборке книги" и "К экспорту" применяют сохранённую версию и
  закрывают окно.
  Уже записанный внешний JSON-файл кнопкой "Отмена" не удаляется.
• Профиль — копируемый шаблон, а не скрытый постоянно активный слой. Применение
  заменяет текущие параметры выбранной области; последующая ручная правка не
  меняет сохранённый профиль. Интерфейс показывает имя лишь при точном
  совпадении и всегда расшифровывает текущие формат, битрейт, частоту, каналы и
  эффекты.
• Раздельное применение: "К сборке книги" меняет также Прямой синтез, потому
  что они используют общий финальный вывод, и заменяет значения в
  "Настройки" -> "Эффекты (Постобработка)". "К экспорту" меняет только
  универсальный сборщик и его локальные эффекты. Последняя явная ручная правка
  побеждает; форматы этих двух областей больше не связаны.
• Auto книги: Для частоты и каналов это канонические 48 кГц mono внутреннего
  кэша. Auto-битрейт означает 128 кбит/с для MP3, 48 кбит/с для Opus,
  quality/VBR кодировщика для OGG/Vorbis и не применяется к WAV.
• Флажок эффектов: Если "Включить эффекты в профиле" выключено, книга и Прямой
  синтез получают чистые speed/pitch 1.0 без эха, а у универсального экспорта
  отключается флажок "Наложить эффекты".
• Локальные ползунки Прямого синтеза применяются к его текущему запуску. Они
  заменяют глобальные значения только после отдельной кнопки "Сделать
  глобальными".

====================================================================
🎵 9. ЭКСПОРТ И СБОРКА (АУДИОКНИГИ, ТЕГИ, ОБЛОЖКИ)
====================================================================
Вкладка "Экспорт и Сборка" — это полноценный комбайн для компиляции и тегирования аудиофайлов:

• Файлы в Корне и Группах: Файлы можно добавлять как в древовидные Группы (Тома), так и прямо в корень проекта.
• Расчет Общего Времени: В шапке дерева в реальном времени отображается суммарная длительность всех аудиофайлов (HH:MM:SS).
• Авто-заполнение папки: При добавлении файлов программа сама подставляет папку их расположения в поле экспорта. Единая память путей сохраняется между выбором файлов и папок.
• Натуральная сортировка: Клик по заголовку "Имя ↕" сортирует файлы по-человечески (Глава 2 встает перед Глава 10).
• Фоновый импорт метаданных: Чтение тегов и извлечение встроенных обложек через ffprobe происходит в фоновом потоке пачками по 10 файлов — интерфейс остается живым.
• Перегруппировка: Кнопка "🔗 Объединить" собирает выбранные группы и/или файлы в одну группу: первая группа по порядку дерева сохраняет имя, теги и параметры, итоговая группа занимает место первого выбранного источника, а дорожки получают текущий визуальный порядок дерева. Остальные выбранные группы удаляются только из списка проекта. Если выбраны только файлы, создаётся новая группа. Не выбранная целиком группа сохраняется даже после переноса всех отмеченных файлов, чтобы её настройки не исчезли молча. "📤 Разгруппировать" переносит файлы выбранных групп в родительский уровень.
• Очистка проекта: Кнопка "🧹 Очистить всё" после подтверждения удаляет только элементы текущего списка (группы и ссылки на файлы), не удаляя исходные аудиофайлы с диска.
• Массовое применение настроек: Кнопка "⚙️ Применить ко всем группам..." открывает диалог, позволяющий в 1 клик применить параметры склейки, подпапок, пауз ко всем томам и сохранить их по умолчанию.
• Компактные настройки группы: Основные параметры размещены нативной ttk-сеткой
  без отдельного Canvas и внутренней полосы прокрутки, поэтому на светлой теме
  не появляется пустая белая область или боковая линия. Флажки склейки и подпапки стоят рядом,
  а массовое применение остаётся на своей отдельной строке.
• Адаптивный статус: Подпись сборки использует короткие стадии "Склейка",
  "Экспорт" и "Теги". Не помещающееся при текущих ширине окна и шрифте имя
  получает многоточие в середине с сохранением начала и окончания. Сокращается
  только временная подпись: полное имя сохраняется в дереве без изменения,
  а индикатор прогресса занимает остаток строки.
• Параметры аудиопрофиля: Краткая расшифровка, включая состояние
  "Пользовательские параметры", находится в собственной строке под эффектами и
  не отнимает место у флажка тегов и кнопок запуска.
• Склеивание в один трек: Склеивает все файлы группы в один большой аудиофайл с настройкой паузы между ними (в мс).
• Потоковая склейка длинных групп: MP3/WAV/OGG/Opus декодируются и объединяются самим FFmpeg без общего PCM-буфера Pydub и промежуточного RIFF. Поэтому книга не упирается в 4-ГиБ заголовок WAV; для очень большого результата WAV FFmpeg автоматически использует RF64.
• Профиль результата: Поля «Битрейт», «Частота» и «Каналы» относятся только к этой вкладке, сохраняются в настройках и одинаково применяются к одиночному файлу, несклеенной группе и общей дорожке. Режим «Авто» (в настройках — auto) предсказуемо сохраняет исходные параметры одиночного файла или однородной группы, включая известный общий битрейт: MP3 128 кбит/с при выводе в Opus останется 128 кбит/с, поэтому результат не обязан быть меньше простой MP3-склейки. Для сжатия речевой книги явно выберите Opus 48 кбит/с или 32 кбит/с (меньше размер, но выше потеря качества). Для смешанной группы «Авто» выбирает максимальную частоту и stereo при наличии многоканального входа, а при неизвестном профиле использует безопасные 48 кГц stereo. Явные частота и каналы поддерживаются MP3, OGG, Opus и WAV в пределах возможностей кодека. Битрейт применяется только к MP3/OGG/Opus; для WAV PCM он отключён и не применяется. OGG остаётся совместимым Ogg/Vorbis (.ogg), а отдельный Ogg/Opus (.opus) создаёт тот же Ogg-контейнер с рекомендованным для Opus расширением. При необходимости файл .opus можно вручную переименовать в .ogg без перекодирования, но приложение не предлагает этот менее совместимый вариант. В «Авто» MP3 получает ближайшую поддерживаемую libmp3lame частоту (не выше 48 кГц); явная неподдерживаемая частота не подменяется. OGG/Vorbis сохраняет 88,2/96 кГц только при битрейте «Авто» в quality/VBR-режиме; Opus поддерживает 8/12/16/24/48 кГц. Несовместимые явные сочетания низкой частоты, mono и высокого битрейта блокируются с объяснением; в «Авто» сохраняются частота и каналы, но несовместимый битрейт не наследуется. Пауза вставляется до общего эффекта скорости и изменяется вместе со всей дорожкой только один раз.
• Независимый формат: Выбор формата этой вкладки не меняет формат будущей
  синтезируемой книги. Старый settings.json и выборочный импорт старого профиля
  копируют прежнее общее значение в обе области, после чего они сохраняются
  раздельно.
• Наследование тегов при склейке: Если поля ID3 группы не заполнены, исполнитель, альбом, исполнитель альбома, жанр, композитор, год и обложка берутся из первого файла группы. Явно заданные значения группы имеют приоритет, а имя итогового файла и тег Title формируются из имени группы.
• Авто-разбивка по времени: Работает как с уже созданными группами, так и только с файлами в корне. Нумерация адаптируется к последнему отображаемому номеру. При ручном добавлении новой группы `01` и `1` считаются одним номером, а уже видимая разрядность сохраняется.
• Безопасность сборки: Пока экспорт активен, дерево, перегруппировка и настройки блокируются, включая macOS-обходы выделения. После разблокировки выбранная группа или файл полностью перечитываются в редактор. Перед стартом проверяются исчезнувшие исходники и совпадающие выходные имена.
• [x] Только обновить теги (In-place tagging): Метаданные меняются без перекодирования аудио; формат, битрейт, частота, каналы и все элементы эффектов на это время блокируются. Новая JPEG/PNG-обложка встраивается для MP3 как ID3v2.3/APIC, для Opus — как METADATA_BLOCK_PICTURE; для OGG-Vorbis и WAV она пропускается с записью в журнал вместо ошибки FFmpeg.
• Редактор тегов и Обложек: Название, исполнитель, альбом, исполнитель альбома, жанр, композитор и год сохраняются в MP3, OGG и Opus. Обложка Opus записывается штатным комментарием без отдельного opusenc; её отображение зависит от поддержки METADATA_BLOCK_PICTURE конкретным проигрывателем (часть проигрывателей Windows её не показывает). «Сохранять в подпапку» доступно только если группа не склеивается.
• Умная сетка применения тегов (2х2):
  - [⬇ К файлам группы]: Копирует теги текущей группы на все входящие в нее файлы.
  - [⬆ В род. группу]: Копирует теги с выделенного файла на его родительскую группу.
  - [☑ К выделенным]: Применяет теги ко всем выделенным элементам.
  - [🔄 Ко всем элементам]: Применяет теги абсолютно ко всем группам и файлам.

====================================================================
💾 10. УПРАВЛЕНИЕ КЭШЕМ И БЕЗОПАСНОСТЬ
====================================================================
• RAM-индекс кэша: Индекс доступных фрагментов загружается в оперативную память,
  поэтому совпавшие фразы находятся быстро и не требуют повторного API-запроса.
  Финальная сборка может выполняться параллельно; её скорость зависит от CPU,
  диска, числа процессов FFmpeg и выбранного профиля вывода.
• Steps и ключ кэша: Сам Steps по умолчанию выключен и не отправляется. Если его включить, флажок учёта Steps в ключе кэша по умолчанию активен, поэтому один текст/голос с разными Steps хранится отдельно. Если снять этот флажок, используется общий старый ключ: legacy-записи доступны, но при известном несовпадении Steps общий файл заменяется последним вариантом и смена значения может потребовать нового запроса.
• Компактный Ogg/Opus-кэш: Новые ответы API и локальные паузы сохраняются как Ogg/Opus 48 кГц mono (целевой битрейт 48 кбит/с). Если автообрезка выключена и API уже вернул совместимый Opus, байты публикуются без повторного lossy-кодирования.
• Обрезка без Vorbis: При включённой автообрезке цепочка имеет вид Opus → PCM в памяти → Opus. Промежуточные WAV и Vorbis в кэше не создаются; PCM — несжатое рабочее представление Pydub для точной обрезки тишины.
• Проверка физического кодека: Перед публикацией и склейкой приложение читает только первые 4 КиБ каждого внутреннего OGG и проверяет реальный OpusHead, а не доверяет одному полю audio_codec в JSON. FFprobe и декодирование для этой проверки не запускаются, поэтому она защищает от смешанного/повреждённого кэша без заметного замедления сборки.
• Миграция старого кэша: Кнопка «Перекодировать Vorbis → Opus» в фоне и атомарно переводит прежние речевые фрагменты из cache/audio, перечисленные в sentence_cache.json, не меняя имён, хэшей и привязки книги. Операцию можно остановить после текущих файлов и позднее продолжить; готовый Opus будет пропущен. Сами заменённые OGG являются контрольными точками, а большой индекс записывается один раз при завершении/остановке. Число процессов берётся из настройки параллельных FFmpeg-сборок; автоматический режим использует до 24. Это необратимое перекодирование между lossy-кодеками, поэтому перед запуском показывается предупреждение.
• Смешанный кэш: Если массовую миграцию не запускать, «Старт (Все)» по исходным TXT преобразует при первом cache-hit только реально встреченные старые Vorbis-фрагменты. Весь остальной каталог не сканируется, поэтому предварительная миграция большого кэша быстрее первой полной пересборки.
• Паузы при миграции: Массовая операция не сканирует cache/silences. При первом использовании старый файл паузы не пережимается из Vorbis, а заново создаётся как чистая тишина той же длительности сразу в Opus и атомарно заменяет прежний файл; настройки пауз не теряются.
• Пропуск готовых файлов: При повторном запуске программа проверяет папку вывода и продолжает работу с того места, где остановилась.
• Раздельное управление (LRU / TTL): В "Настройках" -> "Обработка и Кэш" вы можете независимо включать ограничение по максимальному количеству записей (LRU) или по времени жизни в часах (TTL).
• Просмотр кэша: Двойной клик по строке открывает карточку с исходным и нормализованным текстом, спикером и ползунками тестов.
• Оптимизация кэша: Сканирует файлы в папке с текстами, собирает хэши для всех 3 режимов и удаляет фразы, которых больше нет в текущих TXT. Если в индексе нет явных Steps-вариантов, лишний выбор не показывается. Если варианты есть, приложение заранее показывает их статистику и предлагает безопасную политику: при выключенном Steps — сохранить все либо оставить только legacy; при включённом раздельном ключе — сохранить всё, legacy + текущий Steps (рекомендуется) или только текущий Steps. При общем legacy-ключе отдельно сообщается, что для одной фразы физически хранится лишь один вариант. Никакие варианты качества не удаляются молча.
• Обновление после оптимизации: Если таблица кэша уже загружалась, она автоматически получает итоговый снимок, поэтому удалённые записи не остаются «призраками». До первого нажатия «Обновить» вкладка остаётся ленивой.
• Блокировка от повреждения: В одном экземпляре Studio очистка, удаление, оптимизация, перекодирование и архивация не пересекаются с активным синтезом или друг с другом. Окно миграции немодальное, поэтому остальные вкладки доступны. Межпроцессной блокировки папки нет: не используйте один изменяемый кэш одновременно во втором экземпляре приложения.
• Архивирование: ZIP нельзя сохранить внутрь самой папки кэша. Опция «Очистить кэш после архивации» удаляет только аудиофрагменты, паузы и индекс; glossary.json сохраняется.
• Безопасный индекс: Имена файлов из sentence_cache.json не могут выйти за пределы cache/audio при прослушивании или удалении.

====================================================================
🔐 10.1. ИМПОРТ И ЭКСПОРТ КОНФИГУРАЦИИ
====================================================================
• Настройки переносятся независимыми группами. Если снять «Пути к папкам», чужие input/output/cache/export/import/direct пути не применяются.
• Группа «Нормализатор и применение глоссария» переносит глобальный профиль
  текста без API Token и папок. Отдельный JSON вкладки «Нормализатор» удобнее
  для обмена только этим профилем.
• Пользовательские аудиопрофили входят в группу «Вывод, аудиопрофили и Теги»;
  импорт всей группы заменяет список профилей, а отдельный JSON менеджера
  добавляет ровно один профиль с проверкой совпадающего имени.
• Значения вкладок прямого синтеза и импорта книг находятся в отдельной группе «Параметры вкладок».
• История файловых диалогов и размер шрифта остаются локальными и не попадают в переносимый профиль.
• API Token считается секретом и импортируется/экспортируется только по отдельной выключенной по умолчанию галочке.
• Импорт, сброс, глобальный профиль нормализатора и аудиопрофили сначала
  атомарно записываются и только затем меняют интерфейс. Ошибка диска не
  выдаётся за успех. При восстановлении из settings.json.bak основной файл
  лечится, а повреждённый primary не затирает исправную резервную копию.

====================================================================
⚡ 11. ЛИМИТЫ, КНОПКИ ОСТАНОВКИ И БЕЗОПАСНОСТЬ
====================================================================
Во вкладке "Настройки" -> "API и Лимиты" вы можете гибко управлять нагрузкой на сеть и процессор:

• API URL: По умолчанию используется официальный HTTPS-адрес Silero. Любой
  непустой адрес, введённый пользователем, сохраняется и отправляется без
  скрытой замены протокола или endpoint — это позволяет использовать свой
  сервер или прокси с ожидаемым поведением.
• Экспериментальный Steps (скорость/качество синтеза):
  - Выключено [по умолчанию]: ключ steps не отправляется; это совместимо со старыми конфигами, а значение выбирается сервером.
  - 4: Ещё быстрее, но для большинства голосов качество заметно ухудшается.
  - 8: Быстрее 16, обычно с небольшим снижением качества.
  - 12: Промежуточный вариант для экспериментов.
  - 16: Базовый ориентир EnhancedTTS по качеству; медленнее 8.
  - «Другое»: Целое от 1 до 72. Выше 16 приложение предупреждает о сомнительном приросте, при 32+ — о возможной нестабильности; больше 72 не отправляется, чтобы не получать HTTP 422.
  Сравнивайте пресеты на одном и том же коротком фрагменте и голосе.
• Сетевые лимиты API: Настройка частоты запросов ограничивает нагрузку на API.
  Один лимитер синхронизирует пакетный и Прямой синтез внутри текущего экземпляра
  Studio. Отдельные запущенные экземпляры не координируются между собой, поэтому
  их суммарная нагрузка и соответствие тарифному ограничению остаются
  ответственностью пользователя.
• Параллельных сборок FFmpeg (Аппаратный лимит CPU):
  - Значение [ 0 ]: Режим максимальной скорости без заданного программного
    ограничения; фактическая загрузка зависит от числа и размера файлов.
  - Значение [ 1 ]: Последовательная фоновая обработка — обычно удобнее на
    ноутбуках, когда важно снизить конкуренцию за CPU и диск.
  - Значения [ 2...N ]: Ручной лимит параллельных потоков кодирования.

• Кнопка "⏹ Стоп": Мягкая остановка. Дожидается завершения текущего запроса, сохраняет кэш и останавливает очередь.
• Кнопка "☠️ Принудительно": Экстренная остановка. Мгновенно разрывает сетевые сокеты HTTP-сессии, гарантируя немедленный останов потока и сброс накопившегося кэша на диск без создания "потоков-зомби".

====================================================================
📁 12. ОСОБЕННОСТИ РАБОТЫ НА РАЗНЫХ ОС
====================================================================
• macOS (.app): Из-за системных ограничений Apple (Gatekeeper) скомпилированное приложение автоматически работает через папку Документы (`~/Documents/SileroTTS_Studio/`).
  Проверенная и рекомендуемая связка для исходника — Python 3.13.x + Tcl/Tk 9.0.x (локально проверены Python 3.13.15 и Tk 9.0.4). Это не жёсткое требование для синтеза: Python 3.12/Tk 8.6 поддерживается, но после смены системной светлой/тёмной темы может потребоваться перезапуск. Официальная macOS `.app` собирается на актуальном Homebrew Python 3.13.x и проверяется с Tk 9; Windows/Linux поддерживают штатный Tk 8.6.
  Дочерние FFmpeg/FFprobe и системные утилиты запускаются через безопасный для Tk/CoreFoundation механизм `posix_spawn`, без предупреждений `The process has forked ... You MUST exec()`.
  Старт, восстановление и повторная активация окна переустанавливают текущую
  ttk-тему и инвалидируют системные виджеты. Клик по любой области главного окна
  подтверждает его локальный фокус; Activate/Deactivate не дают прогрессбарам и
  ползункам оставаться серыми. Уже активное окно не перерисовывает тему при
  каждом клике.
  Системные сообщения привязаны к главному окну и после закрытия возвращают локальный фокус прежнему полю через idle, не перехватывая фокус у другого приложения.
• Умный буфер обмена (Кроссплатформенный):
  - Физическая клавиша вставки и виртуальное событие Tk <<Paste>> используют
    один обработчик с защитой от повтора: один жест вставляет текст один раз,
    в том числе в редактор glossary.json.
  - На macOS обрабатывает ⌘C, ⌘V, ⌘X, ⌘A, ⌘Z при русской и английской раскладках и декодирует явные пути Finder (`file://`, `%20`, NFC), не изменяя обычный текст.
  - На Windows и Linux обрабатывает Ctrl+C/V/X/A/Z при русской и английской раскладках, снимает внешние кавычки у явных путей Windows 11 и декодирует `file://`-пути.
• Защита NullWriter: Глобальный перехватчик `sys.stdout/stderr` защищает Portable-версии на Windows, macOS и Linux от экстренных вылетов из-за вызовов `print()` в сторонних библиотеках.
• Атомарное выделение (macOS): Клик с зажатой клавишей ⌘ позволяет выделять и снимать выделение со строк в таблицах.
• Portable-режим: При запуске `.py` файла через консоль (на Mac, Windows, Linux) или `.exe` на Windows программа работает в полностью портативном режиме — все рабочие папки создаются строго рядом со скриптом.
• Умные пути (Smart Paths): Программа запоминает последние открытые папки для каждого поля индивидуально. Если поле пустое, диалог вежливо откроет папку текущего проекта. Вам больше не нужно каждый раз прокликивать путь от корня диска!
"""

        help_text = help_text.replace(
            "{APP_PUBLIC_VERSION}", APP_PUBLIC_VERSION, 1
        )
        self.help_text_widget.insert(tk.END, help_text)
        self.help_text_widget.config(state=tk.DISABLED)

    # --- Логика работы приложения ---
    def on_tab_change(self, event):
        """Не выполняет тяжёлый дисковый I/O при переключении вкладок."""
        # Вкладка «Кэш» намеренно ленивая: индекс читается и Treeview строится
        # только по кнопке «Обновить» (либо после явной операции с кэшем).
        pass

    def load_files(self):
        self.ensure_dirs(keys=("input_dir",))
        self.save_settings()
        old_items = self.tree.get_children()
        if old_items:
            self.tree.delete(*old_items)
        input_dir = Path(self.config["input_dir"]).expanduser()
        try:
            self.txt_files = sorted(input_dir.glob("*.txt"), key=lambda x: x.name)
        except (OSError, RuntimeError, ValueError) as exc:
            logging.error("Не удалось прочитать папку с текстами %s: %s", input_dir, exc)
            self.config["input_dir"] = DEFAULT_INPUT_DIR
            self.ensure_dirs(keys=("input_dir",))
            input_dir = Path(self.config["input_dir"])
            self.txt_files = sorted(input_dir.glob("*.txt"), key=lambda x: x.name)
        for f in self.txt_files:
            self.tree.insert("", tk.END, iid=f.name, values=("⏳ В очереди", f.name), tags=('queued',))
        
        # Сброс прогресс-баров
        self.lbl_total_pct.config(text=f"0/{len(self.txt_files)}")
        self.total_progress['value'] = 0
        self.file_progress['value'] = 0
        self.lbl_file_pct.config(text="0%")
        self._set_status_label(self.lbl_current_text, "Ожидание...", "info")

    def remove_selected_from_queue(self):
        selected = self.tree.selection()
        if not selected:
            return
        for item in selected:
            self.tree.delete(item)
        
        # Обновляем счетчик файлов
        total = len(self.tree.get_children())
        self.lbl_total_pct.config(text=f"0/{total}")

    def update_file_status(self, filename, status_code):
        if not self.tree.exists(filename):
            return
        status_map = {
            "processing": ("🔄 Синтез...", "processing"),
            "encoding": ("⚙️ Сборка аудиофайла...", "processing"), # <-- ВОЗВРАЩЕНО
            "success": ("✅ Готово", "success"),
            "warning": ("⚠️ С ошибками", "warning"),
            "empty": ("⚪ Нет текста", "warning"),
            "error": ("❌ Ошибка", "error")
        }
        text, tag = status_map.get(status_code, ("?", "queued"))
        old_values = self.tree.item(filename, "values")
        old_tags = self.tree.item(filename, "tags")
        if tuple(old_values) != (text, filename) or tuple(old_tags) != (tag,):
            self.tree.item(filename, values=(text, filename), tags=(tag,))
        
        # Только однопоточный синтез определяет «текущий» файл. Фоновые
        # FFmpeg-сборки могут завершаться в любом порядке и не должны менять
        # авто-прокрутку или указатель текущей строки.
        if status_code == "processing":
            self.current_processing_file = filename
            try:
                self.btn_go_current.config(state=tk.NORMAL)
            except (AttributeError, tk.TclError):
                pass
            
            if getattr(self, 'auto_scroll_var', None) and self.auto_scroll_var.get():
                self.scroll_to_current()

    def scroll_to_current(self):
        """Центрирует текущий обрабатываемый файл в таблице"""
        if not hasattr(self, 'current_processing_file') or not self.current_processing_file:
            return
            
        if self.tree.exists(self.current_processing_file):
            children = self.tree.get_children("")
            try:
                idx = children.index(self.current_processing_file)
                total = len(children)
                # Отнимаем 8 позиций, чтобы элемент оказался примерно посередине видимой области
                target_idx = max(0, idx - 8)
                self.tree.yview_moveto(target_idx / total)
            except ValueError:
                self.tree.see(self.current_processing_file)

    def _validate_book_output_profile(self):
        try:
            _select_book_audio_profile(
                self.config.get("output_format", "mp3"),
                sample_rate=self.config.get("output_sample_rate", "auto"),
                channels=self.config.get("output_channels", "auto"),
                bitrate=self.config.get("output_bitrate", "128k"),
            )
            return True
        except (TypeError, ValueError) as exc:
            self._show_error("Некорректный аудиопрофиль", str(exc))
            return False

    def _confirm_prepared_text_run(self):
        return self._ask_yes_no(
            "Текст уже подготовлен",
            "Для этого запуска Studio пропустит пользовательские RegEx, "
            "глоссарий, автообработку, ru-normalizr и финальную очистку.\n\n"
            "Используйте режим только для уже нормализованного текста после "
            "внешней расстановки ударений. Строки-разделители и текущие "
            "настройки пауз продолжат действовать. Продолжить?",
            icon="warning",
        )

    def _prepare_glossary_for_synthesis(self, *, prepared_text=False):
        """Гарантирует, что preview и новый TTSProcessor видят один JSON."""
        if (
            prepared_text
            or not _config_bool(
                self.config.get("glossary_enabled", True), default=True
            )
            or not hasattr(self, "txt_glossary")
        ):
            return True
        if not self._sync_glossary_editor_cache(prompt_if_dirty=True):
            self._show_warning(
                "Синтез не запущен",
                "Сначала сохраните либо перезагрузите глоссарий для текущей "
                "папки кэша.",
            )
            return False

        dirty = bool(getattr(self, "_glossary_dirty", False))
        try:
            dirty = dirty or bool(self.txt_glossary.edit_modified())
        except tk.TclError:
            pass
        if not dirty:
            return True
        if not self._ask_yes_no(
            "Глоссарий изменён",
            "Предпросмотр уже использует изменения из редактора, а синтез "
            "читает glossary.json с диска. Сохранить глоссарий перед запуском?",
            icon="warning",
        ):
            return False
        return self.save_glossary_ui(show_popup=False)
    
    def start_processing(self, only_selected=False):
        if self.batch_processor is not None:
            return
        if self._warn_if_cache_busy_for_synthesis():
            return

        if not self._validate_api_steps_ui():
            return
        self.save_settings()
        if not self._validate_book_output_profile():
            return
        if not self.config.get("api_token"):
            self._show_error("Ошибка", "Введите API Token во вкладке Настройки!")
            return
            
        # Определяем, какие файлы отправлять на синтез
        items_to_process = self.tree.selection() if only_selected else self.tree.get_children()
        
        if not items_to_process:
            self._show_info("Пусто", "Нет файлов для обработки. Выделите файлы или обновите список.")
            return
        prepared_text = bool(self.batch_prepared_text_var.get())
        if prepared_text and not self._confirm_prepared_text_run():
            return
        if not self._prepare_glossary_for_synthesis(
            prepared_text=prepared_text
        ):
            return

        # Блокируем/разблокируем кнопки
        self.btn_start_all.config(state=tk.DISABLED)
        self.btn_start_sel.config(state=tk.DISABLED)
        self.btn_refresh.config(state=tk.DISABLED)
        self.btn_remove_sel.config(state=tk.DISABLED)
        self.chk_batch_prepared_text.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_hard_stop.config(state=tk.NORMAL)
        
        # Сброс прогресс-баров перед стартом
        self.total_progress['value'] = 0
        self.file_progress['value'] = 0
        self.lbl_file_pct.config(text="0%")
        self.lbl_total_pct.config(text=f"0/{len(items_to_process)}")
        self._set_status_label(self.lbl_current_text, "Подготовка...", "text")
        self._batch_hard_stop_requested = False
        
        processing_config = self.config.copy()
        processing_config["text_is_prepared"] = prepared_text
        skip_existing = bool(self.settings_vars["skip_existing"].get())
        try:
            processor = self._create_synthesis_processor(processing_config)
        except Exception as exc:
            logging.error(f"Не удалось создать процессор синтеза: {exc}")
            self.btn_start_all.config(state=tk.NORMAL)
            self.btn_start_sel.config(state=tk.NORMAL)
            self.btn_refresh.config(state=tk.NORMAL)
            self.btn_remove_sel.config(state=tk.NORMAL)
            self.chk_batch_prepared_text.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_hard_stop.config(state=tk.DISABLED)
            self._set_status_label(self.lbl_current_text, "Ошибка подготовки.", "error")
            self._show_error("Ошибка", f"Не удалось подготовить синтез:\n{exc}")
            return
        self.batch_processor = processor
        self.processor = processor
        self.processing_thread = threading.Thread(
            target=self.process_queue,
            args=(processor, items_to_process, processing_config, skip_existing),
            daemon=True,
        )
        try:
            self.processing_thread.start()
        except Exception as exc:
            self.processing_thread = None
            self.batch_processor = None
            if self.processor is processor:
                self.processor = self.direct_processor
            self._release_shared_cache_if_idle()
            self.btn_start_all.config(state=tk.NORMAL)
            self.btn_start_sel.config(state=tk.NORMAL)
            self.btn_refresh.config(state=tk.NORMAL)
            self.btn_remove_sel.config(state=tk.NORMAL)
            self.chk_batch_prepared_text.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_hard_stop.config(state=tk.DISABLED)
            self._set_status_label(self.lbl_current_text, "Ошибка запуска.", "error")
            logging.exception("Не удалось запустить поток пакетного синтеза")
            self._show_error(
                "Ошибка", f"Не удалось запустить синтез:\n{exc}"
            )

    def stop_processing(self):
        if self.batch_processor:
            self.batch_processor.is_stopped = True
            # Снимок делается в worker, чтобы большой JSON не подвешивал Tk.
            processor = self.batch_processor
            threading.Thread(
                target=processor.flush_cache,
                daemon=True,
            ).start()
        self.btn_stop.config(state=tk.DISABLED)
        self._set_status_label(self.lbl_current_text, "Остановка (ожидание завершения текущего запроса)...", "warning")

    def hard_stop_processing(self):
        if self.batch_processor:
            processor = self.batch_processor
            processor.stop()  # Мгновенно рвёт сокеты; JSON пишет worker.
            threading.Thread(
                target=processor.flush_cache,
                daemon=True,
            ).start()

        # Не блокируем главный поток join(): рабочая очередь завершится сама и
        # пришлёт финальное обновление через _post_to_ui().
        self._batch_hard_stop_requested = True
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)

        self._set_status_label(
            self.lbl_current_text,
            "Принудительно остановлено. Кэш сохраняется...",
            "error",
        )
        self._show_warning(
            "Принудительная остановка",
            "Процесс прерван. Текущее предложение не завершено; "
            "накопленный кэш сохраняется в фоне.",
        )

    def finish_processing(self, processor, queue_failed=False):
        if processor is not self.batch_processor:
            return

        self.btn_start_all.config(state=tk.NORMAL)
        self.btn_start_sel.config(state=tk.NORMAL)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_remove_sel.config(state=tk.NORMAL)
        self.batch_prepared_text_var.set(False)
        self.chk_batch_prepared_text.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)
        
        if queue_failed:
            self._set_status_label(self.lbl_current_text, "Обработка завершилась с ошибкой.", "error")
            self._show_error("Ошибка", "Очередь синтеза завершилась с ошибкой. Подробности записаны в лог.")
        elif processor.is_stopped:
            if getattr(self, "_batch_hard_stop_requested", False):
                self._set_status_label(
                    self.lbl_current_text,
                    "Принудительно остановлено. Кэш сохранен.",
                    "error",
                )
            else:
                self._set_status_label(self.lbl_current_text, "Остановлено.", "warning")
                self._show_warning("Остановлено", "Обработка была прервана.")
        else:
            self._set_status_label(self.lbl_current_text, "Ожидание...", "info")
            self._show_info("Готово", "Все выбранные файлы обработаны!")

        self._batch_hard_stop_requested = False
        self.batch_processor = None
        self.processing_thread = None
        if self.processor is processor:
            self.processor = self.direct_processor
        self._release_shared_cache_if_idle()

    def process_queue(self, processor, items_to_process, processing_config, skip_existing):
        queue_failed = False
        try:
            total_files = len(items_to_process)
            input_dir = Path(processing_config["input_dir"])
            output_dir = Path(processing_config["output_dir"])
            output_format = processing_config["output_format"]
            
            for idx, item_id in enumerate(items_to_process):
                if processor.is_stopped: break
                
                filepath = input_dir / item_id
                out_filename = filepath.with_suffix(f'.{output_format}').name
                out_filepath = output_dir / out_filename
                
                if not filepath.exists():
                    processor._mark_output_status(out_filepath, "error")
                    self._post_to_ui(self.update_file_status, item_id, "error")
                    self._post_to_ui(self.update_total_ui, idx + 1, total_files)
                    continue
                
                # === ИЗМЕНЕНО: Читаем статусы прямо из RAM процессора ===
                if skip_existing and out_filepath.exists():
                    file_status = processor.processing_statuses_ram.get(str(out_filepath.resolve()), "success")
                    if file_status == "success":
                        self._post_to_ui(self.update_file_status, filepath.name, "success")
                        self._post_to_ui(self.update_total_ui, idx + 1, total_files)
                        continue 
                
                self._post_to_ui(self.update_file_status, filepath.name, "processing")
                # Общий счётчик показывает позицию именно однопоточного
                # синтеза. Фоновые completion-события FFmpeg его не трогают.
                self._post_to_ui(self.update_total_ui, idx + 1, total_files)

                last_progress_post = 0.0
                last_progress_value = None

                def on_progress(current, total, text):
                    nonlocal last_progress_post, last_progress_value
                    now = time.monotonic()
                    pct = int((current / total) * 100) if total > 0 else 0
                    # На полном кэше фразы проходят быстрее, чем Tk способен их
                    # рисовать. Финальный кадр всегда отправляется, промежуточные
                    # ограничены 10 обновлениями/с без влияния на обработку.
                    if current < total and (
                        pct == last_progress_value or now - last_progress_post < 0.1
                    ):
                        return
                    last_progress_post = now
                    last_progress_value = pct
                    self._post_to_ui(self.update_progress_ui, pct, text)

                def on_encoding(filename):
                    # Этот статус принадлежит завершившему синтез файлу. Общий
                    # счётчик остаётся привязан к однопоточному синтезу.
                    self._post_to_ui(self.update_file_status, filename, "encoding")
                    
                def on_complete(filename, status, audio=None):
                    self._post_to_ui(self.update_file_status, filename, status)

                try:
                    processor.process_text_file(
                        filepath,
                        progress_callback=on_progress,
                        completion_callback=on_complete,
                        encoding_callback=on_encoding,
                    )
                except Exception as e:
                    logging.error(f"Ошибка при обработке файла {filepath.name}: {e}")
                    processor._mark_output_status(out_filepath, "error")
                    self._post_to_ui(self.update_file_status, filepath.name, "error")

            for t in processor.active_threads:
                if t.is_alive():
                    t.join()
            processor.active_threads.clear()
            
        except Exception as e:
            queue_failed = True
            logging.error(f"Критическая ошибка в очереди синтеза: {e}")
        finally:
            # Единственный финальный снимок сохраняет новые записи и RAM-only
            # статистику cache hit даже после исключения в очереди.
            processor.flush_cache()
            # На диске нужны только warning/error для resume. При пустом RAM-
            # словаре старый файл удаляется, а пустой JSON не создаётся.
            processor._save_processing_statuses()
                    
            self._post_to_ui(self.finish_processing, processor, queue_failed)

    def _direct_processing_config(
        self, *, prepared_text, apply_direct_tags, output_dir
    ):
        """Снимок запуска direct с локальными, а не глобальными эффектами."""
        direct_config = self.config.copy()
        direct_config.update(
            {
                "text_is_prepared": bool(prepared_text),
                "apply_output_tags": bool(apply_direct_tags),
                "output_dir": str(output_dir),
                "fx_speed": self.dir_speed_var.get(),
                "fx_pitch": self.dir_pitch_var.get(),
                "fx_echo": self.dir_echo_var.get(),
                "fx_echo_delay": self.dir_echo_delay_var.get(),
                "fx_echo_decay": self.dir_echo_decay_var.get(),
            }
        )
        return direct_config

    def start_direct_processing(self):
        if self.direct_processor is not None:
            return
        if self._warn_if_cache_busy_for_synthesis():
            return

        if not self._validate_api_steps_ui():
            return
        self.save_settings()
        if not self._validate_book_output_profile():
            return
        if not self.config.get("api_token"):
            self._show_error("Ошибка", "Введите API Token во вкладке Настройки!")
            return

        text = self.direct_text.get(1.0, tk.END).strip()
        if not text:
            logging.warning("Прямой синтез пропущен: поле текста пусто.")
            self._set_status_label(
                self.lbl_direct_status,
                "Введите текст для синтеза.",
                "warning",
            )
            return
        prepared_text = bool(self.direct_prepared_text_var.get())
        if prepared_text and not self._confirm_prepared_text_run():
            return
        if not self._prepare_glossary_for_synthesis(
            prepared_text=prepared_text
        ):
            return

        filename = normalize_output_filename(
            self.settings_vars["direct_filename"].get(),
            self.config.get("output_format", "mp3"),
        )
        self.settings_vars["direct_filename"].set(filename)
        force = self.settings_vars["direct_force"].get()
        save_file = self.settings_vars["direct_save"].get()
        direct_output_dir = str(
            self.direct_output_dir_var.get()
        ).strip() or DEFAULT_DIRECT_OUTPUT_DIR
        if save_file:
            try:
                Path(direct_output_dir).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logging.error(f"Некорректная папка прямого синтеза: {exc}")
                self._show_error(
                    "Ошибка папки",
                    f"Не удалось создать папку прямого синтеза:\n{direct_output_dir}\n\n{exc}",
                )
                return
        direct_output_dir = str(Path(direct_output_dir).expanduser())
        self.direct_output_dir_var.set(direct_output_dir)
        settings_var = self.settings_vars.get("direct_output_dir")
        if settings_var is not None:
            settings_var.set(direct_output_dir)
        self.config["direct_output_dir"] = direct_output_dir
        autoplay = bool(self.settings_vars["direct_autoplay"].get())
        apply_direct_tags = _config_bool(
            self.settings_vars["direct_apply_tags"].get()
        )
        # Это намеренно настройка текущего запуска, а не глобальный параметр:
        # следующий запуск приложения снова безопасно стартует без книжных
        # тегов и обложки в direct_output.
        self.config.pop("direct_apply_tags", None)
        self._direct_hard_stop_requested = False

        self.btn_direct_start.config(state=tk.DISABLED)
        self.btn_direct_stop.config(state=tk.NORMAL)
        self.btn_direct_hard_stop.config(state=tk.NORMAL)
        self.chk_direct_prepared_text.config(state=tk.DISABLED)
        self._set_status_label(self.lbl_direct_status, "Обработка...", "text")

        # Пакетный синтез сохраняет глобальные эффекты и книжные теги. Прямой
        # синтез получает отдельный снимок локальной панели и opt-in тегов.
        direct_config = self._direct_processing_config(
            prepared_text=prepared_text,
            apply_direct_tags=apply_direct_tags,
            output_dir=direct_output_dir,
        )
        try:
            processor = self._create_synthesis_processor(direct_config)
        except Exception as exc:
            logging.error(f"Не удалось создать процессор прямого синтеза: {exc}")
            self.btn_direct_start.config(state=tk.NORMAL)
            self.btn_direct_stop.config(state=tk.DISABLED)
            self.btn_direct_hard_stop.config(state=tk.DISABLED)
            self.chk_direct_prepared_text.config(state=tk.NORMAL)
            self._set_status_label(self.lbl_direct_status, "Ошибка подготовки.", "error")
            self._show_error("Ошибка", f"Не удалось подготовить прямой синтез:\n{exc}")
            return
        self.direct_processor = processor
        self.processor = processor

        result = {"fname": filename, "status": None, "audio": None}

        def run_direct():
            last_progress_post = 0.0

            def on_progress(current, total, _text):
                nonlocal last_progress_post
                now = time.monotonic()
                if current < total and now - last_progress_post < 0.05:
                    return
                last_progress_post = now
                self._post_status_label(
                    self.lbl_direct_status,
                    f"Синтез: {current}/{total}...",
                    "info",
                )

            def on_complete(fname, status, audio=None):
                result.update(fname=fname, status=status, audio=audio)

            try:
                processor.process_raw_text(
                    text,
                    filename,
                    force_new=force,
                    save_to_disk=save_file,
                    progress_callback=on_progress,
                    completion_callback=on_complete,
                )
                # Дожидаемся завершения кодирования только в рабочем потоке.
                for thread in processor.active_threads:
                    if thread.is_alive():
                        thread.join()
            except Exception:
                logging.exception("Ошибка прямого синтеза")
                result["status"] = "error"
            finally:
                processor.active_threads.clear()
                processor.flush_cache()
                # Прямой синтез использует тот же RAM-словарь статусов, поэтому
                # он тоже должен сбросить warning/error после завершения.
                processor._save_processing_statuses()
                self._post_to_ui(
                    self._finish_direct_processing,
                    processor,
                    result.copy(),
                    save_file,
                    autoplay,
                )

        self.direct_thread = threading.Thread(target=run_direct, daemon=True)
        try:
            self.direct_thread.start()
        except Exception as exc:
            self.direct_thread = None
            self.direct_processor = None
            if self.processor is processor:
                self.processor = self.batch_processor
            self._release_shared_cache_if_idle()
            self.btn_direct_start.config(state=tk.NORMAL)
            self.btn_direct_stop.config(state=tk.DISABLED)
            self.btn_direct_hard_stop.config(state=tk.DISABLED)
            self.chk_direct_prepared_text.config(state=tk.NORMAL)
            self._set_status_label(self.lbl_direct_status, "Ошибка запуска.", "error")
            logging.exception("Не удалось запустить поток прямого синтеза")
            self._show_error(
                "Ошибка", f"Не удалось запустить прямой синтез:\n{exc}"
            )

    def stop_direct_processing(self):
        if self.direct_processor:
            self.direct_processor.is_stopped = True
            processor = self.direct_processor
            threading.Thread(
                target=processor.flush_cache,
                daemon=True,
            ).start()
        self.btn_direct_stop.config(state=tk.DISABLED)
        self._set_status_label(self.lbl_direct_status, "Остановка...", "warning")

    def hard_stop_direct_processing(self):
        if self.direct_processor:
            processor = self.direct_processor
            processor.stop()  # Мгновенно рвёт HTTP-сокет; JSON пишет worker.
            threading.Thread(
                target=processor.flush_cache,
                daemon=True,
            ).start()

        # Поток завершится самостоятельно; Tk mainloop остаётся отзывчивым.
        self._direct_hard_stop_requested = True
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.btn_direct_hard_stop.config(state=tk.DISABLED)
        self._set_status_label(
            self.lbl_direct_status,
            "Принудительно остановлено. Кэш сохраняется...",
            "error",
        )

    def _finish_direct_processing(self, processor, result, save_file, autoplay):
        if processor is not self.direct_processor:
            return

        self.btn_direct_start.config(state=tk.NORMAL)
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.btn_direct_hard_stop.config(state=tk.DISABLED)
        self.direct_prepared_text_var.set(False)
        self.chk_direct_prepared_text.config(state=tk.NORMAL)

        status = result.get("status")
        audio = result.get("audio")
        if processor.is_stopped:
            if self._direct_hard_stop_requested:
                text = "Принудительно остановлено. Кэш сохранен."
                status_kind = "error"
            else:
                text = "Остановлено."
                status_kind = "warning"
        elif status in ("success", "warning"):
            saved_path = audio or result.get("fname")
            text = f"Готово! Сохранено в {saved_path}" if save_file else "Готово! (Не сохранено)"
            status_kind = status
        elif status == "empty":
            text = "Нет поддерживаемого текста для синтеза."
            status_kind = "warning"
        else:
            text = "Ошибка синтеза. Подробности записаны в лог."
            status_kind = "error"

        # Даже при неожиданном статусе UI и файл журнала должны говорить одно
        # и то же. В штатной ветке ``empty`` подробности уже записывает ядро.
        if status not in ("success", "warning", "empty") and not processor.is_stopped:
            logging.error(
                "Прямой синтез %s завершён со статусом %r без аудиорезультата.",
                result.get("fname"),
                status,
            )

        self._set_status_label(self.lbl_direct_status, text, status_kind)
        self.last_direct_audio = audio if audio and os.path.exists(audio) else None
        self.last_direct_audio_has_effects = bool(save_file and self.last_direct_audio)
        self.btn_direct_play.config(state=tk.NORMAL if self.last_direct_audio else tk.DISABLED)

        if autoplay and self.last_direct_audio and status in ("success", "warning"):
            self.play_last_audio()

        self._direct_hard_stop_requested = False
        self.direct_processor = None
        self.direct_thread = None
        if self.processor is processor:
            self.processor = self.batch_processor
        self._release_shared_cache_if_idle()
    
    def update_progress_ui(self, pct, text):
        pct_text = f"{pct}%"
        if int(float(self.file_progress['value'])) != pct:
            self.file_progress['value'] = pct
        if self.lbl_file_pct.cget("text") != pct_text:
            self.lbl_file_pct.config(text=pct_text)
        
        display_text = text.replace('\n', ' ')
        if len(display_text) > 90:
            display_text = display_text[:87] + "..."
        else:
            display_text = display_text.ljust(90)
            
        label_text = f"Синтез: {display_text}"
        if self.lbl_current_text.cget("text") != label_text:
            self._set_status_label(self.lbl_current_text, label_text, "info")

    def update_total_ui(self, current, total):
        pct = int((current / total) * 100) if total > 0 else 0
        count_text = f"{current}/{total}"
        if int(float(self.total_progress['value'])) != pct:
            self.total_progress['value'] = pct
        if self.lbl_total_pct.cget("text") != count_text:
            self.lbl_total_pct.config(text=count_text)


    def show_critical_error(self, message):
        """Показывает всплывающее окно с ошибкой, пришедшей из фонового потока"""
        self._post_to_ui(self._show_error, "Критическая ошибка API", message)

    def export_config(self):
        self.update_config_from_ui()
        
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Экспорт настроек")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Выберите группы настроек для экспорта:").pack(pady=10, padx=20)
        
        vars_dict = {
            "api": (tk.BooleanVar(value=True), "API и Лимиты (без токена)"),
            "folders": (tk.BooleanVar(value=True), "Пути к папкам (включая прямой синтез)"),
            "pauses": (tk.BooleanVar(value=True), "Паузы и Разделители"),
            "cache": (tk.BooleanVar(value=True), "Настройки Кэша и Очистки"),
            "normalizer": (
                tk.BooleanVar(value=True),
                "Нормализатор и применение глоссария",
            ),
            "effects": (tk.BooleanVar(value=True), "Эффекты (Скорость, Тон, Эхо)"),
            "tags": (
                tk.BooleanVar(value=True),
                "Вывод, аудиопрофили и Теги ID3",
            ),
            "workspace": (
                tk.BooleanVar(value=True),
                "Параметры вкладок (прямой синтез и импорт книг)",
            ),
        }
        
        for key, (var, text) in vars_dict.items():
            ttk.Checkbutton(dialog, text=text, variable=var).pack(anchor=tk.W, padx=30, pady=2)

        include_api_token_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dialog,
            text="Включить API Token в JSON (секрет)",
            variable=include_api_token_var,
        ).pack(anchor=tk.W, padx=50, pady=(0, 4))
            
        def do_export():
            selected_groups = [
                group_key
                for group_key, (var, _) in vars_dict.items()
                if var.get()
            ]
            export_data = select_config_values(
                self.config,
                selected_groups,
                include_api_token=include_api_token_var.get(),
            )
                            
            dialog.destroy()
            if not export_data:
                self._show_warning("Пусто", "Ничего не выбрано для экспорта.")
                return
                
            filepath = filedialog.asksaveasfilename(
                initialdir=self._config_dialog_initial_dir(),
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                initialfile="my_tts_config.json",
            )
            if filepath:
                try:
                    self._write_json_atomic(filepath, export_data)
                    self._remember_dialog_directory("last_config_dir", filepath)
                    self._show_info("Успех", f"Настройки экспортированы в:\n{filepath}")
                except Exception as e:
                    self._show_error("Ошибка", f"Не удалось экспортировать конфиг:\n{e}")
                    
        ttk.Button(dialog, text="Экспортировать", command=do_export).pack(pady=15)
        self._center_popup(dialog, 490, 350, fit_screen=True)

    def export_glossary(self):
        content = self.txt_glossary.get(1.0, tk.END).strip()
        try:
            parsed = canonicalize_glossary_data(json.loads(content))
        except Exception as e:
            self._show_error("Ошибка JSON", f"Исправьте ошибки в редакторе перед экспортом:\n{e}")
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Экспорт Глоссария")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Что экспортировать?").pack(pady=10, padx=20)
        
        var_acc = tk.BooleanVar(value=True)
        var_trm = tk.BooleanVar(value=True)
        var_reg = tk.BooleanVar(value=True)
        
        ttk.Checkbutton(dialog, text="Ударения (+)", variable=var_acc).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="Замена терминов", variable=var_trm).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="RegEx правила", variable=var_reg).pack(anchor=tk.W, padx=30, pady=2)
        
        def do_export():
            export_data = {}
            if var_acc.get():
                export_data["accents_ignore_case"] = parsed.get("accents_ignore_case", [])
                export_data["accents_strict_case"] = parsed.get("accents_strict_case", [])
            if var_trm.get():
                export_data["terms_ignore_case"] = parsed.get("terms_ignore_case", {})
                export_data["terms_strict_case"] = parsed.get("terms_strict_case", {})
            if var_reg.get():
                export_data["regex_rules"] = parsed.get("regex_rules", [])
                
            dialog.destroy()
            if not export_data: return
                
            filepath = filedialog.asksaveasfilename(
                initialdir=self._glossary_dialog_initial_dir(),
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                initialfile="my_glossary.json",
            )
            if filepath:
                try:
                    self._write_json_atomic(filepath, export_data)
                    self._remember_dialog_directory("last_glossary_dir", filepath)
                    self._show_info("Успех", f"Глоссарий экспортирован в:\n{filepath}")
                except Exception as exc:
                    logging.error("Не удалось экспортировать глоссарий: %s", exc)
                    self._show_error(
                        "Ошибка", f"Не удалось экспортировать глоссарий:\n{exc}"
                    )
                
        ttk.Button(dialog, text="Экспортировать", command=do_export).pack(pady=15)
        self._center_popup(dialog, 300, 200)

    def import_glossary(self):
        filepath = filedialog.askopenfilename(
            initialdir=self._glossary_dialog_initial_dir(),
            filetypes=[("JSON files", "*.json")],
        )
        if not filepath: return
        self._remember_dialog_directory("last_glossary_dir", filepath)
        
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                imported_data = canonicalize_glossary_data(json.load(f))
        except Exception as e:
            self._show_error("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Импорт Глоссария")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(
            dialog,
            text=(
                "Что импортировать?\n"
                "Правила объединяются с текущим глоссарием."
            ),
        ).pack(pady=10, padx=20)
        
        var_acc = tk.BooleanVar(value=True)
        var_trm = tk.BooleanVar(value=True)
        var_reg = tk.BooleanVar(value=True)
        
        ttk.Checkbutton(dialog, text="Ударения (+)", variable=var_acc).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="Замена терминов", variable=var_trm).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="RegEx правила", variable=var_reg).pack(anchor=tk.W, padx=30, pady=2)

        ttk.Separator(dialog, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=8)
        ttk.Label(dialog, text="Если исходное правило уже существует:").pack(
            anchor=tk.W, padx=30
        )
        conflict_policy = tk.StringVar(value="keep")
        ttk.Radiobutton(
            dialog,
            text="Сохранить моё текущее правило (рекомендуется)",
            variable=conflict_policy,
            value="keep",
        ).pack(anchor=tk.W, padx=40, pady=2)
        ttk.Radiobutton(
            dialog,
            text="Заменить правилом из импортируемого файла",
            variable=conflict_policy,
            value="replace",
        ).pack(anchor=tk.W, padx=40, pady=2)
        
        def do_import():
            try:
                content = self.txt_glossary.get(1.0, tk.END).strip()
                current = canonicalize_glossary_data(
                    json.loads(content) if content else empty_glossary_data()
                )
                selected = []
                if var_acc.get(): selected.append("accents")
                if var_trm.get(): selected.append("terms")
                if var_reg.get(): selected.append("regex")
                if not selected:
                    self._show_warning("Пусто", "Не выбрана ни одна группа правил.")
                    return
                current, stats = merge_glossary_data(
                    current,
                    imported_data,
                    selected,
                    replace_existing=conflict_policy.get() == "replace",
                )
                shadowed_count = len(glossary_shadowed_accent_rules(current))
            except Exception as exc:
                self._show_error(
                    "Ошибка JSON", f"Не удалось объединить глоссарии:\n{exc}"
                )
                return
                        
            dialog.destroy()
            self._replace_glossary_editor_data(current)
            if not self.save_glossary_ui(show_popup=False):
                return
            summary = [f"Добавлено: {stats['added']}"]
            if stats["replaced"]:
                summary.append(f"заменено: {stats['replaced']}")
            if stats["kept"]:
                summary.append(f"сохранено текущих: {stats['kept']}")
            if stats["duplicates"]:
                summary.append(f"дубликатов пропущено: {stats['duplicates']}")
            if shadowed_count:
                summary.append(
                    f"ударений перекрыто совпадающими терминами: {shadowed_count}"
                )
            self._show_info(
                "Глоссарии объединены",
                "; ".join(summary) + ".",
            )
            
        ttk.Button(dialog, text="Импортировать (Добавить)", command=do_import).pack(pady=15)
        self._center_popup(dialog, 440, 360, fit_screen=True)

if __name__ == "__main__":
    root = tk.Tk()
    app = TTSApp(root)
    root.mainloop()
