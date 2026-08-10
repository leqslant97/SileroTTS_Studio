"""
Silero TTS Studio - Профессиональная среда для для генерации аудиокниг, подкастов и озвучки текста, которая использует API от Silero.
Поддерживает кэширование, постобработку (FFmpeg), работу с глоссариями,
импорт электронных книг (EPUB, FB2, DOCX) и пакетную сборку аудиофайлов.
"""

import os
import re
import io
import json
import time
import uuid
import base64
import hashlib
import threading
import logging
import requests
import shutil
import atexit
import tempfile
import platform
import subprocess
import sys
import urllib.parse
import unicodedata

# === ГЛОБАЛЬНЫЙ ПАТЧ ДЛЯ СКРЫТИЯ КОНСОЛИ НА WINDOWS ===
# Запрещает pydub и ffmpeg моргать черными окнами
if platform.system() == "Windows":
    _original_popen = subprocess.Popen
    def _patched_popen(*args, **kwargs):
        if 'creationflags' not in kwargs:
            kwargs['creationflags'] = 0x08000000  # Флаг CREATE_NO_WINDOW
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _patched_popen
# --------------------------

is_frozen_mac = (sys.platform == "darwin") and getattr(sys, 'frozen', False)

# === ЗАЩИТА ОТ СБРОСА В PYINSTALLER --windowed ===
class NullWriter:
    def write(self, *args, **kwargs): pass
    def flush(self, *args, **kwargs): pass
    
if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()
# ------------------------------
      
# Попытка импорта библиотек для работы с электронными книгами
try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import docx
    IMPORT_LIBS_AVAILABLE = True
except ImportError:
    IMPORT_LIBS_AVAILABLE = False
    logging.warning("Библиотеки для импорта книг (EbookLib, bs4, docx) не установлены.")

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from collections import deque
from razdel import sentenize

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
DEFAULT_CACHE_DIR = str(BASE_DIR / "cache_audio")

# Создаем служебную папку и переходим в корень проекта
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(BASE_DIR) # Рабочей директорией делаем BASE_DIR!

SETTINGS_FILE = APP_DATA_DIR / "settings.json"
LOG_FILE = APP_DATA_DIR / "tts_processor.log"

SAFE_LIMIT = 30000 # Лимит символов для авто-разрыва в режиме full

# === ИМПОРТ PYDUB И НАСТРОЙКА ПУТЕЙ FFMPEG ===
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

def get_ffmpeg_path():
    """Сначала ищет FFmpeg зашитый внутри пакета, затем в системе"""
    if getattr(sys, 'frozen', False):
        bundle_dir = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        bin_name = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
        local_bin = bundle_dir / bin_name
        if local_bin.exists():
            return str(local_bin)
            
    sys_name = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    return shutil.which(sys_name) or ("/opt/homebrew/bin/ffmpeg" if sys.platform == "darwin" and os.path.exists("/opt/homebrew/bin/ffmpeg") else sys_name)

def get_ffprobe_path():
    """Сначала ищет FFprobe зашитый внутри пакета, затем в системе"""
    if getattr(sys, 'frozen', False):
        bundle_dir = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        bin_name = "ffprobe.exe" if platform.system() == "Windows" else "ffprobe"
        local_bin = bundle_dir / bin_name
        if local_bin.exists():
            return str(local_bin)
            
    sys_name = "ffprobe.exe" if platform.system() == "Windows" else "ffprobe"
    return shutil.which(sys_name) or ("/opt/homebrew/bin/ffprobe" if sys.platform == "darwin" and os.path.exists("/opt/homebrew/bin/ffprobe") else sys_name)

# Принудительно скармливаем пути библиотеке Pydub
AudioSegment.converter = get_ffmpeg_path()
AudioSegment.ffprobe = get_ffprobe_path()

# Если запущены под macOS из Finder (.app) — добавляем Homebrew в PATH для subprocess
if is_frozen_mac:
    extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    os.environ["PATH"] = ":".join(extra_paths) + ":" + os.environ.get("PATH", "")
# -------------------------------------------------------------------------

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
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
# =========================================================
try:
    from ru_normalizr import NormalizeOptions, Normalizer
    normalizer = Normalizer(NormalizeOptions.tts())
    logging.info("ru-normalizr загружен.")
except ImportError:
    logging.warning("ru-normalizr не найден. Нормализация пропущена.")
    normalizer = None

# ================= КОНФИГУРАЦИЯ ПО УМОЛЧАНИЮ =================
DEFAULT_CONFIG = {
    "api_token": "",
    "api_url": "http://iq3g.silero.ai/enhanced_voice",
    "speaker": "arthas",
    "input_dir": DEFAULT_INPUT_DIR,  
    "output_dir": DEFAULT_OUTPUT_DIR,
    "cache_dir": DEFAULT_CACHE_DIR,
    "export_dir": "",
    "last_browse_dir": "",
    
    "output_format": "mp3",
    "output_bitrate": "128k",
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

# ================= ОБРАБОТКА ЭФФЕКТОВ (FFmpeg) =================
class AudioEffects:
    """Класс для применения аудиоэффектов (скорость, тон, эхо) через системный FFmpeg."""
    
    @staticmethod
    def apply_effects(audio_segment, speed=1.0, pitch=1.0, echo=False, echo_delay=300, echo_decay=0.3):
        if speed == 1.0 and pitch == 1.0 and not echo:
            return audio_segment

        filters = []
        if pitch != 1.0:
            new_sr = int(48000 * pitch)
            filters.append(f"asetrate={new_sr}")
            filters.append(f"atempo={1/pitch}")
        if speed != 1.0:
            filters.append(f"atempo={speed}")
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

            audio_segment.export(in_path, format="wav")
            command = [get_ffmpeg_path(), "-y", "-i", in_path, "-af", filter_str, out_path]
            
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo, check=True)
            return AudioSegment.from_file(out_path, format="wav")
        except Exception as e:
            logging.error(f"Ошибка применения эффектов FFmpeg: {e}")
            return audio_segment
        finally:
            # Гарантированное удаление файлов при любом исходе
            if in_path and os.path.exists(in_path):
                try: os.remove(in_path)
                except: pass
            if out_path and os.path.exists(out_path):
                try: os.remove(out_path)
                except: pass
# ================= ЯДРО СИНТЕЗА =================
class RateLimiter:
    """Контроллер частоты запросов к API."""
    def __init__(self, max_requests, time_window):
        self.max_requests = max_requests
        self.time_window = time_window
        self.timestamps = deque()

    def wait(self):
        now = time.time()
        while self.timestamps and now - self.timestamps[0] > self.time_window:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_requests:
            sleep_time = self.time_window - (now - self.timestamps[0])
            if sleep_time > 0: time.sleep(sleep_time)
        self.timestamps.append(time.time())

class TTSProcessor:
    """Главный процессор для обработки текста и взаимодействия с API Silero."""
    def __init__(self, config, shared_rate_limiter=None, error_callback=None):
        self.cfg = config
        self.error_callback = error_callback
        
        # Используем общий лимитер, если передан
        self.rate_limiter = shared_rate_limiter or RateLimiter(int(config["api_max_requests"]), float(config["api_time_window"]))
        
        Path(self.cfg["input_dir"]).mkdir(parents=True, exist_ok=True)
        Path(self.cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
        self.cache_dir = Path(self.cfg["cache_dir"])
        self.cache_audio_dir = self.cache_dir / "audio"
        self.cache_audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_index_path = self.cache_dir / "sentence_cache.json"
        self.glossary_path = self.cache_dir / "glossary.json"

        self.session = requests.Session()

        max_enc = int(self.cfg.get("max_parallel_encodes", 0))
        self.encode_semaphore = threading.Semaphore(max_enc) if max_enc > 0 else None
        
        self.cache = self._load_cache()
        self.unsaved_cache_items = 0
        self.active_threads = []
        
        self.glossary_ignore_case = {}
        self.glossary_strict_case = {}
        self.glossary_regex = []
        self.load_glossary_file()
        
        self.is_stopped = False
        self.cache_lock = threading.Lock()
        
        raw_seps = str(self.cfg.get("separator_symbols", ""))
        if "," in raw_seps and "\n" not in raw_seps: raw_seps = raw_seps.replace(",", "\n")
        self.separators = [s.strip() for s in raw_seps.split("\n") if s.strip()]

        self.processing_statuses_ram = {}
        status_file = APP_DATA_DIR / "processing_statuses.json"
        if status_file.exists():
            try:
                with open(status_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # === ИСПРАВЛЕНИЕ: Очищаем старый мусор, загружаем ТОЛЬКО ошибки ===
                    self.processing_statuses_ram = {k: v for k, v in loaded.items() if v != "success"}
            except: pass

    def _load_cache(self):
        if self.cache_index_path.exists():
            bak_file = self.cache_index_path.with_suffix(".json.bak")
            
            def try_parse(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if isinstance(v, str): 
                            data[k] = {"file_name": Path(v).name, "original_text": "migrated", "normalized_text": "migrated", "speaker": self.cfg["speaker"], "created_at": time.time(), "last_accessed": time.time(), "usage_count": 1}
                        elif "file_path" in v:
                            v["file_name"] = Path(v["file_path"]).name
                            del v["file_path"]
                    return data

            try:
                return try_parse(self.cache_index_path)
            except Exception as e:
                logging.error(f"Файл кэша {self.cache_index_path.name} поврежден: {e}")
                if bak_file.exists():
                    try:
                        logging.info("Загрузка резервной копии кэша .bak...")
                        return try_parse(bak_file)
                    except Exception as e_bak:
                        logging.error(f"Не удалось прочитать .bak кэш: {e_bak}")
        return {}

    def _delete_cache_entry(self, text_hash):
        if text_hash in self.cache:
            filename = self.cache[text_hash].get("file_name")
            if filename:
                filepath = self.cache_audio_dir / filename
                if filepath.exists(): os.remove(filepath)
            del self.cache[text_hash]

    def stop(self):
        """Мгновенно рвет сетевые соединения и сохраняет кэш"""
        self.is_stopped = True
        try:
            self.session.close() # Разрывает висящие HTTP-запросы за 1 миллисекунду
        except: pass
        self._save_cache()
    
    def _enforce_cache_limits(self):
        now = time.time()
        
        if self.cfg.get("enable_cache_ttl", False):
            ttl_sec = float(self.cfg.get("cache_ttl_hours", 720.0)) * 3600
            keys_to_delete = [k for k, v in self.cache.items() if now - v["last_accessed"] > ttl_sec]
            for k in keys_to_delete:
                self._delete_cache_entry(k)
                self.unsaved_cache_items += 1
                
        if self.cfg.get("enable_cache_lru", False):
            max_entries = int(self.cfg.get("cache_max_entries", 10000))
            if len(self.cache) > max_entries:
                sorted_keys = sorted(self.cache.keys(), key=lambda k: self.cache[k]["last_accessed"])
                excess = len(self.cache) - max_entries
                for k in sorted_keys[:excess]:
                    self._delete_cache_entry(k)
                    self.unsaved_cache_items += 1

    def _save_cache(self):
        if self.unsaved_cache_items > 0:
            with self.cache_lock:
                self._enforce_cache_limits()
                temp_file = self.cache_index_path.with_suffix(".json.tmp")
                bak_file = self.cache_index_path.with_suffix(".json.bak")
                try:
                    # 1. Записываем во временный файл
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(self.cache, f, ensure_ascii=False, indent=4)
                    
                    # 2. Создаем резервную копию .bak
                    if self.cache_index_path.exists():
                        shutil.copy2(self.cache_index_path, bak_file)
                        
                    # 3. Атомарно заменяем рабочий файл (мгновенная операция на уровне ОС)
                    os.replace(temp_file, self.cache_index_path)
                    self.unsaved_cache_items = 0
                except Exception as e:
                    logging.error(f"Ошибка при атомарном сохранении кэша: {e}")
                    if temp_file.exists():
                        try: os.remove(temp_file)
                        except: pass

    def apply_regex_rules(self, text):
        for rule in self.glossary_regex:
            if not isinstance(rule, dict): continue
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

    def load_glossary_file(self):
        if not os.path.exists(self.glossary_path): return
        try:
            with open(self.glossary_path, 'r', encoding='utf-8') as f: data = json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения файла глоссария: {e}")
            return

        # Очищаем словари и списки перед загрузкой (защита от дубликатов при перевызове)
        self.glossary_ignore_case = {}
        self.glossary_strict_case = {}
        self.compiled_strict_case = []
        self.compiled_ignore_case = []

        for w in data.get("accents_ignore_case", []): 
            if isinstance(w, str) and w.strip():
                self.glossary_ignore_case[w.replace("+", "").lower()] = w
                
        for w in data.get("accents_strict_case", []): 
            if isinstance(w, str) and w.strip():
                self.glossary_strict_case[w.replace("+", "")] = w
                
        for k, v in data.get("terms_ignore_case", {}).items(): 
            if isinstance(k, str) and k.strip():
                self.glossary_ignore_case[k.lower()] = v
                
        for k, v in data.get("terms_strict_case", {}).items(): 
            if isinstance(k, str) and k.strip():
                self.glossary_strict_case[k] = v

        # Предкомпиляция RegEx
        for original, replacement in self.glossary_strict_case.items():
            pattern = re.compile(r'(?<![а-яА-Яa-zA-Z0-9_ёЁ])' + re.escape(original) + r'(?![а-яА-Яa-zA-Z0-9_ёЁ])')
            self.compiled_strict_case.append((pattern, replacement))

        for original_lower, replacement in self.glossary_ignore_case.items():
            pattern = re.compile(r'(?<![а-яА-Яa-zA-Z0-9_ёЁ])' + re.escape(original_lower) + r'(?![а-яА-Яa-zA-Z0-9_ёЁ])', re.IGNORECASE)
            self.compiled_ignore_case.append((pattern, replacement))
                
        self.glossary_regex = data.get("regex_rules", [])

    def apply_glossary(self, text):
        # Использование предкомпилированных регулярных выражений
        for pattern, replacement in getattr(self, 'compiled_strict_case', []):
            text = pattern.sub(replacement, text)
            
        for pattern, replacement in getattr(self, 'compiled_ignore_case', []):
            def match_func(m):
                w = m.group(0)
                if w.isupper(): return replacement.upper()
                elif w.istitle(): return replacement[0].upper() + replacement[1:] if replacement else ""
                return replacement
            text = pattern.sub(match_func, text)
        return text

    def process_sentence_text(self, text):
        """Полный цикл обработки одного предложения (сохраняет чистый исходник отдельно)"""
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
        if self.cfg.get("auto_abbreviations", True):
            def _repl_abbr(match):
                letters = [c for c in match.group(0) if c != '.']
                return "-".join(letters)
            text = re.sub(r'\b(?:[а-яА-Яa-zA-ZёЁ]\.){2,}', _repl_abbr, text)

        # 4. Авто-сокращения (г. -> г, ур. -> ур)
        if self.cfg.get("auto_short_words", True):
            text = re.sub(r'\b([а-яА-ЯёЁa-zA-Z]{1,3})\.', r'\1', text)

        # 5. Глоссарий терминов и ударений
        text = self.apply_glossary(text)

        # 6. Нормализация (числа в слова)
        if normalizer:
            try:
                text = normalizer.normalize(text)
            except Exception as e:
                logging.warning(f"Ошибка нормализации для фразы '{text[:30]}...': {e}")

        # 7. Очистка спецсимволов (не трогаем +, так как он уже для ударений)
        text = re.sub(r'[*|\\/_\#~^()\[\]{}<>"\'«»„“”]', '', text)
        text = re.sub(r'^[ \t]*[-–—−]+\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # 8. Восстановление или добавление финальной пунктуации
        # Если в конце нет знака препинания, который дает паузу (. ! ? … : ;)
        if text and not re.search(r'[.!?…:;]$', text):
            if term_punct:
                # Возвращаем оригинальную пунктуацию (очищенную от кавычек)
                clean_punct = re.sub(r'["\'»”]', '', term_punct)
                text += clean_punct if clean_punct else "."
            else:
                # Если пунктуации не было вообще (например, заголовок), ставим точку
                text += "."

        return text

    def get_hash(self, text):
        return hashlib.md5(f"{text}_{self.cfg['speaker']}".encode('utf-8')).hexdigest()

    def _get_silence_file(self, duration_ms):
        """Генерирует OGG-файл тишины (48000 Hz, Mono, libvorbis), на 100% совпадающий с форматом Silero API"""
        duration_ms = max(1, int(round(duration_ms)))
        silence_dir = self.cache_dir / "silences"
        silence_dir.mkdir(parents=True, exist_ok=True)
        filepath = silence_dir / f"silence_{duration_ms}ms.ogg"

        if not filepath.exists():
            # 48000 Гц + 1 канал (моно) + OGG libvorbis = 100% совпадение со структурой аудио от Silero!
            silence_seg = AudioSegment.silent(duration=duration_ms, frame_rate=48000).set_channels(1)
            silence_seg.export(filepath, format="ogg", codec="libvorbis")

        return filepath

    def _run_ffmpeg_concat(self, audio_files):
        """Склеивает список аудиофайлов через FFmpeg (для Прямого синтеза)"""
        temp_dir = APP_DATA_DIR / "temp"
        temp_dir.mkdir(exist_ok=True)
        
        list_path = temp_dir / f"concat_{uuid.uuid4().hex}.txt"
        temp_out = temp_dir / f"out_{uuid.uuid4().hex}.ogg"
        
        with open(list_path, 'w', encoding='utf-8') as f:
            for p in audio_files:
                safe_path = p.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        
        # Перекодируем в чистый OGG через -c:a libvorbis
        cmd = [get_ffmpeg_path(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c:a", "libvorbis", str(temp_out)]
        
        startupinfo = None
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo)
        
        try: os.remove(list_path)
        except: pass
        
        return temp_out if temp_out.exists() else None

    def synthesize_sentence(self, normalized_text, original_text, force_new=False):
        text_hash = self.get_hash(normalized_text)
        file_name = f"{text_hash}.ogg"
        cache_file = self.cache_audio_dir / file_name
        
        with self.cache_lock:
            if force_new and text_hash in self.cache:
                self._delete_cache_entry(text_hash)
                self.unsaved_cache_items += 1
            
            if self.cfg.get("use_cache", True) and text_hash in self.cache and cache_file.exists():
                cache_info = self.cache[text_hash]
                cache_info["last_accessed"] = time.time()
                cache_info["usage_count"] += 1
                return cache_file, True

        payload = {
            'api_token': self.cfg["api_token"], 'text': normalized_text,
            'sample_rate': 48000, 'speaker': self.cfg["speaker"],
            'remote_id': 'python_script', 'format': 'ogg'
        }
        
        for attempt in range(1, int(self.cfg["max_retries"]) + 1):
            if self.is_stopped: return None, False
            self.rate_limiter.wait()
            try:
                r = self.session.post(self.cfg["api_url"], json=payload, timeout=30)
                r.raise_for_status()
                audio_data = base64.b64decode(r.json()['results'][0]['audio'])
                
                temp_ogg = self.cache_audio_dir / f"temp_{uuid.uuid4().hex}.ogg"
                with open(temp_ogg, "wb") as f: f.write(audio_data)
                    
                if self.cfg.get("auto_trim_silence", True):
                    audio_segment = AudioSegment.from_file(temp_ogg, format="ogg")
                    nonsilent_ranges = detect_nonsilent(audio_segment, min_silence_len=50, silence_thresh=float(self.cfg["silence_threshold"]))
                    if nonsilent_ranges:
                        start_trim = max(0, nonsilent_ranges[0][0] - 20)
                        end_trim = min(len(audio_segment), nonsilent_ranges[-1][1] + 20)
                        audio_segment[start_trim:end_trim].export(cache_file, format="ogg")
                    else:
                        shutil.move(temp_ogg, cache_file)
                else:
                    shutil.move(temp_ogg, cache_file)
                    
                if temp_ogg.exists(): os.remove(temp_ogg)
                
                if self.cfg.get("use_cache", True):
                    self.cache[text_hash] = {
                        "file_name": file_name, "original_text": original_text,
                        "normalized_text": normalized_text, "speaker": self.cfg["speaker"],
                        "created_at": time.time(), "last_accessed": time.time(), "usage_count": 1
                    }
                    self.unsaved_cache_items += 1
                    if self.unsaved_cache_items >= int(self.cfg["cache_save_frequency"]):
                        self._save_cache()
                return cache_file, True
                
            except requests.exceptions.HTTPError as e:
                if r.status_code == 422:
                    try:
                        detail = r.json().get("detail", "")
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
                    except: pass
                logging.warning(f"HTTP Ошибка (попытка {attempt}): {e}")
                if attempt < int(self.cfg["max_retries"]): time.sleep(2)
                else: return self._get_silence_file(int(self.cfg["pause_sentence"])), False
            except Exception as e:
                logging.error(f"Ошибка сети (попытка {attempt}): {e}")
                if attempt < int(self.cfg["max_retries"]): time.sleep(2)
                else: return self._get_silence_file(int(self.cfg["pause_sentence"])), False

    def process_raw_text(self, raw_text, out_filename, force_new=False, save_to_disk=True, progress_callback=None, completion_callback=None):
        raw_text = re.sub(r'[«»“”„]', '"', raw_text)
        raw_text = re.sub(r'^[ \t]*[-–—−]+\s*', '— ', raw_text, flags=re.MULTILINE)
        raw_text = self.apply_regex_rules(raw_text)
        
        separator_token = "___SEPARATOR_TOKEN___"
        for sep in self.separators:
            pattern = r'^[ \t]*' + re.escape(sep) + r'[ \t]*$'
            raw_text = re.sub(pattern, f"\n{separator_token}\n", raw_text, flags=re.MULTILINE)
        
        paragraphs = [p.strip() for p in raw_text.split('\n') if p.strip()]
        
        tasks = []
        prev_ended_with_colon = False
        current_full_text_clean, current_full_text_raw = [], []

        for p_idx, para in enumerate(paragraphs):
            if para == separator_token:
                if self.cfg["synthesis_mode"] == "full" and current_full_text_clean:
                    tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))
                    current_full_text_clean, current_full_text_raw = [], []
                tasks.append(("__SILENCE__", int(self.cfg["pause_separator"]), 0))
                prev_ended_with_colon = False
                continue

            pause_before = int(self.cfg["pause_paragraph"])
            if para.startswith(('—', '"')): pause_before = max(pause_before, int(self.cfg["pause_speech"]))
            if prev_ended_with_colon: pause_before = max(pause_before, int(self.cfg["pause_colon"]))

            sentences = [s.text for s in sentenize(para)]
            prev_ended_with_colon = sentences[-1].strip().endswith(':') if sentences else False

            processed_sentences = []
            for sent_raw in sentences:
                sent_clean = self.process_sentence_text(sent_raw)
                if not re.search(r'[а-яА-ЯёЁa-zA-Z0-9]', sent_clean): continue
                processed_sentences.append((sent_raw, sent_clean))

            if not processed_sentences: continue

            if self.cfg["synthesis_mode"] == "sentence":
                for i, (s_raw, s_clean) in enumerate(processed_sentences):
                    pb = pause_before if i == 0 and p_idx > 0 else (int(self.cfg["pause_sentence"]) if i > 0 else 0)
                    tasks.append((s_clean, s_raw, pb))
            elif self.cfg["synthesis_mode"] == "paragraph":
                para_raw = " ".join([p[0] for p in processed_sentences])
                para_clean = " ".join([p[1] for p in processed_sentences])
                pb = pause_before if p_idx > 0 else 0
                tasks.append((para_clean, para_raw, pb))
            elif self.cfg["synthesis_mode"] == "full":
                para_raw = " ".join([p[0] for p in processed_sentences])
                para_clean = " ".join([p[1] for p in processed_sentences])
                
                current_len = sum(len(t) for t in current_full_text_clean) + len(current_full_text_clean)
                if current_full_text_clean and (current_len + len(para_clean) > SAFE_LIMIT):
                    tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))
                    tasks.append(("__SILENCE__", int(self.cfg["pause_paragraph"]), 0))
                    current_full_text_clean, current_full_text_raw = [], []
                    
                current_full_text_raw.append(para_raw)
                current_full_text_clean.append(para_clean)

        if self.cfg["synthesis_mode"] == "full" and current_full_text_clean:
            tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))

        audio_files = []
        if int(self.cfg["pause_file_start"]) > 0:
            f = self._get_silence_file(int(self.cfg["pause_file_start"]))
            if f: audio_files.append(f)

        file_has_errors = False
        total_tasks = len(tasks)

        for i, task in enumerate(tasks):
            if self.is_stopped:
                self._save_cache()
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
                filepath, success = self.synthesize_sentence(clean_text, raw_text_or_duration, force_new)
                if filepath:
                    audio_files.append(filepath)
                if not success: file_has_errors = True

        if int(self.cfg["pause_file_end"]) > 0:
            f = self._get_silence_file(int(self.cfg["pause_file_end"]))
            if f: audio_files.append(f)

        self._save_cache()

        if not audio_files:
            if completion_callback: completion_callback(out_filename, "error", None)
            return

        if save_to_disk:
            out_filepath = Path(self.cfg["output_dir"]) / out_filename
            t = threading.Thread(target=self._merge_save_and_notify, args=(audio_files, out_filepath, out_filename, file_has_errors, completion_callback))
            self.active_threads.append(t)
            t.start()
        else:
            temp_out = self._run_ffmpeg_concat(audio_files)
            if completion_callback:
                completion_callback(out_filename, "warning" if file_has_errors else "success", temp_out)

    def _merge_save_and_notify(self, audio_files, out_filepath, original_filename, has_errors, callback):
        def _encode():
            # ОПТИМИЗАЦИЯ 1: Изолированная временная папка для concat-файла
            temp_dir = APP_DATA_DIR / "temp"
            temp_dir.mkdir(exist_ok=True)
            
            # Генерируем абсолютно уникальное имя файла для каждого потока
            list_path = temp_dir / f"concat_{uuid.uuid4().hex}.txt"
            
            try:
                with open(list_path, 'w', encoding='utf-8') as f:
                    for p in audio_files:
                        safe_path = p.resolve().as_posix().replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")

                cmd = [get_ffmpeg_path(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
                
                has_cover = False
                cover_path = self.cfg.get("tag_cover", "")
                if cover_path and os.path.exists(cover_path):
                    cmd.extend(["-i", cover_path])
                    has_cover = True
    
                sp = float(self.cfg.get("fx_speed", 1.0))
                pt = float(self.cfg.get("fx_pitch", 1.0))
                ec = bool(self.cfg.get("fx_echo", False))
                ed = int(self.cfg.get("fx_echo_delay", 300))
                ey = float(self.cfg.get("fx_echo_decay", 0.3))
    
                filters = []
                if pt != 1.0:
                    filters.append(f"asetrate={int(48000 * pt)}")
                    filters.append(f"atempo={1/pt}")
                if sp != 1.0:
                    filters.append(f"atempo={sp}")
                if ec:
                    filters.append(f"aecho=0.8:0.8:{int(ed)}:{float(ey)}")
    
                if filters:
                    cmd.extend(["-af", ",".join(filters)])
    
                fmt = self.cfg["output_format"].lower()
                if fmt == "mp3":
                    cmd.extend(["-c:a", "libmp3lame", "-b:a", self.cfg["output_bitrate"]])
                    if has_cover:
                        # ИСПРАВЛЕНИЕ ОБЛОЖКИ ДЛЯ WINDOWS
                        cmd.extend(["-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-id3v2_version", "3", "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"])
                elif fmt == "ogg":
                    cmd.extend(["-c:a", "libvorbis"])
                    if has_cover:
                        cmd.extend(["-map", "0:a", "-map", "1:v", "-disposition:v", "attached_pic"])
                elif fmt == "wav":
                    cmd.extend(["-c:a", "pcm_s16le"])
    
                base_name = out_filepath.stem
                def _apply_tag_template(tmpl_key):
                    val = str(self.cfg.get(tmpl_key, ""))
                    if not val: return ""
                    val = val.replace("{filename}", base_name)
                    val = val.replace("{name}", base_name)
                    val = val.replace("{title}", base_name)
                    return val.strip()
    
                title = _apply_tag_template("tag_title")
                if title: cmd.extend(["-metadata", f"title={title}"])
                
                artist = _apply_tag_template("tag_artist")
                if artist: cmd.extend(["-metadata", f"artist={artist}"])
    
                album_artist = _apply_tag_template("tag_album_artist")
                if album_artist: cmd.extend(["-metadata", f"album_artist={album_artist}"])
                
                album = _apply_tag_template("tag_album")
                if album: cmd.extend(["-metadata", f"album={album}"])
    
                genre = _apply_tag_template("tag_genre")
                if genre: cmd.extend(["-metadata", f"genre={genre}"])
                
                composer = _apply_tag_template("tag_composer")
                if composer: cmd.extend(["-metadata", f"composer={composer}"])
                
                year = _apply_tag_template("tag_year")
                if year: cmd.extend(["-metadata", f"date={year}"])
    
                cmd.append(str(out_filepath))
    
                startupinfo = None
                if platform.system() == "Windows":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
            finally:
                # Блок finally гарантирует, что временный текстовый файл удалится 
                # строго ПОСЛЕ того, как FFmpeg завершит работу, либо если произойдет сбой
                if list_path.exists():
                    try: os.remove(list_path)
                    except: pass

            # --- ЗАЩИТА 1: Если во время сборки нажали «Принудительно» — СТИРАЕМ БИТЫЙ ФАЙЛ ---
            if self.is_stopped:
                if out_filepath.exists():
                    try: os.remove(out_filepath)
                    except: pass
                return
            # -----------------------------------------------------------------------------------

            # --- ЗАЩИТА 2: Если FFmpeg вылетел с ошибкой — СТИРАЕМ ПОЛУГОТОВЫЙ ФАЙЛ ---
            if res.returncode != 0 or not out_filepath.exists():
                err_log = res.stderr.decode('utf-8', errors='ignore') if res.stderr else "Неизвестная ошибка"
                logging.error(f"Ошибка FFmpeg при сохранении {out_filepath.name}:\n{err_log}")
                has_errors_local = True
                if out_filepath.exists():
                    try: os.remove(out_filepath)
                    except: pass
            else:
                has_errors_local = has_errors
            # ---------------------------------------------------------------------------

            # ОПТИМИЗАЦИЯ 2: Безопасное обновление статуса ТОЛЬКО В ПАМЯТИ
            status_str = "warning" if has_errors_local else "success"
            
            with self.cache_lock:
                if has_errors_local:
                    self.processing_statuses_ram[str(out_filepath.resolve())] = status_str
                else:
                    # Если файл собрался успешно, удаляем его из списка ошибок (если он там был)
                    self.processing_statuses_ram.pop(str(out_filepath.resolve()), None)
                
            if not has_errors_local:
                logging.info(f"Файл {out_filepath.name} сохранен с эффектами (Скорость: {sp}x, Тон: {pt}).")
            if callback: 
                callback(original_filename, status_str, str(out_filepath.resolve()))
    
        if self.encode_semaphore:
            with self.encode_semaphore:
                _encode()
        else:
            _encode()

    def get_all_possible_hashes(self, raw_text):
        """Собирает хэши для всех возможных режимов синтеза (используется для оптимизации кэша)."""
        hashes = set()
        
        # 1. Базовая типографика
        raw_text = re.sub(r'[«»“”„]', '"', raw_text)
        raw_text = re.sub(r'^[ \t]*[-–—−]+\s*', '— ', raw_text, flags=re.MULTILINE)
        
        # 2. RegEx правила из глоссария
        raw_text = self.apply_regex_rules(raw_text)
        
        # 3. Обработка разделителей
        separator_token = "___SEPARATOR_TOKEN___"
        for sep in self.separators:
            pattern = r'^[ \t]*' + re.escape(sep) + r'[ \t]*$'
            raw_text = re.sub(pattern, f"\n{separator_token}\n", raw_text, flags=re.MULTILINE)
        
        paragraphs = [p.strip() for p in raw_text.split('\n') if p.strip()]
        current_full_text_clean = []

        for para in paragraphs:
            if para == separator_token:
                if current_full_text_clean:
                    full_clean = "\n".join(current_full_text_clean)
                    hashes.add(self.get_hash(full_clean))
                    current_full_text_clean = []
                continue

            sentences = [s.text for s in sentenize(para)]
            processed_sentences = []
            
            for sent_raw in sentences:
                sent_clean = self.process_sentence_text(sent_raw)
                if not re.search(r'[а-яА-ЯёЁa-zA-Z0-9]', sent_clean): 
                    continue
                processed_sentences.append(sent_clean)

            if not processed_sentences: 
                continue

            # 1. Хэши предложений (SENTENCE)
            for s_clean in processed_sentences:
                hashes.add(self.get_hash(s_clean))
                
            # 2. Хэш параграфа (PARAGRAPH)
            para_clean = " ".join(processed_sentences)
            hashes.add(self.get_hash(para_clean))
            
            # 3. Накопление для режима FULL с защитой (30 000)
            current_len = sum(len(t) for t in current_full_text_clean) + len(current_full_text_clean)
            if current_full_text_clean and (current_len + len(para_clean) > SAFE_LIMIT):
                full_clean = "\n".join(current_full_text_clean)
                hashes.add(self.get_hash(full_clean))
                current_full_text_clean = []
                
            current_full_text_clean.append(para_clean)

        # Хэш последнего блока FULL
        if current_full_text_clean:
            full_clean = "\n".join(current_full_text_clean)
            hashes.add(self.get_hash(full_clean))

        return hashes
    
    def process_text_file(self, filepath, dry_run=False, progress_callback=None, completion_callback=None):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: raw_text = f.read()
        except Exception as e:
            logging.error(f"Не удалось прочитать файл {filepath.name}: {e}")
            if completion_callback: completion_callback(filepath.name, "error", None)
            return
            
        if dry_run:
            self.cfg["use_cache"] = False
        else:
            out_filename = filepath.with_suffix(f'.{self.cfg["output_format"]}').name
            self.process_raw_text(raw_text, out_filename, False, True, progress_callback, lambda fname, status, audio: completion_callback(filepath.name, status) if completion_callback else None)

        

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
        except: pass

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
        with open(filepath, 'r', encoding='utf-8') as f:
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
        except: pass

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
        with open(filepath, 'r', encoding='utf-8') as f:
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
        pad = len(str(total))
        out_dir = Path(out_dir)
        out_dir.mkdir(exist_ok=True)
        
        saved_files = []
        name_no_ext = Path(orig_filename).stem
        
        match_start = re.search(r'\{num:(\d+)\}', template)
        start_index = int(match_start.group(1)) if match_start else 1

        for idx, (title, content) in enumerate(chapters, 0):
            safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:50]
            current_num = str(start_index + idx).zfill(pad)
            
            filename = template
            filename = re.sub(r'\{num(?::\d+)?\}', current_num, filename)
            filename = filename.replace("{name}", name_no_ext)
            filename = filename.replace("{book}", name_no_ext)
            filename = filename.replace("{title}", safe_title)
            filename = filename.replace("{author}", author if author else "Автор")
            
            if not filename.endswith(".txt"): filename += ".txt"
            
            out_path = out_dir / filename
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(content)
            saved_files.append(filename)
            
        return saved_files
# ==============================================================
    
# ================= ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (GUI) =================
class TTSApp:
    """Главный класс графического интерфейса приложения."""
    def __init__(self, root):
        self.root = root
        self.root.title("Silero TTS Studio")
        
        self.settings_vars = {}
        self.config = self.load_settings()

        # Динамический размер окна (70% от экрана по центру)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        w = int(screen_width * 0.7)
        h = int(screen_height * 0.7)
        x = int((screen_width - w) / 2)
        y = int((screen_height - h) / 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        
        self.processor = None
        self.processing_thread = None
        self.last_direct_audio = None

        self._export_lock = False
        
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
        self.tab_cache = ttk.Frame(self.notebook)
        self.tab_help = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_main, text="Синтез из папки")
        self.notebook.add(self.tab_direct, text="Прямой синтез")
        self.notebook.add(self.tab_import, text="Импорт книг")
        self.notebook.add(self.tab_utils, text="Экспорт и Сборка")
        self.notebook.add(self.tab_settings, text="Настройки")
        self.notebook.add(self.tab_glossary, text="Глоссарий")
        self.notebook.add(self.tab_cache, text="Кэш")
        self.notebook.add(self.tab_help, text="Справка")
        
        self.setup_main_tab()
        self.setup_direct_tab()
        self.setup_import_tab()
        self.setup_utils_tab()
        self.setup_settings_tab()
        self.setup_glossary_tab()
        self.setup_cache_tab()
        self.setup_help_tab()
        
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_change)
        self.load_files()
        self.update_fonts()

        self.apply_theme()
        
        # Перехват закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        if sys.platform == "darwin":
            self._setup_mac_hotkeys()
            # Отложенный жесткий фокус для macOS (имитация клика по окну)
            self.root.after(500, self._force_mac_focus)
        else:
            # Фикс буфера обмена для кириллической раскладки на Windows/Linux
            self._fix_cyrillic_clipboard()

        # Общий лимитер для всех вкладок
        self.shared_rate_limiter = RateLimiter(int(self.config.get("api_max_requests", 15)), float(self.config.get("api_time_window", 15.0)))
        
        self.root.after(300, self._silent_pre_warm_tabs)
        

    def _silent_pre_warm_tabs(self):
        """Тихий фоновый прогрев всех вкладок в памяти без дублирования задач"""
        tabs_list = list(self.notebook.tabs())
        
        def _step():
            if not tabs_list:
                return
            tab_id = tabs_list.pop(0)
            try:
                widget = self.notebook.nametowidget(tab_id)
                widget.update_idletasks()
            except Exception:
                pass
            if tabs_list:
                self.root.after(15, _step)

        self.root.after(100, _step)

    def _force_mac_focus(self):
        """Жестко выводит окно на передний план на macOS после отрисовки"""
        if is_frozen_mac:
            try:
                import ctypes, ctypes.util
                appkit = ctypes.cdll.LoadLibrary(ctypes.util.find_library('AppKit'))
                appkit.objc_msgSend(appkit.objc_msgSend(appkit.objc_getClass('NSApplication'), appkit.sel_registerName('sharedApplication')), appkit.sel_registerName('activateIgnoringOtherApps:'), True)
            except: pass
        # Трюк Tkinter: делаем окно поверх всех и сразу возвращаем обратно. Это дает окну системный фокус!
        self.root.attributes('-topmost', True)
        self.root.update()
        self.root.attributes('-topmost', False)
        self.root.focus_force()

    def _fix_cyrillic_clipboard(self):
        """Чинит кириллицу и добавляет умную вставку (очистка путей) для Win/Linux"""
        # Копирование, Вырезание, Выделить всё - оставляем нативные (они работают идеально)
        self.root.bind('<Control-с>', lambda e: self.root.event_generate('<<Copy>>'))
        self.root.bind('<Control-ч>', lambda e: self.root.event_generate('<<Cut>>'))
        self.root.bind('<Control-ф>', lambda e: self.root.event_generate('<<SelectAll>>'))

        # А вот вставку перехватываем для стандартного Ctrl+V и кириллического Ctrl+М
        self.root.bind('<Control-v>', self._smart_win_lin_paste)
        self.root.bind('<Control-м>', self._smart_win_lin_paste)

    def _smart_win_lin_paste(self, event):
        """Умная вставка: вычищает кавычки (Windows 11) и file:// (Linux)"""
        try:
            clip = self.root.clipboard_get()
        except Exception:
            return "break" # Буфер пуст или содержит картинку/файл

        if clip:
            # Очистка мусора из путей
            clip = clip.strip()
            if clip.startswith("file://"):
                clip = clip[7:]
            if "%" in clip:
                try: clip = urllib.parse.unquote(clip)
                except: pass
            clip = clip.strip('"\'') # Убираем кавычки от Windows 11 "Copy as path"

            w = event.widget
            if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
                w = self.root.focus_get()

            # Безопасная вставка в активный виджет
            if isinstance(w, tk.Text):
                if w.tag_ranges(tk.SEL):
                    w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                w.insert(tk.INSERT, clip)
                return "break"
            elif isinstance(w, (ttk.Entry, tk.Entry)):
                if w.selection_present():
                    w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                w.insert(tk.INSERT, clip)
                return "break"
                
        return "break"

    def get_status_color(self, status="info"):
        """Централизованная палитра статусов (WCAG AAA)"""
        colors = {
            "info": "#003366",     # Глубокий темно-сапфировый
            "success": "#15803D",  # Темно-зеленый
            "warning": "#C2410C",  # Темно-оранжевый
            "error": "#B91C1C",    # Темно-красный
            "text": "#000000"      # Стандартный черный текст
        }
        return colors.get(status, "#000000")
        
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
        messagebox.showinfo("Успех", "Глобальные эффекты сброшены по умолчанию!")

    def _natural_sort_key(self, text):
        """Ключ для сортировки файлов человеком, а не машиной (Глава 2 будет перед Глава 10)"""
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(text))]

    def _sort_export_tree(self, col, reverse):
        """Натуральная сортировка дерева экспорта (Глава 2 будет перед Глава 10)"""
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
        selected = self.export_tree.selection()
        groups = [i for i in selected if i in self.export_groups]
        
        if not groups:
            messagebox.showinfo("Внимание", "Выделите группу(ы) для разгруппировки.")
            return
            
        for g_id in groups:
            parent = self.export_tree.parent(g_id)
            # Переносим всех детей на уровень выше (или в корень)
            for child in self.export_tree.get_children(g_id):
                self.export_tree.move(child, parent, tk.END)
            # Удаляем саму группу
            del self.export_groups[g_id]
            self.export_tree.delete(g_id)
    
    def _mac_multiselect(self, event, tree):
        """Атомарное выделение с гарантированной зачисткой призраков на macOS"""
        item = tree.identify_row(event.y)
        if item:
            current_sel = set(tree.selection())
            if item in current_sel:
                current_sel.remove(item)
            else:
                current_sel.add(item)
            
            # Атомарно задаем весь новый массив выделения и принудительно перерисовываем
            tree.selection_set(tuple(current_sel))
            tree.update()
        return "break"

    def _setup_mac_hotkeys(self):
        """Обработка горячих клавиш macOS и безопасный патч первого клика"""
        # Универсальные хоткеи для работы с буфером обмена
        for widget_cls in ("Text", "Entry", "TEntry"):
            self.root.bind_class(widget_cls, "<Command-Key>", self._dispatch_mac_cmd)


    def _dispatch_mac_cmd(self, event):
        """Нативная обработка горячих клавиш macOS с декодированием путей Finder (unquote + NFC)"""
        w = event.widget
        kc = event.keycode
        char = str(event.char).lower()

        if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
            w = self.root.focus_get()
            if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
                return

        # --- ВСТАВКА (⌘V) С АВТО-ДЕКОДИРОВАНИЕМ КИРИЛЛИЦЫ И ПУТЕЙ FINDER ---
        if kc == 9 or char in ('v', 'м', '\x16'):
            clip = ""
            try:
                clip = self.root.clipboard_get()
            except Exception:
                pass
            
            if not clip:
                try:
                    clip = subprocess.check_output(["pbpaste"], text=True, stderr=subprocess.DEVNULL)
                except Exception:
                    clip = ""

            if clip:
                try:
                    # 💡 АВТО-ОЧИСТКА И ДЕКОДИРОВАНИЕ ПУТЕЙ FINDER (unquote + NFC)
                    clip = clip.strip()
                    if clip.startswith("file://"):
                        clip = clip[7:]
                    if "%" in clip:
                        try:
                            clip = urllib.parse.unquote(clip)
                        except Exception: pass
                    clip = unicodedata.normalize('NFC', clip).strip('"\'')

                    if isinstance(w, tk.Text):
                        if w.tag_ranges(tk.SEL):
                            w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                        w.insert(tk.INSERT, clip)
                    elif isinstance(w, (ttk.Entry, tk.Entry)):
                        if w.selection_present():
                            w.delete(tk.SEL_FIRST, tk.SEL_LAST)
                        w.insert(tk.INSERT, clip)
                except Exception as e:
                    logging.debug(f"Ошибка вставки на Mac: {e}")
            return "break"

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
                    except: pass
            except Exception: pass
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
                    except: pass
            except Exception: pass
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
            except Exception: pass
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
        """Возвращает умный начальный путь для диалогов выбора файлов/папок"""
        if current_path:
            p = Path(current_path)
            if is_file:
                p = p.parent
            if p.exists() and p.is_dir():
                return str(p)
        return str(BASE_DIR)
    
    def _center_popup(self, dialog, width, height):
        """Центрирует диалоговое окно и мгновенно проявляет его БЕЗ мигания"""
        self.root.update_idletasks()
        
        # Получаем истинные экранные координаты и размеры главного окна
        p_x = self.root.winfo_rootx()
        p_y = self.root.winfo_rooty()
        p_w = self.root.winfo_width()
        p_h = self.root.winfo_height()
        
        # Высчитываем координаты центра
        x = max(0, p_x + (p_w - width) // 2)
        y = max(0, p_y + (p_h - height) // 2)
        
        # Задаем геометрические координаты
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        
        # Проявляем окно уже строго в правильной позиции!
        dialog.deiconify()
        
    def _create_wait_popup(self, title, message):
        popup = tk.Toplevel(self.root)
        popup.withdraw() # 👈 Прячем при создании
        
        popup.title(title)
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.grab_set()
        
        ttk.Label(popup, text=message, font=("", 10)).pack(pady=(15, 5))
        bar = ttk.Progressbar(popup, mode='indeterminate')
        bar.pack(fill=tk.X, padx=20, pady=5)
        bar.start(10)
        
        self._center_popup(popup, 320, 100) # 👈 Проявляем в конце
        
        return popup

    def _tree_select_all(self, event):
        """Рекурсивно выделяет все элементы (включая вложенные файлы) в дереве Экспорта"""
        tree = event.widget
        def get_all(item=""):
            res = []
            for c in tree.get_children(item):
                res.append(c)
                res.extend(get_all(c))
            return res
        tree.selection_set(get_all())
        return "break"
    
    def on_closing(self):
        """Безопасное закрытие программы с очисткой временных файлов"""
        # 1. Если идет синтез, мягко его останавливаем и сохраняем кэш
        if self.processor and not self.processor.is_stopped:
            self.processor.stop()
            
        self.save_settings()
        
        # Очищаем временные обложки и временные файлы склейки
        for folder_name in ("covers", "temp"):
            tmp_dir = APP_DATA_DIR / folder_name
            if tmp_dir.exists():
                try: shutil.rmtree(tmp_dir)
                except Exception as e: logging.error(f"Не удалось удалить {folder_name}: {e}")
                
        self.root.destroy()

    def update_fonts(self, *args):
        """Обновление размера шрифта во всех текстовых блоках (разделители зафиксированы)"""
        size = self.font_size_var.get()
        if hasattr(self, 'direct_text'): self.direct_text.config(font=("Arial", size))
        if hasattr(self, 'txt_glossary'): self.txt_glossary.config(font=("Courier", size))
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

    def apply_theme(self, *args):
        """Включает нативную системную тему ОС"""
        try:
            self.root.title("Silero TTS Studio")
        except: pass
        
        # 1. Включаем самую быструю нативную тему ОС
        style = ttk.Style()
        os_name = platform.system()
        default_theme = 'vista' if os_name == "Windows" else 'aqua' if os_name == "Darwin" else 'clam'
        try:
            style.theme_use(default_theme)
        except:
            style.theme_use('default')

        # 2. Обновляем цвета тегов в таблицах из единой палитры
        if hasattr(self, 'tree'):
            self.tree.tag_configure('success', foreground=self.get_status_color("success"))
            self.tree.tag_configure('warning', foreground=self.get_status_color("warning"))
            self.tree.tag_configure('error', foreground=self.get_status_color("error"))
            self.tree.tag_configure('processing', foreground=self.get_status_color("text"), font=('', 10, 'bold'))

        # 3. Перекрашиваем статусные надписи
        status_labels = [
            getattr(self, 'lbl_current_text', None),
            getattr(self, 'lbl_direct_status', None),
            getattr(self, 'lbl_import_status', None),
            getattr(self, 'lbl_export_status', None),
            getattr(self, 'lbl_cache_count', None)
        ]
        
        for lbl in status_labels:
            if lbl and lbl.winfo_exists():
                try:
                    lbl.config(foreground=self.get_status_color("info"))
                except Exception:
                    pass

    def full_ui_refresh(self):
        """Обновление значений UI и шрифтов"""
        self._is_updating_ui = True
        try:
            self.set_ui_from_config()
            self.update_fonts()
            self.root.update_idletasks()
        finally:
            self._is_updating_ui = False

    def ensure_dirs(self):
        """Гарантирует существование всех системных и рабочих папок на лету (даже если их удалили во время работы)"""
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            if hasattr(self, 'config'):
                for dir_key in ("input_dir", "output_dir", "cache_dir"):
                    path_str = self.config.get(dir_key)
                    if path_str:
                        Path(path_str).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error(f"Ошибка авто-создания директорий: {e}")
            
    def load_settings(self, path=SETTINGS_FILE):
        """Безопасная загрузка настроек с авто-созданием папки"""
        self.ensure_dirs()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        cfg = DEFAULT_CONFIG.copy()
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg.update(json.load(f))
            except Exception as e:
                logging.error(f"Ошибка загрузки конфига {path}: {e}")
        return cfg


    def update_config_from_ui(self):
        for key, var in list(self.settings_vars.items()):
            try:
                self.config[key] = var.get()
            except Exception:
                pass
                
        # Собираем разделители из всех активных полей
        if hasattr(self, 'separator_entries'):
            seps = [ent.get().strip() for ent in self.separator_entries if ent.get().strip()]
            self.config["separator_symbols"] = "\n".join(seps)
            
    def save_settings(self, path=SETTINGS_FILE, show_popup=False):
        """Безопасное сохранение настроек с авто-созданием папки и уведомлением"""
        self.ensure_dirs()
        self.update_config_from_ui()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # === НОВОЕ: Обновляем глобальный лимитер ===
        if hasattr(self, 'shared_rate_limiter'):
            self.shared_rate_limiter.max_requests = int(self.config.get("api_max_requests", 15))
            self.shared_rate_limiter.time_window = float(self.config.get("api_time_window", 15.0))
        
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            if show_popup:
                messagebox.showinfo("Успех", "Настройки успешно сохранены!")
        except Exception as e:
            logging.error(f"Ошибка сохранения конфига {path}: {e}")
            if show_popup:
                messagebox.showerror("Ошибка", f"Не удалось сохранить настройки:\n{e}")

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
                                var.set(bool(val))
                            elif isinstance(var, tk.IntVar):
                                var.set(int(float(val)))
                            elif isinstance(var, tk.DoubleVar):
                                var.set(float(val))
                            else:
                                var.set(str(val))
                        except Exception as e:
                            try: var.set(str(val))
                            except: pass
    
            # 3. Поля на вкладке "Экспорт"
            if hasattr(self, 'export_outdir_var'):
                try: self.export_outdir_var.set(str(self.config.get("export_dir", "")))
                except: pass
            if hasattr(self, 'export_fmt_var'):
                try: self.export_fmt_var.set(str(self.config.get("output_format", "mp3")))
                except: pass
            if hasattr(self, 'export_bitrate_var'):
                try: self.export_bitrate_var.set(str(self.config.get("output_bitrate", "128k")))
                except: pass
    
            # 4. Ползунки и текстовые подписи чисел
            try:
                sp = float(self.config.get("fx_speed", 1.0))
                pt = float(self.config.get("fx_pitch", 1.0))
                ec = bool(self.config.get("fx_echo", False))
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
                    self.exp_speed_var.set(sp)
                    self.exp_pitch_var.set(pt)
                    self.exp_echo_var.set(ec)
                    self.exp_delay_var.set(ed)
                    self.exp_decay_var.set(ey)
                    if hasattr(self, 'lbl_exp_speed'): self.lbl_exp_speed.config(text=f"{sp:.1f}x")
                    if hasattr(self, 'lbl_exp_pitch'): self.lbl_exp_pitch.config(text=f"{pt:.2f}")
                    if hasattr(self, 'lbl_exp_delay'): self.lbl_exp_delay.config(text=f"{ed}")
                    if hasattr(self, 'lbl_exp_decay'): self.lbl_exp_decay.config(text=f"{ey:.1f}")
            except Exception as e:
                logging.error(f"Ошибка ползунков: {e}")
    
            # 5. Шрифт и тема
            if hasattr(self, 'font_size_var') and "ui_font_size" in self.config:
                try: self.font_size_var.set(int(self.config["ui_font_size"]))
                except: pass
    
        finally:
            self._is_updating_ui = False

    def import_config(self):
        filepath = filedialog.askopenfilename(initialdir=str(APP_DATA_DIR), filetypes=[("JSON files", "*.json")])
        if not filepath: return
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                imported_data = json.load(f)
            if not isinstance(imported_data, dict): raise ValueError()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Импорт настроек")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Какие настройки из файла применить?").pack(pady=10, padx=20)
        
        vars_dict = {
            "api": (tk.BooleanVar(value=True), "API и Лимиты"),
            "folders": (tk.BooleanVar(value=True), "Пути к папкам"),
            "pauses": (tk.BooleanVar(value=True), "Паузы и Разделители"),
            "cache": (tk.BooleanVar(value=True), "Настройки Кэша"),
            "effects": (tk.BooleanVar(value=True), "Эффекты (Скорость, Тон)"),
            "tags": (tk.BooleanVar(value=True), "Вывод и Теги ID3")
        }
        
        for key, (var, text) in vars_dict.items():
            ttk.Checkbutton(dialog, text=text, variable=var).pack(anchor=tk.W, padx=30, pady=2)
            
        def do_import():
            key_groups = {
                "api": ["api_", "speaker", "max_retries"],
                "folders": ["input_dir", "output_dir", "cache_dir", "export_dir", "import_outdir"],
                "pauses": ["pause_", "separator_symbols", "default_group_pause"],
                "cache": ["auto_", "silence_threshold", "use_cache", "cache_", "enable_cache_"],
                "effects": ["fx_"],
                "tags": ["output_", "synthesis_mode", "tag_", "default_group_name"]
            }
            
            for group_key, (var, _) in vars_dict.items():
                if var.get():
                    prefixes = key_groups[group_key]
                    for k, v in imported_data.items():
                        if any(k.startswith(p) for p in prefixes):
                            self.config[k] = v
                            
            dialog.destroy()
            self.set_ui_from_config()
            self.save_settings(SETTINGS_FILE)
            self.full_ui_refresh()
            self.load_files()
            messagebox.showinfo("Успех", "Выбранные настройки успешно применены!")
            
        ttk.Button(dialog, text="Импортировать", command=do_import).pack(pady=15)
        self._center_popup(dialog, 350, 280)

    def reset_config(self):
        """Сброс рабочих настроек с сохранением темы оформления и размера шрифта"""
        if messagebox.askyesno("Сбросить настройки", "Сбросить рабочие настройки (паузы, лимиты, пути) к значениям по умолчанию?"):
            try:
                self.ensure_dirs()
                
                # 1. Сохраняем текущие визуальные предпочтения пользователя
                current_font_size = self.config.get("ui_font_size", 10)

                # 2. Загружаем дефолты и восстанавливаем визуал
                full_config = DEFAULT_CONFIG.copy()
                full_config["ui_font_size"] = current_font_size
                self.config = full_config

                # 3. Записываем обновленные дефолты в settings.json на диск
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)

                # 4. Полный проход (перетасовка) для идеально чистой перерисоки
                self.full_ui_refresh()
                self.load_files()

                messagebox.showinfo("Успех", "Все рабочие настройки сброшены!\n(Тема и размер шрифта сохранены).")
            except Exception as e:
                logging.error(f"Ошибка сброса настроек: {e}")
                messagebox.showerror("Ошибка", f"Не удалось сбросить настройки:\n{e}")

    # --- Вкладка "Синтез из папки" ---
    def setup_main_tab(self):
        list_frame = ttk.Frame(self.tab_main, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        # --- ИЗМЕНЕНИЕ: Системно-зависимая подсказка ---
        ctrl_key = "Command (⌘)" if sys.platform == "darwin" else "Ctrl"
        ttk.Label(list_frame, text=f"💡 Вы можете выделять несколько строк мышкой с зажатым {ctrl_key} или Shift", font=("", 8, "italic"), foreground="gray").pack(anchor=tk.W, pady=(0,5))
        # -----------------------------------------------
        
        # ДОБАВЛЕНО: selectmode="extended" для множественного выделения
        self.tree = ttk.Treeview(list_frame, columns=("status", "filename"), show="headings", selectmode="extended")
        if sys.platform == "darwin":
            self.tree.bind("<Command-Button-1>", lambda e: self._mac_multiselect(e, self.tree))
        self.tree.heading("status", text="Статус")
        self.tree.heading("filename", text="Имя файла")
        self.tree.column("status", width=120, anchor=tk.CENTER)
        self.tree.column("filename", width=600)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree.tag_configure('queued', foreground='gray')
        self.tree.tag_configure('processing', foreground='black', font=('', 10, 'bold'))
        self.tree.tag_configure('success', foreground='green')
        self.tree.tag_configure('warning', foreground='orange')
        self.tree.tag_configure('error', foreground='red')

        prog_frame = ttk.Frame(self.tab_main, padding=10)
        prog_frame.pack(fill=tk.X)
        
        self.lbl_current_text = ttk.Label(prog_frame, text="Ожидание...", font=('', 10, 'italic'), foreground=self.get_status_color("info"), width=110)
        self.lbl_current_text.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        
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
        
        self.direct_text = tk.Text(frame, wrap=tk.WORD, height=10, undo=True, maxundo=50)
        self.direct_text.pack(fill=tk.BOTH, expand=True, pady=5)
        
        ctrl_frame = ttk.Frame(frame)
        ctrl_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(ctrl_frame, text="Имя файла:").pack(side=tk.LEFT)
        self.settings_vars["direct_filename"] = tk.StringVar(value=self.config.get("direct_filename", "direct_output.mp3"))
        ttk.Entry(ctrl_frame, textvariable=self.settings_vars["direct_filename"], width=20).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_save"] = tk.BooleanVar(value=self.config.get("direct_save", True))
        ttk.Checkbutton(ctrl_frame, text="Сохранить", variable=self.settings_vars["direct_save"]).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_force"] = tk.BooleanVar(value=self.config.get("direct_force", False))
        ttk.Checkbutton(ctrl_frame, text="Игнорировать кэш", variable=self.settings_vars["direct_force"]).pack(side=tk.LEFT, padx=5)
        
        self.settings_vars["direct_autoplay"] = tk.BooleanVar(value=self.config.get("direct_autoplay", True))
        ttk.Checkbutton(ctrl_frame, text="Авто-воспроизведение", variable=self.settings_vars["direct_autoplay"]).pack(side=tk.LEFT, padx=5)
        
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
        self.lbl_dir_delay = ttk.Label(mid_fx, text=f"{self.dir_echo_delay_var.get()}мс", width=5)
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
            messagebox.showinfo("Успех", "Эффекты сохранены в глобальные настройки!")
            
        ttk.Button(bot_fx, text="💾 Сделать глобальными", command=apply_to_global).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bot_fx, text="🔄 Сбросить эффекты", command=self.reset_direct_fx).pack(side=tk.RIGHT, padx=5)
        # ------------------------------------------------
        
        self.lbl_direct_status = ttk.Label(frame, text="", foreground=self.get_status_color("info"))
        self.lbl_direct_status.pack(anchor=tk.W)
        
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
            try: winsound.PlaySound(None, winsound.SND_PURGE)
            except: pass
        else:
            if hasattr(self, 'current_playback_process') and self.current_playback_process:
                try: self.current_playback_process.terminate()
                except: pass
                self.current_playback_process = None

    def play_audio_segment(self, audio_segment):
        """Проигрывает аудиосегмент с защитой от наложения (останавливает предыдущий)"""
        self.stop_audio_playback()

        def _play():
            # Используем один фиксированный файл, чтобы не засорять папку Temp тысячами файлов
            temp_dir = APP_DATA_DIR / "temp"
            temp_dir.mkdir(exist_ok=True)
            temp_path = temp_dir / "current_playback.wav"

            try:
                audio_segment.export(str(temp_path), format="wav")

                if platform.system() == "Windows":
                    # SND_ASYNC позволяет UI не зависать, пока играет звук
                    winsound.PlaySound(str(temp_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                elif platform.system() == "Darwin":
                    self.current_playback_process = subprocess.Popen(["afplay", str(temp_path)])
                    self.current_playback_process.wait()
                else:
                    self.current_playback_process = subprocess.Popen(["aplay", str(temp_path)])
                    self.current_playback_process.wait()
            except Exception as e:
                logging.error(f"Ошибка воспроизведения: {e}")

        threading.Thread(target=_play, daemon=True).start()

    def play_audio_file(self, filepath):
        if not os.path.exists(filepath): return
        try:
            seg = AudioSegment.from_file(filepath)
            self.play_audio_segment(seg)
        except Exception as e:
            logging.error(f"Ошибка чтения файла для плеера: {e}")

    def play_last_audio(self):
        if self.last_direct_audio and os.path.exists(self.last_direct_audio):
            seg = AudioSegment.from_file(self.last_direct_audio)
            sp = self.dir_speed_var.get()
            pt = self.dir_pitch_var.get()
            ec = self.dir_echo_var.get()
            ed = self.dir_echo_delay_var.get()
            ey = self.dir_echo_decay_var.get()

            processed_segment = AudioEffects.apply_effects(seg, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
            self.play_audio_segment(processed_segment)

    # --- Вкладка "Импорт книг" ---
    def setup_import_tab(self):
        frame = ttk.Frame(self.tab_import, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        if not IMPORT_LIBS_AVAILABLE:
            ttk.Label(frame, text="⚠️ Для работы импорта установите библиотеки:\npip install EbookLib beautifulsoup4 python-docx lxml", foreground=self.get_status_color("error")).pack(pady=10)
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
        
        self.btn_import_start = ttk.Button(frame, text="⚡ Извлечь и Нарезать", command=self.start_import)
        self.btn_import_start.pack(pady=5)

    def start_import(self):
        self.save_settings()
        filepath = self.import_filepath_var.get()
        if not filepath or not os.path.exists(filepath):
            messagebox.showerror("Ошибка", "Выберите существующий файл!")
            return
            
        out_dir = self.settings_vars["import_outdir"].get()
        template = self.settings_vars["import_template"].get()
        regex_pattern = self.settings_vars["import_regex"].get()
        single_file = self.settings_vars["import_single_file"].get()
        
        self.btn_import_start.config(state=tk.DISABLED)
        self.lbl_import_status.config(text="Анализ и извлечение текста...", foreground=self.get_status_color("text"))
        
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
                        with open(filepath, 'r', encoding='utf-8') as f: chapters = [("Книга", f.read())]
                else:
                    if ext == ".epub": chapters, author = BookExtractor.extract_epub(filepath)
                    elif ext == ".fb2": chapters, author = BookExtractor.extract_fb2(filepath)
                    elif ext == ".docx": chapters = BookExtractor.extract_docx(filepath)
                    elif ext == ".txt": chapters = BookExtractor.split_txt_by_regex(filepath, regex_pattern)
                
                if not chapters:
                    raise ValueError("Не удалось найти текст или главы в файле.")
                    
                self.root.after(0, lambda: self.lbl_import_status.config(text=f"Найдено глав: {len(chapters)}. Сохранение...", foreground=self.get_status_color("warning")))
                
                # Передаем автора в сохранение
                saved_files = BookExtractor.save_chapters(chapters, out_dir, filepath, template, author=author)
                
                msg = f"Успешно извлечено и сохранено файлов: {len(saved_files)}\nПапка: {out_dir}"
                self.root.after(0, lambda: self.lbl_import_status.config(text="Готово!", foreground=self.get_status_color("success")))
                self.root.after(0, lambda: messagebox.showinfo("Успех", msg))
                self.root.after(0, self.load_files)
                
            except Exception as e:
                logging.error(f"Ошибка импорта: {e}")
                self.root.after(0, lambda: self.lbl_import_status.config(text="Ошибка!", foreground=self.get_status_color("error")))
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось обработать файл:\n{e}"))
            finally:
                self.root.after(0, lambda: self.btn_import_start.config(state=tk.NORMAL))
                
        threading.Thread(target=run, daemon=True).start()

    # ================= Вкладка "Экспорт и Сборка" =================
    def setup_utils_tab(self):
        frame = ttk.Frame(self.tab_utils, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        self.export_groups = {} 
        self.export_files = {}
        self.group_counter = 0

        # === 1. ПРОГРЕСС-БАР (Пакуем в самый низ, чтобы не пропадал) ===
        prog_frame = ttk.Frame(frame)
        prog_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))
        self.lbl_export_status = ttk.Label(prog_frame, text="Ожидание...", foreground=self.get_status_color("info"))
        self.lbl_export_status.pack(side=tk.LEFT)
        self.export_progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=400, mode='determinate')
        self.export_progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))

        # === 2. ПАНЕЛЬ ЭКСПОРТА ===
        export_frame = ttk.LabelFrame(frame, text="Экспорт", padding=5)
        export_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=5)
        
        row1 = ttk.Frame(export_frame)
        row1.pack(fill=tk.X, pady=2)
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
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Формат:").pack(side=tk.LEFT, padx=(5, 2))
        self.export_fmt_var = tk.StringVar(value=self.config.get("output_format", "mp3"))
        self.cb_export_fmt = ttk.Combobox(row2, textvariable=self.export_fmt_var, values=["mp3", "wav", "ogg"], width=5, state="readonly")
        self.cb_export_fmt.pack(side=tk.LEFT)
        
        ttk.Label(row2, text="Битрейт:").pack(side=tk.LEFT, padx=(10, 2))
        self.export_bitrate_var = tk.StringVar(value=self.config.get("output_bitrate", "128k"))
        self.cb_export_bitrate = ttk.Combobox(row2, textvariable=self.export_bitrate_var, values=["64k", "128k", "192k", "256k", "320k"], width=5, state="readonly")
        self.cb_export_bitrate.pack(side=tk.LEFT)
        
        self.export_apply_fx_var = tk.BooleanVar(value=False)
        self.chk_export_fx = ttk.Checkbutton(row2, text="Наложить эффекты", variable=self.export_apply_fx_var)
        self.chk_export_fx.pack(side=tk.LEFT, padx=10)

        # НОВАЯ ГАЛОЧКА: Только теги
        self.export_tags_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Только обновить теги (в исходных файлах)", variable=self.export_tags_only_var).pack(side=tk.LEFT, padx=10)
        
        # ИСПРАВЛЕНИЕ: Блокировка UI при включении галочки "Только обновить теги"
        def toggle_export_mode(*args):
            state = tk.DISABLED if self.export_tags_only_var.get() else tk.NORMAL
            cb_state = tk.DISABLED if self.export_tags_only_var.get() else "readonly"
            
            self.btn_export_dir.config(state=state)
            self.ent_export_dir.config(state=state)
            self.cb_export_fmt.config(state=cb_state)
            self.cb_export_bitrate.config(state=cb_state)
            self.chk_export_fx.config(state=state)
            
        self.export_tags_only_var.trace("w", toggle_export_mode)
        
        self.btn_export_start = ttk.Button(row2, text="🚀 Начать Сборку", command=self.start_export_process)
        self.btn_export_start.pack(side=tk.RIGHT, padx=5)
        self.btn_export_stop = ttk.Button(row2, text="⏹ Стоп", command=self.stop_export_process, state=tk.DISABLED)
        self.btn_export_stop.pack(side=tk.RIGHT, padx=5)
        
        row3 = ttk.Frame(export_frame)
        row3.pack(fill=tk.X, pady=2)
        
        ttk.Label(row3, text="Скор:").pack(side=tk.LEFT, padx=1)
        self.exp_speed_var = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        self.lbl_exp_speed = ttk.Label(row3, text=f"{self.exp_speed_var.get():.1f}x", width=4)
        self.lbl_exp_speed.pack(side=tk.LEFT)
        ttk.Scale(row3, from_=0.5, to_=3.0, variable=self.exp_speed_var, command=lambda v: self.lbl_exp_speed.config(text=f"{float(v):.1f}x")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        ttk.Label(row3, text="Тон:").pack(side=tk.LEFT, padx=(4, 1))
        self.exp_pitch_var = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        self.lbl_exp_pitch = ttk.Label(row3, text=f"{self.exp_pitch_var.get():.2f}", width=4)
        self.lbl_exp_pitch.pack(side=tk.LEFT)
        ttk.Scale(row3, from_=0.5, to_=2.0, variable=self.exp_pitch_var, command=lambda v: self.lbl_exp_pitch.config(text=f"{float(v):.2f}")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.exp_echo_var = tk.BooleanVar(value=self.config.get("fx_echo", False))
        ttk.Checkbutton(row3, text="Эхо", variable=self.exp_echo_var).pack(side=tk.LEFT, padx=(4, 2))
        
        ttk.Label(row3, text="Зад:").pack(side=tk.LEFT, padx=1)
        self.exp_delay_var = tk.IntVar(value=self.config.get("fx_echo_delay", 300))
        self.lbl_exp_delay = ttk.Label(row3, text=f"{self.exp_delay_var.get()}", width=4)
        self.lbl_exp_delay.pack(side=tk.LEFT)
        ttk.Scale(row3, from_=50, to_=1000, variable=self.exp_delay_var, command=lambda v: self.lbl_exp_delay.config(text=f"{int(float(v))}")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        ttk.Label(row3, text="Сил:").pack(side=tk.LEFT, padx=(4, 1))
        self.exp_decay_var = tk.DoubleVar(value=self.config.get("fx_echo_decay", 0.3))
        self.lbl_exp_decay = ttk.Label(row3, text=f"{self.exp_decay_var.get():.1f}", width=3)
        self.lbl_exp_decay.pack(side=tk.LEFT)
        ttk.Scale(row3, from_=0.1, to_=0.8, variable=self.exp_decay_var, command=lambda v: self.lbl_exp_decay.config(text=f"{float(v):.1f}")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(row3, text="🔄 Сброс", command=self.reset_export_fx).pack(side=tk.RIGHT, padx=2)

        # === 3. ПАНЕЛЬ КНОПОК (В ДВА РЯДА) ===
        self.export_mid_frame = ttk.Frame(frame)
        self.export_mid_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=5)
        
        mid_row1 = ttk.Frame(self.export_mid_frame)
        mid_row1.pack(fill=tk.X, pady=1)
        ttk.Button(mid_row1, text="📁 Добавить группу", command=self.add_export_group).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row1, text="📂 Добавить папку", command=self.add_export_folder).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row1, text="🎵 Добавить аудио", command=self.add_export_files).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row1, text="📦 В новую группу", command=self.group_selected_into_new).pack(side=tk.LEFT, padx=(10, 1))
        # ИСПРАВЛЕНИЕ: Разгруппировать перенесено в первый ряд!
        ttk.Button(mid_row1, text="📤 Разгруппировать", command=self.ungroup_export_items).pack(side=tk.LEFT, padx=1)
        
        mid_row2 = ttk.Frame(self.export_mid_frame)
        mid_row2.pack(fill=tk.X, pady=1)
        ttk.Button(mid_row2, text="➖ Удалить", command=self.remove_export_items).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row2, text="⬇", width=3, command=lambda: self.move_export_item(1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row2, text="⬆", width=3, command=lambda: self.move_export_item(-1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(mid_row2, text="⏱ Авто-разбивка", command=self.auto_split_export).pack(side=tk.LEFT, padx=(10, 1))

        # === 4. ВЕРХНЯЯ ПАНЕЛЬ С ДЕРЕВОМ ===
        # Изменены веса, чтобы дерево занимало больше места (weight=4 vs weight=1)
        top_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        top_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        
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
            self.export_tree.bind("<Command-Button-1>", lambda e: self._mac_multiselect(e, self.export_tree))
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
        self.settings_vars["default_group_name"].trace("w", lambda *args: None if getattr(self, '_is_updating_ui', False) else self.save_settings())
        ttk.Entry(tmpl_frame, textvariable=self.settings_vars["default_group_name"]).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.grp_notebook = ttk.Notebook(self.group_settings_frame)
        self.grp_notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        
        self.grp_tab_basic = ttk.Frame(self.grp_notebook, padding=5)
        self.grp_tab_tags = ttk.Frame(self.grp_notebook, padding=5)
        self.grp_notebook.add(self.grp_tab_basic, text="Основные")
        self.grp_notebook.add(self.grp_tab_tags, text="Теги")

        self.lbl_grp_name = ttk.Label(self.grp_tab_basic, text="Имя группы / Название трека:")
        self.lbl_grp_name.pack(anchor=tk.W, pady=(0, 2))
        self.grp_name_var = tk.StringVar()
        self.grp_name_var.trace("w", self.save_export_item_settings)
        ttk.Entry(self.grp_tab_basic, textvariable=self.grp_name_var).pack(fill=tk.X, pady=(0, 10))
        
        self.grp_merge_var = tk.BooleanVar()
        self.grp_merge_var.trace("w", self.save_export_item_settings)
        self.chk_merge = ttk.Checkbutton(self.grp_tab_basic, text="Склеить файлы в один трек", variable=self.grp_merge_var)
        self.chk_merge.pack(anchor=tk.W, pady=2)
        
        self.grp_subfolder_var = tk.BooleanVar(value=True)
        self.grp_subfolder_var.trace("w", self.save_export_item_settings)
        self.chk_subfolder = ttk.Checkbutton(self.grp_tab_basic, text="Сохранять в подпапку", variable=self.grp_subfolder_var)
        self.chk_subfolder.pack(anchor=tk.W, pady=(0, 5))
        
        ttk.Label(self.grp_tab_basic, text="Пауза между файлами (мс):").pack(anchor=tk.W, pady=(5, 2))
        self.grp_pause_var = tk.IntVar()
        self.grp_pause_var.trace("w", self.save_export_item_settings)
        self.ent_pause = ttk.Entry(self.grp_tab_basic, textvariable=self.grp_pause_var, width=10)
        self.ent_pause.pack(anchor=tk.W, pady=(0, 10))
        
        # ЭЛЕГАНТНАЯ КНОПКА МАССОВОГО ПРИМЕНЕНИЯ
        self.btn_mass_apply_basic = ttk.Button(self.grp_tab_basic, text="⚙️ Применить ко всем группам...", command=self.open_mass_apply_dialog)
        self.btn_mass_apply_basic.pack(anchor=tk.W, pady=5)
        
        # -- Вкладка: Теги --
        tag_grid = ttk.Frame(self.grp_tab_tags)
        tag_grid.pack(fill=tk.X, pady=5)
        
        def add_grp_tag(parent, label, var_name, r, c):
            ttk.Label(parent, text=label).grid(row=r, column=c, sticky=tk.W, pady=2, padx=2)
            var = tk.StringVar()
            var.trace("w", self.save_export_item_settings)
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
        self.grp_cover_var.trace("w", self.save_export_item_settings)
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
    def _disable_export_settings(self):
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            for child in tab.winfo_children():
                try: child.configure(state=tk.DISABLED)
                except: pass
                
        # Явно отключаем вложенные элементы
        try:
            self.ent_pause.configure(state=tk.DISABLED)
            self.btn_mass_apply_basic.configure(state=tk.DISABLED) # ИСПРАВЛЕНО
            self.btn_apply_to_group_files.configure(state=tk.DISABLED)
            self.btn_apply_to_parent.configure(state=tk.DISABLED)
            self.btn_apply_to_selected.configure(state=tk.DISABLED)
            self.btn_apply_to_all.configure(state=tk.DISABLED)
        except: pass

    def _enable_export_settings(self, item):
        is_group = item in self.export_groups
        parent = self.export_tree.parent(item)
        is_group_file = (not is_group) and bool(parent) # Файл внутри группы
        
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            for child in tab.winfo_children():
                try: child.configure(state=tk.NORMAL)
                except: pass
                
        # Явно включаем общие элементы
        try:
            self.ent_pause.configure(state=tk.NORMAL)
            self.btn_apply_to_selected.configure(state=tk.NORMAL)
            self.btn_apply_to_all.configure(state=tk.NORMAL)
        except: pass
        
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

    def on_export_tree_select(self, event):
        selected = self.export_tree.selection()
        if not selected:
            self.current_selected_export_item = None
            self._disable_export_settings()
            return
            
        item = selected[0]
        self._is_updating_ui = True 
        self.current_selected_export_item = item
        
        # Передаем сам item, чтобы функция поняла, файл это, группа или корень
        self._enable_export_settings(item)
        
        is_group = item in self.export_groups
        settings = self.export_groups.get(item) if is_group else self.export_files.get(item)
        if not settings: 
            self._is_updating_ui = False
            return
        
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
            
        self._is_updating_ui = False

    def save_export_item_settings(self, *args):
        if getattr(self, '_is_updating_ui', False): return 
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
            
        messagebox.showinfo("Успех", f"Пауза {val} мс применена ко всем группам и сохранена как значение по умолчанию!")

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
        if not self.current_selected_export_item or self.current_selected_export_item not in self.export_groups:
            messagebox.showwarning("Внимание", "Выберите группу-эталон, настройки которой вы хотите применить к остальным.")
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
            try: val_pause = self.grp_pause_var.get()
            except: val_pause = 1000
            
            for g_id, g_data in self.export_groups.items():
                if var_merge.get(): g_data["merge"] = val_merge
                if var_subfolder.get(): g_data["subfolder"] = val_subfolder
                if var_pause.get(): g_data["pause"] = val_pause
                
            if var_save_default.get():
                if var_pause.get(): self.config["default_group_pause"] = val_pause
                self.save_settings()
                
            dialog.destroy()
            messagebox.showinfo("Успех", "Настройки успешно применены ко всем группам!")
            
        ttk.Button(dialog, text="Применить", command=apply_changes).pack(pady=15)
        
        # 👈 2. Проявляем окно уже готовым и отцентрированным в самом конце!
        self._center_popup(dialog, 350, 250)
        
        def apply_changes():
            val_merge = self.grp_merge_var.get()
            val_subfolder = self.grp_subfolder_var.get()
            try: val_pause = self.grp_pause_var.get()
            except: val_pause = 1000
            
            for g_id, g_data in self.export_groups.items():
                if var_merge.get(): g_data["merge"] = val_merge
                if var_subfolder.get(): g_data["subfolder"] = val_subfolder
                if var_pause.get(): g_data["pause"] = val_pause
                
            if var_save_default.get():
                if var_pause.get(): self.config["default_group_pause"] = val_pause
                # Можно добавить сохранение дефолтов merge/subfolder в config, если нужно
                self.save_settings()
                
            dialog.destroy()
            messagebox.showinfo("Успех", "Настройки успешно применены ко всем группам!")
            
        ttk.Button(dialog, text="Применить", command=apply_changes).pack(pady=15)
    def apply_tags_mass(self, scope="group_files"):
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
            messagebox.showinfo("Успех", "Теги применены ко всем выделенным элементам!")
            
        elif scope == "parent_group" and not is_group:
            if parent_g_id in self.export_groups:
                apply_to_item(parent_g_id)
            messagebox.showinfo("Успех", "Теги скопированы в родительскую группу!")
            
        elif scope == "group_files":
            if parent_g_id:
                for f_id in self.export_tree.get_children(parent_g_id):
                    apply_to_item(f_id)
            messagebox.showinfo("Успех", "Теги применены ко всем файлам в группе!")
            
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
            messagebox.showinfo("Успех", "Теги применены абсолютно ко всем группам и файлам!")

    def move_export_item(self, direction):
        selected = self.export_tree.selection()
        if not selected: return
        for item in selected:
            parent = self.export_tree.parent(item)
            idx = self.export_tree.index(item)
            self.export_tree.move(item, parent, idx + direction)

    def group_selected_into_new(self):
        """Забирает выделенные файлы из любых мест и переносит в новую чистую группу"""
        selected_items = self.export_tree.selection()
        files_to_move = [item for item in selected_items if item in self.export_files]
        
        if not files_to_move:
            messagebox.showwarning("Внимание", "Выделите аудиофайлы в списке для объединения в группу.")
            return

        new_g_id = self.add_export_group()
        
        for f_id in files_to_move:
            old_parent = self.export_tree.parent(f_id)
            self.export_tree.move(f_id, new_g_id, tk.END)
            if old_parent:
                self.update_group_duration(old_parent)

        self.update_group_duration(new_g_id)
        self.export_tree.selection_set(new_g_id)
    
    def add_export_group(self, name=None):
        g_id = f"group_{uuid.uuid4().hex[:8]}"
        
        if not name:
            template = self.settings_vars["default_group_name"].get()
            
            # Определяем стартовый номер (например "{num:0}" -> 0, "{num}" -> 1)
            match_start = re.search(r'\{num:(\d+)\}', template)
            start_index = int(match_start.group(1)) if match_start else 1
            
            num = start_index
            while True:
                # Заменяем как {num:0}, так и {num}
                g_name = re.sub(r'\{num(?::\d+)?\}', str(num), template)
                if not any(g["name"] == g_name for g in self.export_groups.values()):
                    break
                num += 1
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
        """Блокирует или разблокирует кнопки панели экспорта"""
        if hasattr(self, 'export_mid_frame'):
            for child in self.export_mid_frame.winfo_children():
                try: child.configure(state=state)
                except: pass

    def add_export_files(self, files=None, target_group=None):
        if getattr(self, '_export_lock', False): return
        
        if files is None:
            # Берём последний зафиксированный путь
            last_dir = self.config.get("last_browse_dir", "")
            init_dir = last_dir if last_dir and os.path.exists(last_dir) else None
            
            files = filedialog.askopenfilenames(
                initialdir=init_dir,
                filetypes=[("Audio Files", "*.mp3 *.wav *.ogg")]
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

            self.lbl_export_status.config(text="Чтение тегов и извлечение обложек...", foreground=self.get_status_color("warning"))
            
            # === ФОНОВЫЙ ПОТОК ===
            def run_import():
                try:
                    added_count = 0
                    total_files = len(files)
                    batch_data = [] # Накопитель для пакетной отрисовки
                    
                    for i, f in enumerate(files):
                        if f in existing_paths: continue
                            
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
                                
                                self.lbl_export_status.config(text=f"Добавлено {curr}/{total_files}...", foreground=self.get_status_color("warning"))
                            
                            self.root.after(0, update_ui, batch_data.copy(), i+1)
                            batch_data.clear() # Очищаем накопитель для следующей пачки
                        
                    # Финализация
                    def finish_ui():
                        if target_group != "":
                            self.update_group_duration(target_group)
                        
                        self.update_total_export_duration()
                        
                        if added_count == 0:
                            self.lbl_export_status.config(text="Файлы уже присутствуют.", foreground=self.get_status_color("info"))
                        else:
                            self.lbl_export_status.config(text="Ожидание...", foreground=self.get_status_color("info"))
                            
                        self._export_lock = False
                        self._set_export_ui_state(tk.NORMAL)
                        
                    self.root.after(0, finish_ui)
                    
                except Exception as e:
                    logging.error(f"Ошибка при добавлении файлов: {e}")
                    def fail_ui():
                        self.lbl_export_status.config(text="Ошибка при добавлении!", foreground=self.get_status_color("error"))
                        self._export_lock = False
                        self._set_export_ui_state(tk.NORMAL)
                    self.root.after(0, fail_ui)

            threading.Thread(target=run_import, daemon=True).start()
            
        except Exception as e:
            logging.error(f"Ошибка инициализации импорта: {e}")
            self._export_lock = False
            self._set_export_ui_state(tk.NORMAL)

    def add_export_folder(self):
        if getattr(self, '_export_lock', False):
             messagebox.showwarning("Занято", "Дождитесь окончания предыдущего импорта файлов.")
             return
             
        # Берём последний зафиксированный путь
        last_dir = self.config.get("last_browse_dir", "")
        init_dir = last_dir if last_dir and os.path.exists(last_dir) else None
        
        folder = filedialog.askdirectory(initialdir=init_dir)
        if not folder: return
        
        # Запоминаем родительскую папку выбранной директории
        self.config["last_browse_dir"] = str(Path(folder).parent)
        self.save_settings()
        
        create_group = messagebox.askyesno("Добавление папки", f"Создать отдельную группу для папки '{Path(folder).name}'?\n\nДа - создать группу\nНет - добавить файлы в корень (или текущую группу)")
        
        files = sorted([str(p) for p in Path(folder).glob("*.*") if p.suffix.lower() in ['.mp3', '.wav', '.ogg']], key=self._natural_sort_key)
        if not files:
            messagebox.showinfo("Пусто", "В папке нет аудиофайлов.")
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
            
            tags = {k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()}
            duration = float(data.get("format", {}).get("duration", 0.0))
            
            cover_path = ""
            # Ищем видеопоток (в аудиофайлах это обложка)
            has_cover = any(s.get("codec_type") == "video" for s in data.get("streams", []))
            if has_cover:
                covers_dir = APP_DATA_DIR / "covers"
                covers_dir.mkdir(exist_ok=True)
                cover_file = covers_dir / f"cover_{uuid.uuid4().hex[:8]}.jpg"
                
                # Извлекаем 1 кадр обложки
                ffmpeg_cmd = [get_ffmpeg_path(), "-y", "-i", filepath, "-an", "-vframes", "1", str(cover_file)]
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo)
                
                if cover_file.exists():
                    cover_path = str(cover_file.resolve())

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

    def auto_split_export(self):
        all_files = []
        for g_id in self.export_tree.get_children():
            for f_id in self.export_tree.get_children(g_id):
                all_files.append(f_id)
                
        if not all_files:
            messagebox.showinfo("Пусто", "Сначала добавьте аудиофайлы.")
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
                limit_sec = limit_var.get() * 60
            except tk.TclError:
                messagebox.showerror("Ошибка", "Введите корректное число минут!")
                return
            
            template = template_var.get()
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
            
            groups_data = []
            current_group = []
            current_dur = 0
            for f_id in all_files:
                dur = file_durs[f_id]
                if not current_group or (current_dur + dur > limit_sec and current_dur > 0):
                    current_group = []
                    groups_data.append(current_group)
                    current_dur = 0
                current_group.append(f_id)
                current_dur += dur
                
            if not groups_data: return
            
            pad = max(2, len(str(len(groups_data))))
            
            for item in self.export_tree.get_children(): 
                self.export_tree.delete(item)
            self.export_groups.clear()
            
            # Определяем стартовый номер
            match_start = re.search(r'\{num:(\d+)\}', template)
            start_index = int(match_start.group(1)) if match_start else 1
            
            for idx, group_files in enumerate(groups_data, 0):
                current_num = str(start_index + idx).zfill(pad)
                g_name = re.sub(r'\{num(?::\d+)?\}', current_num, template)
                
                g_id = self.add_export_group(name=g_name)
                for f_id in group_files:
                    title = self.export_files[f_id]["title"]
                    self.export_tree.insert(g_id, tk.END, iid=f_id, text=title, values=(self.format_duration(file_durs[f_id]),))
                self.update_group_duration(g_id)
            
        ttk.Button(dialog, text="Разбить", command=do_split).pack(pady=10)
        
        self._center_popup(dialog, 350, 220)
        
    # --- Процесс Экспорта ---
    def stop_export_process(self):
        self.is_export_stopped = True
        self.btn_export_stop.config(state=tk.DISABLED)
        self.lbl_export_status.config(text="Остановка сборки (ожидание завершения текущего файла)...", foreground=self.get_status_color("warning"))

    def _update_file_tags_inplace(self, fp, f_tags, cov, title_for_status):
        """Обновляет теги файла прямо в его исходной папке на диске без конвертации"""
        self.root.after(0, lambda f=title_for_status: self.lbl_export_status.config(text=f"Обновление тегов: {f}...", foreground=self.get_status_color("warning")))

        temp_file = Path(fp).with_suffix(f".tmp_{uuid.uuid4().hex[:6]}{Path(fp).suffix}")
        cmd = [get_ffmpeg_path(), "-y", "-i", str(fp)]

        ext = Path(fp).suffix.lower()
        if cov and os.path.exists(cov):
            cmd.extend(["-i", str(cov), "-map", "0:a", "-map", "1:v"])
            if ext == ".mp3":
                cmd.extend(["-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3", "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"])
            else:
                cmd.extend(["-c:a", "copy", "-c:v", "copy", "-disposition:v", "attached_pic"])
        else:
            cmd.extend(["-c", "copy"])

        if f_tags:
            for k, v in f_tags.items():
                if v: cmd.extend(["-metadata", f"{k}={v}"])

        cmd.append(str(temp_file))

        startupinfo = None
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
        if res.returncode == 0 and temp_file.exists():
            try:
                shutil.move(str(temp_file), str(fp))
            except Exception as e_m:
                logging.error(f"Не удалось заменить файл {fp}: {e_m}")
                if temp_file.exists(): os.remove(temp_file)
        else:
            err_log = res.stderr.decode('utf-8', errors='ignore') if res.stderr else "Неизвестная ошибка"
            logging.error(f"Ошибка FFmpeg при обновлении тегов {fp}:\n{err_log}")
            if temp_file.exists(): os.remove(temp_file)
    
    def start_export_process(self):
        items = self.export_tree.get_children()
        if not items:
            messagebox.showwarning("Пусто", "Список пуст. Добавьте аудиофайлы или группы.")
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
            messagebox.showwarning("Нет файлов", "Добавьте аудиофайлы перед началом сборки.")
            return
        
        # ИСПРАВЛЕНИЕ: Если включено "Только теги", игнорируем пустую папку!
        tags_only = self.export_tags_only_var.get()
        out_dir_str = self.export_outdir_var.get().strip()
        
        if not tags_only and not out_dir_str:
            messagebox.showwarning("Папка не выбрана", "Пожалуйста, укажите папку для сохранения результатов экспорта.")
            chosen = filedialog.askdirectory()
            if chosen:
                self.export_outdir_var.set(chosen)
                out_dir_str = chosen
                self.config["export_dir"] = chosen
                self.save_settings()
            else:
                return

        out_dir = Path(out_dir_str) if out_dir_str else None
        if out_dir and not tags_only:
            out_dir.mkdir(parents=True, exist_ok=True)

        self.save_settings() 
        self.btn_export_start.config(state=tk.DISABLED)
        self.btn_export_stop.config(state=tk.NORMAL)
        self.export_progress['value'] = 0
        self.is_export_stopped = False
        
        def run_export():
            try:
                total_files = len(self.export_files)
                processed_files = 0
                tags_only = self.export_tags_only_var.get()
                
                apply_fx = self.export_apply_fx_var.get()
                if apply_fx and not tags_only:
                    sp = float(self.exp_speed_var.get())
                    pt = float(self.exp_pitch_var.get())
                    ec = bool(self.exp_echo_var.get())
                    ed = int(self.exp_delay_var.get())
                    ey = float(self.exp_decay_var.get())
                else:
                    sp, pt, ec, ed, ey = 1.0, 1.0, False, 300, 0.3
                    
                fmt = self.export_fmt_var.get()
                bitrate = self.export_bitrate_var.get()
                
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
                        if item_id in self.export_groups:
                            g_set = self.export_groups[item_id]
                            g_tags = build_tags(g_set)
                            files = self.export_tree.get_children(item_id)

                            for f_id in files:
                                if self.is_export_stopped: break
                                f_set = self.export_files[f_id]
                                fp = f_set["path"] # Взяли ТОЧНЫЙ исходный путь файла!

                                f_tags = build_tags(f_set)
                                for k in ["artist", "album", "album_artist", "genre", "composer", "date"]:
                                    if k not in f_tags and g_tags.get(k):
                                        f_tags[k] = g_tags[k]
                                cov = f_set.get("cover") or g_set.get("cover")

                                self._update_file_tags_inplace(fp, f_tags, cov, f_set.get("title", ""))

                                processed_files += 1
                                pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                                self.root.after(0, lambda p=pct: self.export_progress.config(value=p))

                        # ЭТО ОДИНОЧНЫЙ ФАЙЛ
                        elif item_id in self.export_files:
                            f_set = self.export_files[item_id]
                            fp = f_set["path"] # Взяли ТОЧНЫЙ исходный путь файла!

                            f_tags = build_tags(f_set)
                            cov = f_set.get("cover")

                            self._update_file_tags_inplace(fp, f_tags, cov, f_set.get("title", ""))

                            processed_files += 1
                            pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                            self.root.after(0, lambda p=pct: self.export_progress.config(value=p))

                # =========================================================================
                # РЕЖИМ 2: ПОЛНЫЙ ЭКСПОРТ (Конвертация с сохранением в Папку Экспорта)
                # =========================================================================
                else:
                    for item_idx, item_id in enumerate(items):
                        if self.is_export_stopped: break
                        
                        if item_id in self.export_groups:
                            g_id = item_id
                            g_set = self.export_groups[g_id]
                            g_name = g_set["name"]
                            files = self.export_tree.get_children(g_id)
                            if not files: continue
                            
                            self.root.after(0, lambda n=g_name: self.lbl_export_status.config(text=f"Обработка: {n}...", foreground=self.get_status_color("text")))

                            if g_set["merge"]:
                                final_audio = AudioSegment.empty()
                                pause_ms = g_set["pause"]
                                if apply_fx and sp != 1.0: pause_ms = int(pause_ms / sp)
                                pause_seg = AudioSegment.silent(duration=pause_ms)

                                first_f_set = self.export_files.get(files[0], {})
                                for key in ["artist", "album", "album_artist", "genre", "composer", "year", "cover"]:
                                    if not g_set.get(key) and first_f_set.get(key):
                                        g_set[key] = first_f_set[key]
                                
                                for i, f_id in enumerate(files):
                                    if self.is_export_stopped: break
                                    fp = self.export_files[f_id]["path"]
                                    final_audio += AudioSegment.from_file(fp)
                                    if i < len(files) - 1 and pause_ms > 0: final_audio += pause_seg
                                    
                                    processed_files += 1
                                    pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                                    self.root.after(0, lambda p=pct: self.export_progress.config(value=p))
                                
                                if self.is_export_stopped: break
                                    
                                if apply_fx:
                                    self.root.after(0, lambda: self.lbl_export_status.config(text=f"Применение эффектов и сохранение {g_name}...", foreground=self.get_status_color("warning")))
                                    final_audio = AudioEffects.apply_effects(final_audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                                else:
                                    self.root.after(0, lambda: self.lbl_export_status.config(text=f"Сохранение {g_name}...", foreground=self.get_status_color("warning")))
                                
                                export_kwargs = {"format": fmt}
                                if fmt == "mp3": export_kwargs["bitrate"] = bitrate
                                
                                g_set["title"] = g_name
                                tags = build_tags(g_set)
                                if tags: export_kwargs["tags"] = tags
                                if g_set.get("cover") and os.path.exists(g_set["cover"]): export_kwargs["cover"] = g_set["cover"]
                                    
                                out_file = out_dir / f"{g_name}.{fmt}"
                                final_audio.export(out_file, **export_kwargs)
                                
                            else:
                                target_dir = out_dir / g_name if g_set.get("subfolder") else out_dir
                                target_dir.mkdir(exist_ok=True)
                                
                                for i, f_id in enumerate(files):
                                    if self.is_export_stopped: break
                                    
                                    f_set = self.export_files[f_id]
                                    fp = f_set["path"]
                                    
                                    f_tags = build_tags(f_set)
                                    g_tags = build_tags(g_set)
                                    for k in ["artist", "album", "album_artist", "genre", "composer", "date"]:
                                        if k not in f_tags and g_tags.get(k):
                                            f_tags[k] = g_tags[k]
                                            
                                    cov = f_set.get("cover") or g_set.get("cover")
                                    
                                    self.root.after(0, lambda f=f_set['title']: self.lbl_export_status.config(text=f"Конвертация: {f}...", foreground=self.get_status_color("warning")))
                                    audio = AudioSegment.from_file(fp)
                                    
                                    if apply_fx:
                                        audio = AudioEffects.apply_effects(audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                                    
                                    export_kwargs = {"format": fmt}
                                    if fmt == "mp3": export_kwargs["bitrate"] = bitrate
                                    
                                    if f_tags: export_kwargs["tags"] = f_tags
                                    if cov and os.path.exists(cov): export_kwargs["cover"] = cov
                                    
                                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', f_set["title"])
                                    out_file = target_dir / f"{safe_name}.{fmt}"
                                    audio.export(out_file, **export_kwargs)

                                    processed_files += 1
                                    pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                                    self.root.after(0, lambda p=pct: self.export_progress.config(value=p))

                        elif item_id in self.export_files:
                            f_id = item_id
                            f_set = self.export_files[f_id]
                            fp = f_set["path"]
                            
                            f_tags = build_tags(f_set)
                            cov = f_set.get("cover")
                            
                            self.root.after(0, lambda f=f_set['title']: self.lbl_export_status.config(text=f"Конвертация: {f}...", foreground=self.get_status_color("text")))
                            audio = AudioSegment.from_file(fp)
                            
                            if apply_fx:
                                audio = AudioEffects.apply_effects(audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                            
                            export_kwargs = {"format": fmt}
                            if fmt == "mp3": export_kwargs["bitrate"] = bitrate
                            
                            if f_tags: export_kwargs["tags"] = f_tags
                            if cov and os.path.exists(cov): export_kwargs["cover"] = cov
                            
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', f_set["title"])
                            out_file = out_dir / f"{safe_name}.{fmt}"
                            audio.export(out_file, **export_kwargs)
                            
                            processed_files += 1
                            pct = int((processed_files / total_files) * 100) if total_files > 0 else 0
                            self.root.after(0, lambda p=pct: self.export_progress.config(value=p))

                if self.is_export_stopped:
                    self.root.after(0, lambda: self.lbl_export_status.config(text="Сборка прервана!", foreground=self.get_status_color("error")))
                    self.root.after(0, lambda: messagebox.showwarning("Остановлено", "Процесс сборки был прерван пользователем."))
                else:
                    msg = "Теги успешно обновлены в исходных файлах!" if tags_only else f"Сборка успешно завершена!\nСохранено в: {out_dir}"
                    self.root.after(0, lambda: self.lbl_export_status.config(text="Готово!", foreground=self.get_status_color("success")))
                    self.root.after(0, lambda m=msg: messagebox.showinfo("Успех", m))
                
            except Exception as e:
                logging.error(f"Ошибка сборки: {e}")
                self.root.after(0, lambda: self.lbl_export_status.config(text="Ошибка!", foreground=self.get_status_color("error")))
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Произошла ошибка при сборке:\n{e}"))
            finally:
                self.root.after(0, lambda: self.btn_export_start.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_export_stop.config(state=tk.DISABLED))

        threading.Thread(target=run_export, daemon=True).start()

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
        ttk.Button(btn_frame, text="💾 Сохранить", command=lambda: self.save_settings(show_popup=True)).pack(side=tk.LEFT, padx=5)
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
        tab_effects = ttk.Frame(set_notebook, padding=10)
        tab_output = ttk.Frame(set_notebook, padding=10)
        
        set_notebook.add(tab_api, text="API и Лимиты")
        set_notebook.add(tab_folders, text="Папки")
        set_notebook.add(tab_pauses, text="Паузы и Разделители")
        set_notebook.add(tab_cache, text="Обработка и Кэш")
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
        ttk.Separator(tab_api, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)
        add_entry(tab_api, "Макс. запросов API:", "api_max_requests", 4, tk.IntVar)
        add_entry(tab_api, "Окно времени (сек):", "api_time_window", 5, tk.DoubleVar)
        add_entry(tab_api, "Кол-во попыток при ошибке:", "max_retries", 6, tk.IntVar)
        ttk.Separator(tab_api, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)
        add_entry(tab_api, "Параллельных сборок FFmpeg (0 = Макс.):", "max_parallel_encodes", 8, tk.IntVar)

        # 2. Папки
        add_dir_entry(tab_folders, "Папка с текстами:", "input_dir", 0)
        add_dir_entry(tab_folders, "Папка для аудио:", "output_dir", 1)
        add_dir_entry(tab_folders, "Папка для кэша:", "cache_dir", 2)

        # 3. Паузы
        add_entry(tab_pauses, "Начало файла (мс):", "pause_file_start", 0, tk.IntVar)
        add_entry(tab_pauses, "Конец файла (мс):", "pause_file_end", 1, tk.IntVar)
        add_entry(tab_pauses, "Между предложениями (мс):", "pause_sentence", 2, tk.IntVar)
        add_entry(tab_pauses, "Между абзацами (мс):", "pause_paragraph", 3, tk.IntVar)
        add_entry(tab_pauses, "Перед диалогом (мс):", "pause_speech", 4, tk.IntVar)
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
        add_check(tab_cache, "Авто-исправление аббревиатур (И.И. -> И-И)", "auto_abbreviations", 0)
        add_check(tab_cache, "Авто-сокращения (г., ул., ур. -> г, ул, ур)", "auto_short_words", 1)
        add_check(tab_cache, "Авто-обрезка тишины от Silero", "auto_trim_silence", 2)
        add_entry(tab_cache, "Порог тишины (dBFS):", "silence_threshold", 3, tk.DoubleVar)
        ttk.Separator(tab_cache, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)
        add_check(tab_cache, "Включить кэширование", "use_cache", 5)
        add_entry(tab_cache, "Сохранять кэш на диск каждые (фраз):", "cache_save_frequency", 6, tk.IntVar)
        add_check(tab_cache, "Ограничить количество записей (LRU):", "enable_cache_lru", 7)
        add_entry(tab_cache, "Макс. записей в кэше:", "cache_max_entries", 8, tk.IntVar)
        add_check(tab_cache, "Удалять старые записи по времени (TTL):", "enable_cache_ttl", 9)
        add_entry(tab_cache, "Время жизни кэша (часов):", "cache_ttl_hours", 10, tk.DoubleVar)

        # 5. Эффекты
        ttk.Label(tab_effects, text="Эти эффекты применяются к аудио ПОСЛЕ генерации (без затрат API).", font=("", 9, "italic"), foreground="gray").grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        
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
        self.lbl_delay_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_echo_delay'].get()}мс", width=5)
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

        # 6. Вывод и Теги
        add_combobox(tab_output, "Режим синтеза:", "synthesis_mode", 0, ["sentence", "paragraph", "full"])
        add_combobox(tab_output, "Формат аудио:", "output_format", 1, ["mp3", "wav", "ogg"])
        add_combobox(tab_output, "Битрейт (для mp3):", "output_bitrate", 2, ["64k", "128k", "192k", "256k", "320k"])
        ttk.Separator(tab_output, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(tab_output, text="Теги ID3 (для mp3/ogg):").grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=5)
        
        tags_frame = ttk.Frame(tab_output)
        tags_frame.grid(row=5, column=0, columnspan=2, sticky="ew", padx=5)
        
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

        for tab in (tab_api, tab_folders, tab_pauses, tab_cache, tab_effects, tab_output):
            tab.columnconfigure(1, weight=1)

        self.set_ui_from_config()

    # --- Вкладка "Глоссарий" ---
    def setup_glossary_tab(self):
        frame = ttk.Frame(self.tab_glossary, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        add_frame = ttk.LabelFrame(frame, text="Добавить правило", padding=10)
        add_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.glos_type = tk.StringVar(value="accent")
        ttk.Radiobutton(add_frame, text="Ударение (+)", variable=self.glos_type, value="accent", command=self.toggle_glos_fields).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(add_frame, text="Замена термина", variable=self.glos_type, value="term", command=self.toggle_glos_fields).grid(row=0, column=1, sticky=tk.W)
        ttk.Radiobutton(add_frame, text="RegEx (Паттерн)", variable=self.glos_type, value="regex", command=self.toggle_glos_fields).grid(row=0, column=2, sticky=tk.W)
        
        self.glos_strict = tk.BooleanVar(value=False)
        ttk.Checkbutton(add_frame, text="Чувствительно к регистру (не для RegEx)", variable=self.glos_strict).grid(row=0, column=3, padx=15, sticky=tk.W)
        
        # Поля раположены СТРОГО друг под другом
        self.lbl_glos_word1 = ttk.Label(add_frame, text="Слово (с '+' или исходное):")
        self.lbl_glos_word1.grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(5,0))
        self.glos_word1 = tk.StringVar()
        ttk.Entry(add_frame, textvariable=self.glos_word1).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
        
        self.lbl_glos_word2 = ttk.Label(add_frame, text="Замена:")
        self.glos_word2 = tk.StringVar()
        self.ent_glos_word2 = ttk.Entry(add_frame, textvariable=self.glos_word2)
        
        ttk.Button(add_frame, text="➕ Добавить в JSON", command=self.add_glossary_rule).grid(row=5, column=0, columnspan=4, pady=10)
        
        self.toggle_glos_fields()

        # 1. Сначала пакуем нижние кнопки и прибиваем их ко дну!
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))
        ttk.Button(btn_frame, text="💾 Сохранить файл", command=self.save_glossary_ui).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="🔄 Перезагрузить", command=self.load_glossary_ui).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Импорт", command=self.import_glossary).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="📤 Экспорт", command=self.export_glossary).pack(side=tk.RIGHT, padx=5)

        # 2. Затем пакуем панель с выбором шрифта
        lbl_frame = ttk.Frame(frame)
        lbl_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(lbl_frame, text="Редактор glossary.json:").pack(side=tk.LEFT)
        ttk.Label(lbl_frame, text="Шрифт:").pack(side=tk.RIGHT, padx=5)
        font_cb = ttk.Combobox(lbl_frame, textvariable=self.font_size_var, values=[10, 12, 14, 16, 18, 20, 24], state="readonly", width=5)
        font_cb.pack(side=tk.RIGHT)
        font_cb.bind("<<ComboboxSelected>>", lambda e: self.root.after(10, self.update_fonts))
        
        # 3. В самом конце пакуем текстовое поле, чтобы оно сжималось/растягивалось
        self.txt_glossary = tk.Text(frame, wrap=tk.WORD, font=("Courier", self.font_size_var.get()), undo=True, maxundo=50)
        self.txt_glossary.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)

        self.load_glossary_ui()

    def toggle_glos_fields(self):
        gtype = self.glos_type.get()
        if gtype == "term":
            self.lbl_glos_word1.config(text="Исходное слово:")
            self.lbl_glos_word2.grid(row=3, column=0, columnspan=4, sticky=tk.W)
            self.ent_glos_word2.grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
        elif gtype == "regex":
            self.lbl_glos_word1.config(text="Регулярное выражение:")
            self.lbl_glos_word2.grid(row=3, column=0, columnspan=4, sticky=tk.W)
            self.ent_glos_word2.grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=(0,5))
        else:
            self.lbl_glos_word1.config(text="Слово (с '+' или исходное):")
            self.lbl_glos_word2.grid_remove()
            self.ent_glos_word2.grid_remove()

    def add_glossary_rule(self):
        w1 = self.glos_word1.get().strip()
        w2 = self.glos_word2.get() # Разрешаем пустоту
        strict = self.glos_strict.get()
        gtype = self.glos_type.get()
        
        if not w1: return 
        
        try:
            content = self.txt_glossary.get(1.0, tk.END).strip()
            data = json.loads(content) if content else {"accents_ignore_case": [], "accents_strict_case": [], "terms_ignore_case": {}, "terms_strict_case": {}, "regex_rules": []}
            
            if gtype == "accent":
                if strict: data.setdefault("accents_strict_case", []).append(w1)
                else: data.setdefault("accents_ignore_case", []).append(w1)
            elif gtype == "term":
                if strict: data.setdefault("terms_strict_case", {})[w1] = w2
                else: data.setdefault("terms_ignore_case", {})[w1] = w2
            elif gtype == "regex":
                data.setdefault("regex_rules", []).append({"pattern": w1, "repl": w2})
                
            self.txt_glossary.delete(1.0, tk.END)
            self.txt_glossary.insert(tk.END, json.dumps(data, indent=4, ensure_ascii=False))
            self.glos_word1.set("")
            self.glos_word2.set("")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить JSON: {e}")

    def load_glossary_ui(self):
        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        cache_dir.mkdir(exist_ok=True)
        path = cache_dir / "glossary.json"
        self.config["glossary_path"] = str(path)
        
        self.txt_glossary.delete(1.0, tk.END)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                self.txt_glossary.insert(tk.END, f.read())
        else:
            default_glos = {"accents_ignore_case": [], "accents_strict_case": [], "terms_ignore_case": {}, "terms_strict_case": {}, "regex_rules": []}
            self.txt_glossary.insert(tk.END, json.dumps(default_glos, indent=4, ensure_ascii=False))

    def save_glossary_ui(self):
        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        cache_dir.mkdir(exist_ok=True)
        path = cache_dir / "glossary.json"
        content = self.txt_glossary.get(1.0, tk.END).strip()
        try:
            parsed = json.loads(content)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(parsed, f, indent=4, ensure_ascii=False)
            messagebox.showinfo("Успех", "Глоссарий сохранен!")
        except Exception as e:
            messagebox.showerror("Ошибка JSON", f"Неверный формат JSON:\n{e}")

# --- Вкладка "Кэш" ---
    def setup_cache_tab(self):
        frame = ttk.Frame(self.tab_cache, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        search_frame = ttk.Frame(frame)
        search_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(search_frame, text="Поиск по тексту:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda name, index, mode: self.filter_cache())
        ttk.Entry(search_frame, textvariable=self.search_var, width=50).pack(side=tk.LEFT, padx=5)
        
        self.lbl_cache_count = ttk.Label(search_frame, text="Всего записей: 0", foreground=self.get_status_color("info"))
        self.lbl_cache_count.pack(side=tk.RIGHT, padx=5)
        
        # --- ИЗМЕНЕНИЕ: Системно-зависимая подсказка ---
        ctrl_key = "Command (⌘)" if sys.platform == "darwin" else "Ctrl"
        ttk.Label(frame, text=f"💡 Вы можете выделять несколько строк мышкой с зажатым {ctrl_key} или Shift", font=("", 8, "italic"), foreground="gray").pack(anchor=tk.W, pady=(0,5))
        # -----------------------------------------------

        columns = ("hash", "text", "speaker", "uses")
        # selectmode="extended" разрешает выделение множества строк
        self.cache_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        if sys.platform == "darwin":
            self.cache_tree.bind("<Command-Button-1>", lambda e: self._mac_multiselect(e, self.cache_tree))
        self.cache_tree.heading("hash", text="Хэш")
        self.cache_tree.heading("text", text="Текст")
        self.cache_tree.heading("speaker", text="Спикер")
        self.cache_tree.heading("uses", text="Использований")
        
        self.cache_tree.column("hash", width=80, stretch=False)
        self.cache_tree.column("text", width=500)
        self.cache_tree.column("speaker", width=80, stretch=False)
        self.cache_tree.column("uses", width=100, stretch=False, anchor=tk.CENTER)
        
        self.cache_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.cache_tree.yview)
        self.cache_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.cache_tree.bind("<Double-1>", self.on_cache_double_click)
        
        btn_frame = ttk.Frame(self.tab_cache)
        btn_frame.pack(fill=tk.X, pady=5, side=tk.BOTTOM)
        ttk.Button(btn_frame, text="🗑 Удалить выбранные", command=self.delete_selected_cache).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="🧹 Оптимизировать кэш", command=self.optimize_cache).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔥 Очистить ВСЁ", command=self.clear_entire_cache).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.load_cache_ui).pack(side=tk.LEFT, padx=5)
        
        self.del_after_zip = tk.BooleanVar(value=False)
        ttk.Button(btn_frame, text="📦 Создать ZIP архив", command=self.archive_cache).pack(side=tk.RIGHT)
        ttk.Checkbutton(btn_frame, text="Удалить папку после", variable=self.del_after_zip).pack(side=tk.RIGHT, padx=10)

    def on_cache_double_click(self, event):
        selected = self.cache_tree.selection()
        if not selected: return
        hash_key = selected[0]
        data = self.cache_data.get(hash_key)
        if not data: return
        
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.title(f"Детали кэша: {hash_key}")
        top.transient(self.root)
        
        
        ttk.Label(top, text="Исходный текст:").pack(anchor=tk.W, padx=10, pady=(10,0))
        t1 = tk.Text(top, height=5, wrap=tk.WORD, font=("Arial", self.font_size_var.get()))
        t1.pack(fill=tk.X, padx=10)
        t1.insert(tk.END, data.get("original_text", ""))
        
        ttk.Label(top, text="Нормализованный текст (отправлен в API):").pack(anchor=tk.W, padx=10, pady=(10,0))
        t2 = tk.Text(top, height=5, wrap=tk.WORD, font=("Arial", self.font_size_var.get()))
        t2.pack(fill=tk.X, padx=10)
        t2.insert(tk.END, data.get("normalized_text", ""))
        
        info = f"Спикер: {data.get('speaker', '')}\n"
        info += f"Использований: {data.get('usage_count', 0)}\n"
        info += f"Создано: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('created_at', 0)))}\n"
        ttk.Label(top, text=info, justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=5)
        
        # --- МИНИ-ПАНЕЛЬ ЭФФЕКТОВ ДЛЯ КЭША ---
        fx_frame = ttk.LabelFrame(top, text="Локальные эффекты (только для тестирования)", padding=5)
        fx_frame.pack(fill=tk.X, padx=10, pady=5)
        
        cache_speed_var = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        cache_pitch_var = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        cache_echo_var = tk.BooleanVar(value=self.config.get("fx_echo", False))
        cache_echo_delay_var = tk.IntVar(value=self.config.get("fx_echo_delay", 300))
        cache_echo_decay_var = tk.DoubleVar(value=self.config.get("fx_echo_decay", 0.3))
        
        # Ряд 1: Скорость и Тон
        top_fx = ttk.Frame(fx_frame)
        top_fx.pack(fill=tk.X, pady=2)
        
        ttk.Label(top_fx, text="Скорость:").pack(side=tk.LEFT, padx=5)
        lbl_spd = ttk.Label(top_fx, text=f"{cache_speed_var.get():.1f}x", width=4)
        lbl_spd.pack(side=tk.LEFT)
        sc_spd = ttk.Scale(top_fx, from_=0.5, to_=3.0, variable=cache_speed_var, command=lambda v: lbl_spd.config(text=f"{float(v):.1f}x"))
        sc_spd.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(top_fx, text="Тон:").pack(side=tk.LEFT, padx=5)
        lbl_pch = ttk.Label(top_fx, text=f"{cache_pitch_var.get():.2f}", width=4)
        lbl_pch.pack(side=tk.LEFT)
        sc_pch = ttk.Scale(top_fx, from_=0.5, to_=2.0, variable=cache_pitch_var, command=lambda v: lbl_pch.config(text=f"{float(v):.2f}"))
        sc_pch.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Ряд 2: Эхо
        mid_fx = ttk.Frame(fx_frame)
        mid_fx.pack(fill=tk.X, pady=2)
        
        ttk.Checkbutton(mid_fx, text="Эхо", variable=cache_echo_var).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(mid_fx, text="Задержка:").pack(side=tk.LEFT, padx=(10, 2))
        lbl_delay = ttk.Label(mid_fx, text=f"{cache_echo_delay_var.get()}мс", width=5)
        lbl_delay.pack(side=tk.LEFT)
        sc_delay = ttk.Scale(mid_fx, from_=50, to_=1000, variable=cache_echo_delay_var, command=lambda v: lbl_delay.config(text=f"{int(float(v))}мс"))
        sc_delay.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        ttk.Label(mid_fx, text="Сила:").pack(side=tk.LEFT, padx=(10, 2))
        lbl_decay = ttk.Label(mid_fx, text=f"{cache_echo_decay_var.get():.1f}", width=4)
        lbl_decay.pack(side=tk.LEFT)
        sc_decay = ttk.Scale(mid_fx, from_=0.1, to_=0.8, variable=cache_echo_decay_var, command=lambda v: lbl_decay.config(text=f"{float(v):.1f}"))
        sc_decay.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Кнопка сохранения
        def apply_cache_to_global():
            self.settings_vars["fx_speed"].set(cache_speed_var.get())
            self.settings_vars["fx_pitch"].set(cache_pitch_var.get())
            self.settings_vars["fx_echo"].set(cache_echo_var.get())
            self.settings_vars["fx_echo_delay"].set(cache_echo_delay_var.get())
            self.settings_vars["fx_echo_decay"].set(cache_echo_decay_var.get())
            self.save_settings()
            messagebox.showinfo("Успех", "Эффекты сохранены в глобальные настройки!")
            
        ttk.Button(fx_frame, text="💾 Сделать глобальными", command=apply_cache_to_global).pack(side=tk.RIGHT, padx=5, pady=2)
        # -------------------------------------
        
        filepath = Path(self.config.get("cache_dir", "cache_audio")) / "audio" / data.get("file_name", "")

        self._center_popup(top, 750, 500)
        top.lift()
        top.focus_force()
        
        def play_cache_audio():
            if not os.path.exists(filepath): return
            try:
                seg = AudioSegment.from_file(filepath)
                sp = cache_speed_var.get()
                pt = cache_pitch_var.get()
                ec = cache_echo_var.get()
                ed = cache_echo_delay_var.get()
                ey = cache_echo_decay_var.get()
                
                processed_seg = AudioEffects.apply_effects(seg, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                self.play_audio_segment(processed_seg)
            except Exception as e:
                logging.error(f"Ошибка чтения файла для плеера: {e}")
                
        # --- ОБНОВЛЕННЫЕ КНОПКИ ПЛЕЕРА В КЭШЕ ---
        btn_play = ttk.Button(fx_frame, text="🔊 Слушать", command=play_cache_audio)
        btn_play.pack(side=tk.LEFT, padx=5, pady=2)

        btn_stop_audio = ttk.Button(fx_frame, text="🔇", width=3, command=self.stop_audio_playback)
        btn_stop_audio.pack(side=tk.LEFT, padx=2, pady=2)

    def load_cache_ui(self):
        self.cache_data = {}
        cache_path = Path(self.config.get("cache_dir", "cache_audio")) / "sentence_cache.json"
        bak_path = cache_path.with_suffix(".json.bak")
        
        target_path = cache_path if cache_path.exists() else (bak_path if bak_path.exists() else None)
        
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    self.cache_data = json.load(f)
            except Exception as e:
                logging.error(f"Ошибка загрузки JSON кэша в UI: {e}")
                messagebox.showerror("Ошибка кэша", f"Файл {target_path.name} поврежден.\n\nПрограмма защищена от вылета. Исправьте файл или удалите его для пересоздания.")
                
        self.lbl_cache_count.config(text=f"Всего записей: {len(self.cache_data)}")
        self.filter_cache()

    def filter_cache(self):
        if not hasattr(self, 'cache_data'):
            self.cache_data = {}
        query = self.search_var.get().lower()
        for item in self.cache_tree.get_children():
            self.cache_tree.delete(item)
            
        count = 0
        for h, data in self.cache_data.items():
            text = data.get("original_text", "")
            if query in text.lower() or query in h.lower():
                # --- ИСПРАВЛЕНИЕ: Убираем переносы строк для красивого отображения в таблице ---
                display_text = text.replace("\n", " ↵ ")
                # -------------------------------------------------------------------------------
                self.cache_tree.insert("", tk.END, iid=h, values=(h[:8]+"...", display_text, data.get("speaker", ""), data.get("usage_count", 0)))
                count += 1
                if count > 1000: break

    def delete_selected_cache(self):
        if self.is_synthesis_running():
            messagebox.showwarning("Занято", "Нельзя удалять записи из кэша во время активного синтеза!")
            return
        selected = self.cache_tree.selection()
        if not selected: return
        if not messagebox.askyesno("Удаление", f"Удалить выбранные записи ({len(selected)} шт.) из кэша?"): return
        
        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        for h in selected:
            filename = self.cache_data[h].get("file_name")
            if filename:
                filepath = cache_dir / "audio" / filename
                if filepath.exists(): os.remove(filepath)
            del self.cache_data[h]
            self.cache_tree.delete(h)
            
        with open(cache_dir / "sentence_cache.json", 'w', encoding='utf-8') as f:
            json.dump(self.cache_data, f, ensure_ascii=False, indent=4)
        self.load_cache_ui()
        messagebox.showinfo("Успех", "Записи удалены.")

    def is_synthesis_running(self):
        """Проверяет, запущен ли основной или прямой синтез в данный момент"""
        batch_active = bool(self.processing_thread and self.processing_thread.is_alive())
        direct_active = bool(hasattr(self, 'direct_thread') and self.direct_thread and self.direct_thread.is_alive())
        return batch_active or direct_active

    def optimize_cache(self):
        if self.is_synthesis_running():
            messagebox.showwarning("Занято", "Нельзя оптимизировать кэш во время активного синтеза!")
            return
            
        if not messagebox.askyesno("Оптимизация", "Скрипт просканирует папку с текстами и удалит из кэша все аудиофрагменты, которых нет в текущих текстах.\n\n(Будут сохранены фразы для всех режимов: по предложениям, абзацам и целиком).\n\nПродолжить?"):
            return
            
        self.save_settings()
        processor = TTSProcessor(self.config)
        
        # 1. Создаем модальное окно ожидания
        popup = self._create_wait_popup("Оптимизация", "Сканирование текстов и проверка кэша...\nПожалуйста, подождите.")
        
        def run_opt():
            try:
                txt_files = list(Path(self.config["input_dir"]).glob("*.txt"))
                
                if not txt_files:
                    self.root.after(0, lambda: messagebox.showwarning("Отмена", f"В папке '{self.config['input_dir']}' не найдено текстовых файлов (.txt).\nОптимизация отменена, чтобы защитить кэш."))
                    return

                processor.cache = processor._load_cache()
                if not processor.cache:
                    self.root.after(0, lambda: messagebox.showinfo("Информация", "Кэш пуст."))
                    return

                required_hashes = set()
                errors_occurred = False
                
                for f in txt_files:
                    try:
                        with open(f, 'r', encoding='utf-8') as file: raw_text = file.read()
                        file_hashes = processor.get_all_possible_hashes(raw_text)
                        required_hashes.update(file_hashes)
                    except Exception as e:
                        logging.error(f"Ошибка при сканировании {f.name}: {e}")
                        errors_occurred = True

                if not required_hashes:
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось извлечь хэши из текстов. Оптимизация отменена."))
                    return

                keys_to_delete = [k for k in processor.cache.keys() if k not in required_hashes]
                
                for k in keys_to_delete: 
                    processor._delete_cache_entry(k)
                    
                if keys_to_delete:
                    processor.unsaved_cache_items += len(keys_to_delete)
                    processor._save_cache()
                    msg = f"Оптимизация завершена.\nУдалено неиспользуемых записей: {len(keys_to_delete)}"
                    if errors_occurred: msg += "\n\n(Внимание: во время чтения некоторых файлов возникли ошибки)"
                    self.root.after(0, lambda: messagebox.showinfo("Успех", msg))
                else:
                    self.root.after(0, lambda: messagebox.showinfo("Готово", "Оптимизация завершена. Лишних записей не найдено."))
                    
            finally:
                # 2. Гарантированно закрываем окно ожидания и обновляем UI
                self.root.after(0, popup.destroy)
                self.root.after(0, self.load_cache_ui)
            
        threading.Thread(target=run_opt, daemon=True).start()

    def clear_entire_cache(self):
        """Полная очистка кэша"""
        if self.is_synthesis_running():
            messagebox.showwarning("Занято", "Нельзя полностью очищать кэш во время активного синтеза!")
            return
        if not messagebox.askyesno("🔥 КРИТИЧЕСКОЕ ДЕЙСТВИЕ", "Вы уверены, что хотите ПОЛНОСТЬЮ очистить весь кэш?\nВсе сгенерированные аудиофрагменты будут безвозвратно удалены!", icon="warning"):
            return
            
        cache_dir = Path(self.config.get("cache_dir", "cache_audio"))
        try:
            # Удаляем папку с аудио и пересоздаем ее
            audio_dir = cache_dir / "audio"
            if audio_dir.exists():
                shutil.rmtree(audio_dir)
            audio_dir.mkdir(parents=True, exist_ok=True)
            
            # Очищаем индексный файл
            index_path = cache_dir / "sentence_cache.json"
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
                
            self.load_cache_ui()
            messagebox.showinfo("Готово", "Кэш полностью очищен.")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось очистить кэш:\n{e}")

    def archive_cache(self):
        cache_dir = self.config.get("cache_dir", "cache_audio")
        if not os.path.exists(cache_dir):
            messagebox.showinfo("Пусто", "Папка кэша не существует.")
            return
            
        out_zip = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("ZIP Archive", "*.zip")], initialfile="cache_audio_backup.zip")
        if not out_zip: return
        
        # Создаем окно ожидания
        popup = self._create_wait_popup("Архивация", "Создание ZIP-архива кэша...\nПожалуйста, подождите.")
        
        def run_zip():
            try:
                base_name = out_zip.replace('.zip', '')
                shutil.make_archive(base_name, 'zip', cache_dir)
                if self.del_after_zip.get():
                    shutil.rmtree(cache_dir)
                    Path(cache_dir).mkdir(exist_ok=True)
                
                # Закрываем окно ожидания в главном потоке
                self.root.after(0, popup.destroy)
                self.root.after(0, lambda: messagebox.showinfo("Успех", f"Архив создан:\n{out_zip}"))
                self.root.after(0, self.load_cache_ui)
            except Exception as e:
                self.root.after(0, popup.destroy)
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось создать архив:\n{e}"))
                
        threading.Thread(target=run_zip, daemon=True).start()

    # --- Вкладка "Справка" ---
    def setup_help_tab(self):
        ctrl_frame = ttk.Frame(self.tab_help)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(ctrl_frame, text="Размер шрифта:").pack(side=tk.LEFT)
        font_cb = ttk.Combobox(ctrl_frame, textvariable=self.font_size_var, values=[10, 12, 14, 16, 18, 20, 24], state="readonly", width=5)
        font_cb.pack(side=tk.LEFT, padx=5)
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
        
        help_text = r"""Добро пожаловать в Silero TTS Studio v1.4!

Это профессиональная рабочая среда для генерации аудиокниг, подкастов и озвучки текста с помощью нейросети Silero. Программа разработана с акцентом на бережное отношение к API-лимитам, молниеносное O(1) RAM-кэширование, гибридную постобработку звука и автоматизацию сборки.

====================================================================
🚀 1. БЫСТРЫЙ СТАРТ
====================================================================
1. Перейдите во вкладку "Настройки" -> "API и Лимиты" и введите ваш API Token.
2. Во вкладке "Импорт книг" выберите файл книги (EPUB, FB2, DOCX, TXT) и нажмите "Извлечь и Нарезать". Скрипт автоматически разобьет книгу на главы и сохранит их в папку input_texts.
3. Перейдите во вкладку "Синтез из папки" и нажмите "▶ Старт (Все)".
4. Готовые аудиофайлы появятся в папке output_audio.
5. В процессе работы список файлов автоматически прокручивается. Вы всегда можете вернуться к активному файлу кнопкой "📍 К текущему файлу".

====================================================================
🎙 2. РЕЖИМЫ СИНТЕЗА (SYNTHESIS MODES)
====================================================================
В "Настройках" -> "Вывод и Теги" доступно 3 режима генерации:

• По предложениям (sentence) — [РЕКОМЕНДУЕТСЯ]:
  Текст разбивается на отдельные предложения.
  - Плюсы: Максимальная точность кэширования. Если вы измените или опечатаетесь в одном предложении книги на 500 страниц, программа переозвучит ТОЛЬКО это одно предложение, взяв остальные 99.9% из кэша!
  - Гибкость: Позволяет высчитывать индивидуальные паузы между фразами, прямой речью и перед двоеточиями.

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
• Имя файла: Назовите итоговый трек (по умолчанию direct_output.mp3).
• Чекбоксы управления:
  - [x] Сохранить: Сохраняет аудиофайл на диск в папку вывода.
  - [x] Игнорировать кэш: Принудительно генерирует речь заново через API.
  - [x] Авто-воспроизведение: Мгновенно проигрывает результат через системный плеер.
• Умный плеер: Кнопка "🔊 Слушать" мгновенно прерывает старый трек при повторном нажатии (никакой звуковой каши). Кнопка "🔇" принудительно останавливает любой звук.
• Эффекты: Индивидуальные ползунки скорости, тона и эхо. Нажатие кнопки "💾 Сделать глобальными" мгновенно применяет эти эффекты ко всем настройкам программы.

====================================================================
⏱ 4. ТОНКАЯ НАСТРОЙКА ПАУЗ И РАЗДЕЛИТЕЛЕЙ
====================================================================
Во вкладке "Настройки" -> "Паузы и Разделители" вы можете настроить идеальный ритм повествования (длительность указывается в миллисекундах, 1000 мс = 1 сек):

• Пауза в начале / конце файла: Задает тишину на старте и в самом финише трека (удобно для плееров).
• Между предложениями: Базовая пауза между обычными предложениями внутри абзаца.
• Между абзацами: Пауза при переходе на новую строку текста.
• Перед диалогом (Прямая речь): Автоматически УВЕЛИЧИВАЕТ паузу перед абзацем, если он начинается с тире (—) или кавычек (").
• После двоеточия: Увеличивает паузу перед следующим абзацем, если предыдущий заканчивался на двоеточие (:).
• Символы-разделители (☆☆☆, ***, ###, ---):
  Управление разделителями осуществляется через динамические поля (добавление строчки кнопкой "➕ Добавить", удаление — "❌"). Когда программа встречает указанный символ в тексте, она полностью вырезает его и вставляет на его место чистую тишину заданной длины ("Пауза разделителя").

====================================================================
⚙️ 5. ПОЛНЫЙ ЦИКЛ ОБРАБОТКИ И НОРМАЛИЗАЦИИ ТЕКСТА
====================================================================
Чтобы нейросеть правильно озвучила текст, программа бережно обрабатывает каждую фразу строго в следующем порядке:

1. Математика и плюсы: Математические плюсы (1 + 1) и одиночные плюсы заменяются на слово "плюс". Плюсы внутри и в начале слов (з+амок, +аура) маскируются для защиты ручных ударений.
2. RegEx-правила: Применяются шаблоны замены из Глоссария (ДО разбивки на предложения).
3. Сегментация: Текст разбивается на предложения с помощью библиотеки Razdel.
4. Глоссарий терминов и ударений: Заменяются слова и расставляются плюсы ударений.
5. Авто-аббревиатуры: Превращает "И.И.", "к.п.д." в "И-И", "к-п-д", чтобы нейросеть произносила их по буквам.
6. Авто-сокращения: Убирает точки у слов из 1-3 букв ("г.", "ул.", "ур."), чтобы диктор не делал фальшивую паузу посреди фразы.
7. Нормализация (ru-normalizr): Преобразует числа, даты и числительные в пропись ("10" -> "десять").
8. Защита пунктуации: Если после обработки у предложения пропала финальная точка, программа насильно возвращает её, гарантируя правильную интонацию.
9. Очистка: Удаляются лишние кавычки, скобки и спецсимволы, после чего чистая фраза уходит в API.

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
• Управление шрифтом: В правом верхнем углу расположен выпадающий список размера шрифта (10–24) для комфортного чтения редактора.
• Выборочный импорт/экспорт: При сохранении или загрузке Настроек и Глоссария появляются удобные окна с галочками — вы сами решаете, какие именно группы параметров (папки, эффекты, API) перенести в другой проект.

====================================================================
🎛 8. АУДИОЭФФЕКТЫ И ПОСТОБРАБОТКА (FFMPEG)
====================================================================
Вы можете менять звучание голоса БЕЗ повторных запросов к API и БЕЗ траты лимитов! Эффекты накладываются локально через FFmpeg:

• Скорость (Tempo): Ускорение или замедление речи от 0.5x до 3.0x без изменения высоты голоса.
• Тон (Pitch): Изменение высоты голоса (сделать голос басистее или выше).
• Эхо (Reverb/Delay): Настройка задержки (мс) и затухания (decay). Идеально для мыслей персонажей, воспоминаний или фэнтези-существ.
• Естественное сжатие пауз: При изменении скорости речи паузы между предложениями пропорционально адаптируются, сохраняя живой ритм.

Эффекты можно настраивать во вкладках "Прямой синтез", "Кэш", "Экспорт и Сборка", а также глобально в "Настройках".

====================================================================
🎵 9. ЭКСПОРТ И СБОРКА (АУДИОКНИГИ, ТЕГИ, ОБЛОЖКИ)
====================================================================
Вкладка "Экспорт и Сборка" — это полноценный комбайн для компиляции и тегирования аудиофайлов:

• Файлы в Корне и Группах: Файлы можно добавлять как в древовидные Группы (Тома), так и прямо в корень проекта.
• Расчет Общего Времени: В шапке дерева в реальном времени отображается суммарная длительность всех аудиофайлов (HH:MM:SS).
• Авто-заполнение папки: При добавлении файлов программа сама подставляет папку их расположения в поле экспорта. Единая память путей сохраняется между выбором файлов и папок.
• Натуральная сортировка: Клик по заголовку "Имя ↕" сортирует файлы по-человечески (Глава 2 встает перед Глава 10).
• Фоновый импорт метаданных: Чтение тегов и извлечение встроенных обложек через ffprobe происходит в фоновом потоке пачками по 10 файлов — интерфейс остается живым.
• Перепаковка и Разгруппировка: Выделите файлы и используйте кнопки "📦 В новую группу" или "📤 Разгруппировать" (кнопка перенесена на верхнюю панель для удобства).
• Массовое применение настроек: Кнопка "⚙️ Применить ко всем группам..." открывает диалог, позволяющий в 1 клик применить параметры склейки, подпапок, пауз ко всем томам и сохранить их по умолчанию.
• Склеивание в один трек: Склеивает все файлы группы в один большой аудиофайл с настройкой паузы между ними (в мс).
• Авто-разбивка по времени: Укажите лимит длительности (например, 60 минут), и программа сама разложит файлы по томам с нумерацией.
• [x] Только обновить теги (In-place tagging): Уникальный режим без перекодирования звука! Мгновенно вшивает метаданные и обложки прямо в исходные файлы на диске с нулевой потерей качества.
• Редактор ID3-тегов и Обложек: Установка метаданных и автоматическое извлечение обложек из файлов через ffprobe.
• Умная сетка применения тегов (2х2):
  - [⬇ К файлам группы]: Копирует теги текущей группы на все входящие в нее файлы.
  - [⬆ В род. группу]: Копирует теги с выделенного файла на его родительскую группу.
  - [☑ К выделенным]: Применяет теги ко всем выделенным элементам.
  - [🔄 Ко всем элементам]: Применяет теги абсолютно ко всем группам и файлам.

====================================================================
💾 10. УПРАВЛЕНИЕ КЭШЕМ И БЕЗОПАСНОСТЬ
====================================================================
• O(1) RAM-Архитектура: Чтение из кэша происходит исключительно в оперативной памяти без блокировки жесткого диска. Сборка готовой книги на сотни часов занимает считанные минуты, полностью загружая процессор для параллельного рендера!
• Пропуск готовых файлов: При повторном запуске программа проверяет папку вывода и продолжает работу с того места, где остановилась.
• Раздельное управление (LRU / TTL): В "Настройках" -> "Обработка и Кэш" вы можете независимо включать ограничение по максимальному количеству записей (LRU) или по времени жизни в часах (TTL).
• Просмотр кэша: Двойной клик по строке открывает карточку с исходным и нормализованным текстом, спикером и ползунками тестов.
• Оптимизация кэша: Сканирует файлы в папке с текстами, собирает все хэши (для всех 3 режимов) и безопасно удаляет из кэша устаревшие фразы.
• Блокировка от повреждения: Операции очистки, удаления и оптимизации кэша автоматически блокируются, если в данный момент запущен основной или прямой синтез.
• Архивирование: Возможность упаковать весь кэш в ZIP-архив.

====================================================================
⚡ 11. ЛИМИТЫ, КНОПКИ ОСТАНОВКИ И БЕЗОПАСНОСТЬ
====================================================================
Во вкладке "Настройки" -> "API и Лимиты" вы можете гибко управлять нагрузкой на сеть и процессор:

• Сетевые лимиты API: Настройка частоты запросов защищает ваш токен от блокировки. Лимитер является Глобальным — он синхронизирует запросы между вкладкой папок и Прямым синтезом, гарантируя отсутствие банов.
• Параллельных сборок FFmpeg (Аппаратный лимит CPU):
  - Значение [ 0 ]: Режим максимальной скорости (без ограничений). Задействует 100% ресурсов процессора для мгновенной сборки из кэша.
  - Значение [ 1 ]: "Тихий / Фоновый режим" — [РЕКОМЕНДУЕТСЯ ДЛЯ НОУТБУКОВ]. Файлы собираются строго по очереди один за другим. Процессор и кулеры остаются абсолютно тихими и холодными, а система не лагает.
  - Значения [ 2...N ]: Ручной лимит параллельных потоков кодирования.

• Кнопка "⏹ Стоп": Мягкая остановка. Дожидается завершения текущего запроса, сохраняет кэш и останавливает очередь.
• Кнопка "☠️ Принудительно": Экстренная остановка. Мгновенно разрывает сетевые сокеты HTTP-сессии, гарантируя немедленный останов потока и сброс накопившегося кэша на диск без создания "потоков-зомби".

====================================================================
📁 12. ОСОБЕННОСТИ РАБОТЫ НА РАЗНЫХ ОС
====================================================================
• macOS (.app): Из-за системных ограничений Apple (Gatekeeper) скомпилированное приложение автоматически работает через папку Документы (`~/Documents/SileroTTS_Studio/`).
• Умный буфер обмена (Кроссплатформенный):
  - На macOS гарантирует работу ⌘C, ⌘V, ⌘X, ⌘A, ⌘Z при любой раскладке с авто-декодированием путей Finder (unquote + NFC).
  - На Windows и Linux чинит работу русской раскладки (Ctrl+С, Ctrl+М) и автоматически очищает вставляемые пути от кавычек (Windows 11 "Копировать как путь") и префиксов `file://`.
• Защита NullWriter: Глобальный перехватчик `sys.stdout/stderr` защищает Portable-версии на Windows, macOS и Linux от экстренных вылетов из-за вызовов `print()` в сторонних библиотеках.
• Атомарное выделение (macOS): Клик с зажатой клавишей ⌘ позволяет выделять и снимать выделение со строк в таблицах.
• Portable-режим: При запуске `.py` файла через консоль (на Mac, Windows, Linux) или `.exe` на Windows программа работает в полностью портативном режиме — все рабочие папки создаются строго рядом со скриптом.
• Умные пути (Smart Paths): Программа запоминает последние открытые папки для каждого поля индивидуально. Если поле пустое, диалог вежливо откроет папку текущего проекта. Вам больше не нужно каждый раз прокликивать путь от корня диска!
"""

        self.help_text_widget.insert(tk.END, help_text)
        self.help_text_widget.config(state=tk.DISABLED)

    # --- Логика работы приложения ---
    def on_tab_change(self, event):
        #selected_tab = event.widget.select()
        #if "glossary" in selected_tab.lower(): self.load_glossary_ui()
        #elif "cache" in selected_tab.lower(): self.load_cache_ui()
        pass

    def load_files(self):
        self.save_settings()
        for item in self.tree.get_children(): self.tree.delete(item)
        Path(self.config["input_dir"]).mkdir(exist_ok=True)
        self.txt_files = sorted(list(Path(self.config["input_dir"]).glob("*.txt")), key=lambda x: x.name)
        for f in self.txt_files:
            self.tree.insert("", tk.END, iid=f.name, values=("⏳ В очереди", f.name), tags=('queued',))
        
        # Сброс прогресс-баров
        self.lbl_total_pct.config(text=f"0/{len(self.txt_files)}")
        self.total_progress['value'] = 0
        self.file_progress['value'] = 0
        self.lbl_file_pct.config(text="0%")
        self.lbl_current_text.config(text="Ожидание...", foreground=self.get_status_color("text"))

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
        status_map = {
            "processing": ("🔄 Синтез...", "processing"),
            "encoding": ("⚙️ Сборка аудиофайла...", "processing"), # <-- ВОЗВРАЩЕНО
            "success": ("✅ Готово", "success"),
            "warning": ("⚠️ С ошибками", "warning"),
            "error": ("❌ Ошибка", "error")
        }
        text, tag = status_map.get(status_code, ("?", "queued"))
        self.tree.item(filename, values=(text, filename), tags=(tag,))
        
        if status_code in ("processing", "encoding"):
            self.current_processing_file = filename
            try: self.btn_go_current.config(state=tk.NORMAL)
            except: pass
            
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
    
    def start_processing(self, only_selected=False):
        self.save_settings()
        if not self.config.get("api_token"):
            messagebox.showerror("Ошибка", "Введите API Token во вкладке Настройки!")
            return
            
        # Определяем, какие файлы отправлять на синтез
        items_to_process = self.tree.selection() if only_selected else self.tree.get_children()
        
        if not items_to_process:
            messagebox.showinfo("Пусто", "Нет файлов для обработки. Выделите файлы или обновите список.")
            return

        # Блокируем/разблокируем кнопки
        self.btn_start_all.config(state=tk.DISABLED)
        self.btn_start_sel.config(state=tk.DISABLED)
        self.btn_refresh.config(state=tk.DISABLED)
        self.btn_remove_sel.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_hard_stop.config(state=tk.NORMAL)
        
        # Сброс прогресс-баров перед стартом
        self.total_progress['value'] = 0
        self.file_progress['value'] = 0
        self.lbl_file_pct.config(text="0%")
        self.lbl_total_pct.config(text=f"0/{len(items_to_process)}")
        self.lbl_current_text.config(text="Подготовка...", foreground=self.get_status_color("text"))
        
        self.processor = TTSProcessor(self.config, shared_rate_limiter=self.shared_rate_limiter, error_callback=self.show_critical_error)
        self.processing_thread = threading.Thread(target=self.process_queue, args=(items_to_process,))
        self.processing_thread.start()

    def stop_processing(self):
        if self.processor: self.processor.is_stopped = True
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_current_text.config(text="Остановка (ожидание завершения текущего запроса)...", foreground=self.get_status_color("warning"))

    def hard_stop_processing(self):
        if self.processor: 
            self.processor.stop() # Мгновенно рвет сокеты и сохраняет кэш
        
        # Безопасно ждем завершения фонового потока до 1 секунды
        if self.processing_thread and self.processing_thread.is_alive():
            self.processing_thread.join(timeout=1.0)
        
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)
        self.btn_start_all.config(state=tk.NORMAL)
        self.btn_start_sel.config(state=tk.NORMAL)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_remove_sel.config(state=tk.NORMAL)
        
        self.lbl_current_text.config(text="Принудительно остановлено. Кэш сохранен.", foreground=self.get_status_color("error"))
        messagebox.showwarning("Принудительная остановка", "Процесс прерван. Текущее предложение не завершено, но весь накопленный кэш записан на диск.")

    def finish_processing(self):
        self.btn_start_all.config(state=tk.NORMAL)
        self.btn_start_sel.config(state=tk.NORMAL)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_remove_sel.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)
        
        self.lbl_current_text.config(text="Ожидание...", foreground=self.get_status_color("text"))
        if self.processor and self.processor.is_stopped:
            if self.lbl_current_text.cget("text") != "Принудительно остановлено. Кэш сохранен.":
                messagebox.showwarning("Остановлено", "Обработка была прервана.")
        else:
            messagebox.showinfo("Готово", "Все выбранные файлы обработаны!")

    def process_queue(self, items_to_process):
        try:
            total_files = len(items_to_process)
            skip_existing = self.settings_vars["skip_existing"].get()
            input_dir = Path(self.config["input_dir"])
            
            for idx, item_id in enumerate(items_to_process):
                if self.processor.is_stopped: break
                
                filepath = input_dir / item_id
                
                if not filepath.exists():
                    self.root.after(0, self.update_file_status, item_id, "error")
                    continue
                
                out_filename = filepath.with_suffix(f'.{self.config["output_format"]}').name
                out_filepath = Path(self.config["output_dir"]) / out_filename
                
                # === ИЗМЕНЕНО: Читаем статусы прямо из RAM процессора ===
                if skip_existing and out_filepath.exists():
                    file_status = self.processor.processing_statuses_ram.get(str(out_filepath.resolve()), "success")
                    if file_status == "success":
                        self.root.after(0, self.update_file_status, filepath.name, "success")
                        self.root.after(0, self.update_total_ui, idx + 1, total_files)
                        continue 
                
                self.root.after(0, self.update_file_status, filepath.name, "processing")
                
                def on_progress(current, total, text):
                    pct = int((current / total) * 100) if total > 0 else 0
                    self.root.after(0, self.update_progress_ui, pct, text)
                    
                def on_complete(filename, status):
                    self.root.after(0, self.update_file_status, filename, status)
                    self.root.after(0, self.update_total_ui, idx + 1, total_files)

                try:
                    self.processor.process_text_file(filepath, progress_callback=on_progress, completion_callback=on_complete)
                except Exception as e:
                    logging.error(f"Ошибка при обработке файла {filepath.name}: {e}")
                    self.root.after(0, self.update_file_status, filepath.name, "error")

                if not self.processor.is_stopped:
                    self.root.after(0, self.update_file_status, filepath.name, "encoding")

            for t in self.processor.active_threads: 
                if t.is_alive():
                    t.join()
            self.processor.active_threads.clear()
            
            # === НОВОЕ: Финальное сохранение статусов на диск после всей очереди ===
            self.processor._save_cache()
            
        except Exception as e:
            logging.error(f"Критическая ошибка в очереди синтеза: {e}")
        finally:
            # === ФИНАЛЬНОЕ СОХРАНЕНИЕ СТАТУСОВ НА ДИСК (Один раз за весь процесс) ===
            if self.processor:
                status_file = APP_DATA_DIR / "processing_statuses.json"
                try:
                    with open(status_file, 'w', encoding='utf-8') as f:
                        json.dump(self.processor.processing_statuses_ram, f, ensure_ascii=False, indent=4)
                except Exception as e:
                    logging.error(f"Не удалось записать статусы на диск: {e}")
                    
            self.root.after(0, self.finish_processing)

    def start_direct_processing(self):
            # Явно копируем все локальные настройки прямого синтеза в config
            self.config["fx_speed"] = self.dir_speed_var.get()
            self.config["fx_pitch"] = self.dir_pitch_var.get()
            self.config["fx_echo"] = self.dir_echo_var.get()
            self.config["fx_echo_delay"] = self.dir_echo_delay_var.get()
            self.config["fx_echo_decay"] = self.dir_echo_decay_var.get()
    
            self.save_settings()
            if not self.config.get("api_token"):
                messagebox.showerror("Ошибка", "Введите API Token во вкладке Настройки!")
                return
                
            text = self.direct_text.get(1.0, tk.END).strip()
            if not text: return
            
            filename = self.settings_vars["direct_filename"].get().strip()
            if not filename: filename = "direct_output.mp3"
            force = self.settings_vars["direct_force"].get()
            save_file = self.settings_vars["direct_save"].get()
            
            self.btn_direct_start.config(state=tk.DISABLED)
            self.btn_direct_stop.config(state=tk.NORMAL)
            self.btn_direct_hard_stop.config(state=tk.NORMAL)
            self.lbl_direct_status.config(text="Обработка...", foreground=self.get_status_color("text"))
            
            self.processor = TTSProcessor(self.config, shared_rate_limiter=self.shared_rate_limiter, error_callback=self.show_critical_error)
            
            def run_direct():
                def on_progress(current, total, txt):
                    self.root.after(0, lambda: self.lbl_direct_status.config(text=f"Синтез: {current}/{total}...", foreground=self.get_status_color("info")))
                def on_complete(fname, status, audio=None):
                    msg = f"Готово! Сохранено в {fname}" if save_file else "Готово! (Не сохранено)"
                    self.root.after(0, lambda: self.lbl_direct_status.config(text=msg, foreground=self.get_status_color("success")))
                    self.root.after(0, lambda: self.btn_direct_start.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.btn_direct_stop.config(state=tk.DISABLED))
                    self.root.after(0, lambda: self.btn_direct_hard_stop.config(state=tk.DISABLED))
                    
                    self.last_direct_audio = audio
                    self.root.after(0, lambda: self.btn_direct_play.config(state=tk.NORMAL if audio else tk.DISABLED))
                    
                    if self.settings_vars["direct_autoplay"].get() and audio and os.path.exists(audio):
                        self.play_audio_file(audio)
                    
                self.processor.process_raw_text(text, filename, force_new=force, save_to_disk=save_file, progress_callback=on_progress, completion_callback=on_complete)
                # Дожидаемся завершения кодирования
                for t in self.processor.active_threads: 
                    if t.is_alive():
                        t.join()
                # Очищаем память от завершенных потоков сборки
                self.processor.active_threads.clear()
    
            self.direct_thread = threading.Thread(target=run_direct, daemon=True)
            self.direct_thread.start()

    def stop_direct_processing(self):
        if self.processor: self.processor.is_stopped = True
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.lbl_direct_status.config(text="Остановка...", foreground=self.get_status_color("warning"))

    def hard_stop_direct_processing(self):
        if self.processor: 
            self.processor.stop() # Мгновенно рвет HTTP-сокет и сохраняет кэш
            
        # Ждем завершения фонового потока прямого синтеза до 1 секунды
        if hasattr(self, 'direct_thread') and self.direct_thread and self.direct_thread.is_alive():
            self.direct_thread.join(timeout=1.0)
            
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.btn_direct_hard_stop.config(state=tk.DISABLED)
        self.btn_direct_start.config(state=tk.NORMAL)
        self.lbl_direct_status.config(text="Принудительно остановлено. Кэш сохранен.", foreground=self.get_status_color("error"))
    
    def update_progress_ui(self, pct, text):
        self.file_progress['value'] = pct
        self.lbl_file_pct.config(text=f"{pct}%")
        
        display_text = text.replace('\n', ' ')
        if len(display_text) > 90:
            display_text = display_text[:87] + "..."
        else:
            display_text = display_text.ljust(90)
            
        self.lbl_current_text.config(text=f"Синтез: {display_text}", foreground=self.get_status_color("info"))

    def update_total_ui(self, current, total):
        pct = int((current / total) * 100) if total > 0 else 0
        self.total_progress['value'] = pct
        self.lbl_total_pct.config(text=f"{current}/{total}")


    def show_critical_error(self, message):
        """Показывает всплывающее окно с ошибкой, пришедшей из фонового потока"""
        # Используем after(0, ...), чтобы безопасно вызвать messagebox из главного потока
        self.root.after(0, lambda: messagebox.showerror("Критическая ошибка API", message))

    def export_config(self):
        self.update_config_from_ui()
        
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Экспорт настроек")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Выберите группы настроек для экспорта:").pack(pady=10, padx=20)
        
        vars_dict = {
            "api": (tk.BooleanVar(value=True), "API и Лимиты (Токен, Голос)"),
            "folders": (tk.BooleanVar(value=True), "Пути к папкам (Ввод, Вывод, Кэш)"),
            "pauses": (tk.BooleanVar(value=True), "Паузы и Разделители"),
            "cache": (tk.BooleanVar(value=True), "Настройки Кэша и Очистки"),
            "effects": (tk.BooleanVar(value=True), "Эффекты (Скорость, Тон, Эхо)"),
            "tags": (tk.BooleanVar(value=True), "Вывод и Теги ID3")
        }
        
        for key, (var, text) in vars_dict.items():
            ttk.Checkbutton(dialog, text=text, variable=var).pack(anchor=tk.W, padx=30, pady=2)
            
        def do_export():
            key_groups = {
                "api": ["api_", "speaker", "max_retries"],
                "folders": ["input_dir", "output_dir", "cache_dir", "export_dir", "import_outdir"],
                "pauses": ["pause_", "separator_symbols", "default_group_pause"],
                "cache": ["auto_", "silence_threshold", "use_cache", "cache_", "enable_cache_"],
                "effects": ["fx_"],
                "tags": ["output_", "synthesis_mode", "tag_", "default_group_name"]
            }
            export_data = {}
            for group_key, (var, _) in vars_dict.items():
                if var.get():
                    prefixes = key_groups[group_key]
                    for k, v in self.config.items():
                        if any(k.startswith(p) for p in prefixes):
                            export_data[k] = v
                            
            dialog.destroy()
            if len(export_data) <= 1:
                messagebox.showwarning("Пусто", "Ничего не выбрано для экспорта.")
                return
                
            filepath = filedialog.asksaveasfilename(initialdir=str(APP_DATA_DIR), defaultextension=".json", filetypes=[("JSON files", "*.json")], initialfile="my_tts_config.json")
            if filepath:
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(export_data, f, indent=4, ensure_ascii=False)
                    messagebox.showinfo("Успех", f"Настройки экспортированы в:\n{filepath}")
                except Exception as e:
                    messagebox.showerror("Ошибка", f"Не удалось экспортировать конфиг:\n{e}")
                    
        ttk.Button(dialog, text="Экспортировать", command=do_export).pack(pady=15)
        self._center_popup(dialog, 350, 280)

    def export_glossary(self):
        content = self.txt_glossary.get(1.0, tk.END).strip()
        try:
            parsed = json.loads(content)
        except Exception as e:
            messagebox.showerror("Ошибка JSON", f"Исправьте ошибки в редакторе перед экспортом:\n{e}")
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
                
            filepath = filedialog.asksaveasfilename(initialdir=str(APP_DATA_DIR), defaultextension=".json", filetypes=[("JSON files", "*.json")], initialfile="my_glossary.json")
            if filepath:
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, indent=4, ensure_ascii=False)
                messagebox.showinfo("Успех", f"Глоссарий экспортирован в:\n{filepath}")
                
        ttk.Button(dialog, text="Экспортировать", command=do_export).pack(pady=15)
        self._center_popup(dialog, 300, 200)

    def import_glossary(self):
        filepath = filedialog.askopenfilename(initialdir=str(APP_DATA_DIR), filetypes=[("JSON files", "*.json")])
        if not filepath: return
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                imported_data = json.load(f)
            if not isinstance(imported_data, dict): raise ValueError()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Импорт Глоссария")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Что импортировать?\n(Данные будут добавлены к текущим)").pack(pady=10, padx=20)
        
        var_acc = tk.BooleanVar(value=True)
        var_trm = tk.BooleanVar(value=True)
        var_reg = tk.BooleanVar(value=True)
        
        ttk.Checkbutton(dialog, text="Ударения (+)", variable=var_acc).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="Замена терминов", variable=var_trm).pack(anchor=tk.W, padx=30, pady=2)
        ttk.Checkbutton(dialog, text="RegEx правила", variable=var_reg).pack(anchor=tk.W, padx=30, pady=2)
        
        def do_import():
            content = self.txt_glossary.get(1.0, tk.END).strip()
            current = json.loads(content) if content else {}
            
            if var_acc.get():
                current.setdefault("accents_ignore_case", []).extend(imported_data.get("accents_ignore_case", []))
                current.setdefault("accents_strict_case", []).extend(imported_data.get("accents_strict_case", []))
                current["accents_ignore_case"] = list(set(current["accents_ignore_case"]))
                current["accents_strict_case"] = list(set(current["accents_strict_case"]))
                
            if var_trm.get():
                current.setdefault("terms_ignore_case", {}).update(imported_data.get("terms_ignore_case", {}))
                current.setdefault("terms_strict_case", {}).update(imported_data.get("terms_strict_case", {}))
                
            if var_reg.get():
                # Простая защита от дубликатов RegEx (по паттерну)
                existing_patterns = {r.get("pattern") for r in current.get("regex_rules", [])}
                for r in imported_data.get("regex_rules", []):
                    if r.get("pattern") not in existing_patterns:
                        current.setdefault("regex_rules", []).append(r)
                        existing_patterns.add(r.get("pattern"))
                        
            dialog.destroy()
            self.txt_glossary.delete(1.0, tk.END)
            self.txt_glossary.insert(tk.END, json.dumps(current, indent=4, ensure_ascii=False))
            self.save_glossary_ui()
            messagebox.showinfo("Успех", "Правила успешно добавлены в глоссарий!")
            
        ttk.Button(dialog, text="Импортировать (Добавить)", command=do_import).pack(pady=15)
        self._center_popup(dialog, 320, 220)

if __name__ == "__main__":
    root = tk.Tk()
    app = TTSApp(root)
    root.mainloop()