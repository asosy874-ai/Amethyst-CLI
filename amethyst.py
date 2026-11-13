#!/usr/bin/env python3
"""
Amethyst — консольный клиент YouTube Music с реальным звуком (mpv/PipeWire),
обложкой, синхронизированным текстом, плейлистами и входом в Google.

Возможности:
  • поиск треков (ytmusicapi)
  • воспроизведение через mpv (IPC: пауза/продолжить/стоп/прогресс)
  • обложка в терминале (ANSI truecolor, PIL)
  • синхронный текст (LRCLIB, fallback — тайминги YouTube Music, затем обычный текст)
  • плейлисты: локальные и импортированные из аккаунта
  • случайный порядок плейлиста без повторов
  • вход в YouTube Music из браузера (cookie Firefox/Chromium) без Google Cloud,
    плюс ручная вставка заголовков (как в Kanon) и OAuth как опция

Установка:
    ./install.sh
    Amethyst

ВНИМАНИЕ: неофициальный клиент. Соблюдайте Условия использования YouTube/Google.
"""

import io
import os
import re
import sys
import json
import time
import random
import shutil
import socket
import select
import termios
import tty
import subprocess
import threading
import sqlite3
import tempfile
import configparser
import glob
import hashlib
from typing import Optional, List, Dict, Tuple

import requests

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Пользовательские данные (cookie, OAuth, плейлисты) живут отдельно от кода,
# чтобы случайно не попасть в git.
#   Linux: $XDG_CONFIG_HOME/amethyst  (по умолчанию ~/.config/amethyst)
#   macOS: ~/Library/Application Support/amethyst
#   Windows: %APPDATA%\amethyst
def _config_base() -> str:
    if os.name == "nt":
        return os.environ.get("APPDATA") or os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _default_data_dir() -> str:
    return os.path.join(_config_base(), "amethyst")


def _legacy_data_dir() -> str:
    # До переименования проекта данные лежали в ytmusic-cli.
    return os.path.join(_config_base(), "ytmusic-cli")


DATA_DIR = (
    os.environ.get("AMETHYST_DATA_DIR")
    or os.environ.get("YTMUSIC_CLI_DATA_DIR")
    or _default_data_dir()
)
OAUTH_FILE = os.path.join(DATA_DIR, "oauth.json")
CLIENT_FILE = os.path.join(DATA_DIR, "oauth_client.json")
BROWSER_AUTH_FILE = os.path.join(DATA_DIR, "browser_auth.json")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
ACCOUNT_PLAYLISTS_FILE = os.path.join(DATA_DIR, "account_playlists.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MPV_BIN = "mpv"

# Старые версии хранили данные рядом со скриптом — переносим их один раз.
LEGACY_DATA_FILES = (
    "oauth.json",
    "oauth_client.json",
    "browser_auth.json",
    "playlists.json",
    "account_playlists.json",
)


def _move_legacy_file(source: str, target: str, name: str):
    if os.path.abspath(source) == os.path.abspath(target):
        return
    if not os.path.exists(source) or os.path.exists(target):
        return
    try:
        shutil.move(source, target)
        if name in ("oauth_client.json", "browser_auth.json", "account_playlists.json"):
            os.chmod(target, 0o600)
    except OSError:
        pass


def ensure_data_dir():
    """Создаёт папку данных и переносит legacy-файлы."""
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    except OSError:
        return
    old_dir = _legacy_data_dir()
    for name in LEGACY_DATA_FILES:
        target = os.path.join(DATA_DIR, name)
        _move_legacy_file(os.path.join(BASE_DIR, name), target, name)
        _move_legacy_file(os.path.join(old_dir, name), target, name)
    try:
        if os.path.isdir(old_dir) and not os.listdir(old_dir):
            os.rmdir(old_dir)
    except OSError:
        pass


# --------------------------------------------------------------------------
#  i18n: English source strings + Russian translations
# --------------------------------------------------------------------------
LANG_EN = "en"
LANG_RU = "ru"
LANG = LANG_EN
LANG_NAMES = {"en": "English", "ru": "Русский"}

RU: Dict[str, str] = {
    # ---------- common ----------
    "Unknown": "Неизвестно",
    "account": "аккаунт",
    "[!] Cancelled": "[!] Отменено",
    "[!] Invalid command": "[!] Неверная команда",
    "[!] Enter a number": "[!] Введите число",
    "Bye!": "Пока!",
    "[!] Interrupted by user": "[!] Прервано пользователем",

    # ---------- main menu ----------
    "1. Search for a track": "1. Поиск трека",
    "2. Pick a track from results": "2. Выбрать трек из результатов",
    "3. Play (player: cover + lyrics)": "3. Воспроизвести (плеер: обложка + текст)",
    "4. Pause / Resume": "4. Пауза / Продолжить",
    "5. Stop": "5. Остановить",
    "6. Show current track": "6. Показать текущий трек",
    "7. Playlists": "7. Плейлисты",
    "8. Sign in to YouTube Music (from browser)": "8. Войти в YouTube Music (из браузера)",
    "9. Volume (0-100%)": "9. Громкость (0-100%)",
    "10. Language / Язык": "10. Язык / Language",
    "0. Exit": "0. Выход",

    # ---------- playlist menu ----------
    "--- Playlists ---": "--- Плейлисты ---",
    "1. List playlists": "1. Список плейлистов",
    "2. Create a playlist": "2. Создать плейлист",
    "3. Add current track to a playlist": "3. Добавить текущий трек в плейлист",
    "4. Show a playlist": "4. Показать плейлист",
    "5. Remove a track from a playlist": "5. Удалить трек из плейлиста",
    "6. Play a playlist": "6. Воспроизвести плейлист",
    "7. Import/refresh playlists from account": "7. Импортировать/обновить плейлисты из аккаунта",
    "8. Play a playlist shuffled (no repeats)": "8. Воспроизвести плейлист в случайном порядке (без повторов)",
    "0. Back": "0. Назад",
    "Playlist name: ": "Название плейлиста: ",
    "Number to remove: ": "Номер для удаления: ",

    # ---------- prompts ----------
    "Enter a query: ": "Введите запрос: ",
    "Track number: ": "Номер трека: ",
    "Volume 0-100: ": "Громкость 0-100: ",
    "[!] Enter a number 0-100": "[!] Введите число 0-100",
    "[+] Selected: {title}": "[+] Выбрано: {title}",
    "[!] Search first": "[!] Сначала выполните поиск",
    "[!] Nothing is playing": "[!] Ничего не играет",
    "[+] Volume: {value}%": "[+] Громкость: {value}%",

    # ---------- startup / account ----------
    "[!] mpv not found. Install it: sudo pacman -S mpv": "[!] mpv не найден. Установите: sudo pacman -S mpv",
    "[!] ytmusicapi is not installed": "[!] ytmusicapi не установлен",
    "[+] YTMusic: signed in (OAuth)": "[+] YTMusic: вход в аккаунт выполнен (OAuth)",
    "[+] YTMusic: signed in (browser cookie)": "[+] YTMusic: вход выполнен (cookie браузера)",
    "[+] YTMusic in guest mode (no account)": "[+] YTMusic в гостевом режиме (без аккаунта)",
    "[!] YTMusic error: {error}": "[!] Ошибка YTMusic: {error}",
    "[+] YTMusic in guest mode (fallback)": "[+] YTMusic в гостевом режиме (fallback)",
    "[!] Could not save the playlist list: {error}": "[!] Не удалось сохранить список плейлистов: {error}",
    "[!] Sign in first (option 8)": "[!] Сначала войдите в аккаунт (пункт 8)",
    "[*] Loading playlists from your account…": "[*] Загружаю плейлисты из аккаунта…",
    "[!] Could not load playlists: {error}": "[!] Не удалось получить плейлисты: {error}",
    "[+] Playlists imported: {count}": "[+] Импортировано плейлистов: {count}",
    "[!] No playlists found in the account": "[!] В аккаунте не найдено плейлистов",
    "[!] Account playlists require sign-in (option 8)": "[!] Для плейлистов аккаунта нужен вход (пункт 8)",
    "[*] Loading '{name}' from YouTube Music…": "[*] Загружаю '{name}' из YouTube Music…",
    "[!] Could not load the playlist: {error}": "[!] Не удалось загрузить плейлист: {error}",
    "{title} [account]": "{title} [аккаунт]",
    "{title} [account {suffix}]": "{title} [аккаунт {suffix}]",

    # ---------- sign-in ----------
    "=== Sign in to YouTube Music ===": "=== Вход в YouTube Music ===",
    "1. Automatically from browser (Firefox/Chromium) — like Kanon": "1. Автоматически из браузера (Firefox/Chromium) — как в Kanon",
    "2. Paste headers/curl from browser manually": "2. Вставить заголовки/curl из браузера вручную",
    "3. OAuth via Google Cloud (if you have client id/secret)": "3. OAuth через Google Cloud (если есть client id/secret)",
    "[*] Looking for an active YouTube Music session in browsers…": "[*] Ищу активную сессию YouTube Music в браузерах…",
    "[*] Found cookie in Firefox/{profile}, checking…": "[*] Нашёл cookie в Firefox/{profile}, проверяю…",
    "[+] Signed in as: {name}": "[+] Вход выполнен: {name}",
    "    Saved to {path}": "    Данные сохранены в {path}",
    "[*] Found cookie in {browser}, checking…": "[*] Нашёл cookie в {browser}, проверяю…",
    "[!] Checked: {tried}": "[!] Проверено: {tried}",
    "[!] No active YouTube Music session found.": "[!] Активная сессия YouTube Music не найдена.",
    "    Sign in at music.youtube.com in your browser and try option 8 again,": "    Войди на music.youtube.com в браузере и повтори пункт 8,",
    "    or paste headers manually (option 2).": "    либо используй ручную вставку заголовков (пункт 2).",
    "=== Manual sign-in (like Kanon) ===": "=== Ручной вход (как в Kanon) ===",
    "1) Open music.youtube.com in your browser and sign in": "1) Открой music.youtube.com в браузере и войди в аккаунт",
    "2) F12 → Network → reload the page → choose the 'browse' request": "2) F12 → Network → обнови страницу → выбери запрос 'browse'",
    "3) Right-click the request → Copy → 'Copy request headers'": "3) ПКМ по запросу → Copy → 'Copy request headers'",
    "   (or 'Copy as cURL')": "   (или 'Copy as cURL')",
    "4) Paste here and finish with an empty line": "4) Вставь сюда и закончи пустой строкой",
    "[!] Could not parse curl": "[!] Не удалось разобрать curl",
    "[!] Could not parse headers: {error}": "[!] Не удалось разобрать заголовки: {error}",
    "[!] Headers did not work: the account is not authorized or the session expired.": "[!] Заголовки не подошли: аккаунт не авторизован или устарел.",
    "=== Google Cloud OAuth sign-in ===": "=== Вход через Google Cloud OAuth ===",
    "You need an OAuth client_id and client_secret from Google Cloud Console.": "Нужны OAuth client_id и client_secret из Google Cloud Console.",
    "Quick version: console.cloud.google.com → create a project →": "Кратко: console.cloud.google.com → создать проект →",
    "  enable 'YouTube Data API v3' → OAuth consent screen →": "  включить 'YouTube Data API v3' → OAuth consent screen →",
    "  Credentials → Create OAuth client → type 'TVs and Limited Input devices'.": "  Credentials → Create OAuth client → тип 'TVs and Limited Input devices'.",
    "Details: https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html": "Подробнее: https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html",
    "Google client ID: ": "Google client ID: ",
    "Google client secret: ": "Google client secret: ",
    "[*] Opening the browser. Finish signing in and come back to the terminal.": "[*] Открываю браузер. Заверши вход и вернись в терминал.",
    "[!] Sign-in error: {error}": "[!] Ошибка входа: {error}",
    "[+] Signed in": "[+] Вход выполнен",
    "[!] oauth.json was not created": "[!] Файл oauth.json не создан",

    # ---------- search ----------
    "[!] API is not initialized": "[!] API не инициализирован",
    "[!] Search error: {error}": "[!] Ошибка поиска: {error}",
    "[!] Nothing found": "[!] Ничего не найдено",
    "=== Search results ===": "=== Результаты поиска ===",
    "[!] Invalid track number": "[!] Неверный номер трека",
    "[!] yt-dlp is not installed": "[!] yt-dlp не установлен",
    "[!] Could not get the audio stream: {error}": "[!] Не удалось получить аудиопоток: {error}",

    # ---------- playback ----------
    "[■] Stopped": "[■] Остановлено",
    "[!] No track selected. Search and pick one first (options 1-2).": "[!] Нет выбранного трека. Сначала поиск и выбор (пункты 1-2).",
    "[!] Playlist '{name}' not found": "[!] Плейлист '{name}' не найден",
    "[!] Playlist is empty or not found": "[!] Плейлист пуст или не найден",
    "[🔀] Playlist: {name} ({tracks}, shuffled)": "[🔀] Плейлист: {name} ({tracks}, случайный порядок)",
    "[▶] Playlist: {name} ({tracks})": "[▶] Плейлист: {name} ({tracks})",
    "[!] Track has no videoId": "[!] У трека нет videoId",
    "[*] Direct open failed, extracting the audio stream via yt-dlp...": "[*] Прямое открытие не удалось, извлекаю аудиопоток через yt-dlp...",
    "[!] Could not get the audio stream": "[!] Не удалось получить аудиопоток",
    "[!] mpv failed to start": "[!] mpv не запустился",
    "[■] Track finished": "[■] Трек завершён",
    "[⏸] Paused": "[⏸] Пауза",
    "[▶] Resumed": "[▶] Продолжаем",
    "Current track: {title}": "Текущий трек: {title}",
    "Artist(s): {artists}": "Исполнитель(и): {artists}",
    "Video ID: {video_id}": "Видео ID: {video_id}",
    "Status: {status}": "Статус: {status}",
    "paused": "пауза",
    "playing": "играет",
    "stopped": "остановлен",
    "[!] No track selected": "[!] Трек не выбран",

    # ---------- player view ----------
    " {status} {pos} / {dur}  vol. {vol:3d}%  {bar}": " {status} {pos} / {dur}  громк. {vol:3d}%  {bar}",
    "  loading cover and lyrics…": "  загрузка обложки и текста…",
    "Lyrics not found": "Текст не найден",
    " [+/-] vol.  [space] pause  [n] next  [s] stop  [q] quit": " [+/-] громк.  [пробел] пауза  [n] след.  [s] стоп  [q] выход",

    # ---------- playlists ----------
    "[!] No playlists": "[!] Плейлистов нет",
    "=== Local playlists ===": "=== Локальные плейлисты ===",
    "=== YouTube Music account playlists ===": "=== Плейлисты аккаунта YouTube Music ===",
    "[!] Playlist '{name}' already exists": "[!] Плейлист '{name}' уже существует",
    "[+] Playlist '{name}' created": "[+] Плейлист '{name}' создан",
    "[!] Track is already in '{name}'": "[!] Трек уже есть в '{name}'",
    "[+] Added to '{name}': {title}": "[+] Добавлено в '{name}': {title}",
    "[!] Playlist '{name}' is empty or unavailable": "[!] Плейлист '{name}' пуст или недоступен",
    "=== Playlist: {name} ({tracks}) ===": "=== Плейлист: {name} ({tracks}) ===",
    "[-] Removed: {title}": "[-] Удалено: {title}",
    "[!] Invalid number": "[!] Неверный номер",

    # ---------- language ----------
    "=== Language / Язык ===": "=== Язык / Language ===",
    "1. English": "1. English",
    "2. Русский": "2. Русский",
    "[+] Language set to English": "[+] Язык переключён на английский",
    "[+] Language set to Russian": "[+] Язык переключён на русский",
    "[!] Could not save the language setting": "[!] Не удалось сохранить настройку языка",
}


def load_settings() -> Dict:
    """Читает settings.json; при ошибке возвращает пустой словарь."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(updates: Dict) -> bool:
    """Атомарно обновляет settings.json, сохраняя остальные ключи."""
    ensure_data_dir()
    data = load_settings()
    data.update(updates)
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _norm_lang(value: str) -> Optional[str]:
    value = (value or "").strip().lower().replace("_", "-")
    if value.startswith("ru"):
        return LANG_RU
    if value.startswith("en"):
        return LANG_EN
    return None


def detect_lang() -> str:
    """AMETHYST_LANG → settings.json → locale → English."""
    env = _norm_lang(os.environ.get("AMETHYST_LANG", ""))
    if env:
        return env
    saved = _norm_lang(str(load_settings().get("language", "")))
    if saved:
        return saved
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        loc = _norm_lang(os.environ.get(var, ""))
        if loc:
            return loc
    return LANG_EN


def set_language(lang: str) -> bool:
    global LANG
    LANG = _norm_lang(lang) or LANG_EN
    return save_settings({"language": LANG})


def t(text: str, *args, **kwargs) -> str:
    """Переводит строку и подставляет параметры через str.format."""
    template = RU.get(text, text) if LANG == LANG_RU else text
    if args or kwargs:
        try:
            return template.format(*args, **kwargs)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def tracks_label(count: int) -> str:
    """«1 трек», «2 трека», «5 треков» / «1 track», «2 tracks»."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if LANG == LANG_RU:
        if count % 10 == 1 and count % 100 != 11:
            word = "трек"
        elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            word = "трека"
        else:
            word = "треков"
        return f"{count} {word}"
    return f"{count} track" if count == 1 else f"{count} tracks"


LANG = detect_lang()

YTM_ORIGIN = "https://music.youtube.com"

LRCLIB_HEADERS = {"User-Agent": "amethyst/1.0 (https://github.com/)"}


# --------------------------------------------------------------------------
#  Вход без Google Cloud Console: cookie из браузера (как в Kanon)
# --------------------------------------------------------------------------
FIREFOX_ROOTS = [
    os.path.expanduser("~/.mozilla/firefox"),
    os.path.expanduser("~/.config/mozilla/firefox"),
    os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),
]

CHROMIUM_BROWSERS = [
    ("chromium", "~/.config/chromium"),
    ("chrome", "~/.config/google-chrome"),
    ("brave", "~/.config/BraveSoftware/Brave-Browser"),
    ("edge", "~/.config/microsoft-edge"),
    ("vivaldi", "~/.config/vivaldi"),
    ("opera", "~/.config/opera"),
]


def _firefox_cookie_dbs() -> List[str]:
    """Пути к cookies.sqlite всех профилей Firefox."""
    dbs: List[str] = []
    for root in FIREFOX_ROOTS:
        if not os.path.isdir(root):
            continue
        profiles: List[str] = []
        ini = os.path.join(root, "profiles.ini")
        if os.path.exists(ini):
            cfg = configparser.ConfigParser()
            try:
                cfg.read(ini)
            except Exception:
                cfg = None  # type: ignore[assignment]
            if cfg is not None:
                for section in cfg.sections():
                    if not section.lower().startswith("profile"):
                        continue
                    path = cfg.get(section, "Path", fallback="").strip()
                    if not path:
                        continue
                    try:
                        relative = cfg.getboolean(section, "IsRelative", fallback=True)
                    except ValueError:
                        relative = True
                    profiles.append(os.path.join(root, path) if relative else path)
        for path in profiles + glob.glob(os.path.join(root, "*")):
            db = os.path.join(path, "cookies.sqlite")
            if os.path.exists(db) and db not in dbs:
                dbs.append(db)
    return dbs


def _read_firefox_cookies(db: str) -> Dict[str, str]:
    """Читает cookie youtube/google из Firefox (значения не шифруются)."""
    tmpdir = tempfile.mkdtemp(prefix="ytm_cookies_")
    try:
        dst = os.path.join(tmpdir, "cookies.sqlite")
        shutil.copy(db, dst)
        for suffix in ("-wal", "-shm"):
            src = db + suffix
            if os.path.exists(src):
                try:
                    shutil.copy(src, dst + suffix)
                except Exception:
                    pass
        con = sqlite3.connect(dst)
        try:
            rows = con.execute(
                "select host, name, value, expiry from moz_cookies "
                "where host in ('.youtube.com', '.google.com', 'music.youtube.com')"
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    now = time.time()
    cookies: Dict[str, str] = {}
    for host, name, value, expiry in rows:
        if not value:
            continue
        try:
            exp = float(expiry or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp > 10 ** 11:  # миллисекунды
            exp /= 1000.0
        if exp and exp < now:
            continue
        # cookie с youtube-домена приоритетнее google-домена
        if name not in cookies or host.endswith("youtube.com"):
            cookies[name] = value
    return cookies


def _read_browser_cookies_ytdlp(browser: str) -> Dict[str, str]:
    """Cookie Chromium-подобных браузеров через yt-dlp (умеет расшифровку)."""
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp and os.access(venv_bin, os.X_OK):
        ytdlp = venv_bin
    if not ytdlp:
        return {}
    fd, path = tempfile.mkstemp(prefix="ytm_ck_", suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(
            [
                ytdlp,
                "--cookies-from-browser",
                browser,
                "--cookies",
                path,
                "--simulate",
                "--skip-download",
                "--print",
                "%(id)s",
                "https://music.youtube.com/watch?v=kJQP7kiw5Fk",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        cookies: Dict[str, str] = {}
        now = time.time()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, _cpath, _secure, expiry, name, value = parts[:7]
                if not (domain.endswith("youtube.com") or domain.endswith("google.com")):
                    continue
                try:
                    exp = float(expiry or 0)
                except ValueError:
                    exp = 0
                if exp and exp < now:
                    continue
                if not value:
                    continue
                if name not in cookies or domain.endswith("youtube.com"):
                    cookies[name] = value
        return cookies
    except Exception:
        return {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def build_browser_headers(cookies: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Собирает заголовки ytmusicapi из cookie. Хеш SAPISIDHASH обновляется
    библиотекой автоматически на каждом запросе, поэтому хранить его не страшно.
    """
    sapisid = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid:
        return None
    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
    ts = str(int(time.time()))
    digest = hashlib.sha1(
        f"{ts} {sapisid} {YTM_ORIGIN}".encode("utf-8")
    ).hexdigest()
    return {
        "cookie": cookie,
        "authorization": f"SAPISIDHASH {ts}_{digest}",
        "origin": YTM_ORIGIN,
        "x-origin": YTM_ORIGIN,
        "x-goog-authuser": "0",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "accept": "*/*",
        "content-type": "application/json",
    }


def account_name_for_headers(headers: Dict[str, str]) -> Optional[str]:
    """Проверяет авторизацию реальным (read-only) запросом к аккаунту."""
    if not YTMusic:
        return None
    try:
        ytm = YTMusic(headers)
        info = ytm.get_account_info() or {}
        name = info.get("accountName") or info.get("channelHandle")
        return name or t("account")
    except Exception:
        return None


# --------------------------------------------------------------------------
#  Вспомогательные функции
# --------------------------------------------------------------------------
def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def duration_seconds(track: Dict) -> Optional[int]:
    ds = track.get("duration_seconds")
    if ds:
        return int(ds)
    d = track.get("duration")
    if d and ":" in d:
        try:
            parts = [int(x) for x in d.split(":")]
            total = 0
            for p in parts:
                total = total * 60 + p
            return total
        except ValueError:
            return None
    return None


def parse_lrc(text: str) -> List[Tuple[float, str]]:
    """Парсит LRC в список (секунды, текст), отсортированный по времени."""
    pattern = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    entries: List[Tuple[float, str]] = []
    for line in text.splitlines():
        stamps = pattern.findall(line)
        if not stamps:
            continue
        content = pattern.sub("", line).strip()
        for mm, ss, frac in stamps:
            secs = int(mm) * 60 + int(ss)
            if frac:
                if len(frac) == 1:
                    secs += int(frac) / 10.0
                elif len(frac) == 2:
                    secs += int(frac) / 100.0
                else:
                    secs += int(frac) / 1000.0
            entries.append((secs, content))
    entries.sort(key=lambda e: e[0])
    return entries


def render_image_ansi(img, cols: int) -> List[str]:
    """Рендерит изображение в ANSI truecolor полублоками (2 пикселя на строку)."""
    if Image is None:
        return []
    try:
        img = img.convert("RGB")
    except Exception:
        return []
    w, h = img.size
    if w == 0 or h == 0:
        return []
    cols = max(8, cols)
    rows = max(1, round(cols * h / (2.0 * w)))
    img = img.resize((cols, rows * 2), Image.LANCZOS)
    px = img.load()
    out: List[str] = []
    for y in range(rows):
        parts = []
        for x in range(cols):
            r1, g1, b1 = px[x, 2 * y]
            r2, g2, b2 = px[x, 2 * y + 1]
            parts.append(
                f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m\u2580"
            )
        parts.append("\x1b[0m")
        out.append("".join(parts))
    return out


# --------------------------------------------------------------------------
#  Kitty graphics protocol (нативные картинки, если терминал умеет)
# --------------------------------------------------------------------------
KITTY_IMAGE_ID = 1


def kitty_supported() -> bool:
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    term = os.environ.get("TERM", "")
    prog = os.environ.get("TERM_PROGRAM", "")
    return ("kitty" in term) or ("ghostty" in prog) or ("ghostty" in term)


def kitty_delete_image(image_id: int = KITTY_IMAGE_ID) -> str:
    return f"\x1b_Ga=d,d=I,i={image_id}\x1b\\"


def kitty_display_png(
    png: bytes, cols: int, rows: int, image_id: int = KITTY_IMAGE_ID
) -> str:
    """Передаёт PNG в kitty и размещает его в области cols×rows (клетки)."""
    import base64

    payload = base64.b64encode(png).decode("ascii")
    chunks = [payload[i : i + 4096] for i in range(0, len(payload), 4096)] or [""]
    parts = []
    for idx, chunk in enumerate(chunks):
        more = 1 if idx < len(chunks) - 1 else 0
        if idx == 0:
            parts.append(
                f"\x1b_Gf=100,a=T,q=2,c={cols},r={rows},i={image_id},m={more};{chunk}\x1b\\"
            )
        else:
            parts.append(f"\x1b_Gm={more};{chunk}\x1b\\")
    return "".join(parts)


def upscale_thumb_url(url: str) -> str:
    """Просит у YouTube/Google превью максимального разрешения.

    ytmusicapi обычно отдаёт ссылки вида ``...=w120-h120-l90-rj`` — меняем
    размер на 1080, остальные параметры (сжатие и т.п.) оставляем.
    """
    if url.startswith("//"):
        url = "https:" + url
    if "googleusercontent.com" in url:
        if re.search(r"=w\d+-h\d+", url):
            return re.sub(r"=w\d+-h\d+", "=w1080-h1080", url)
        if re.search(r"=s\d+", url):
            return re.sub(r"=s\d+", "=s1080", url)
        return url + "=w1080-h1080-l90-rj"
    if "ytimg.com" in url:
        for name in ("sddefault", "hqdefault", "mqdefault", "default"):
            if f"/{name}.jpg" in url:
                return url.replace(f"/{name}.jpg", "/maxresdefault.jpg")
    return url


def cover_url_for(track: Dict) -> Optional[str]:
    """Лучший (самый крупный) URL обложки трека в максимальном качестве."""
    thumbs = track.get("thumbnails") or []
    if thumbs:
        best = max(
            thumbs,
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        )
        url = best.get("url")
        if url:
            return upscale_thumb_url(url)
    vid = track.get("videoId")
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    return None


def encode_png(img) -> Optional[bytes]:
    if Image is None or img is None:
        return None
    try:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def fit_text(text: str, cols: int) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    if cols <= 1:
        return text[:cols]
    if len(text) > cols:
        return text[: max(0, cols - 1)] + "…"
    return text


# --------------------------------------------------------------------------
#  Управление mpv через IPC
# --------------------------------------------------------------------------
class MpvController:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path = f"/tmp/amethyst_mpv_{os.getpid()}.sock"
        self.sock: Optional[socket.socket] = None
        self._req_id = 0
        self._lock = threading.Lock()

    def _connect(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(self.sock_path)
                self.sock = s
                return True
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                time.sleep(0.1)
        return False

    def start(self, url: str) -> bool:
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        try:
            self.proc = subprocess.Popen(
                [
                    MPV_BIN,
                    "--no-video",
                    "--no-terminal",
                    "--really-quiet",
                    "--audio-display=no",
                    "--force-window=no",
                    "--ytdl-format=bestaudio/best",
                    f"--input-ipc-server={self.sock_path}",
                    url,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(t("[!] mpv not found. Install it: sudo pacman -S mpv"))
            return False
        return self._connect()

    def _read_response(self, rid: int):
        buf = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            if self.sock is None:
                return None
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if msg.get("request_id") == rid:
                    return msg
        return None

    def command(self, *args):
        if self.sock is None:
            return None
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
            try:
                self.sock.sendall(payload.encode())
            except OSError:
                return None
            return self._read_response(rid)

    def get(self, prop: str):
        msg = self.command("get_property", prop)
        if msg and msg.get("error") == "success":
            return msg.get("data")
        return None

    def set_pause(self, state: bool):
        return self.command("set_property", "pause", state)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        try:
            self.command("quit")
        except Exception:
            pass
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
#  Плеер
# --------------------------------------------------------------------------
class YouTubeMusicPlayer:
    def __init__(self):
        ensure_data_dir()
        self.ytmusic = None
        self.current_track: Optional[Dict] = None
        self.last_results: List[Dict] = []
        self.playlists: Dict[str, List[Dict]] = {}
        self.account_playlists: Dict[str, Dict] = {}
        self.queue: List[Dict] = []
        self.is_playing = False
        self.is_paused = False
        self.mpv: Optional[MpvController] = None
        self.view_active = False
        self._track_stop: Optional[threading.Event] = None
        self._track_lock = threading.RLock()
        self._sync_thread: Optional[threading.Thread] = None

        # метаданные для вида плеера
        self._meta_lock = threading.Lock()
        self._meta_video_id: Optional[str] = None
        self.cover_ansi: Optional[List[str]] = None
        self.cover_image = None
        self.lyrics: Optional[List[Tuple[float, str]]] = None
        self.lyrics_plain: Optional[str] = None
        self.volume = 100

        self._load_playlists()
        self._load_account_playlists()
        self._init_ytmusic()

        # После обновления плеера кеша плейлистов может ещё не быть —
        # один раз подтягиваем его при наличии сохранённого входа.
        if (
            self.ytmusic is not None
            and getattr(self.ytmusic, "auth_type", None) is not None
            and not os.path.exists(ACCOUNT_PLAYLISTS_FILE)
        ):
            self.import_account_playlists(quiet=True)

    # ---------- Аккаунт ----------

    def _load_client_credentials(self) -> Optional[Tuple[str, str]]:
        if not os.path.exists(CLIENT_FILE):
            return None
        try:
            with open(CLIENT_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            cid, csec = d.get("client_id"), d.get("client_secret")
            if cid and csec:
                return cid, csec
        except Exception:
            pass
        return None

    def _save_client_credentials(self, client_id: str, client_secret: str):
        with open(CLIENT_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"client_id": client_id, "client_secret": client_secret},
                f,
                ensure_ascii=False,
                indent=2,
            )
        try:
            os.chmod(CLIENT_FILE, 0o600)
        except Exception:
            pass

    def _load_browser_auth(self) -> Optional[Dict[str, str]]:
        if not os.path.exists(BROWSER_AUTH_FILE):
            return None
        try:
            with open(BROWSER_AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("cookie"):
                return data
        except Exception:
            pass
        return None

    def _save_browser_auth(self, headers: Dict[str, str]):
        with open(BROWSER_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(headers, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(BROWSER_AUTH_FILE, 0o600)
        except Exception:
            pass

    def _init_ytmusic(self, guest: bool = False):
        if not YTMusic:
            print(t("[!] ytmusicapi is not installed"))
            return
        try:
            if not guest and os.path.exists(OAUTH_FILE):
                creds = self._load_client_credentials()
                if creds:
                    from ytmusicapi.auth.oauth import OAuthCredentials

                    self.ytmusic = YTMusic(
                        OAUTH_FILE,
                        oauth_credentials=OAuthCredentials(creds[0], creds[1]),
                    )
                    print(t("[+] YTMusic: signed in (OAuth)"))
                    return
            if not guest:
                headers = self._load_browser_auth()
                if headers:
                    self.ytmusic = YTMusic(headers)
                    print(t("[+] YTMusic: signed in (browser cookie)"))
                    return
            self.ytmusic = YTMusic()
            print(t("[+] YTMusic in guest mode (no account)"))
        except Exception as e:
            print(t("[!] YTMusic error: {error}", error=e))
            try:
                self.ytmusic = YTMusic()
                print(t("[+] YTMusic in guest mode (fallback)"))
            except Exception:
                self.ytmusic = None

    # ---------- Плейлисты аккаунта ----------

    def _load_account_playlists(self):
        self.account_playlists = {}
        if not os.path.exists(ACCOUNT_PLAYLISTS_FILE):
            return
        try:
            with open(ACCOUNT_PLAYLISTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.account_playlists = {
                    key: meta
                    for key, meta in data.items()
                    if isinstance(meta, dict) and meta.get("playlistId")
                }
        except Exception:
            self.account_playlists = {}

    def _save_account_playlists(self):
        ensure_data_dir()
        try:
            with open(ACCOUNT_PLAYLISTS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    self.account_playlists, f, ensure_ascii=False, indent=2
                )
            try:
                os.chmod(ACCOUNT_PLAYLISTS_FILE, 0o600)
            except OSError:
                pass
        except OSError as e:
            print(t("[!] Could not save the playlist list: {error}", error=e))

    def import_account_playlists(self, quiet: bool = False) -> int:
        """Импортирует список плейлистов из библиотеки YouTube Music.

        Треки не скачиваются сразу — они подгружаются при открытии или
        воспроизведении плейлиста, чтобы вход не зависел от размера библиотеки.
        """
        if not self.ytmusic:
            if not quiet:
                print(t("[!] Sign in first (option 8)"))
            return 0
        if not quiet:
            print(t("[*] Loading playlists from your account…"))
        try:
            items = self.ytmusic.get_library_playlists(limit=None) or []
        except Exception as e:
            if not quiet:
                print(t("[!] Could not load playlists: {error}", error=e))
            return 0

        imported: Dict[str, Dict] = {}
        for item in items:
            playlist_id = item.get("playlistId")
            if not playlist_id:
                continue
            title = (item.get("title") or playlist_id).strip() or playlist_id
            key = title
            if key in self.playlists:
                key = t("{title} [account]", title=title)
            suffix = 2
            while key in imported:
                key = t("{title} [account {suffix}]", title=title, suffix=suffix)
                suffix += 1

            author = item.get("author")
            if isinstance(author, list):
                author = ", ".join(
                    a.get("name", "") for a in author if isinstance(a, dict)
                )
            elif isinstance(author, dict):
                author = author.get("name", "")
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0

            imported[key] = {
                "playlistId": playlist_id,
                "title": title,
                "count": count,
                "author": author or "",
            }

        self.account_playlists = imported
        self._save_account_playlists()

        if not quiet:
            if imported:
                print(t("[+] Playlists imported: {count}", count=len(imported)))
                for key, meta in imported.items():
                    count = (
                        f" ({tracks_label(meta['count'])})"
                        if meta.get("count")
                        else ""
                    )
                    print(f"    • {key}{count}")
            else:
                print(t("[!] No playlists found in the account"))
        return len(imported)

    def _account_playlist_tracks(self, name: str) -> List[Dict]:
        meta = self.account_playlists.get(name)
        if not meta:
            return []
        if not self.ytmusic:
            print(t("[!] Account playlists require sign-in (option 8)"))
            return []
        playlist_id = meta["playlistId"]
        print(t("[*] Loading '{name}' from YouTube Music…", name=name))
        try:
            raw = self.ytmusic.get_playlist(playlist_id, limit=None) or {}
        except Exception as e:
            print(t("[!] Could not load the playlist: {error}", error=e))
            return []
        tracks = [
            track for track in (raw.get("tracks") or []) if track.get("videoId")
        ]
        if tracks:
            meta["count"] = len(tracks)
            self._save_account_playlists()
        return tracks

    def _playlist_tracks(self, name: str) -> List[Dict]:
        if name in self.playlists:
            return self.playlists[name]
        if name in self.account_playlists:
            return self._account_playlist_tracks(name)
        return []

    # ---------- Вход в аккаунт ----------

    def login_google(self):
        print("\n" + t("=== Sign in to YouTube Music ==="))
        print(t("1. Automatically from browser (Firefox/Chromium) — like Kanon"))
        print(t("2. Paste headers/curl from browser manually"))
        print(t("3. OAuth via Google Cloud (if you have client id/secret)"))
        print(t("0. Back"))
        choice = input("> ").strip()
        if choice == "1":
            self.login_from_browser()
        elif choice == "2":
            self.login_manual_headers()
        elif choice == "3":
            self.login_oauth()
        elif choice not in ("", "0"):
            print(t("[!] Invalid command"))

    def _install_browser_headers(self, headers: Dict[str, str]) -> Optional[str]:
        name = account_name_for_headers(headers)
        if not name:
            return None
        self._save_browser_auth(headers)
        self._init_ytmusic()
        return name

    def login_from_browser(self) -> bool:
        print(t("[*] Looking for an active YouTube Music session in browsers…"))
        tried: List[str] = []

        for db in _firefox_cookie_dbs():
            profile = os.path.basename(os.path.dirname(db))
            tried.append(f"Firefox/{profile}")
            cookies = _read_firefox_cookies(db)
            if "__Secure-3PAPISID" not in cookies:
                continue
            headers = build_browser_headers(cookies)
            if not headers:
                continue
            print(t("[*] Found cookie in Firefox/{profile}, checking…", profile=profile))
            name = self._install_browser_headers(headers)
            if name:
                print(t("[+] Signed in as: {name}", name=name))
                print(t("    Saved to {path}", path=BROWSER_AUTH_FILE))
                self.import_account_playlists()
                return True

        for browser, path in CHROMIUM_BROWSERS:
            if not os.path.isdir(os.path.expanduser(path)):
                continue
            tried.append(browser)
            cookies = _read_browser_cookies_ytdlp(browser)
            if "__Secure-3PAPISID" not in cookies:
                continue
            headers = build_browser_headers(cookies)
            if not headers:
                continue
            print(t("[*] Found cookie in {browser}, checking…", browser=browser))
            name = self._install_browser_headers(headers)
            if name:
                print(t("[+] Signed in as: {name}", name=name))
                self.import_account_playlists()
                return True

        if tried:
            print(t("[!] Checked: {tried}", tried=", ".join(tried)))
        print(t("[!] No active YouTube Music session found."))
        print(t("    Sign in at music.youtube.com in your browser and try option 8 again,"))
        print(t("    or paste headers manually (option 2)."))
        return False

    @staticmethod
    def _curl_to_headers(cmd: str) -> str:
        """Преобразует 'Copy as cURL' в строки заголовков для setup_browser."""
        lines: List[str] = []
        for match in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", cmd, re.S):
            lines.append(match.group(2))
        for match in re.finditer(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", cmd, re.S):
            lines.append("cookie: " + match.group(2))
        return "\n".join(lines)

    def login_manual_headers(self):
        print("\n" + t("=== Manual sign-in (like Kanon) ==="))
        print(t("1) Open music.youtube.com in your browser and sign in"))
        print(t("2) F12 → Network → reload the page → choose the 'browse' request"))
        print(t("3) Right-click the request → Copy → 'Copy request headers'"))
        print(t("   (or 'Copy as cURL')"))
        print(t("4) Paste here and finish with an empty line") + "\n")
        lines: List[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            lines.append(line)
        raw = "\n".join(lines)
        if not raw.strip():
            print(t("[!] Cancelled"))
            return
        if raw.strip().lower().startswith("curl"):
            raw = self._curl_to_headers(raw)
        if not raw.strip():
            print(t("[!] Could not parse curl"))
            return
        if "x-goog-authuser" not in raw.lower():
            raw += "\nx-goog-authuser: 0"
        try:
            from ytmusicapi.auth.browser import setup_browser

            headers = json.loads(setup_browser(headers_raw=raw))
        except Exception as e:
            print(t("[!] Could not parse headers: {error}", error=e))
            return
        name = self._install_browser_headers(headers)
        if not name:
            print(t("[!] Headers did not work: the account is not authorized or the session expired."))
            return
        print(t("[+] Signed in as: {name}", name=name))
        self.import_account_playlists()

    def login_oauth(self):
        print("\n" + t("=== Google Cloud OAuth sign-in ==="))
        creds = self._load_client_credentials()
        if creds is None:
            print(t("You need an OAuth client_id and client_secret from Google Cloud Console."))
            print(t("Quick version: console.cloud.google.com → create a project →"))
            print(t("  enable 'YouTube Data API v3' → OAuth consent screen →"))
            print(t("  Credentials → Create OAuth client → type 'TVs and Limited Input devices'."))
            print(t("Details: https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html") + "\n")
            cid = input(t("Google client ID: ")).strip()
            csec = input(t("Google client secret: ")).strip()
            if not cid or not csec:
                print(t("[!] Cancelled"))
                return
            self._save_client_credentials(cid, csec)
            creds = (cid, csec)

        print(t("[*] Opening the browser. Finish signing in and come back to the terminal."))
        try:
            from ytmusicapi.setup import setup_oauth

            setup_oauth(
                client_id=creds[0],
                client_secret=creds[1],
                filepath=OAUTH_FILE,
                open_browser=True,
            )
        except Exception as e:
            print(t("[!] Sign-in error: {error}", error=e))
            return

        if os.path.exists(OAUTH_FILE):
            self._init_ytmusic(guest=False)
            print(t("[+] Signed in"))
            self.import_account_playlists()
        else:
            print(t("[!] oauth.json was not created"))

    # ---------- Поиск ----------

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        if not self.ytmusic:
            print(t("[!] API is not initialized"))
            return []
        try:
            results = self.ytmusic.search(query, filter="songs", limit=limit)
            self.last_results = results
            return results
        except Exception as e:
            print(t("[!] Search error: {error}", error=e))
            return []

    def show_results(self, results: List[Dict]):
        if not results:
            print(t("[!] Nothing found"))
            return
        print("\n" + t("=== Search results ==="))
        for i, track in enumerate(results, 1):
            title = track.get("title", t("Unknown"))
            artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
            album = track.get("album", {}).get("name", "") if track.get("album") else ""
            duration = track.get("duration", "")
            video_id = track.get("videoId", "")
            print(f"{i}. {title} — {artists} | {album} | {duration} | id: {video_id}")

    def pick_track(self, index: int) -> Optional[Dict]:
        if 1 <= index <= len(self.last_results):
            self.current_track = self.last_results[index - 1]
            return self.current_track
        print(t("[!] Invalid track number"))
        return None

    # ---------- Аудио ----------

    def get_audio_url(self, video_id: str) -> Optional[str]:
        if not yt_dlp:
            print(t("[!] yt-dlp is not installed"))
            return None
        try:
            ydl_opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://music.youtube.com/watch?v={video_id}", download=False
                )
                return info.get("url")
        except Exception as e:
            print(t("[!] Could not get the audio stream: {error}", error=e))
            return None

    # ---------- Метаданные: обложка + текст ----------

    def _fetch_meta(self, track: Dict):
        vid = track.get("videoId")
        cover_lines: Optional[List[str]] = None
        cover_img = None
        lyrics = None
        plain = None

        # --- обложка ---
        try:
            if Image is not None:
                candidates: List[str] = []
                hi = cover_url_for(track)
                if hi:
                    candidates.append(hi)
                thumbs = track.get("thumbnails") or []
                if thumbs:
                    best = max(
                        thumbs,
                        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
                    )
                    raw = best.get("url")
                    if raw:
                        if raw.startswith("//"):
                            raw = "https:" + raw
                        if raw not in candidates:
                            candidates.append(raw)
                data = None
                for candidate in candidates:
                    try:
                        r = requests.get(candidate, timeout=10)
                        if r.status_code != 200 or not r.content:
                            continue
                        Image.open(io.BytesIO(r.content)).verify()
                        data = r.content
                        break
                    except Exception:
                        continue
                if data:
                    cover_img = Image.open(io.BytesIO(data)).convert("RGB")
                    cols = self._cover_cols()
                    cover_lines = render_image_ansi(cover_img, cols)
        except Exception:
            cover_lines = None
            cover_img = None

        # --- текст: LRCLIB ---
        try:
            lyrics, plain = self._fetch_lrclib(track)
        except Exception:
            lyrics, plain = None, None

        # --- fallback: тайминги/текст из YouTube Music ---
        if lyrics is None and plain is None:
            try:
                lyrics, plain = self._fetch_ytm_lyrics(vid)
            except Exception:
                pass

        with self._meta_lock:
            cur = self.current_track or {}
            if cur.get("videoId") == vid:
                self.cover_ansi = cover_lines
                self.cover_image = cover_img
                self.lyrics = lyrics
                self.lyrics_plain = plain
                self._meta_video_id = vid

    def _fetch_lrclib(self, track: Dict):
        title = track.get("title", "")
        artists = track.get("artists") or []
        artist = artists[0].get("name", "") if artists else ""
        album = (track.get("album") or {}).get("name", "")
        dur = duration_seconds(track)

        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if dur:
            params["duration"] = dur

        try:
            r = requests.get(
                "https://lrclib.net/api/get",
                params=params,
                headers=LRCLIB_HEADERS,
                timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                synced = d.get("syncedLyrics")
                plain = d.get("plainLyrics")
                if synced:
                    return parse_lrc(synced), plain
                if plain:
                    return None, plain
        except Exception:
            pass

        # fallback: поиск
        try:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"track_name": title, "artist_name": artist},
                headers=LRCLIB_HEADERS,
                timeout=10,
            )
            if r.status_code == 200:
                arr = r.json()
                if isinstance(arr, list) and arr:
                    best = None
                    best_diff = 1e9
                    for d in arr:
                        dd = d.get("duration")
                        diff = abs((dd or 0) - (dur or 0)) if dur else 0
                        if d.get("syncedLyrics") and diff < best_diff:
                            best, best_diff = d, diff
                    if best is None:
                        for d in arr:
                            if d.get("plainLyrics"):
                                best = d
                                break
                    if best:
                        if best.get("syncedLyrics"):
                            return parse_lrc(best["syncedLyrics"]), best.get("plainLyrics")
                        return None, best.get("plainLyrics")
        except Exception:
            pass
        return None, None

    def _fetch_ytm_lyrics(self, video_id: Optional[str]):
        if not video_id or not self.ytmusic:
            return None, None
        try:
            watch = self.ytmusic.get_watch_playlist(videoId=video_id, limit=1)
            browse_id = watch.get("lyrics")
            if not browse_id:
                return None, None
            try:
                timed = self.ytmusic.get_lyrics(browse_id, timestamps=True)
            except Exception:
                timed = None
            if timed and getattr(timed, "has_timestamps", False):
                entries = []
                for line in getattr(timed, "lyrics", []):
                    text = getattr(line, "text", None) or (line.get("text") if isinstance(line, dict) else None)
                    start = getattr(line, "start_time", None)
                    if start is None and isinstance(line, dict):
                        start = line.get("start_time")
                    if text and start is not None:
                        entries.append((float(start), text))
                if entries:
                    entries.sort()
                    return entries, None
            plain = self.ytmusic.get_lyrics(browse_id)
            if plain and getattr(plain, "lyrics", None):
                return None, plain.lyrics
        except Exception:
            pass
        return None, None

    # ---------- Воспроизведение ----------

    def _halt(self):
        # Переходы треков сериализуются, чтобы старый поток синхронизации
        # не мог одновременно переключить очередь.
        with self._track_lock:
            event = self._track_stop
            self._track_stop = None
            if event is not None:
                event.set()
            mpv = self.mpv
            self.mpv = None
            self.is_playing = False
            self.is_paused = False
            if mpv is not None:
                mpv.stop()

    def stop(self):
        with self._track_lock:
            self.queue = []
            self._halt()
        print("\n" + t("[■] Stopped"))

    def play(self):
        if not self.current_track:
            print(t("[!] No track selected. Search and pick one first (options 1-2)."))
            return
        self._start_track(self.current_track)
        if sys.stdin.isatty():
            self._player_view()

    @staticmethod
    def _dedupe_tracks(tracks: List[Dict]) -> List[Dict]:
        """Убирает треки без videoId и повторяющиеся videoId."""
        unique: List[Dict] = []
        seen = set()
        for track in tracks:
            video_id = track.get("videoId")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            unique.append(track)
        return unique

    def play_playlist(self, name: str, shuffle: bool = False):
        if name not in self.playlists and name not in self.account_playlists:
            print(t("[!] Playlist '{name}' not found", name=name))
            return
        tracks = self._dedupe_tracks(self._playlist_tracks(name))
        if not tracks:
            print(t("[!] Playlist is empty or not found"))
            return
        if shuffle:
            random.shuffle(tracks)
            print(
                "\n"
                + t(
                    "[🔀] Playlist: {name} ({tracks}, shuffled)",
                    name=name,
                    tracks=tracks_label(len(tracks)),
                )
            )
        else:
            print(
                "\n"
                + t(
                    "[▶] Playlist: {name} ({tracks})",
                    name=name,
                    tracks=tracks_label(len(tracks)),
                )
            )
        with self._track_lock:
            self.queue = list(tracks)
            first = self.queue.pop(0)
            self._start_track(first)
        if sys.stdin.isatty():
            self._player_view()

    def _start_track(self, track: Dict):
        with self._track_lock:
            self._start_track_locked(track)

    def _start_track_locked(self, track: Dict):
        video_id = track.get("videoId")
        if not video_id:
            print(t("[!] Track has no videoId"))
            return

        self._halt()
        stop_event = threading.Event()
        self._track_stop = stop_event
        self.current_track = track
        with self._meta_lock:
            self._meta_video_id = None
            self.cover_ansi = None
            self.cover_image = None
            self.lyrics = None
            self.lyrics_plain = None

        title = track.get("title", t("Unknown"))
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
        if not self.view_active:
            print(f"\n[▶] {title} — {artists}")

        threading.Thread(target=self._fetch_meta, args=(dict(track),), daemon=True).start()

        page_url = f"https://music.youtube.com/watch?v={video_id}"
        mpv = MpvController()
        if mpv.start(page_url):
            self.mpv = mpv
        else:
            if not self.view_active:
                print(t("[*] Direct open failed, extracting the audio stream via yt-dlp..."))
            direct = self.get_audio_url(video_id)
            if not direct:
                print(t("[!] Could not get the audio stream"))
                if mpv.proc:
                    mpv.stop()
                return
            mpv = MpvController()
            if not mpv.start(direct):
                print(t("[!] mpv failed to start"))
                if mpv.proc:
                    mpv.stop()
                return
            self.mpv = mpv

        if self.mpv is not None:
            self.mpv.command("set_property", "volume", self.volume)
        self.is_playing = True
        self.is_paused = False
        self._sync_thread = threading.Thread(
            target=self._sync_loop, args=(mpv, stop_event), daemon=True
        )
        self._sync_thread.start()

    def _sync_loop(self, mpv: MpvController, stop_event: threading.Event):
        while not stop_event.is_set() and self._track_stop is stop_event:
            if not mpv.alive():
                break
            if not self.view_active:
                pos = mpv.get("time-pos")
                dur = mpv.get("duration")
                if pos is not None:
                    title = (self.current_track or {}).get("title", "")
                    print(
                        f"\r[⏱] {fmt_time(pos)} / {fmt_time(dur)}  {title}",
                        end="",
                        flush=True,
                    )
            time.sleep(1)
        if not self.view_active:
            print()

        # Трек уже сменили вручную или остановили — не трогаем очередь.
        with self._track_lock:
            if stop_event.is_set() or self._track_stop is not stop_event:
                return
            self.is_playing = False
            if self.mpv is mpv:
                self.mpv = None
            if self.queue:
                nxt = self.queue.pop(0)
                self._start_track(nxt)
            elif not self.view_active:
                print(t("[■] Track finished"))

    def pause(self):
        if self.is_playing and not self.is_paused and self.mpv is not None:
            self.mpv.set_pause(True)
            self.is_paused = True
            if not self.view_active:
                print("\n" + t("[⏸] Paused"))

    def resume(self):
        if self.is_paused and self.mpv is not None:
            self.mpv.set_pause(False)
            self.is_paused = False
            if not self.view_active:
                print("\n" + t("[▶] Resumed"))

    def set_volume(self, value: int) -> int:
        v = max(0, min(100, int(round(value))))
        self.volume = v
        if self.mpv is not None:
            self.mpv.command("set_property", "volume", v)
        return v

    def change_volume(self, delta: int) -> int:
        return self.set_volume(self.volume + delta)

    def skip_next(self):
        with self._track_lock:
            if not self.queue:
                return False
            nxt = self.queue.pop(0)
            self._start_track(nxt)
            return True

    def show_current(self):
        if self.current_track:
            print("\n" + t("Current track: {title}", title=self.current_track.get("title")))
            artists = ", ".join(
                a.get("name", "") for a in self.current_track.get("artists", [])
            )
            print(t("Artist(s): {artists}", artists=artists))
            print(t("Video ID: {video_id}", video_id=self.current_track.get("videoId")))
            status = (
                t("paused")
                if self.is_paused
                else (t("playing") if self.is_playing else t("stopped"))
            )
            print(t("Status: {status}", status=status))
        else:
            print(t("[!] No track selected"))

    # ---------- Полноэкранный вид плеера ----------

    def _fit_cover(self, cols: int, rows: int, img_w: int, img_h: int) -> Tuple[int, int]:
        """Подбирает размер обложки в клетках так, чтобы влезли шапка,
        текст и подвал. Возвращает (0, 0), если места под картинку нет.
        """
        if cols < 20 or rows < 14 or not img_w or not img_h:
            return 0, 0

        header = 2          # название + прогресс/громкость
        footer = 1          # подсказка по клавишам
        margin = 1
        # минимум строк под текст: половина свободной высоты, но 5..14
        free = max(1, rows - header - footer - margin)
        min_lyrics = max(5, min(14, free // 2))
        avail_rows = free - min_lyrics
        if avail_rows < 4:
            return 0, 0

        aspect = float(img_h) / float(img_w)   # h/w
        # высота картинки в клетках ≈ cols * aspect / 2
        max_by_height = int(avail_rows * 2.0 / aspect)
        max_by_width = cols - 2
        cc = max(8, min(48, max_by_width, max_by_height))
        cr = max(1, int(round(cc * aspect / 2.0)))
        if cr > avail_rows:
            cr = avail_rows
            cc = max(8, int(round(cr * 2.0 / aspect)))
        return cc, cr

    def _cover_cols(self) -> int:
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        except Exception:
            cols, rows = 80, 24
        cc, _ = self._fit_cover(cols, rows, 1, 1)
        return cc if cc > 0 else 16

    def _build_text_lines(self, track, cols, rows, cover_rows, pos, dur):
        vid = track.get("videoId") if track else None
        total_rows = max(6, rows - 1)
        lyr_rows = max(2, total_rows - cover_rows - 3)

        title = track.get("title", t("Unknown")) if track else ""
        artists = ", ".join(a.get("name", "") for a in (track or {}).get("artists", []))
        head1 = fit_text(f" {title} — {artists}", cols)

        status = "⏸" if self.is_paused else "▶"
        bar_w = max(8, cols - 44)
        if dur and dur > 0:
            filled = int(bar_w * min(1.0, max(0.0, (pos or 0) / dur)))
        else:
            filled = 0
        bar = "█" * filled + "░" * (bar_w - filled)
        vol = self.volume
        head2 = fit_text(
            t(
                " {status} {pos} / {dur}  vol. {vol:3d}%  {bar}",
                status=status,
                pos=fmt_time(pos),
                dur=fmt_time(dur),
                vol=vol,
                bar=bar,
            ),
            cols,
        )

        lines = [head1 + "\x1b[K\n", head2 + "\x1b[K\n"]

        with self._meta_lock:
            meta_ready = self._meta_video_id == vid
            lyrics = self.lyrics
            plain = self.lyrics_plain

        lyric_lines: List[str] = []
        if not meta_ready:
            lyric_lines = [t("  loading cover and lyrics…")]
        elif lyrics:
            cur_idx = -1
            p = (pos or 0) + 0.15
            for i, (stamp, _) in enumerate(lyrics):
                if stamp <= p:
                    cur_idx = i
                else:
                    break
            half = lyr_rows // 2
            start = max(0, cur_idx - half)
            if start + lyr_rows > len(lyrics):
                start = max(0, len(lyrics) - lyr_rows)
            for i in range(start, min(len(lyrics), start + lyr_rows)):
                text = lyrics[i][1] or "♪"
                text = fit_text(text, cols - 4)
                if i == cur_idx:
                    lyric_lines.append(f"\x1b[1;33m▶ {text}\x1b[0m")
                else:
                    lyric_lines.append(f"  \x1b[2m{text}\x1b[0m")
        elif plain:
            plines = [ln for ln in plain.splitlines() if ln.strip()]
            for ln in plines[:lyr_rows]:
                lyric_lines.append(f"  {fit_text(ln, cols - 4)}")
        else:
            lyric_lines = ["  \x1b[2m" + t("Lyrics not found") + "\x1b[0m"]

        # ровно lyr_rows строк
        lyric_lines = lyric_lines[:lyr_rows]
        while len(lyric_lines) < lyr_rows:
            lyric_lines.append("")
        for ln in lyric_lines:
            lines.append(ln + "\x1b[K\n")

        footer = fit_text(
            t(" [+/-] vol.  [space] pause  [n] next  [s] stop  [q] quit"),
            cols,
        )
        lines.append("\x1b[2m" + footer + "\x1b[0m" + "\x1b[K\n")
        return lines

    def _read_key(self, timeout: float):
        """Читает событие клавиатуры/мыши.

        Возвращает ("key", ch) | ("esc", None) | ("wheel", ±1) |
        ("arrow", "up|down|left|right") | ("mouse", btn) | ("seq", str) | None.
        """
        fd = sys.stdin.fileno()
        try:
            dr, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not dr:
            return None
        try:
            raw = os.read(fd, 1)
        except OSError:
            return None
        if not raw:
            return None
        ch = raw.decode("utf-8", "replace")

        if ch != "\x1b":
            return ("key", ch)

        # Возможно, это ESC-последовательность. Дочитываем остаток.
        seq = b""
        end = time.time() + 0.05
        while time.time() < end:
            remain = end - time.time()
            if remain <= 0:
                break
            r, _, _ = select.select([fd], [], [], remain)
            if not r:
                break
            try:
                b = os.read(fd, 1)
            except OSError:
                break
            if not b:
                break
            seq += b
            if seq.startswith(b"[<") and b in (b"M", b"m"):
                break  # SGR mouse
            if seq.startswith(b"O") and len(seq) >= 2:
                break  # SS3 (F-клавиши и т.п.)
            if seq.startswith(b"[") and len(seq) >= 2 and seq[-1:].isalpha():
                break  # CSI (стрелки и т.п.)

        if not seq:
            return ("esc", None)

        s = seq.decode("utf-8", "replace")
        full = "\x1b" + s

        if s.startswith("[<") and s[-1:] in ("M", "m"):
            try:
                btn = int(s[2:-1].split(";")[0])
            except (ValueError, IndexError):
                btn = 0
            if btn == 64:
                return ("wheel", 1)
            if btn == 65:
                return ("wheel", -1)
            return ("mouse", btn)

        arrows = {
            "\x1b[A": "up",
            "\x1b[B": "down",
            "\x1b[C": "right",
            "\x1b[D": "left",
            "\x1bOA": "up",
            "\x1bOB": "down",
            "\x1bOC": "right",
            "\x1bOD": "left",
        }
        if full in arrows:
            return ("arrow", arrows[full])
        return ("seq", full)

    def _seek(self, delta: int):
        if self.mpv is not None:
            self.mpv.command("seek", delta, "relative")

    def _player_view(self):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        self.view_active = True
        last_vid = None
        last_cover_ready = None
        last_size = None
        text_start = 0
        idle_frames = 0
        try:
            tty.setcbreak(fd)
            # alternate screen + скрыть курсор + включить mouse tracking (SGR)
            sys.stdout.write(
                "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H\x1b[?1000h\x1b[?1006h"
            )
            sys.stdout.flush()

            while True:
                # условие выхода: трек закончился, очередь пуста, ничего не играет
                if not self.is_playing and self.mpv is None and not self.queue:
                    idle_frames += 1
                    if idle_frames >= 3:
                        break
                else:
                    idle_frames = 0

                cols, rows = shutil.get_terminal_size(fallback=(80, 24))
                track = self.current_track or {}
                vid = track.get("videoId")

                with self._meta_lock:
                    cover_img = self.cover_image if self._meta_video_id == vid else None
                    cover_ansi = self.cover_ansi if self._meta_video_id == vid else None

                # подбираем масштаб обложки под текущий размер окна
                ccols = 0
                crows = 0
                ansi_lines: Optional[List[str]] = None
                if cover_img is not None:
                    ccols, crows = self._fit_cover(
                        cols, rows, cover_img.width, cover_img.height
                    )
                use_kitty = kitty_supported() and ccols > 0

                if ccols > 0 and cover_img is not None:
                    if use_kitty:
                        cover_rows = crows
                    else:
                        ansi_lines = render_image_ansi(cover_img, ccols)
                        cover_rows = len(ansi_lines)
                elif cover_ansi:
                    # нет PIL/картинки — используем закэшированную ANSI-версию,
                    # но не даём ей вылезти за пределы экрана
                    max_rows = max(0, rows - 7)
                    ansi_lines = cover_ansi[:max_rows]
                    cover_rows = len(ansi_lines)
                else:
                    cover_rows = 0
                cover_ready = bool(cover_img or cover_ansi)

                pos = None
                dur = None
                mpv_now = self.mpv
                if mpv_now is not None:
                    pos = mpv_now.get("time-pos")
                    dur = mpv_now.get("duration")

                # полная перерисовка при смене трека/обложки/размера
                full = (
                    vid != last_vid
                    or cover_ready != last_cover_ready
                    or (cols, rows) != last_size
                )

                text_lines = self._build_text_lines(
                    track, cols, rows, cover_rows, pos, dur
                )

                if full:
                    last_vid = vid
                    last_cover_ready = cover_ready
                    last_size = (cols, rows)
                    text_start = cover_rows  # 0-based строка начала текста
                    buf = ["\x1b[2J\x1b[H"]
                    if kitty_supported():
                        buf.append(kitty_delete_image())
                    drawn_kitty = False
                    if use_kitty:
                        png = encode_png(cover_img)
                        if png:
                            buf.append("\x1b[1;1H")
                            buf.append(kitty_display_png(png, ccols, crows))
                            buf.append(f"\x1b[{crows + 1};1H")
                            drawn_kitty = True
                    if not drawn_kitty and ansi_lines:
                        buf.append("\n".join(ansi_lines))
                        buf.append("\n")
                    buf.extend(text_lines)
                    sys.stdout.write("".join(buf))
                else:
                    sys.stdout.write(f"\x1b[{text_start + 1};1H")
                    sys.stdout.write("".join(text_lines))
                sys.stdout.flush()

                ev = self._read_key(0.35)
                if ev:
                    kind, val = ev
                    if kind == "esc" or (kind == "key" and val in ("q", "Q", "\x03")):
                        break
                    elif kind == "key":
                        if val == " ":
                            if self.is_paused:
                                self.resume()
                            else:
                                self.pause()
                        elif val in ("n", "N"):
                            self.skip_next()
                        elif val in ("+", "="):
                            self.change_volume(5)
                        elif val in ("-", "_"):
                            self.change_volume(-5)
                        elif val == "]":
                            self.change_volume(1)
                        elif val == "[":
                            self.change_volume(-1)
                        elif val in ("s", "S"):
                            break
                    elif kind == "wheel":
                        self.change_volume(5 if val > 0 else -5)
                    elif kind == "arrow":
                        if val == "up":
                            self.change_volume(5)
                        elif val == "down":
                            self.change_volume(-5)
                        elif val == "right":
                            self._seek(5)
                        elif val == "left":
                            self._seek(-5)
                    # mouse / прочие последовательности — игнорируем
        finally:
            with self._track_lock:
                self._halt()
                self.queue = []
            self.view_active = False
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass
            if kitty_supported():
                sys.stdout.write(kitty_delete_image())
            sys.stdout.write(
                "\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?25h\x1b[?1049l"
            )
            sys.stdout.flush()

    # ---------- Плейлисты ----------

    def _load_playlists(self):
        if os.path.exists(PLAYLISTS_FILE):
            try:
                with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
                    self.playlists = json.load(f)
            except Exception:
                self.playlists = {}

    def _save_playlists(self):
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.playlists, f, ensure_ascii=False, indent=2)

    def list_playlists(self):
        if not self.playlists and not self.account_playlists:
            print(t("[!] No playlists"))
            return
        if self.playlists:
            print("\n" + t("=== Local playlists ==="))
            for i, name in enumerate(self.playlists.keys(), 1):
                print(f"{i}. {name} ({tracks_label(len(self.playlists[name]))})")
        if self.account_playlists:
            print("\n" + t("=== YouTube Music account playlists ==="))
            for i, (name, meta) in enumerate(self.account_playlists.items(), 1):
                count = meta.get("count")
                suffix = f" ({tracks_label(count)})" if count else ""
                print(f"{i}. {name}{suffix}")

    def create_playlist(self, name: str):
        if name in self.playlists:
            print(t("[!] Playlist '{name}' already exists", name=name))
            return
        self.playlists[name] = []
        self._save_playlists()
        print(t("[+] Playlist '{name}' created", name=name))

    def add_to_playlist(self, playlist_name: str, track: Dict):
        if playlist_name not in self.playlists:
            print(t("[!] Playlist '{name}' not found", name=playlist_name))
            return
        vid = track.get("videoId")
        if any(track.get("videoId") == vid for track in self.playlists[playlist_name]):
            print(t("[!] Track is already in '{name}'", name=playlist_name))
            return
        self.playlists[playlist_name].append(track)
        self._save_playlists()
        print(
            t(
                "[+] Added to '{name}': {title}",
                name=playlist_name,
                title=track.get("title"),
            )
        )

    def show_playlist(self, name: str):
        if name not in self.playlists and name not in self.account_playlists:
            print(t("[!] Playlist '{name}' not found", name=name))
            return
        tracks = self._playlist_tracks(name)
        if not tracks:
            print(t("[!] Playlist '{name}' is empty or unavailable", name=name))
            return
        print(
            "\n"
            + t(
                "=== Playlist: {name} ({tracks}) ===",
                name=name,
                tracks=tracks_label(len(tracks)),
            )
        )
        for i, track in enumerate(tracks, 1):
            artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
            print(f"{i}. {track.get('title')} — {artists} | id: {track.get('videoId')}")

    def remove_from_playlist(self, name: str, index: int):
        if name not in self.playlists:
            print(t("[!] Playlist '{name}' not found", name=name))
            return
        try:
            removed = self.playlists[name].pop(index - 1)
            self._save_playlists()
            print(t("[-] Removed: {title}", title=removed.get("title")))
        except IndexError:
            print(t("[!] Invalid number"))


# --------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------
def print_menu():
    print("\n=== Amethyst ===")
    print(t("1. Search for a track"))
    print(t("2. Pick a track from results"))
    print(t("3. Play (player: cover + lyrics)"))
    print(t("4. Pause / Resume"))
    print(t("5. Stop"))
    print(t("6. Show current track"))
    print(t("7. Playlists"))
    print(t("8. Sign in to YouTube Music (from browser)"))
    print(t("9. Volume (0-100%)"))
    print(t("10. Language / Язык"))
    print(t("0. Exit"))


def language_menu():
    print("\n" + t("=== Language / Язык ==="))
    print(t("1. English"))
    print(t("2. Русский"))
    print(t("0. Back"))
    choice = input("> ").strip()
    ok = True
    if choice == "1":
        ok = set_language("en")
        print(t("[+] Language set to English"))
    elif choice == "2":
        ok = set_language("ru")
        print(t("[+] Language set to Russian"))
    elif choice not in ("", "0"):
        print(t("[!] Invalid command"))
        return
    else:
        return
    if not ok:
        print(t("[!] Could not save the language setting"))


def playlist_menu(player: YouTubeMusicPlayer):
    while True:
        print("\n" + t("--- Playlists ---"))
        print(t("1. List playlists"))
        print(t("2. Create a playlist"))
        print(t("3. Add current track to a playlist"))
        print(t("4. Show a playlist"))
        print(t("5. Remove a track from a playlist"))
        print(t("6. Play a playlist"))
        print(t("7. Import/refresh playlists from account"))
        print(t("8. Play a playlist shuffled (no repeats)"))
        print(t("0. Back"))
        choice = input("> ").strip()

        if choice == "1":
            player.list_playlists()
        elif choice == "2":
            name = input(t("Playlist name: ")).strip()
            if name:
                player.create_playlist(name)
        elif choice == "3":
            if not player.current_track:
                print(t("[!] No track selected"))
                continue
            name = input(t("Playlist name: ")).strip()
            if name:
                if name not in player.playlists:
                    player.create_playlist(name)
                player.add_to_playlist(name, player.current_track)
        elif choice == "4":
            name = input(t("Playlist name: ")).strip()
            if name:
                player.show_playlist(name)
        elif choice == "5":
            name = input(t("Playlist name: ")).strip()
            if name:
                player.show_playlist(name)
                try:
                    idx = int(input(t("Number to remove: ")).strip())
                    player.remove_from_playlist(name, idx)
                except ValueError:
                    print(t("[!] Enter a number"))
        elif choice == "6":
            name = input(t("Playlist name: ")).strip()
            if name:
                player.play_playlist(name)
        elif choice == "7":
            player.import_account_playlists()
        elif choice == "8":
            name = input(t("Playlist name: ")).strip()
            if name:
                player.play_playlist(name, shuffle=True)
        elif choice == "0":
            break
        else:
            print(t("[!] Invalid command"))


def main():
    ensure_data_dir()
    player = YouTubeMusicPlayer()

    while True:
        print_menu()
        choice = input("\n> ").strip()

        if choice == "1":
            query = input(t("Enter a query: ")).strip()
            if query:
                player.show_results(player.search(query))
        elif choice == "2":
            if not player.last_results:
                print(t("[!] Search first"))
                continue
            try:
                idx = int(input(t("Track number: ")).strip())
                track = player.pick_track(idx)
                if track:
                    print(t("[+] Selected: {title}", title=track.get("title")))
            except ValueError:
                print(t("[!] Enter a number"))
        elif choice == "3":
            player.play()
        elif choice == "4":
            if player.is_paused:
                player.resume()
            elif player.is_playing:
                player.pause()
            else:
                print(t("[!] Nothing is playing"))
        elif choice == "5":
            player.stop()
        elif choice == "6":
            player.show_current()
        elif choice == "7":
            playlist_menu(player)
        elif choice == "8":
            player.login_google()
        elif choice == "9":
            try:
                v = int(input(t("Volume 0-100: ")).strip())
                print(t("[+] Volume: {value}%", value=player.set_volume(v)))
            except ValueError:
                print(t("[!] Enter a number 0-100"))
        elif choice == "10":
            language_menu()
        elif choice == "0":
            player.stop()
            print(t("Bye!"))
            break
        else:
            print(t("[!] Invalid command"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n" + t("[!] Interrupted by user"))
        sys.exit(0)
