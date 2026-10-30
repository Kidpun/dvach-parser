# 🧵 2ch Parser

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Status](https://img.shields.io/badge/status-working-brightgreen?style=flat-square)
![API](https://img.shields.io/badge/API-2ch.org-orange?style=flat-square)
![Code style](https://img.shields.io/badge/code%20style-clean-blueviolet?style=flat-square)

Автономный Python-скрипт для парсинга тредов с [2ch.org](https://2ch.org) через официальный JSON API. Один файл — никаких лишних зависимостей и архитектуры.

---

## ✨ Возможности

- 📦 **Один файл** — `parser.py`, никаких пакетов и модулей
- 🔌 **JSON API** — использует `2ch.org/{board}/catalog.json` и `res/{thread_id}.json`
- 🔄 **Два режима** — последние N тредов или конкретные треды по ID
- 🧹 **Очистка HTML** — убирает теги, декодирует entities, нормализует переносы строк
- 💾 **Два формата** — сохранение в `JSON` или `TXT`
- ♻️ **Auto-retry** — повторяет запросы при 429, 5xx, таймаутах и ошибках соединения
- ⚙️ **Конфиг** — все настройки в `config.json`
- 📋 **Логирование** — понятный вывод с временными метками

---

## 📁 Структура

```
.
├── parser.py        # основной скрипт
├── config.json      # настройки
├── requirements.txt # зависимости
└── output/          # результаты (создаётся автоматически)
    ├── thread_123456.json
    └── thread_654321.txt
```

---

## 🚀 Быстрый старт

### 1. Установить зависимости

```bash
pip install -r requirements.txt
```

### 2. Настроить `config.json`

```json
{
  "board": "b",
  "mode": "latest",
  "threads_limit": 5,
  "thread_ids": [123456, 654321],
  "output_format": "json",
  "retry_count": 5,
  "retry_backoff_factor": 1,
  "timeout": 15,
  "delay": 1
}
```

### 3. Запустить

```bash
python3 parser.py
```

Флаги CLI имеют приоритет над `config.json`:

```bash
python3 parser.py -b news -m latest -l 3 -f txt -o ./out
python3 parser.py -m list -t 123456 654321 -d 2 -v
```

| Флаг | Описание |
|------|----------|
| `-b`, `--board` | раздел |
| `-m`, `--mode` | `latest` или `list` |
| `-l`, `--limit` | сколько последних тредов (для `latest`) |
| `-t`, `--threads` | ID тредов (для `list`) |
| `-f`, `--format` | `json` или `txt` |
| `-o`, `--output` | папка для результатов |
| `-d`, `--delay` | пауза между тредами, сек |
| `-c`, `--config` | путь к `config.json` |
| `-v`, `--verbose` | подробный лог |

Коды выхода: `0` — успех, `1` — ни один тред не загружен, `2` — ошибка настроек.

---

## ⚙️ Параметры конфига

| Параметр        | Тип      | Описание                                             |
|-----------------|----------|------------------------------------------------------|
| `board`         | `string` | Раздел двача (`b`, `dev`, `po` и т.д.)               |
| `mode`          | `string` | Режим: `latest` или `list`                           |
| `threads_limit` | `int`    | Кол-во последних тредов (для режима `latest`)        |
| `thread_ids`    | `array`  | Список ID тредов (для режима `list`)                 |
| `output_format` | `string` | Формат вывода: `json` или `txt`                      |
| `retry_count`   | `int`    | Кол-во попыток при ошибке                            |
| `retry_backoff_factor` | `float` | Множитель экспоненциальной задержки между попытками |
| `timeout`       | `int`    | Таймаут HTTP-запроса (секунды)                       |
| `delay`         | `float`  | Пауза между тредами (секунды)                        |
| `output_dir`    | `string` | Папка для результатов (по умолчанию `output`)        |

---

## 📤 Форматы вывода

### JSON (`output/thread_123456.json`)

```json
{
  "thread_id": 123456,
  "posts": [
    {
      "post_id": 111,
      "comment": "текст сообщения"
    }
  ]
}
```

### TXT (`output/thread_123456.txt`)

```
[111]
текст сообщения

[112]
другое сообщение
```

---

## 🔄 Режимы работы

### `latest` — последние N тредов

Загружает каталог и берёт `threads_limit` свежих тредов.

```json
{
  "mode": "latest",
  "threads_limit": 10
}
```

### `list` — конкретные треды по ID

Загружает только треды из списка `thread_ids`.

```json
{
  "mode": "list",
  "thread_ids": [123456, 654321, 999999]
}
```

---

## 🛡️ Retry-логика

Скрипт автоматически повторяет запросы при:

| Условие             | Поведение               |
|---------------------|-------------------------|
| `HTTP 429`          | повтор с задержкой      |
| `HTTP 500/502/503/504` | повтор с задержкой   |
| Таймаут соединения  | повтор                  |
| Ошибка соединения   | повтор                  |

Используется `urllib3.Retry` с экспоненциальной задержкой (`backoff_factor`).

---

## 🧰 Зависимости

```
requests>=2.31.0
beautifulsoup4>=4.12.0
urllib3>=2.0.0
cloudscraper>=1.2.71
```

---

## 📋 Примеры вывода

```
15:07:08  INFO      Плата: /b/  Режим: latest  Формат: json
15:07:08  INFO      Загружаю каталог: https://2ch.org/b/catalog.json
15:07:09  INFO      Получено 5 тредов из каталога.
15:07:09  INFO      [1/5] Обрабатываю тред 333483235
15:07:12  INFO      Тред 333483235: 64 постов.
15:07:12  INFO      Сохранено: output/thread_333483235.json
...
15:07:14  INFO      Готово. Результаты в папке: output
```

---

## 📌 Требования

- Python **3.11+**
- Доступ к `2ch.org`

---

## 📄 Лицензия

MIT — используй свободно.
