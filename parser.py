"""
parser.py — автономный скрипт для парсинга 2ch.org через JSON API.

Использование:
    python parser.py

Все настройки берутся из config.json рядом со скриптом.
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path

import cloudscraper
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────────────────────────────────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Пути
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent          # директория скрипта
CONFIG_PATH = BASE_DIR / "config.json"   # путь к конфигу
OUTPUT_DIR = BASE_DIR / "output"         # директория для результатов

# ──────────────────────────────────────────────────────────────────────────────
# Загрузка конфига
# ──────────────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    """Читает и возвращает конфиг из JSON-файла."""
    if not path.exists():
        raise FileNotFoundError(f"Конфиг не найден: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)

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
    # cloudscraper — надстройка над requests, умеет решать JS-задачи Cloudflare
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

    # Заменяем <br> / <br/> на перенос строки ДО удаления тегов
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)

    # BeautifulSoup убирает оставшиеся теги и декодирует entities
    text = BeautifulSoup(text, "html.parser").get_text()

    # Убираем лишние пустые строки (3+ подряд → 2)
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
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("Таймаут при запросе: %s", url)
    except requests.exceptions.ConnectionError as e:
        log.error("Ошибка соединения (%s): %s", url, e)
    except requests.exceptions.HTTPError as e:
        log.error("HTTP-ошибка (%s): %s", url, e)
    except requests.exceptions.JSONDecodeError:
        log.error("Не удалось распарсить JSON: %s", url)
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

    # В каталоге поле "threads" содержит список тредов
    threads = data.get("threads") or []
    if not threads:
        log.warning("Каталог пуст или имеет неожиданный формат.")
        return []

    # безопасно: пропускаем элементы без поля "num"
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
    posts = []
    for p in raw_posts:
        posts.append({
            "post_id": int(p.get("num", 0)),
            "comment": clean_html(p.get("comment", "")),
        })

    log.info("Тред %d: %d постов.", thread_id, len(posts))
    return posts

# ──────────────────────────────────────────────────────────────────────────────
# Сохранение результатов
# ──────────────────────────────────────────────────────────────────────────────

def save_json(thread_id: int, posts: list[dict], out_dir: Path) -> None:
    """Сохраняет посты треда в JSON-файл."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"thread_{thread_id}.json"
    payload = {"thread_id": thread_id, "posts": posts}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("Сохранено: %s", path)


def save_txt(thread_id: int, posts: list[dict], out_dir: Path) -> None:
    """Сохраняет посты треда в текстовый файл."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"thread_{thread_id}.txt"
    with path.open("w", encoding="utf-8") as f:
        for post in posts:
            f.write(f"[{post['post_id']}]\n")
            f.write(post["comment"])
            f.write("\n\n")
    log.info("Сохранено: %s", path)

# ──────────────────────────────────────────────────────────────────────────────
# Основная логика
# ──────────────────────────────────────────────────────────────────────────────

def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2ch.org JSON API Thread Parser")
    parser.add_argument("-b", "--board", type=str, help="Имя раздела (например, b, news, tech)")
    parser.add_argument("-m", "--mode", choices=["latest", "list"], help="Режим парсинга: latest или list")
    parser.add_argument("-l", "--limit", type=int, help="Лимит тредов для режима latest")
    parser.add_argument("-t", "--threads", nargs="+", type=int, help="Список ID тредов для режима list")
    parser.add_argument("-f", "--format", choices=["json", "txt"], help="Формат вывода: json или txt")
    parser.add_argument("-c", "--config", type=str, default=str(CONFIG_PATH), help="Путь к config.json")
    return parser.parse_args()


def main() -> None:
    args = parse_cli_args()
    config_file = Path(args.config)

    # 1. Загружаем конфиг
    cfg = load_config(config_file) if config_file.exists() else {}
    board         = args.board or cfg.get("board", "b")
    mode          = args.mode or cfg.get("mode", "latest")
    threads_limit = args.limit or cfg.get("threads_limit", 5)
    thread_ids    = args.threads or cfg.get("thread_ids", [])
    out_fmt       = args.format or cfg.get("output_format", "json")
    retry_count          = cfg.get("retry_count", 5)
    retry_backoff_factor = cfg.get("retry_backoff_factor", 1)
    timeout              = cfg.get("timeout", 15)

    # валидация board: только a-z0-9, длина 1–10
    if not re.match(r"^[a-z0-9]{1,10}$", board):
        raise ValueError(f"Некорректное имя раздела: {board!r}. Допустимо: a-z0-9, до 10 символов.")

    log.info("Раздел: /%s/  Режим: %s  Формат: %s", board, mode, out_fmt)

    # 2. Создаём сессию (context manager гарантирует закрытие connection pool)
    with build_session(retry_count, retry_backoff_factor) as session:

        # 3. Определяем список ID тредов
        if mode == "latest":
            ids = get_latest_thread_ids(session, board, threads_limit, timeout)
        elif mode == "list":
            ids = [int(i) for i in thread_ids]
            log.info("Список тредов из конфига: %s", ids)
        else:
            log.error("Неизвестный режим: %s. Допустимые: latest, list.", mode)
            return

        if not ids:
            log.error("Нет тредов для загрузки.")
            return

        # 4. Скачиваем и сохраняем каждый тред
        save_fn = save_json if out_fmt == "json" else save_txt

        for idx, thread_id in enumerate(ids, start=1):
            log.info("[%d/%d] Обрабатываю тред %d", idx, len(ids), thread_id)
            posts = get_thread_posts(session, board, thread_id, timeout)
            if posts:
                save_fn(thread_id, posts, OUTPUT_DIR)
            else:
                log.warning("Тред %d: постов нет, файл не создаётся.", thread_id)

            # Небольшая пауза между запросами, чтобы не перегружать сервер
            if idx < len(ids):
                time.sleep(1)

    log.info("Готово. Результаты в папке: %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
