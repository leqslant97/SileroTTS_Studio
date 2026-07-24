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
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from razdel import sentenize

# Для воспроизведения аудио на Windows
if platform.system() == "Windows":
    import winsound

# ================= ИНИЦИАЛИЗАЦИЯ ПАПКИ ДАННЫХ =================
# Проверяем, запущены ли мы как скомпилированное приложение (.app) на macOS
is_frozen_mac = (sys.platform == "darwin") and getattr(sys, 'frozen', False)

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

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
console_handler.setFormatter(console_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

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
    
    "output_format": "mp3",
    "output_bitrate": "128k",
    "synthesis_mode": "sentence",
    
    "tag_title": "{filename}",
    "tag_artist": "",
    "tag_album": "",
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
    "auto_clean_cache": False,
    "cache_max_entries": 10000,
    "cache_ttl_hours": 720.0,
    
    "separator_symbols": "☆☆☆\n***\n###\n---", 
    "api_max_requests": 15,
    "api_time_window": 15.0,
    "max_retries": 5,

    "fx_speed": 1.0,
    "fx_pitch": 1.0,
    "fx_echo": False,
    "fx_echo_delay": 300,
    "fx_echo_decay": 0.3,
    "scale_pauses": True,
    
    "default_group_name": "Том {num}",
    "default_group_pause": 1000,
    
    "ui_font_size": 10,
    
    "direct_filename": "direct_output.mp3",
    "direct_save": True,
    "direct_force": False,
    "direct_autoplay": True,

    "skip_existing": True,
    
    "import_outdir": "input_texts",
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
            # aecho=in_gain:out_gain:delays:decays
            d_ms = int(echo_delay)
            d_cy = float(echo_decay)
            filters.append(f"aecho=0.8:0.8:{d_ms}:{d_cy}")

        filter_str = ",".join(filters)

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in, \
                 tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
                in_path = f_in.name
                out_path = f_out.name

            audio_segment.export(in_path, format="wav")

            command = ["ffmpeg", "-y", "-i", in_path, "-af", filter_str, out_path]
            
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo, check=True)

            processed_audio = AudioSegment.from_file(out_path, format="wav")
            os.remove(in_path)
            os.remove(out_path)
            return processed_audio
            
        except Exception as e:
            logging.error(f"Ошибка применения эффектов FFmpeg: {e}")
            return audio_segment
# ===============================================================

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
    def __init__(self, config, error_callback=None):
        self.cfg = config
        self.error_callback = error_callback
        self.rate_limiter = RateLimiter(int(config["api_max_requests"]), float(config["api_time_window"]))
        
        Path(self.cfg["input_dir"]).mkdir(exist_ok=True)
        Path(self.cfg["output_dir"]).mkdir(exist_ok=True)
        self.cache_dir = Path(self.cfg["cache_dir"])
        self.cache_audio_dir = self.cache_dir / "audio"
        self.cache_audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_index_path = self.cache_dir / "sentence_cache.json"
        self.glossary_path = self.cache_dir / "glossary.json"
        
        self.cache = self._load_cache()
        self.unsaved_cache_items = 0
        self.active_threads = []
        
        self.glossary_ignore_case = {}
        self.glossary_strict_case = {}
        self.glossary_regex = []
        self.load_glossary_file()
        
        self.is_stopped = False
        
        raw_seps = str(self.cfg.get("separator_symbols", ""))
        if "," in raw_seps and "\n" not in raw_seps: raw_seps = raw_seps.replace(",", "\n")
        self.separators = [s.strip() for s in raw_seps.split("\n") if s.strip()]

    def _load_cache(self):
        if self.cache_index_path.exists():
            with open(self.cache_index_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in data.items():
                    if isinstance(v, str): 
                        data[k] = {"file_name": Path(v).name, "original_text": "migrated", "normalized_text": "migrated", "speaker": self.cfg["speaker"], "created_at": time.time(), "last_accessed": time.time(), "usage_count": 1}
                    elif "file_path" in v:
                        v["file_name"] = Path(v["file_path"]).name
                        del v["file_path"]
                return data
        return {}

    def _delete_cache_entry(self, text_hash):
        if text_hash in self.cache:
            filename = self.cache[text_hash].get("file_name")
            if filename:
                filepath = self.cache_audio_dir / filename
                if filepath.exists(): os.remove(filepath)
            del self.cache[text_hash]

    def _enforce_cache_limits(self):
        if not self.cfg.get("auto_clean_cache", False): return
        now = time.time()
        ttl_sec = float(self.cfg.get("cache_ttl_hours", 720.0)) * 3600
        keys_to_delete = [k for k, v in self.cache.items() if now - v["last_accessed"] > ttl_sec]
        for k in keys_to_delete:
            self._delete_cache_entry(k)
            self.unsaved_cache_items += 1
            
        max_entries = int(self.cfg.get("cache_max_entries", 10000))
        if len(self.cache) > max_entries:
            sorted_keys = sorted(self.cache.keys(), key=lambda k: self.cache[k]["last_accessed"])
            excess = len(self.cache) - max_entries
            for k in sorted_keys[:excess]:
                self._delete_cache_entry(k)
                self.unsaved_cache_items += 1

    def _save_cache(self):
        if self.unsaved_cache_items > 0:
            self._enforce_cache_limits()
            with open(self.cache_index_path, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=4)
            self.unsaved_cache_items = 0

    def load_glossary_file(self):
        if not os.path.exists(self.glossary_path): return
        with open(self.glossary_path, 'r', encoding='utf-8') as f: data = json.load(f)
        for w in data.get("accents_ignore_case", []): self.glossary_ignore_case[w.replace("+", "").lower()] = w
        for w in data.get("accents_strict_case", []): self.glossary_strict_case[w.replace("+", "")] = w
        for k, v in data.get("terms_ignore_case", {}).items(): self.glossary_ignore_case[k.lower()] = v
        for k, v in data.get("terms_strict_case", {}).items(): self.glossary_strict_case[k] = v
        self.glossary_regex = data.get("regex_rules", [])

    def apply_regex_rules(self, text):
        for rule in self.glossary_regex:
            try: text = re.sub(rule["pattern"], rule["repl"], text)
            except Exception as e: logging.error(f"Ошибка в RegEx {rule['pattern']}: {e}")
        return text

    def apply_glossary(self, text):
        for original, replacement in self.glossary_strict_case.items():
            pattern = r'(?<![а-яА-Яa-zA-Z0-9_ёЁ])' + re.escape(original) + r'(?![а-яА-Яa-zA-Z0-9_ёЁ])'
            text = re.sub(pattern, replacement, text)
            
        for original_lower, replacement in self.glossary_ignore_case.items():
            pattern = r'(?<![а-яА-Яa-zA-Z0-9_ёЁ])' + re.escape(original_lower) + r'(?![а-яА-Яa-zA-Z0-9_ёЁ])'
            def match_func(m):
                w = m.group(0)
                if w.isupper(): return replacement.upper()
                elif w.istitle(): return replacement[0].upper() + replacement[1:] if replacement else ""
                return replacement
            text = re.sub(pattern, match_func, text, flags=re.IGNORECASE)
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
            text = normalizer.normalize(text)

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

    def synthesize_sentence(self, normalized_text, original_text, force_new=False):
        """Синтезирует аудио через API или достает из кэша."""
        text_hash = self.get_hash(normalized_text)
        
        if force_new and text_hash in self.cache:
            self._delete_cache_entry(text_hash)
            self.unsaved_cache_items += 1
        
        if self.cfg.get("use_cache", True) and text_hash in self.cache:
            cache_info = self.cache[text_hash]
            filepath = self.cache_audio_dir / cache_info["file_name"]
            if filepath.exists():
                cache_info["last_accessed"] = time.time()
                cache_info["usage_count"] += 1
                self.unsaved_cache_items += 1
                return AudioSegment.from_file(filepath), True

        payload = {
            'api_token': self.cfg["api_token"],
            'text': normalized_text,
            'sample_rate': 48000,
            'speaker': self.cfg["speaker"],
            'remote_id': 'python_script',
            'format': 'ogg'
        }
        
        for attempt in range(1, int(self.cfg["max_retries"]) + 1):
            if self.is_stopped: return AudioSegment.silent(duration=0), False
            self.rate_limiter.wait()
            try:
                r = requests.post(self.cfg["api_url"], json=payload, timeout=30)
                r.raise_for_status()
                audio_data = base64.b64decode(r.json()['results'][0]['audio'])
                audio_segment = AudioSegment.from_file(io.BytesIO(audio_data), format="ogg")
                
                if self.cfg.get("auto_trim_silence", True):
                    nonsilent_ranges = detect_nonsilent(audio_segment, min_silence_len=50, silence_thresh=float(self.cfg["silence_threshold"]))
                    if nonsilent_ranges:
                        start_trim = max(0, nonsilent_ranges[0][0] - 20)
                        end_trim = min(len(audio_segment), nonsilent_ranges[-1][1] + 20)
                        audio_segment = audio_segment[start_trim:end_trim]
                
                if self.cfg.get("use_cache", True):
                    file_name = f"{text_hash}.ogg"
                    cache_file = self.cache_audio_dir / file_name
                    audio_segment.export(cache_file, format="ogg")
                    self.cache[text_hash] = {
                        "file_name": file_name, "original_text": original_text,
                        "normalized_text": normalized_text, "speaker": self.cfg["speaker"],
                        "created_at": time.time(), "last_accessed": time.time(), "usage_count": 1
                    }
                    self.unsaved_cache_items += 1
                    if self.unsaved_cache_items >= int(self.cfg["cache_save_frequency"]):
                        self._save_cache()
                return audio_segment, True
                
            except requests.exceptions.HTTPError as e:
                if r.status_code == 422:
                    try:
                        detail = r.json().get("detail", "")
                        if "unknown api token" in detail.lower():
                            msg = "КРИТИЧЕСКАЯ ОШИБКА: Неверный API Token!"
                            logging.error(msg)
                            self.is_stopped = True
                            if self.error_callback: self.error_callback(msg) # <-- Вызов в GUI
                            return AudioSegment.silent(duration=0), False
                        elif "unknown speaker" in detail.lower():
                            short_detail = " ".join(detail.split()[:3]).strip(',')
                            msg = f"КРИТИЧЕСКАЯ ОШИБКА: {short_detail}"
                            logging.error(msg)
                            self.is_stopped = True
                            if self.error_callback: self.error_callback(msg) # <-- Вызов в GUI
                            return AudioSegment.silent(duration=0), False
                    except Exception:
                        pass
                
                logging.warning(f"HTTP Ошибка синтеза (попытка {attempt}): {e}")
                if attempt < int(self.cfg["max_retries"]): time.sleep(2)
                else: return AudioSegment.silent(duration=int(self.cfg["pause_sentence"])), False

    def process_raw_text(self, raw_text, out_filename, force_new=False, save_to_disk=True, progress_callback=None, completion_callback=None):
        """Разбивает текст на задачи и управляет процессом синтеза всего файла."""
        # 1. Базовая типографика
        raw_text = re.sub(r'[«»“”„]', '"', raw_text)
        raw_text = re.sub(r'^[ \t]*[-–—−]+\s*', '— ', raw_text, flags=re.MULTILINE)
        
        # 2. RegEx правила из глоссария
        raw_text = self.apply_regex_rules(raw_text)
        
        # 3. Обработка разделителей
        separator_token = "___SEPARATOR_TOKEN___"
        for sep in self.separators:
            raw_text = raw_text.replace(sep, f"\n{separator_token}\n")
        
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
                # В КЭШ ИДЕТ ЧИСТЫЙ ИСХОДНИК (sent_raw), а нормализация идет в sent_clean
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
                
                # ЗАЩИТА ОТ ПЕРЕПОЛНЕНИЯ (Авто-разрыв)
                current_len = sum(len(t) for t in current_full_text_clean) + len(current_full_text_clean)
                if current_full_text_clean and (current_len + len(para_clean) > SAFE_LIMIT):
                    tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))
                    # Вставляем искусственную паузу между разорванными блоками
                    tasks.append(("__SILENCE__", int(self.cfg["pause_paragraph"]), 0))
                    current_full_text_clean, current_full_text_raw = [], []
                    
                current_full_text_raw.append(para_raw)
                current_full_text_clean.append(para_clean)

        if self.cfg["synthesis_mode"] == "full" and current_full_text_clean:
            tasks.append(("\n".join(current_full_text_clean), "\n".join(current_full_text_raw), 0))

        audio_segments = []
        if int(self.cfg["pause_file_start"]) > 0:
            audio_segments.append(AudioSegment.silent(duration=int(self.cfg["pause_file_start"])))

        file_has_errors = False
        total_tasks = len(tasks)

        for i, task in enumerate(tasks):
            if self.is_stopped:
                # ВАЖНО: Сохраняем накопившийся кэш перед тем, как прервать работу
                self._save_cache()
                if completion_callback: completion_callback(out_filename, "error", None)
                return

            clean_text, raw_text_or_duration, pause_before = task
            
            if pause_before > 0:
                audio_segments.append(AudioSegment.silent(duration=pause_before))

            if clean_text == "__SILENCE__":
                audio_segments.append(AudioSegment.silent(duration=raw_text_or_duration))
                if progress_callback: progress_callback(i + 1, total_tasks, "[ПАУЗА РАЗДЕЛИТЕЛЯ]")
            else:
                if progress_callback: progress_callback(i + 1, total_tasks, clean_text)
                segment, success = self.synthesize_sentence(clean_text, raw_text_or_duration, force_new)
                audio_segments.append(segment)
                if not success: file_has_errors = True

        if int(self.cfg["pause_file_end"]) > 0:
            audio_segments.append(AudioSegment.silent(duration=int(self.cfg["pause_file_end"])))

        # Сохраняем кэш после успешного завершения файла
        self._save_cache()

        final_audio = AudioSegment.empty()
        for segment in audio_segments: final_audio += segment

        if save_to_disk:
            out_filepath = Path(self.cfg["output_dir"]) / out_filename
            t = threading.Thread(target=self._merge_save_and_notify, args=(final_audio, out_filepath, out_filename, file_has_errors, completion_callback))
            self.active_threads.append(t)
            t.start()
        else:
            if completion_callback:
                completion_callback(out_filename, "warning" if file_has_errors else "success", final_audio)

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
            raw_text = raw_text.replace(sep, f"\n{separator_token}\n")
        
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

    def _merge_save_and_notify(self, final_audio, out_filepath, original_filename, has_errors, callback):
        # 1. Применяем эффекты к итоговой аудиодорожке перед сохранением
        sp = float(self.cfg.get("fx_speed", 1.0))
        pt = float(self.cfg.get("fx_pitch", 1.0))
        ec = bool(self.cfg.get("fx_echo", False))
        ed = int(self.cfg.get("fx_echo_delay", 300))
        ey = float(self.cfg.get("fx_echo_decay", 0.3))

        # Если галочка "Масштабировать паузы" СНЯТА (пользователь НЕ хочет, чтобы тишина ускорялась),
        # то при ускорении всего файла тишина сжимается. Чтобы она НЕ сжималась, если scale_pauses=False,
        # мы должны прогнать через эффекты весь файл целиком. 
        # На самом деле, так как мы сначала склеили аудио + тишину, запуск `atempo=sp` на весь файл 
        # АВТОМАТИЧЕСКИ ускоряет и речь, и паузы пропорционально! 
        # Поэтому scale_pauses=True работает "из коробки" при обработке целого файла через FFmpeg!
        
        final_audio = AudioEffects.apply_effects(final_audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)

        export_kwargs = {"format": self.cfg["output_format"]}
        if self.cfg["output_format"].lower() == "mp3": 
            export_kwargs["bitrate"] = self.cfg["output_bitrate"]
            
        tags = {}
        base_name = out_filepath.stem
        if self.cfg.get("tag_title"): tags["title"] = self.cfg["tag_title"].replace("{filename}", base_name)
        if self.cfg.get("tag_artist"): tags["artist"] = self.cfg["tag_artist"]
        if self.cfg.get("tag_album"): tags["album"] = self.cfg["tag_album"]
        if self.cfg.get("tag_composer"): tags["composer"] = self.cfg["tag_composer"]
        if self.cfg.get("tag_year"): tags["date"] = self.cfg["tag_year"]
        
        if tags: export_kwargs["tags"] = tags
        
        cover_path = self.cfg.get("tag_cover", "")
        if cover_path and os.path.exists(cover_path):
            export_kwargs["cover"] = cover_path

        final_audio.export(out_filepath, **export_kwargs)
        # --- НОВОЕ: Сохраняем статус файла в JSON ---
        status_file = APP_DATA_DIR / "processing_statuses.json"
        statuses = {}
        if status_file.exists():
            try:
                with open(status_file, 'r', encoding='utf-8') as f: statuses = json.load(f)
            except: pass
            
        # Используем абсолютный путь (resolve) как уникальный ключ для файла
        statuses[str(out_filepath.resolve())] = "warning" if has_errors else "success"
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump(statuses, f, ensure_ascii=False, indent=4)
        # --------------------------------------------
        logging.info(f"Файл {out_filepath.name} сохранен с эффектами (Скорость: {sp}x, Тон: {pt}).")
        if callback: callback(original_filename, "warning" if has_errors else "success", None)

        

# ================= ЭКСТРАКТОР И НАРЕЗЧИК КНИГ =================
class BookExtractor:
    """Утилита для извлечения текста из различных форматов книг (EPUB, FB2, DOCX)."""
    
    @staticmethod
    def extract_epub(filepath):
        book = epub.read_epub(str(filepath))
        chapters = []
        
        # Рекурсивный парсинг оглавления (TOC)
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
                        text = soup.get_text(separator='\n', strip=True)
                        if text: chapters.append((link.title, text))
                        break
        else:
            # Резервный вариант, если TOC пустой (читаем по порядку страниц)
            for item_id in book.spine:
                item = book.get_item_with_id(item_id[0])
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    html_content = item.get_content().decode('utf-8', errors='ignore')
                    soup = BeautifulSoup(html_content, 'html.parser')
                    text = soup.get_text(separator='\n', strip=True)
                    if text: chapters.append(("Глава", text))
                    
        return chapters

    @staticmethod
    def extract_fb2(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'xml')
            
        chapters = []
        body = soup.find('body')
        if not body: return [("Книга", soup.get_text(separator='\n', strip=True))]
        
        sections = body.find_all('section', recursive=False)
        if not sections: sections = body.find_all('section')
        if not sections: return [("Книга", body.get_text(separator='\n', strip=True))]

        for sec in sections:
            title_tag = sec.find('title')
            title = title_tag.get_text(strip=True) if title_tag else "Глава"
            text = sec.get_text(separator='\n', strip=True)
            if text: chapters.append((title, text))
            
        return chapters

    @staticmethod
    def extract_docx(filepath):
        doc = docx.Document(filepath)
        chapters = []
        current_title = "Вступление"
        current_text = []
        
        for p in doc.paragraphs:
            if p.style.name.startswith('Heading'):
                if current_text:
                    chapters.append((current_title, "\n".join(current_text)))
                    current_text = []
                current_title = p.text.strip() or "Глава"
                current_text.append(current_title) # <-- ИСПРАВЛЕНИЕ: Сохраняем заголовок в тексте
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
            intro = text[:matches[0].start()].strip('\n') # <-- ИСПРАВЛЕНИЕ: strip('\n') вместо strip()
            if intro.strip(): chapters.append(("Вступление", intro))
            
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            content = text[start:end].strip('\n') # <-- ИСПРАВЛЕНИЕ: сохраняем пробелы
            title = match.group(0).strip()
            if content.strip(): chapters.append((title, content))
            
        return chapters

    @staticmethod
    def save_chapters(chapters, out_dir, orig_filename, template):
        total = len(chapters)
        pad = len(str(total)) # Динамический zfill (01 для 10, 001 для 100)
        out_dir = Path(out_dir)
        out_dir.mkdir(exist_ok=True)
        
        saved_files = []
        name_no_ext = Path(orig_filename).stem
        
        for idx, (title, content) in enumerate(chapters, 1):
            safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:50] # Очистка и обрезка длинных названий
            
            filename = template.replace("{name}", name_no_ext)
            filename = filename.replace("{num}", str(idx).zfill(pad))
            filename = filename.replace("{title}", safe_title)
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
        self.root.geometry("1280x800")
        
        self.settings_vars = {}
        self.config = self.load_settings()
        self.processor = None
        self.processing_thread = None
        self.last_direct_audio = None

        font_frame = ttk.Frame(self.root)
        font_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=5)
        ttk.Label(font_frame, text="Размер шрифта:").pack(side=tk.LEFT)
        self.font_size_var = tk.IntVar(value=self.config.get("ui_font_size", 10))
        scale = ttk.Scale(font_frame, from_=10, to_=24, variable=self.font_size_var, command=self.update_fonts)
        scale.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)
        
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
        
        # --- НОВОЕ: Перехват закрытия окна ---
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_closing(self):
        """Безопасное закрытие программы с очисткой временных файлов"""
        # 1. Если идет синтез, мягко его останавливаем и сохраняем кэш
        if self.processor and not self.processor.is_stopped:
            self.processor.is_stopped = True
            self.processor._save_cache()
            
        self.save_settings()
        
        # 2. Очищаем временную папку с извлеченными обложками
        covers_dir = APP_DATA_DIR / "covers"
        if covers_dir.exists():
            try:
                shutil.rmtree(covers_dir)
            except Exception as e:
                logging.error(f"Не удалось удалить временные обложки: {e}")
                
        self.root.destroy()

    def update_fonts(self, *args):
        size = self.font_size_var.get()
        if hasattr(self, 'direct_text'): self.direct_text.config(font=("Arial", size))
        if hasattr(self, 'txt_glossary'): self.txt_glossary.config(font=("Courier", size))
        if hasattr(self, 'help_text_widget'): self.help_text_widget.config(font=("Arial", size))
        self.config["ui_font_size"] = size
        self.save_settings()

    def load_settings(self, path=SETTINGS_FILE):
        cfg = DEFAULT_CONFIG.copy()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg.update(json.load(f))
            except Exception as e:
                logging.error(f"Ошибка загрузки конфига: {e}")
        return cfg

    def save_settings(self, path=SETTINGS_FILE):
        self.update_config_from_ui()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)

    def update_config_from_ui(self):
        for key, var in self.settings_vars.items():
            self.config[key] = var.get()
        if hasattr(self, 'txt_separators'):
            self.config["separator_symbols"] = self.txt_separators.get(1.0, tk.END).strip()

    def set_ui_from_config(self):
        # Сначала заполняем текстовые поля, чтобы случайные срабатывания trace 
        # при обновлении переменных не перезаписали конфиг пустотой
        if hasattr(self, 'txt_separators'):
            self.txt_separators.delete(1.0, tk.END)
            raw_seps = str(self.config.get("separator_symbols", ""))
            if "," in raw_seps and "\n" not in raw_seps: raw_seps = raw_seps.replace(",", "\n")
            self.txt_separators.insert(tk.END, raw_seps)

        # Затем обновляем все остальные переменные (галочки, ползунки, строки)
        for key, var in self.settings_vars.items():
            if key in self.config:
                var.set(self.config[key])

    # --- Вкладка "Синтез из папки" ---
    def setup_main_tab(self):
        list_frame = ttk.Frame(self.tab_main, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(list_frame, text="💡 Вы можете выделять несколько строк мышкой с зажатым Ctrl или Shift", font=("", 8, "italic"), foreground="gray").pack(anchor=tk.W, pady=(0,5))
        
        # ДОБАВЛЕНО: selectmode="extended" для множественного выделения
        self.tree = ttk.Treeview(list_frame, columns=("status", "filename"), show="headings", selectmode="extended")
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
        
        self.lbl_current_text = ttk.Label(prog_frame, text="Ожидание...", font=('', 10, 'italic'), foreground="blue", width=110)
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
        ttk.Checkbutton(prog_frame, text="Пропускать готовые файлы (продолжить синтез)", variable=self.settings_vars["skip_existing"]).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(5,0))

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
        
        ttk.Label(frame, text="Вставьте текст для синтеза:").pack(anchor=tk.W)
        self.direct_text = tk.Text(frame, wrap=tk.WORD, height=10) # Чуть уменьшил высоту для новых ползунков
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
        self.dir_scale_pauses_var = tk.BooleanVar(value=self.config.get("scale_pauses", True))
        
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
        
        # Ряд 3: Паузы и Кнопка сохранения
        bot_fx = ttk.Frame(fx_frame)
        bot_fx.pack(fill=tk.X, pady=2)
        
        ttk.Checkbutton(bot_fx, text="Масштабировать паузы", variable=self.dir_scale_pauses_var).pack(side=tk.LEFT, padx=5)
        
        def apply_to_global():
            self.settings_vars["fx_speed"].set(self.dir_speed_var.get())
            self.settings_vars["fx_pitch"].set(self.dir_pitch_var.get())
            self.settings_vars["fx_echo"].set(self.dir_echo_var.get())
            self.settings_vars["fx_echo_delay"].set(self.dir_echo_delay_var.get())
            self.settings_vars["fx_echo_decay"].set(self.dir_echo_decay_var.get())
            self.settings_vars["scale_pauses"].set(self.dir_scale_pauses_var.get())
            self.save_settings()
            messagebox.showinfo("Успех", "Эффекты сохранены в глобальные настройки!")
            
        ttk.Button(bot_fx, text="💾 Сделать глобальными", command=apply_to_global).pack(side=tk.RIGHT, padx=5)
        # ------------------------------------------------
        
        self.lbl_direct_status = ttk.Label(frame, text="", foreground="blue")
        self.lbl_direct_status.pack(anchor=tk.W)
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=5)
        self.btn_direct_start = ttk.Button(btn_frame, text="▶ Синтезировать", command=self.start_direct_processing)
        self.btn_direct_start.pack(side=tk.LEFT)
        
        self.btn_direct_stop = ttk.Button(btn_frame, text="⏹ Стоп", command=self.stop_direct_processing, state=tk.DISABLED)
        self.btn_direct_stop.pack(side=tk.LEFT, padx=5)
        
        self.btn_direct_hard_stop = ttk.Button(btn_frame, text="☠️ Принудительно", command=self.hard_stop_direct_processing, state=tk.DISABLED)
        self.btn_direct_hard_stop.pack(side=tk.LEFT, padx=5)
        
        self.btn_direct_play = ttk.Button(btn_frame, text="🔊 Слушать (с эффектами)", command=self.play_last_audio, state=tk.DISABLED)
        self.btn_direct_play.pack(side=tk.LEFT, padx=10)

    def play_audio_segment(self, audio_segment):
        """Проигрывает готовый аудиосегмент через системный плеер"""
        def _play():
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    temp_path = f.name
                audio_segment.export(temp_path, format="wav")
                
                if platform.system() == "Windows":
                    winsound.PlaySound(temp_path, winsound.SND_FILENAME)
                elif platform.system() == "Darwin":
                    subprocess.call(["afplay", temp_path])
                else:
                    subprocess.call(["aplay", temp_path])
                    
                os.remove(temp_path)
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
        if self.last_direct_audio:
            sp = self.dir_speed_var.get()
            pt = self.dir_pitch_var.get()
            ec = self.dir_echo_var.get()
            ed = self.dir_echo_delay_var.get()
            ey = self.dir_echo_decay_var.get()

            processed_segment = AudioEffects.apply_effects(self.last_direct_audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
            self.play_audio_segment(processed_segment)

    # --- Вкладка "Импорт книг" ---
    def setup_import_tab(self):
        frame = ttk.Frame(self.tab_import, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        if not IMPORT_LIBS_AVAILABLE:
            ttk.Label(frame, text="⚠️ Для работы импорта установите библиотеки:\npip install EbookLib beautifulsoup4 python-docx lxml", foreground="red").pack(pady=10)
            return

        # Выбор файла
        file_frame = ttk.LabelFrame(frame, text="Исходный файл (EPUB, FB2, DOCX, TXT)", padding=10)
        file_frame.pack(fill=tk.X, pady=5)
        
        self.import_filepath_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.import_filepath_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(file_frame, text="Выбрать файл", command=lambda: self.import_filepath_var.set(filedialog.askopenfilename(filetypes=[("Книги", "*.epub *.fb2 *.docx *.txt")]))).pack(side=tk.LEFT)

        # Настройки нарезки
        split_frame = ttk.LabelFrame(frame, text="Настройки нарезки и сохранения", padding=10)
        split_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(split_frame, text="Папка для сохранения (.txt):").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_outdir"] = tk.StringVar(value=self.config.get("import_outdir", "input_texts"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_outdir"], width=40).grid(row=0, column=1, padx=5)
        ttk.Button(split_frame, text="📁", width=3, command=lambda: self.settings_vars["import_outdir"].set(filedialog.askdirectory())).grid(row=0, column=2)
        
        ttk.Label(split_frame, text="Шаблон имени файла:\nДоступно: {name}, {num}, {title}").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_template"] = tk.StringVar(value=self.config.get("import_template", "{num} - {name} - {title}"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_template"], width=40).grid(row=1, column=1, padx=5)
        
        ttk.Label(split_frame, text="RegEx для TXT (нарезка по главам):\nПример: ^Глава \\d+").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.settings_vars["import_regex"] = tk.StringVar(value=self.config.get("import_regex", r"^Глава \d+"))
        ttk.Entry(split_frame, textvariable=self.settings_vars["import_regex"], width=40).grid(row=2, column=1, padx=5)
        
        self.settings_vars["import_single_file"] = tk.BooleanVar(value=self.config.get("import_single_file", False))
        ttk.Checkbutton(split_frame, text="Не делить на главы (сохранить как один файл)", variable=self.settings_vars["import_single_file"]).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=5)
        
        # Кнопка и статус
        self.lbl_import_status = ttk.Label(frame, text="", foreground="blue")
        self.lbl_import_status.pack(pady=5)
        
        self.btn_import_start = ttk.Button(frame, text="⚡ Извлечь и Нарезать", command=self.start_import)
        self.btn_import_start.pack(pady=5)

    def start_import(self):
        self.save_settings() # Сохраняем введенные шаблоны
        filepath = self.import_filepath_var.get()
        if not filepath or not os.path.exists(filepath):
            messagebox.showerror("Ошибка", "Выберите существующий файл!")
            return
            
        out_dir = self.settings_vars["import_outdir"].get()
        template = self.settings_vars["import_template"].get()
        regex_pattern = self.settings_vars["import_regex"].get()
        single_file = self.settings_vars["import_single_file"].get()
        
        self.btn_import_start.config(state=tk.DISABLED)
        self.lbl_import_status.config(text="Анализ и извлечение текста...", foreground="black")
        
        def run():
            try:
                ext = Path(filepath).suffix.lower()
                chapters = []
                
                if single_file:
                    # Извлекаем все и склеиваем в один текст
                    if ext == ".epub": chapters = [("Книга", "\n\n".join([c[1] for c in BookExtractor.extract_epub(filepath)]))]
                    elif ext == ".fb2": chapters = [("Книга", "\n\n".join([c[1] for c in BookExtractor.extract_fb2(filepath)]))]
                    elif ext == ".docx": chapters = [("Книга", "\n\n".join([c[1] for c in BookExtractor.extract_docx(filepath)]))]
                    elif ext == ".txt": 
                        with open(filepath, 'r', encoding='utf-8') as f: chapters = [("Книга", f.read())]
                else:
                    if ext == ".epub": chapters = BookExtractor.extract_epub(filepath)
                    elif ext == ".fb2": chapters = BookExtractor.extract_fb2(filepath)
                    elif ext == ".docx": chapters = BookExtractor.extract_docx(filepath)
                    elif ext == ".txt": chapters = BookExtractor.split_txt_by_regex(filepath, regex_pattern)
                
                if not chapters:
                    raise ValueError("Не удалось найти текст или главы в файле.")
                    
                self.root.after(0, lambda: self.lbl_import_status.config(text=f"Найдено глав: {len(chapters)}. Сохранение...", foreground="orange"))
                
                saved_files = BookExtractor.save_chapters(chapters, out_dir, filepath, template)
                
                msg = f"Успешно извлечено и сохранено файлов: {len(saved_files)}\nПапка: {out_dir}"
                self.root.after(0, lambda: self.lbl_import_status.config(text="Готово!", foreground="green"))
                self.root.after(0, lambda: messagebox.showinfo("Успех", msg))
                self.root.after(0, self.load_files)
                
            except Exception as e:
                logging.error(f"Ошибка импорта: {e}")
                self.root.after(0, lambda: self.lbl_import_status.config(text="Ошибка!", foreground="red"))
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось обработать файл:\n{e}"))
            finally:
                self.root.after(0, lambda: self.btn_import_start.config(state=tk.NORMAL))
                
        threading.Thread(target=run, daemon=True).start()

    # ================= Вкладка "Экспорт и Сборка" =================
    def setup_utils_tab(self):
        frame = ttk.Frame(self.tab_utils, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        
        self.export_groups = {} 
        self.export_files = {} # Хранилище настроек для каждого файла
        self.group_counter = 0
        
        # --- ВЕРХНЯЯ ПАНЕЛЬ ---
        top_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        top_pane.pack(fill=tk.BOTH, expand=True, pady=5)
        
        tree_frame = ttk.Frame(top_pane)
        top_pane.add(tree_frame, weight=3)
        
        ttk.Label(tree_frame, text="Группы и файлы:").pack(anchor=tk.W)
        self.export_tree = ttk.Treeview(tree_frame, columns=("duration",), selectmode="extended")
        self.export_tree.heading("#0", text="Имя")
        self.export_tree.heading("duration", text="Длительность")
        self.export_tree.column("duration", width=100, anchor=tk.CENTER, stretch=False)
        self.export_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.export_tree.yview)
        self.export_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.export_tree.bind("<<TreeviewSelect>>", self.on_export_tree_select)
        
        # Настройки выбранного элемента
        self.group_settings_frame = ttk.LabelFrame(top_pane, text="Настройки", padding=5)
        top_pane.add(self.group_settings_frame, weight=2)
        
        self.grp_notebook = ttk.Notebook(self.group_settings_frame)
        self.grp_notebook.pack(fill=tk.BOTH, expand=True)
        
        self.grp_tab_basic = ttk.Frame(self.grp_notebook, padding=10)
        self.grp_tab_tags = ttk.Frame(self.grp_notebook, padding=10)
        
        self.grp_notebook.add(self.grp_tab_basic, text="Основные")
        self.grp_notebook.add(self.grp_tab_tags, text="Теги (ID3)")
        
        # -- Вкладка: Основные --
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
        self.chk_subfolder = ttk.Checkbutton(self.grp_tab_basic, text="Сохранять в подпапку (если не склеивать)", variable=self.grp_subfolder_var)
        self.chk_subfolder.pack(anchor=tk.W, pady=(0, 10))
        
        ttk.Label(self.grp_tab_basic, text="Пауза между файлами (мс):").pack(anchor=tk.W, pady=(5, 2))
        self.grp_pause_var = tk.IntVar()
        self.grp_pause_var.trace("w", self.save_export_item_settings)
        
        pause_frame = ttk.Frame(self.grp_tab_basic)
        pause_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.ent_pause = ttk.Entry(pause_frame, textvariable=self.grp_pause_var, width=10)
        self.ent_pause.pack(side=tk.LEFT)
        
        # Сохраняем кнопку в self, чтобы иметь к ней доступ
        self.btn_apply_pause = ttk.Button(pause_frame, text="Применить ко всем", command=self.apply_pause_mass)
        self.btn_apply_pause.pack(side=tk.LEFT, padx=5)
        
        # -- Вкладка: Теги --
        def add_tag_entry(parent, label, var_name):
            ttk.Label(parent, text=label).pack(anchor=tk.W, pady=(2, 0))
            var = tk.StringVar()
            var.trace("w", self.save_export_item_settings)
            setattr(self, var_name, var)
            ttk.Entry(parent, textvariable=var).pack(fill=tk.X)

        add_tag_entry(self.grp_tab_tags, "Исполнитель:", "grp_artist_var")
        add_tag_entry(self.grp_tab_tags, "Альбом:", "grp_album_var")
        add_tag_entry(self.grp_tab_tags, "Композитор:", "grp_composer_var")
        add_tag_entry(self.grp_tab_tags, "Год:", "grp_year_var")
        
        ttk.Label(self.grp_tab_tags, text="Обложка (путь к jpg/png):").pack(anchor=tk.W, pady=(2, 0))
        cov_frame = ttk.Frame(self.grp_tab_tags)
        cov_frame.pack(fill=tk.X)
        self.grp_cover_var = tk.StringVar()
        self.grp_cover_var.trace("w", self.save_export_item_settings)
        ttk.Entry(cov_frame, textvariable=self.grp_cover_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cov_frame, text="📁", width=3, command=lambda: self.grp_cover_var.set(filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.jpeg *.png")]))).pack(side=tk.RIGHT)
        
        ttk.Separator(self.grp_tab_tags, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        # --- ИЗМЕНЕНИЯ ЗДЕСЬ: Новые кнопки массового применения тегов ---
        self.btn_apply_to_selected = ttk.Button(self.grp_tab_tags, text="☑ Применить к ВЫДЕЛЕННЫМ элементам", command=lambda: self.apply_tags_mass("selected"))
        self.btn_apply_to_selected.pack(fill=tk.X, pady=2)
        
        self.btn_apply_to_group_files = ttk.Button(self.grp_tab_tags, text="⬇ Применить к файлам этой группы", command=lambda: self.apply_tags_mass("group_files"))
        self.btn_apply_to_group_files.pack(fill=tk.X, pady=2)
        
        self.btn_apply_to_parent = ttk.Button(self.grp_tab_tags, text="⬆ Копировать в родительскую группу", command=lambda: self.apply_tags_mass("parent_group"))
        self.btn_apply_to_parent.pack(fill=tk.X, pady=2)
        
        self.btn_apply_to_all = ttk.Button(self.grp_tab_tags, text="🔄 Применить абсолютно ко всем", command=lambda: self.apply_tags_mass("all"))
        self.btn_apply_to_all.pack(fill=tk.X, pady=2)
        # ----------------------------------------------------------------
        
        self.current_selected_export_item = None
        self._disable_export_settings()

        # --- СРЕДНЯЯ ПАНЕЛЬ: Кнопки и Шаблон ---
        mid_frame = ttk.Frame(frame)
        mid_frame.pack(fill=tk.X, pady=5)
        
        # Переменная для шаблона имени группы
        self.settings_vars["default_group_name"] = tk.StringVar(value=self.config.get("default_group_name", "Том {num}"))
        self.settings_vars["default_group_name"].trace("w", lambda *args: self.save_settings())
        
        # Верхний ряд (Шаблон и Добавление)
        add_frame = ttk.Frame(mid_frame)
        add_frame.pack(fill=tk.X, pady=2)
        
        ttk.Label(add_frame, text="Шаблон новой группы:").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Entry(add_frame, textvariable=self.settings_vars["default_group_name"], width=15).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(add_frame, text="📁 Добавить группу", command=self.add_export_group).pack(side=tk.LEFT, padx=5)
        ttk.Button(add_frame, text="📂 Добавить папку", command=self.add_export_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(add_frame, text="🎵 Добавить аудио", command=self.add_export_files).pack(side=tk.LEFT, padx=2)
        
        # Нижний ряд (Удаление, Сортировка, Авто-разбивка)
        ctrl_frame = ttk.Frame(mid_frame)
        ctrl_frame.pack(fill=tk.X, pady=2)
        
        ttk.Button(ctrl_frame, text="➖ Удалить", command=self.remove_export_items).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(ctrl_frame, text="⬇", width=3, command=lambda: self.move_export_item(1)).pack(side=tk.RIGHT, padx=2)
        ttk.Button(ctrl_frame, text="⬆", width=3, command=lambda: self.move_export_item(-1)).pack(side=tk.RIGHT, padx=2)
        ttk.Button(ctrl_frame, text="⏱ Авто-разбивка", command=self.auto_split_export).pack(side=tk.RIGHT, padx=10)

        # --- НИЖНЯЯ ПАНЕЛЬ: Экспорт ---
        export_frame = ttk.LabelFrame(frame, text="Экспорт", padding=10)
        export_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(export_frame, text="Папка:").pack(side=tk.LEFT, padx=5)
        self.export_outdir_var = tk.StringVar(value=self.config.get("output_dir", "output_audio"))
        ttk.Entry(export_frame, textvariable=self.export_outdir_var, width=20).pack(side=tk.LEFT, padx=5)
        ttk.Button(export_frame, text="📁", width=3, command=lambda: self.export_outdir_var.set(filedialog.askdirectory())).pack(side=tk.LEFT)
        
        ttk.Label(export_frame, text="Формат:").pack(side=tk.LEFT, padx=(15, 2))
        self.export_fmt_var = tk.StringVar(value=self.config.get("output_format", "mp3"))
        ttk.Combobox(export_frame, textvariable=self.export_fmt_var, values=["mp3", "wav", "ogg"], width=5, state="readonly").pack(side=tk.LEFT)
        
        ttk.Label(export_frame, text="Битрейт:").pack(side=tk.LEFT, padx=(10, 2))
        self.export_bitrate_var = tk.StringVar(value=self.config.get("output_bitrate", "128k"))
        ttk.Combobox(export_frame, textvariable=self.export_bitrate_var, values=["64k", "128k", "192k", "256k", "320k"], width=5, state="readonly").pack(side=tk.LEFT)
        
        self.export_apply_fx_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(export_frame, text="Наложить эффекты", variable=self.export_apply_fx_var).pack(side=tk.LEFT, padx=15)
        
        # --- ИЗМЕНЕНИЯ ЗДЕСЬ: Добавлена кнопка Стоп ---
        self.btn_export_start = ttk.Button(export_frame, text="🚀 Начать Сборку", command=self.start_export_process)
        self.btn_export_start.pack(side=tk.RIGHT, padx=5)
        
        self.btn_export_stop = ttk.Button(export_frame, text="⏹ Стоп", command=self.stop_export_process, state=tk.DISABLED)
        self.btn_export_stop.pack(side=tk.RIGHT, padx=5)
        # ----------------------------------------------
        
        prog_frame = ttk.Frame(frame)
        prog_frame.pack(fill=tk.X, pady=5)
        self.lbl_export_status = ttk.Label(prog_frame, text="Ожидание...", foreground="blue")
        self.lbl_export_status.pack(side=tk.LEFT)
        self.export_progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, length=400, mode='determinate')
        self.export_progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))
        
    # --- Логика интерфейса Сборщика ---
    def _disable_export_settings(self):
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            for child in tab.winfo_children():
                try: child.configure(state=tk.DISABLED)
                except: pass
                
        # Явно отключаем вложенные элементы
        try:
            self.ent_pause.configure(state=tk.DISABLED)
            self.btn_apply_pause.configure(state=tk.DISABLED)
        except: pass

    def _enable_export_settings(self, is_group=True):
        for tab in (self.grp_tab_basic, self.grp_tab_tags):
            for child in tab.winfo_children():
                try: child.configure(state=tk.NORMAL)
                except: pass
                
        # Явно включаем вложенные элементы
        try:
            self.ent_pause.configure(state=tk.NORMAL)
            self.btn_apply_pause.configure(state=tk.NORMAL)
        except: pass
        
        if not is_group:
            self.chk_merge.configure(state=tk.DISABLED)
            self.chk_subfolder.configure(state=tk.DISABLED)
            self.ent_pause.configure(state=tk.DISABLED)
            self.btn_apply_pause.configure(state=tk.DISABLED)
            self.lbl_grp_name.config(text="Название трека (Title):")
            self.group_settings_frame.config(text="Настройки файла")
            self.btn_apply_to_parent.configure(state=tk.NORMAL)
        else:
            self.lbl_grp_name.config(text="Имя группы (имя файла/папки):")
            self.group_settings_frame.config(text="Настройки группы")
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
        
        is_group = item in self.export_groups
        self._enable_export_settings(is_group)
        
        settings = self.export_groups.get(item) if is_group else self.export_files.get(item)
        if not settings: 
            self._is_updating_ui = False
            return
        
        self.grp_name_var.set(settings.get("name" if is_group else "title", ""))
        self.grp_artist_var.set(settings.get("artist", ""))
        self.grp_album_var.set(settings.get("album", ""))
        self.grp_composer_var.set(settings.get("composer", ""))
        self.grp_year_var.set(settings.get("year", ""))
        self.grp_cover_var.set(settings.get("cover", ""))
        
        if is_group:
            self.grp_merge_var.set(settings.get("merge", True))
            self.grp_subfolder_var.set(settings.get("subfolder", True))
            self.grp_pause_var.set(settings.get("pause", 1000))
            
        self._is_updating_ui = False # Разблокируем сохранение

    def save_export_item_settings(self, *args):
        # Если поля обновляются программно при клике, игнорируем сохранение
        if getattr(self, '_is_updating_ui', False): return 
        
        item = self.current_selected_export_item
        if not item: return
        
        is_group = item in self.export_groups
        target_dict = self.export_groups if is_group else self.export_files
        
        if is_group:
            target_dict[item]["name"] = self.grp_name_var.get()
            target_dict[item]["merge"] = self.grp_merge_var.get()
            target_dict[item]["subfolder"] = self.grp_subfolder_var.get()
            target_dict[item]["pause"] = self.grp_pause_var.get()
        else:
            target_dict[item]["title"] = self.grp_name_var.get()
            
        target_dict[item]["artist"] = self.grp_artist_var.get()
        target_dict[item]["album"] = self.grp_album_var.get()
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
    
    def apply_tags_mass(self, scope="group_files"):
        if not self.current_selected_export_item: return
        
        artist = self.grp_artist_var.get()
        album = self.grp_album_var.get()
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
            for g_id in self.export_groups:
                apply_to_item(g_id)
                for f_id in self.export_tree.get_children(g_id):
                    apply_to_item(f_id)
            messagebox.showinfo("Успех", "Теги применены абсолютно ко всем группам и файлам!")

    def move_export_item(self, direction):
        selected = self.export_tree.selection()
        if not selected: return
        for item in selected:
            parent = self.export_tree.parent(item)
            idx = self.export_tree.index(item)
            self.export_tree.move(item, parent, idx + direction)

    def add_export_group(self, name=None):
        g_id = f"group_{uuid.uuid4().hex[:8]}"
        
        if not name:
            # Читаем шаблон из нового поля в интерфейсе
            template = self.settings_vars["default_group_name"].get()
            num = 1
            while True:
                g_name = template.replace("{num}", str(num))
                if not any(g["name"] == g_name for g in self.export_groups.values()):
                    break
                num += 1
        else:
            g_name = name
            
        self.export_groups[g_id] = {
            "name": g_name, "merge": True, "subfolder": True, 
            "pause": self.config.get("default_group_pause", 1000),
            "artist": "", "album": "", "composer": "", "year": "", "cover": ""
        }
        self.export_tree.insert("", tk.END, iid=g_id, text=g_name, open=True)
        self.export_tree.selection_set(g_id)
        return g_id

    def add_export_files(self, files=None, target_group=None):
        if files is None:
            files = filedialog.askopenfilenames(filetypes=[("Audio Files", "*.mp3 *.wav *.ogg")])
        if not files: return
        
        if not self.export_groups: self.add_export_group()
        
        if not target_group:
            selected = self.export_tree.selection()
            target_group = selected[0] if selected else self.export_tree.get_children()[-1]
            if self.export_tree.parent(target_group): target_group = self.export_tree.parent(target_group)
        
        self.lbl_export_status.config(text="Чтение тегов и извлечение обложек...", foreground="orange")
        self.root.update()
        
        for f in files:
            meta = self.get_audio_metadata(f)
            f_id = f"file_{uuid.uuid4().hex[:8]}"
            
            self.export_files[f_id] = {
                "path": f,
                "title": meta["title"],
                "artist": meta["artist"],
                "album": meta["album"],
                "composer": meta["composer"],
                "year": meta["year"],
                "cover": meta["cover"] # <-- Теперь обложка подтягивается!
            }
            self.export_tree.insert(target_group, tk.END, iid=f_id, text=meta["title"], values=(self.format_duration(meta["duration"]),))
            
        self.update_group_duration(target_group)
        self.lbl_export_status.config(text="Ожидание...", foreground="blue")

    def add_export_folder(self):
        folder = filedialog.askdirectory()
        if not folder: return
        
        create_group = messagebox.askyesno("Добавление папки", f"Создать отдельную группу для папки '{Path(folder).name}'?\n\nДа - создать группу\nНет - добавить в текущую")
        
        files = sorted([str(p) for p in Path(folder).glob("*.*") if p.suffix.lower() in ['.mp3', '.wav', '.ogg']])
        if not files:
            messagebox.showinfo("Пусто", "В папке нет аудиофайлов.")
            return
            
        target_group = self.add_export_group(name=Path(folder).name) if create_group else None
        self.add_export_files(files=files, target_group=target_group)

    def get_audio_metadata(self, filepath):
        """Читает длительность, теги и извлекает обложку через ffprobe/ffmpeg"""
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", filepath]
        try:
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            out = subprocess.check_output(cmd, startupinfo=startupinfo)
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
                ffmpeg_cmd = ["ffmpeg", "-y", "-i", filepath, "-an", "-vframes", "1", str(cover_file)]
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo)
                
                if cover_file.exists():
                    cover_path = str(cover_file.resolve())

            return {
                "duration": duration,
                "title": tags.get("title", Path(filepath).stem),
                "artist": tags.get("artist", ""),
                "album": tags.get("album", ""),
                "composer": tags.get("composer", ""),
                "year": tags.get("date", tags.get("year", "")),
                "cover": cover_path
            }
        except Exception as e:
            logging.error(f"Ошибка чтения метаданных {filepath}: {e}")
            return {"duration": 0.0, "title": Path(filepath).stem, "artist": "", "album": "", "composer": "", "year": "", "cover": ""}

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

    def remove_export_items(self):
        selected = self.export_tree.selection()
        groups_to_update = set()
        
        for item in selected:
            if item in self.export_groups:
                del self.export_groups[item]
            elif item in self.export_files:
                del self.export_files[item]
                parent = self.export_tree.parent(item)
                if parent: groups_to_update.add(parent)
            self.export_tree.delete(item)
            
        for g in groups_to_update:
            if self.export_tree.exists(g): self.update_group_duration(g)

    def auto_split_export(self):
        all_files = []
        for g_id in self.export_tree.get_children():
            for f_id in self.export_tree.get_children(g_id):
                all_files.append(f_id)
                
        if not all_files:
            messagebox.showinfo("Пусто", "Сначала добавьте аудиофайлы.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Авто-разбивка")
        dialog.geometry("350x200")
        
        ttk.Label(dialog, text="Максимальная длительность группы (минут):").pack(pady=(10, 0))
        limit_var = tk.IntVar(value=60)
        ttk.Entry(dialog, textvariable=limit_var, justify=tk.CENTER).pack(pady=5)
        
        ttk.Label(dialog, text="Шаблон имени группы (доступно {num}):").pack(pady=(10, 0))
        # Берем текущее значение из главной панели
        template_var = tk.StringVar(value=self.settings_vars["default_group_name"].get())
        ttk.Entry(dialog, textvariable=template_var, justify=tk.CENTER).pack(fill=tk.X, padx=20, pady=5)
        
        def do_split():
            limit_sec = limit_var.get() * 60
            template = template_var.get()
            
            # Обновляем главную панель и сохраняем
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
            
            # --- Умная группировка "в памяти" для расчета нулей (zfill) ---
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
            
            # Динамический padding (если групп 15, будет 2 нуля. Если 150 - 3 нуля)
            pad = max(2, len(str(len(groups_data))))
            
            # Очищаем дерево
            for item in self.export_tree.get_children(): 
                self.export_tree.delete(item)
            self.export_groups.clear()
            
            # Создаем группы и вставляем файлы
            for idx, group_files in enumerate(groups_data, 1):
                g_name = template.replace("{num}", str(idx).zfill(pad))
                g_id = self.add_export_group(name=g_name)
                for f_id in group_files:
                    title = self.export_files[f_id]["title"]
                    self.export_tree.insert(g_id, tk.END, iid=f_id, text=title, values=(self.format_duration(file_durs[f_id]),))
                self.update_group_duration(g_id)
            
        ttk.Button(dialog, text="Разбить", command=do_split).pack(pady=10)

    # --- Процесс Экспорта ---
    def stop_export_process(self):
        self.is_export_stopped = True
        self.btn_export_stop.config(state=tk.DISABLED)
        self.lbl_export_status.config(text="Остановка сборки (ожидание завершения текущего файла)...", foreground="orange")

    def start_export_process(self):
        groups = self.export_tree.get_children()
        if not groups: return
        
        out_dir = Path(self.export_outdir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        
        self.save_settings() 
        self.btn_export_start.config(state=tk.DISABLED)
        self.btn_export_stop.config(state=tk.NORMAL) # Активируем Стоп
        self.export_progress['value'] = 0
        self.is_export_stopped = False # Сбрасываем флаг остановки
        
        def run_export():
            try:
                total_groups = len(groups)
                apply_fx = self.export_apply_fx_var.get()
                
                if apply_fx:
                    sp = float(self.config.get("fx_speed", 1.0))
                    pt = float(self.config.get("fx_pitch", 1.0))
                    ec = bool(self.config.get("fx_echo", False))
                    ed = int(self.config.get("fx_echo_delay", 300))
                    ey = float(self.config.get("fx_echo_decay", 0.3))
                else:
                    sp, pt, ec, ed, ey = 1.0, 1.0, False, 300, 0.3
                    
                scale_p = bool(self.config.get("scale_pauses", True))
                fmt = self.export_fmt_var.get()
                bitrate = self.export_bitrate_var.get()
                
                for g_idx, g_id in enumerate(groups):
                    if self.is_export_stopped: break # Проверка остановки
                    
                    g_set = self.export_groups[g_id]
                    g_name = g_set["name"]
                    files = self.export_tree.get_children(g_id)
                    if not files: continue
                    
                    self.root.after(0, lambda n=g_name: self.lbl_export_status.config(text=f"Обработка: {n}...", foreground="black"))
                    
                    def build_tags(settings_dict):
                        t = {}
                        if settings_dict.get("title"): t["title"] = settings_dict["title"]
                        if settings_dict.get("artist"): t["artist"] = settings_dict["artist"]
                        if settings_dict.get("album"): t["album"] = settings_dict["album"]
                        if settings_dict.get("composer"): t["composer"] = settings_dict["composer"]
                        if settings_dict.get("year"): t["date"] = settings_dict["year"]
                        return t

                    if g_set["merge"]:
                        final_audio = AudioSegment.empty()
                        pause_ms = g_set["pause"]
                        if apply_fx and scale_p and sp != 1.0: pause_ms = int(pause_ms / sp)
                        pause_seg = AudioSegment.silent(duration=pause_ms)
                        
                        for i, f_id in enumerate(files):
                            if self.is_export_stopped: break # Проверка остановки внутри склейки
                            fp = self.export_files[f_id]["path"]
                            final_audio += AudioSegment.from_file(fp)
                            if i < len(files) - 1 and pause_ms > 0: final_audio += pause_seg
                            
                        if self.is_export_stopped: break
                            
                        if apply_fx:
                            self.root.after(0, lambda: self.lbl_export_status.config(text=f"Применение эффектов и сохранение {g_name}...", foreground="orange"))
                            final_audio = AudioEffects.apply_effects(final_audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                        else:
                            self.root.after(0, lambda: self.lbl_export_status.config(text=f"Сохранение {g_name}...", foreground="orange"))
                        
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
                            if self.is_export_stopped: break # Проверка остановки
                            
                            f_set = self.export_files[f_id]
                            fp = f_set["path"]
                            
                            self.root.after(0, lambda f=f_set['title']: self.lbl_export_status.config(text=f"Конвертация: {f}...", foreground="orange"))
                            audio = AudioSegment.from_file(fp)
                            
                            if apply_fx:
                                audio = AudioEffects.apply_effects(audio, speed=sp, pitch=pt, echo=ec, echo_delay=ed, echo_decay=ey)
                            
                            export_kwargs = {"format": fmt}
                            if fmt == "mp3": export_kwargs["bitrate"] = bitrate
                            
                            f_tags = build_tags(f_set)
                            for k in ["artist", "album", "composer", "date"]:
                                if k not in f_tags and build_tags(g_set).get(k):
                                    f_tags[k] = build_tags(g_set)[k]
                                    
                            if f_tags: export_kwargs["tags"] = f_tags
                            
                            cov = f_set.get("cover") or g_set.get("cover")
                            if cov and os.path.exists(cov): export_kwargs["cover"] = cov
                            
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', f_set["title"])
                            out_file = target_dir / f"{safe_name}.{fmt}"
                            audio.export(out_file, **export_kwargs)
                            
                    pct = int(((g_idx + 1) / total_groups) * 100)
                    self.root.after(0, lambda p=pct: self.export_progress.config(value=p))
                    
                if self.is_export_stopped:
                    self.root.after(0, lambda: self.lbl_export_status.config(text="Сборка прервана!", foreground="red"))
                    self.root.after(0, lambda: messagebox.showwarning("Остановлено", "Процесс сборки был прерван пользователем."))
                else:
                    self.root.after(0, lambda: self.lbl_export_status.config(text="Готово!", foreground="green"))
                    self.root.after(0, lambda: messagebox.showinfo("Успех", f"Сборка успешно завершена!\nСохранено в: {out_dir}"))
                
            except Exception as e:
                logging.error(f"Ошибка сборки: {e}")
                self.root.after(0, lambda: self.lbl_export_status.config(text="Ошибка!", foreground="red"))
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Произошла ошибка при сборке:\n{e}"))
            finally:
                self.root.after(0, lambda: self.btn_export_start.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_export_stop.config(state=tk.DISABLED))

        threading.Thread(target=run_export, daemon=True).start()

    # --- Вкладка "Настройки" ---
    def setup_settings_tab(self):
        canvas = tk.Canvas(self.tab_settings)
        scrollbar = ttk.Scrollbar(self.tab_settings, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        set_notebook = ttk.Notebook(scrollable_frame)
        set_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        tab_api = ttk.Frame(set_notebook, padding=10)
        tab_folders = ttk.Frame(set_notebook, padding=10)
        tab_pauses = ttk.Frame(set_notebook, padding=10)
        tab_cache = ttk.Frame(set_notebook, padding=10)
        tab_effects = ttk.Frame(set_notebook, padding=10) # <-- НОВАЯ ВКЛАДКА
        tab_output = ttk.Frame(set_notebook, padding=10)
        
        set_notebook.add(tab_api, text="API и Лимиты")
        set_notebook.add(tab_folders, text="Папки")
        set_notebook.add(tab_pauses, text="Паузы и Разделители")
        set_notebook.add(tab_cache, text="Обработка и Кэш")
        set_notebook.add(tab_effects, text="Эффекты (Постобработка)") # <-- НОВАЯ ВКЛАДКА
        set_notebook.add(tab_output, text="Вывод и Теги")

        def add_entry(parent, label, key, row, vtype=tk.StringVar):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            var = vtype(value=self.config.get(key, ""))
            self.settings_vars[key] = var
            ttk.Entry(parent, textvariable=var, width=40).grid(row=row, column=1, sticky=tk.W, pady=2, padx=5)

        def add_combobox(parent, label, key, row, values):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            var = tk.StringVar(value=self.config.get(key, values[0]))
            self.settings_vars[key] = var
            cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=37)
            cb.grid(row=row, column=1, sticky=tk.W, pady=2, padx=5)

        def add_dir_entry(parent, label, key, row, is_file=False):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=2, padx=5)
            var = tk.StringVar(value=self.config.get(key, ""))
            self.settings_vars[key] = var
            f = ttk.Frame(parent)
            f.grid(row=row, column=1, sticky=tk.W, pady=2, padx=5)
            ttk.Entry(f, textvariable=var, width=33).pack(side=tk.LEFT)
            cmd = lambda: var.set(filedialog.askopenfilename() if is_file else filedialog.askdirectory() or var.get())
            ttk.Button(f, text="📁", width=3, command=cmd).pack(side=tk.LEFT, padx=2)

        def add_check(parent, label, key, row):
            var = tk.BooleanVar(value=self.config.get(key, False))
            self.settings_vars[key] = var
            ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=2, padx=5)

        add_entry(tab_api, "API Token:", "api_token", 0)
        add_entry(tab_api, "API URL:", "api_url", 1)
        add_entry(tab_api, "Спикер (Голос):", "speaker", 2)
        ttk.Separator(tab_api, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)
        add_entry(tab_api, "Макс. запросов API:", "api_max_requests", 4, tk.IntVar)
        add_entry(tab_api, "Окно времени (сек):", "api_time_window", 5, tk.DoubleVar)
        add_entry(tab_api, "Кол-во попыток при ошибке:", "max_retries", 6, tk.IntVar)

        add_dir_entry(tab_folders, "Папка с текстами:", "input_dir", 0)
        add_dir_entry(tab_folders, "Папка для аудио:", "output_dir", 1)
        add_dir_entry(tab_folders, "Папка для кэша:", "cache_dir", 2)

        add_entry(tab_pauses, "Начало файла (мс):", "pause_file_start", 0, tk.IntVar)
        add_entry(tab_pauses, "Конец файла (мс):", "pause_file_end", 1, tk.IntVar)
        add_entry(tab_pauses, "Между предложениями (мс):", "pause_sentence", 2, tk.IntVar)
        add_entry(tab_pauses, "Между абзацами (мс):", "pause_paragraph", 3, tk.IntVar)
        add_entry(tab_pauses, "Перед диалогом (мс):", "pause_speech", 4, tk.IntVar)
        add_entry(tab_pauses, "Перед двоеточием (мс):", "pause_colon", 5, tk.IntVar)
        ttk.Separator(tab_pauses, orient=tk.HORIZONTAL).grid(row=6, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(tab_pauses, text="Символы-разделители\n(каждый с новой строки):").grid(row=7, column=0, sticky=tk.NW, pady=2, padx=5)
        self.txt_separators = tk.Text(tab_pauses, height=6, width=30)
        self.txt_separators.grid(row=7, column=1, sticky=tk.W, pady=2, padx=5)

        add_check(tab_cache, "Авто-исправление аббревиатур (И.И. -> И-И)", "auto_abbreviations", 0)
        add_check(tab_cache, "Авто-сокращения (г., ул., ур. -> г, ул, ур)", "auto_short_words", 1)
        add_check(tab_cache, "Авто-обрезка тишины от Silero", "auto_trim_silence", 2)
        add_entry(tab_cache, "Порог тишины (dBFS):", "silence_threshold", 3, tk.DoubleVar)
        ttk.Separator(tab_cache, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)
        add_check(tab_cache, "Включить кэширование", "use_cache", 5)
        add_check(tab_cache, "Авто-очистка кэша (LRU/TTL)", "auto_clean_cache", 6)
        add_entry(tab_cache, "Сохранять индекс каждые N фраз:", "cache_save_frequency", 7, tk.IntVar)
        add_entry(tab_cache, "Макс. записей в кэше:", "cache_max_entries", 8, tk.IntVar)
        add_entry(tab_cache, "Время жизни кэша (часов):", "cache_ttl_hours", 9, tk.DoubleVar)

        # --- 5. Эффекты (Постобработка) ---
        ttk.Label(tab_effects, text="Эти эффекты применяются к аудио ПОСЛЕ генерации (без затрат API).", font=("", 9, "italic"), foreground="gray").grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        
        # Скорость
        ttk.Label(tab_effects, text="Скорость (Tempo):").grid(row=1, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_speed"] = tk.DoubleVar(value=self.config.get("fx_speed", 1.0))
        lbl_speed_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_speed'].get():.1f}x", width=5)
        lbl_speed_val.grid(row=1, column=2, sticky=tk.W)
        scale_speed = ttk.Scale(tab_effects, from_=0.5, to_=3.0, variable=self.settings_vars["fx_speed"], command=lambda v: lbl_speed_val.config(text=f"{float(v):.1f}x"))
        scale_speed.grid(row=1, column=1, sticky=tk.EW, padx=10)
        
        # Тон
        ttk.Label(tab_effects, text="Тон (Pitch):").grid(row=2, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_pitch"] = tk.DoubleVar(value=self.config.get("fx_pitch", 1.0))
        lbl_pitch_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_pitch'].get():.2f}", width=5)
        lbl_pitch_val.grid(row=2, column=2, sticky=tk.W)
        scale_pitch = ttk.Scale(tab_effects, from_=0.5, to_=2.0, variable=self.settings_vars["fx_pitch"], command=lambda v: lbl_pitch_val.config(text=f"{float(v):.2f}"))
        scale_pitch.grid(row=2, column=1, sticky=tk.EW, padx=10)
        
        ttk.Separator(tab_effects, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        
        # Эхо
        add_check(tab_effects, "Включить Эхо (Reverb/Delay)", "fx_echo", 4)
        
        ttk.Label(tab_effects, text="Задержка эхо (мс):").grid(row=5, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_echo_delay"] = tk.IntVar(value=self.config.get("fx_echo_delay", 300))
        lbl_delay_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_echo_delay'].get()}мс", width=5)
        lbl_delay_val.grid(row=5, column=2, sticky=tk.W)
        scale_delay = ttk.Scale(tab_effects, from_=50, to_=1000, variable=self.settings_vars["fx_echo_delay"], command=lambda v: lbl_delay_val.config(text=f"{int(float(v))}мс"))
        scale_delay.grid(row=5, column=1, sticky=tk.EW, padx=10)

        ttk.Label(tab_effects, text="Сила эхо (Decay):").grid(row=6, column=0, sticky=tk.W, pady=5, padx=5)
        self.settings_vars["fx_echo_decay"] = tk.DoubleVar(value=self.config.get("fx_echo_decay", 0.3))
        lbl_decay_val = ttk.Label(tab_effects, text=f"{self.settings_vars['fx_echo_decay'].get():.1f}", width=5)
        lbl_decay_val.grid(row=6, column=2, sticky=tk.W)
        scale_decay = ttk.Scale(tab_effects, from_=0.1, to_=0.8, variable=self.settings_vars["fx_echo_decay"], command=lambda v: lbl_decay_val.config(text=f"{float(v):.1f}"))
        scale_decay.grid(row=6, column=1, sticky=tk.EW, padx=10)

        ttk.Separator(tab_effects, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=3, sticky="ew", pady=10)
        add_check(tab_effects, "Масштабировать паузы (сокращать тишину при ускорении)", "scale_pauses", 8)
        
        tab_effects.columnconfigure(1, weight=1)
        # ----------------------------------------------


        add_combobox(tab_output, "Режим синтеза:", "synthesis_mode", 0, ["sentence", "paragraph", "full"])
        add_combobox(tab_output, "Формат аудио:", "output_format", 1, ["mp3", "wav", "ogg"])
        add_combobox(tab_output, "Битрейт (для mp3):", "output_bitrate", 2, ["64k", "128k", "192k", "256k", "320k"])
        ttk.Separator(tab_output, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(tab_output, text="Теги ID3 (для mp3/ogg):").grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=5)
        add_entry(tab_output, "Название трека ({filename}):", "tag_title", 5)
        add_entry(tab_output, "Исполнитель:", "tag_artist", 6)
        add_entry(tab_output, "Альбом:", "tag_album", 7)
        add_entry(tab_output, "Композитор:", "tag_composer", 8)
        add_entry(tab_output, "Год:", "tag_year", 9)
        add_dir_entry(tab_output, "Обложка (jpg/png):", "tag_cover", 10, is_file=True)

        btn_frame = ttk.Frame(scrollable_frame, padding=10)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="💾 Сохранить", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Загрузить конфиг", command=self.import_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📤 Экспорт конфига", command=self.export_config).pack(side=tk.LEFT, padx=5) # <-- Новая кнопка
        ttk.Button(btn_frame, text="🔄 Сбросить", command=self.reset_config).pack(side=tk.LEFT, padx=5)
        
        self.set_ui_from_config()

    def import_config(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if filepath:
            self.config = self.load_settings(filepath)
            self.set_ui_from_config()
            self.save_settings()
            messagebox.showinfo("Успех", "Настройки загружены!")

    def reset_config(self):
        self.config = DEFAULT_CONFIG.copy()
        self.set_ui_from_config()
        self.save_settings()

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

        ttk.Label(frame, text="Редактор glossary.json:").pack(anchor=tk.W)
        self.txt_glossary = tk.Text(frame, wrap=tk.WORD, font=("Courier", 10))
        self.txt_glossary.pack(fill=tk.BOTH, expand=True, pady=5)
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="💾 Сохранить файл", command=self.save_glossary_ui).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="🔄 Перезагрузить", command=self.load_glossary_ui).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Импорт", command=self.import_glossary).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="📤 Экспорт", command=self.export_glossary).pack(side=tk.RIGHT, padx=5)

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
        w2 = self.glos_word2.get().strip()
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
                if not w2: return
                if strict: data.setdefault("terms_strict_case", {})[w1] = w2
                else: data.setdefault("terms_ignore_case", {})[w1] = w2
            elif gtype == "regex":
                if not w2: return
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
        
        self.lbl_cache_count = ttk.Label(search_frame, text="Всего записей: 0", foreground="blue")
        self.lbl_cache_count.pack(side=tk.RIGHT, padx=5)
        
        ttk.Label(frame, text="💡 Вы можете выделять несколько строк мышкой с зажатым Ctrl или Shift", font=("", 8, "italic"), foreground="gray").pack(anchor=tk.W, pady=(0,5))

        columns = ("hash", "text", "speaker", "uses")
        # selectmode="extended" разрешает выделение множества строк
        self.cache_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
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
        top.title(f"Детали кэша: {hash_key}")
        top.geometry("750x500") # Чуть увеличил высоту для ползунков
        
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
                
        btn_play = ttk.Button(top, text="🔊 Слушать с эффектами", command=play_cache_audio)
        btn_play.pack(anchor=tk.W, padx=10, pady=5)

    def load_cache_ui(self):
        self.cache_data = {}
        cache_path = Path(self.config.get("cache_dir", "cache_audio")) / "sentence_cache.json"
        if cache_path.exists():
            with open(cache_path, 'r', encoding='utf-8') as f:
                self.cache_data = json.load(f)
        self.lbl_cache_count.config(text=f"Всего записей: {len(self.cache_data)}")
        self.filter_cache()

    def filter_cache(self):
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

    def optimize_cache(self):
        if not messagebox.askyesno("Оптимизация", "Скрипт просканирует папку с текстами и удалит из кэша все аудиофрагменты, которых нет в текущих текстах.\n\n(Будут сохранены фразы для всех режимов: по предложениям, абзацам и целиком).\n\nПродолжить?"):
            return
            
        self.save_settings()
        processor = TTSProcessor(self.config)
        
        def run_opt():
            txt_files = list(Path(self.config["input_dir"]).glob("*.txt"))
            
            # ЗАЩИТА 1: Проверяем, есть ли файлы для сканирования
            if not txt_files:
                self.root.after(0, lambda: messagebox.showwarning("Отмена", f"В папке '{self.config['input_dir']}' не найдено текстовых файлов (.txt).\nОптимизация отменена, чтобы защитить кэш."))
                return

            processor.cache = processor._load_cache() # Перезагружаем свежий кэш с диска
            
            if not processor.cache:
                self.root.after(0, lambda: messagebox.showinfo("Информация", "Кэш пуст."))
                return

            required_hashes = set()
            errors_occurred = False
            
            # 1. Собираем все необходимые хэши из всех текстовых файлов
            for f in txt_files:
                try:
                    with open(f, 'r', encoding='utf-8') as file: 
                        raw_text = file.read()
                    
                    file_hashes = processor.get_all_possible_hashes(raw_text)
                    required_hashes.update(file_hashes)
                except Exception as e:
                    logging.error(f"Ошибка при сканировании {f.name}: {e}")
                    errors_occurred = True

            # ЗАЩИТА 2: Если во время сбора хэшей ничего не собралось, не удаляем кэш!
            if not required_hashes:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось извлечь хэши из текстов. Оптимизация отменена для защиты данных."))
                return

            # 2. Ищем ключи в кэше, которых нет в required_hashes
            keys_to_delete = [k for k in processor.cache.keys() if k not in required_hashes]
            
            # 3. Удаляем только неиспользуемое
            for k in keys_to_delete: 
                processor._delete_cache_entry(k)
                
            if keys_to_delete:
                processor.unsaved_cache_items += len(keys_to_delete)
                processor._save_cache()
                msg = f"Оптимизация завершена.\nУдалено неиспользуемых записей: {len(keys_to_delete)}"
                if errors_occurred:
                    msg += "\n\n(Внимание: во время чтения некоторых файлов возникли ошибки, подробнее в логе)"
                self.root.after(0, lambda: messagebox.showinfo("Успех", msg))
            else:
                self.root.after(0, lambda: messagebox.showinfo("Готово", "Оптимизация завершена. Лишних записей не найдено."))
                
            # Обновляем таблицу в интерфейсе
            self.root.after(0, self.load_cache_ui)
            
        # Запускаем в фоновом потоке
        threading.Thread(target=run_opt).start()

    def clear_entire_cache(self):
        """Полная очистка кэша"""
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
        
        def run_zip():
            try:
                base_name = out_zip.replace('.zip', '')
                shutil.make_archive(base_name, 'zip', cache_dir)
                if self.del_after_zip.get():
                    shutil.rmtree(cache_dir)
                    Path(cache_dir).mkdir(exist_ok=True)
                messagebox.showinfo("Успех", f"Архив создан:\n{out_zip}")
                self.root.after(0, self.load_cache_ui)
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось создать архив:\n{e}")
                
        threading.Thread(target=run_zip).start()

    # --- Вкладка "Справка" ---
    def setup_help_tab(self):
        self.help_text_widget = tk.Text(self.tab_help, wrap=tk.WORD, font=("Arial", 10), padx=10, pady=10)
        self.help_text_widget.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(self.tab_help, command=self.help_text_widget.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.help_text_widget.config(yscrollcommand=scrollbar.set)
        
        help_text = r"""Добро пожаловать в Silero TTS Studio!

Это профессиональный инструмент для генерации аудиокниг, подкастов и озвучки текста с помощью нейросети Silero. Программа поддерживает умное кэширование, постобработку звука, работу с глоссариями, импорт электронных книг и пакетную сборку файлов.

🚀 БЫСТРЫЙ СТАРТ
1. Во вкладке "Настройки" -> "API и Лимиты" введите ваш API Token.
2. Во вкладке "Импорт книг" выберите файл вашей книги (EPUB, FB2, DOCX, TXT) и нажмите "Извлечь и Нарезать". Скрипт сам разобьет книгу на главы.
3. Перейдите во вкладку "Синтез из папки" и нажмите "Старт".
4. Готовые аудиофайлы появятся в папке вывода.

⚙️ ПОРЯДОК ОБРАБОТКИ ТЕКСТА
Программа бережно относится к вашему тексту и обрабатывает его строго в следующем порядке:
1. Математические плюсы (1 + 1) и изолированные плюсы ( + ) заменяются на слово "плюс".
2. Применяются правила RegEx (Паттерны) из Глоссария.
3. Текст разбивается на предложения.
4. Применяются Термины и Ударения (+) из Глоссария.
5. Срабатывают Авто-аббревиатуры (И.И. -> И-И).
6. Срабатывают Авто-сокращения (г. -> г).
7. Нормализатор переводит цифры в слова (10 -> десять).
8. Если на этапах 4-6 пропала точка в конце предложения, она возвращается на место.
9. Удаляются все спецсимволы, текст отправляется в нейросеть.

📚 ГЛОССАРИЙ
• Ударения (+): Введите слово со знаком +, например: з+амок. (Скрипт сам поймет, что оригинальное слово — "замок" и применит правило только к нему).
• Замена термина: Исходное слово (например, "ОС") и замена ("операционная система").
• RegEx (Паттерн): Применяется ДО разбивки текста на предложения.
  Пример: Заменить "Глава 1 - Название" на "Глава 1. Название":
  Паттерн: (Глава \d+)\s*-\s*(.*)   |   Замена: \1. \2

🛠 АВТО-ИСПРАВЛЕНИЯ
• Авто-аббревиатуры: Превращает "к.п.д.", "И.И." в "к-п-д", "И-И".
• Авто-сокращения: Убирает точки у слов из 1-3 букв ("г.", "ул.", "ур."), чтобы нейросеть не делала паузу посреди предложения.

🎛 ЭФФЕКТЫ (ПОСТОБРАБОТКА)
Вы можете изменять звучание голоса без повторных запросов к API. Эффекты накладываются локально (FFmpeg).
• Скорость (Tempo) и Тон (Pitch): Ускорение речи и изменение высоты голоса.
• Эхо (Reverb/Delay): Настройка задержки и силы эха (идеально для мыслей персонажей).
• Масштабирование пауз: При ускорении речи паузы между предложениями будут пропорционально сокращаться.
*Тестирование:* Во вкладках "Прямой синтез" и "Кэш" есть локальные ползунки. Вы можете крутить их и слушать результат. Чтобы применить их ко всей книге, нажмите "Сделать глобальными".

💾 КЭШ, ПРОДОЛЖЕНИЕ РАБОТЫ И ОСТАНОВКА
• Пропуск готовых файлов: Если синтез прервался, при следующем запуске просто нажмите "Старт". Программа проверит папку с аудио и продолжит работу с того места, где остановилась.
• Кнопка "Стоп": Мягкая остановка. Программа дождется ответа от сервера на текущую фразу, сохранит кэш и остановится.
• Кнопка "Принудительно" (☠️): Жесткая остановка. Если API зависло и не отвечает, эта кнопка мгновенно сбросит фоновый поток и принудительно сохранит весь накопленный кэш на диск.
• Оптимизация кэша: Просканирует папку с текстами и удалит из кэша все аудиофрагменты, которых нет в текущих текстах (с учетом всех режимов синтеза).

🎵 ЭКСПОРТ И СБОРКА
• Теги и Обложки: Вы можете задать метаданные как глобально, так и для каждой отдельной группы файлов.
• Авто-разбивка: Закиньте 100 глав и нажмите "Авто-разбивка", чтобы программа автоматически разложила их по томам (например, по 60 минут).
• Пакетная конвертация: Если снять галочку "Склеить", программа применит теги, эффекты и битрейт к каждому файлу индивидуально и сохранит их в отдельную папку.
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
        self.lbl_current_text.config(text="Ожидание...", foreground="black")

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
            "processing": ("🔄 Обработка", "processing"),
            "success": ("✅ Готово", "success"),
            "warning": ("⚠️ С ошибками", "warning"),
            "error": ("❌ Ошибка", "error")
        }
        text, tag = status_map.get(status_code, ("?", "queued"))
        self.tree.item(filename, values=(text, filename), tags=(tag,))

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
        self.lbl_current_text.config(text="Подготовка...", foreground="black")
        
        self.processor = TTSProcessor(self.config, error_callback=self.show_critical_error)
        self.processing_thread = threading.Thread(target=self.process_queue, args=(items_to_process,))
        self.processing_thread.start()

    def stop_processing(self):
        if self.processor: self.processor.is_stopped = True
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_current_text.config(text="Остановка (ожидание завершения текущего запроса)...", foreground="orange")

    def hard_stop_processing(self):
        if self.processor: 
            self.processor.is_stopped = True
            self.processor._save_cache()
        
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)
        self.btn_start_all.config(state=tk.NORMAL)
        self.btn_start_sel.config(state=tk.NORMAL)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_remove_sel.config(state=tk.NORMAL)
        
        self.lbl_current_text.config(text="Принудительно остановлено. Кэш сохранен.", foreground="red")
        messagebox.showwarning("Принудительная остановка", "Процесс прерван. Текущий файл не будет сохранен, но весь сгенерированный кэш записан на диск.")

    def finish_processing(self):
        self.btn_start_all.config(state=tk.NORMAL)
        self.btn_start_sel.config(state=tk.NORMAL)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_remove_sel.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_hard_stop.config(state=tk.DISABLED)
        
        self.lbl_current_text.config(text="Ожидание...", foreground="black")
        if self.processor and self.processor.is_stopped:
            if self.lbl_current_text.cget("text") != "Принудительно остановлено. Кэш сохранен.":
                messagebox.showwarning("Остановлено", "Обработка была прервана.")
        else:
            messagebox.showinfo("Готово", "Все выбранные файлы обработаны!")

    def process_queue(self, items_to_process):
        total_files = len(items_to_process)
        
        status_file = APP_DATA_DIR / "processing_statuses.json"
        statuses = {}
        if status_file.exists():
            try:
                with open(status_file, 'r', encoding='utf-8') as f: statuses = json.load(f)
            except: pass

        skip_existing = self.settings_vars["skip_existing"].get()
        input_dir = Path(self.config["input_dir"])
        
        for idx, item_id in enumerate(items_to_process):
            if self.processor.is_stopped: break
            
            # item_id - это имя файла (например, "01_Глава.txt"), берем его из таблицы
            filepath = input_dir / item_id
            
            if not filepath.exists():
                self.root.after(0, self.update_file_status, item_id, "error")
                continue
            
            out_filename = filepath.with_suffix(f'.{self.config["output_format"]}').name
            out_filepath = Path(self.config["output_dir"]) / out_filename
            
            if skip_existing and out_filepath.exists():
                file_status = statuses.get(str(out_filepath.resolve()), "success")
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

            self.processor.process_text_file(filepath, progress_callback=on_progress, completion_callback=on_complete)

        for t in self.processor.active_threads: t.join()
        self.root.after(0, self.finish_processing)

    def start_direct_processing(self):
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
        self.btn_direct_hard_stop.config(state=tk.NORMAL) # Активируем кнопку
        self.lbl_direct_status.config(text="Обработка...", foreground="black")
        
        self.processor = TTSProcessor(self.config, error_callback=self.show_critical_error)
        
        def run_direct():
            def on_progress(current, total, txt):
                self.root.after(0, lambda: self.lbl_direct_status.config(text=f"Синтез: {current}/{total}...", foreground="blue"))
            def on_complete(fname, status, audio=None):
                msg = f"Готово! Сохранено в {fname}" if save_file else "Готово! (Не сохранено)"
                self.root.after(0, lambda: self.lbl_direct_status.config(text=msg, foreground="green"))
                self.root.after(0, lambda: self.btn_direct_start.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_direct_stop.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.btn_direct_hard_stop.config(state=tk.DISABLED))
                
                self.last_direct_audio = audio
                self.root.after(0, lambda: self.btn_direct_play.config(state=tk.NORMAL if audio else tk.DISABLED))
                
                if self.settings_vars["direct_autoplay"].get() and audio:
                    self.play_audio_segment(audio)
                
            self.processor.process_raw_text(text, filename, force_new=force, save_to_disk=save_file, progress_callback=on_progress, completion_callback=on_complete)
            for t in self.processor.active_threads: t.join()

        threading.Thread(target=run_direct).start()

    def stop_direct_processing(self):
        if self.processor: self.processor.is_stopped = True
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.lbl_direct_status.config(text="Остановка...", foreground="orange")

    def hard_stop_direct_processing(self):
        if self.processor: 
            self.processor.is_stopped = True
            self.processor._save_cache()
        self.btn_direct_stop.config(state=tk.DISABLED)
        self.btn_direct_hard_stop.config(state=tk.DISABLED)
        self.btn_direct_start.config(state=tk.NORMAL)
        self.lbl_direct_status.config(text="Принудительно остановлено. Кэш сохранен.", foreground="red")
    
    def update_progress_ui(self, pct, text):
        self.file_progress['value'] = pct
        self.lbl_file_pct.config(text=f"{pct}%")
        
        display_text = text.replace('\n', ' ')
        if len(display_text) > 90:
            display_text = display_text[:87] + "..."
        else:
            display_text = display_text.ljust(90)
            
        self.lbl_current_text.config(text=f"Синтез: {display_text}", foreground="blue")

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
        filepath = filedialog.asksaveasfilename(
            defaultextension=".json", 
            filetypes=[("JSON files", "*.json")],
            initialfile="my_tts_config.json"
        )
        if filepath:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                messagebox.showinfo("Успех", f"Настройки экспортированы в:\n{filepath}")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось экспортировать конфиг:\n{e}")

    def import_glossary(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if filepath:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Проверяем, что это действительно глоссарий
                if not isinstance(data, dict): raise ValueError("Файл не является словарем JSON.")
                
                self.txt_glossary.delete(1.0, tk.END)
                self.txt_glossary.insert(tk.END, json.dumps(data, indent=4, ensure_ascii=False))
                self.save_glossary_ui() # Сразу сохраняем в рабочий файл
                messagebox.showinfo("Успех", "Глоссарий успешно импортирован!")
            except Exception as e:
                messagebox.showerror("Ошибка импорта", f"Неверный формат файла:\n{e}")

    def export_glossary(self):
        content = self.txt_glossary.get(1.0, tk.END).strip()
        try:
            parsed = json.loads(content) # Проверяем валидность перед экспортом
            filepath = filedialog.asksaveasfilename(
                defaultextension=".json", 
                filetypes=[("JSON files", "*.json")],
                initialfile="my_glossary.json"
            )
            if filepath:
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(parsed, f, indent=4, ensure_ascii=False)
                messagebox.showinfo("Успех", f"Глоссарий экспортирован в:\n{filepath}")
        except Exception as e:
            messagebox.showerror("Ошибка JSON", f"Исправьте ошибки в редакторе перед экспортом:\n{e}")

if __name__ == "__main__":
    root = tk.Tk()
    app = TTSApp(root)
    root.mainloop()