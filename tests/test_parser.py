import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser as dp  # noqa: E402


def make_args(argv=None):
    return dp.parse_cli_args(argv or [])


# ── clean_html ────────────────────────────────────────────────────────────────

def test_clean_html_br_and_entities():
    assert dp.clean_html("a<br>b<BR/>c &gt; d") == "a\nb\nc > d"


def test_clean_html_collapses_blank_lines():
    assert dp.clean_html("a<br><br><br><br>b") == "a\n\nb"


def test_clean_html_empty():
    assert dp.clean_html("") == ""
    assert dp.clean_html(None) == ""


# ── настройки ─────────────────────────────────────────────────────────────────

def test_defaults():
    s = dp.build_settings(make_args(), {})
    assert (s.board, s.mode, s.threads_limit, s.output_format) == ("b", "latest", 5, "json")


def test_cli_overrides_config():
    s = dp.build_settings(make_args(["-b", "news", "-l", "3"]), {"board": "b", "threads_limit": 9})
    assert (s.board, s.threads_limit) == ("news", 3)


def test_config_used_when_no_cli():
    s = dp.build_settings(make_args(), {"board": "tech", "output_format": "txt"})
    assert (s.board, s.output_format) == ("tech", "txt")


def test_zero_delay_is_respected():
    s = dp.build_settings(make_args(["-d", "0"]), {"delay": 5})
    assert s.delay == 0


@pytest.mark.parametrize("board", ["", "B", "a/b", "../x", "toolongboardname"])
def test_invalid_board(board):
    with pytest.raises(ValueError):
        dp.build_settings(make_args(["-b", board]) if board else make_args(), {"board": board})


def test_list_mode_requires_ids():
    with pytest.raises(ValueError):
        dp.build_settings(make_args(["-m", "list"]), {})


def test_bad_thread_id_in_config():
    with pytest.raises(ValueError):
        dp.build_settings(make_args(["-m", "list"]), {"thread_ids": ["abc"]})


def test_bad_limit():
    with pytest.raises(ValueError):
        dp.build_settings(make_args(["-l", "0"]), {})


def test_load_config_invalid_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        dp.load_config(p)


# ── сохранение ────────────────────────────────────────────────────────────────

POSTS = [{"post_id": 1, "comment": "привет"}, {"post_id": 2, "comment": "мир"}]


def test_save_json(tmp_path):
    path = dp.save_json(42, POSTS, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"thread_id": 42, "posts": POSTS}


def test_save_txt(tmp_path):
    path = dp.save_txt(42, POSTS, tmp_path)
    assert path.read_text(encoding="utf-8") == "[1]\nпривет\n\n[2]\nмир\n\n"


# ── разбор ответов API ────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, url, timeout):
        payload = self.payloads[url]

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return Resp()


def test_get_latest_thread_ids_skips_items_without_num():
    url = "https://2ch.org/b/catalog.json"
    session = FakeSession({url: {"threads": [{"num": "10"}, {"x": 1}, {"num": 12}]}})
    assert dp.get_latest_thread_ids(session, "b", 5, 5) == [10, 12]


def test_get_thread_posts_cleans_comments():
    url = "https://2ch.org/b/res/7.json"
    session = FakeSession({url: {"threads": [{"posts": [{"num": 7, "comment": "a<br>b"}]}]}})
    assert dp.get_thread_posts(session, "b", 7, 5) == [{"post_id": 7, "comment": "a\nb"}]


def test_fetch_json_rejects_non_object():
    session = FakeSession({"u": [1, 2, 3]})
    assert dp.fetch_json(session, "u", 5) is None
