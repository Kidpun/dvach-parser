"""
parser.py — автономный скрипт для парсинга 2ch.org через JSON API.

Использование:
    python parser.py
    python parser.py -b news -m latest -l 3 -f txt
    python parser.py -m list -t 123456 654321 -o ./out

Настройки берутся из config.json рядом со скриптом; флаги CLI имеют приоритет.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cloudscraper
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("dvach-parser")

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_DIR = BASE_DIR / "output"

BOARD_RE = re.compile(r"^[a-z0-9]{1,10}$")
MODES = ("latest", "list")
FORMATS = ("json", "txt")


# ──────────────────────────────────────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    board: str = "b"
    mode: str = "latest"
    threads_limit: int = 5
    thread_ids: list[int] = field(default_factory=list)
    output_format: str = "json"
    output_dir: Path = OUTPUT_DIR
    retry_count: int = 5
    retry_backoff_factor: float = 1
    timeout: int = 15
    delay: float = 1.0

    def validate(self) -> None:
        """Бросает ValueError с понятным текстом, если настройки некорректны."""
        if not BOARD_RE.match(self.board):
            raise ValueError(
                f"Некорректное имя раздела: {self.board!r}. Допустимо: a-z0-9, до 10 символов."
            )
        if self.mode not in MODES:
            raise ValueError(f"Неизвестный режим: {self.mode!r}. Допустимые: {', '.join(MODES)}.")
        if self.output_format not in FORMATS:
            raise ValueError(
                f"Неизвестный формат: {self.output_format!r}. Допустимые: {', '.join(FORMATS)}."
            )
        if self.threads_limit < 1:
            raise ValueError("threads_limit должен быть >= 1.")
        if self.timeout < 1:
            raise ValueError("timeout должен быть >= 1.")
        if self.retry_count < 0 or self.delay < 0:
            raise ValueError("retry_count и delay не могут быть отрицательными.")
        if self.mode == "list" and not self.thread_ids:
            raise ValueError("В режиме list нужен хотя бы один ID треда (thread_ids / --threads).")


def load_config(path: Path) -> dict:
    """Читает и возвращает конфиг из JSON-файла."""
    if not path.exists():
        raise FileNotFoundError(f"Конфиг не найден: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Конфиг {path} — невалидный JSON: {e}") from e
    if not isinstance(cfg, dict):
        raise ValueError(f"Конфиг {path} должен быть JSON-объектом.")
    return cfg


def build_settings(args: argparse.Namespace, cfg: dict) -> Settings:
    """Собирает Settings: значения CLI > config.json > значения по умолчанию."""
    defaults = Settings()

    def pick(cli_value, key: str, default):
        return cli_value if cli_value is not None else cfg.get(key, default)

    try:
        thread_ids = [int(i) for i in pick(args.threads, "thread_ids", defaults.thread_ids)]
        settings = Settings(
            board=str(pick(args.board, "board", defaults.board)),
            mode=pick(args.mode, "mode", defaults.mode),
            threads_limit=int(pick(args.limit, "threads_limit", defaults.threads_limit)),
            thread_ids=thread_ids,
            output_format=pick(args.format, "output_format", defaults.output_format),
            output_dir=Path(pick(args.output, "output_dir", defaults.output_dir)),
            retry_count=int(cfg.get("retry_count", defaults.retry_count)),
            retry_backoff_factor=float(
                cfg.get("retry_backoff_factor", defaults.retry_backoff_factor)
            ),
            timeout=int(cfg.get("timeout", defaults.timeout)),
            delay=float(pick(args.delay, "delay", defaults.delay)),
        )
    except (TypeError, ValueError) as e:
        raise ValueError(f"Некорректное значение в настройках: {e}") from e

    settings.validate()
    return settings


# ──────────────────────────────────────────────────────────────────────────────
# HTTP-сессия с автоматическим retry
# ──────────────────────────────────────────────────────────────────────────────

def build_session(retry_count: int, retry_backoff_factor: float) -> cloudscraper.CloudScraper:
    """
    Создаёт cloudscraper-сессию с настроенным urllib3 Retry.

    cloudscraper автоматически обходит Cloudflare JS-челлендж.
    Повторяет запрос при:
      - HTTP 429 (слишком много запросов),
      - HTTP 500, 502, 503, 504 (серверные ошибки),
      - таймаутах и ошибках соединения.
    """
    retry = Retry(
        total=retry_count,
        backoff_factor=retry_backoff_factor,  # задержка = backoff_factor * 2^(attempt-1)
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ──────────────────────────────────────────────────────────────────────────────
# Очистка HTML
# ──────────────────────────────────────────────────────────────────────────────

def clean_html(raw: str) -> str:
    """
    Убирает HTML-теги, декодирует HTML-сущности,
    заменяет <br> на перевод строки, схлопывает пустые строки.
    """
    if not raw:
        return ""

    # <br> → перенос строки ДО удаления тегов
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = BeautifulSoup(text, "html.parser").get_text()

    # 3+ пустые строки подряд → 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Запросы к API
# ──────────────────────────────────────────────────────────────────────────────

def fetch_json(session: cloudscraper.CloudScraper, url: str, timeout: int) -> dict | None:
    """
    Выполняет GET-запрос и возвращает распарсенный JSON.
    Возвращает None при ошибке (запрос уже retry-ился на уровне адаптера).
    """
    try:
        log.debug("GET %s", url)
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        log.error("Таймаут при запросе: %s", url)
    except requests.exceptions.ConnectionError as e:
        log.error("Ошибка соединения (%s): %s", url, e)
    except requests.exceptions.HTTPError as e:
        log.error("HTTP-ошибка (%s): %s", url, e)
    except requests.exceptions.JSONDecodeError:
        log.error("Не удалось распарсить JSON: %s", url)
    except (requests.exceptions.RequestException, cloudscraper.exceptions.CloudflareException) as e:
        log.error("Ошибка запроса (%s): %s", url, e)
    else:
        if isinstance(data, dict):
            return data
        log.error("Неожиданный формат ответа (ожидался объект): %s", url)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Получение тредов
# ──────────────────────────────────────────────────────────────────────────────

def get_latest_thread_ids(
    session: cloudscraper.CloudScraper,
    board: str,
    limit: int,
    timeout: int,
) -> list[int]:
    """
    Загружает catalog.json и возвращает ID последних `limit` тредов.
    Треды из каталога отсортированы по дате последнего поста (свежие первые).
    """
    url = f"https://2ch.org/{board}/catalog.json"
    log.info("Загружаю каталог: %s", url)
    data = fetch_json(session, url, timeout)
    if data is None:
        return []

    threads = data.get("threads") or []
    if not threads:
        log.warning("Каталог пуст или имеет неожиданный формат.")
        return []

    # пропускаем элементы без поля "num"
    ids = [int(t["num"]) for t in threads[:limit] if "num" in t]
    log.info("Получено %d тредов из каталога.", len(ids))
    return ids


def get_thread_posts(
    session: cloudscraper.CloudScraper,
    board: str,
    thread_id: int,
    timeout: int,
) -> list[dict]:
    """
    Загружает все посты треда через res/{thread_id}.json.
    Возвращает список {"post_id": int, "comment": str}.
    """
    url = f"https://2ch.org/{board}/res/{thread_id}.json"
    log.info("Загружаю тред %d ...", thread_id)
    data = fetch_json(session, url, timeout)
    if data is None:
        log.warning("Тред %d: не удалось загрузить.", thread_id)
        return []

    # Структура: {"threads": [{"posts": [...]}]}
    threads = data.get("threads") or []
    if not threads:
        log.warning("Тред %d: пустой или неожиданный формат.", thread_id)
        return []

    raw_posts = threads[0].get("posts") or []
    posts = [
        {"post_id": int(p.get("num", 0)), "comment": clean_html(p.get("comment", ""))}
        for p in raw_posts
    ]

    log.info("Тред %d: %d постов.", thread_id, len(posts))
    return posts


# ──────────────────────────────────────────────────────────────────────────────
# Сохранение результатов
# ──────────────────────────────────────────────────────────────────────────────

def save_json(thread_id: int, posts: list[dict], out_dir: Path) -> Path:
    """Сохраняет посты треда в JSON-файл."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"thread_{thread_id}.json"
    payload = {"thread_id": thread_id, "posts": posts}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("Сохранено: %s", path)
    return path


def save_txt(thread_id: int, posts: list[dict], out_dir: Path) -> Path:
    """Сохраняет посты треда в текстовый файл."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"thread_{thread_id}.txt"
    with path.open("w", encoding="utf-8") as f:
        for post in posts:
            f.write(f"[{post['post_id']}]\n{post['comment']}\n\n")
    log.info("Сохранено: %s", path)
    return path


SAVERS = {"json": save_json, "txt": save_txt}


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2ch.org JSON API Thread Parser")
    parser.add_argument("-b", "--board", help="Имя раздела (например, b, news, tech)")
    parser.add_argument("-m", "--mode", choices=MODES, help="Режим парсинга: latest или list")
    parser.add_argument("-l", "--limit", type=int, help="Лимит тредов для режима latest")
    parser.add_argument("-t", "--threads", nargs="+", type=int, help="ID тредов для режима list")
    parser.add_argument("-f", "--format", choices=FORMATS, help="Формат вывода: json или txt")
    parser.add_argument("-o", "--output", help="Папка для результатов (по умолчанию ./output)")
    parser.add_argument("-d", "--delay", type=float, help="Пауза между тредами, сек (по умолчанию 1)")
    parser.add_argument("-c", "--config", default=str(CONFIG_PATH), help="Путь к config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный лог (DEBUG)")
    return parser.parse_args(argv)


def run(settings: Settings) -> int:
    """Выполняет парсинг. Возвращает код выхода: 0 — всё ок, 1 — были ошибки."""
    log.info(
        "Раздел: /%s/  Режим: %s  Формат: %s",
        settings.board, settings.mode, settings.output_format,
    )
    save = SAVERS[settings.output_format]
    failed = 0

    # context manager гарантирует закрытие connection pool
    with build_session(settings.retry_count, settings.retry_backoff_factor) as session:
        if settings.mode == "latest":
            ids = get_latest_thread_ids(
                session, settings.board, settings.threads_limit, settings.timeout
            )
        else:
            ids = settings.thread_ids
            log.info("Список тредов: %s", ids)

        if not ids:
            log.error("Нет тредов для загрузки.")
            return 1

        for idx, thread_id in enumerate(ids, start=1):
            log.info("[%d/%d] Обрабатываю тред %d", idx, len(ids), thread_id)
            posts = get_thread_posts(session, settings.board, thread_id, settings.timeout)
            if posts:
                save(thread_id, posts, settings.output_dir)
            else:
                failed += 1
                log.warning("Тред %d: постов нет, файл не создаётся.", thread_id)

            # пауза между запросами, чтобы не перегружать сервер
            if idx < len(ids):
                time.sleep(settings.delay)

    log.info("Готово. Результаты в папке: %s", settings.output_dir)
    if failed:
        log.warning("Не удалось загрузить тредов: %d из %d.", failed, len(ids))
    return 1 if failed == len(ids) else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    config_file = Path(args.config)
    try:
        cfg = load_config(config_file) if config_file.exists() else {}
        settings = build_settings(args, cfg)
    except (ValueError, OSError) as e:
        log.error("%s", e)
        return 2

    try:
        return run(settings)
    except KeyboardInterrupt:
        log.warning("Прервано пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
