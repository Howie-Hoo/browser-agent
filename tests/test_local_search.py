"""local_search 单测 — 书签/历史检索（伪造浏览器目录，不触真实用户数据）。

History 用真实 SQLite 伪造（含 webkit 时间戳换算面）；Bookmarks 用 JSON 伪造。
运行：pytest tests/test_local_search.py -q
"""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import browser_attach as ba  # noqa: E402
from browser_agent.local_search import (  # noqa: E402
    LocalSearchQuery, parse_since, run_local_search,
)
from fakes import make_fake_browser  # noqa: E402


@pytest.fixture
def fake_browser(tmp_path, monkeypatch):
    """一台双 profile 的伪造浏览器：书签 + 历史（Default 新旧各一，Profile 1 高频）。"""
    now = time.time()
    return make_fake_browser(
        tmp_path, monkeypatch, name="MyChrome",
        profiles=[("Default", "Default"), ("Profile 1", "工作")],
        bookmarks=[
            {"type": "url", "name": "内部财务平台",
             "url": "https://fin.internal.corp/dashboard"},
            {"type": "folder", "name": "工具", "children": [
                {"type": "url", "name": "GitHub", "url": "https://github.com"}]},
        ],
        history={
            "Default": [
                ("https://a.com/1", "页面一", 1, now),
                ("https://b.com/2", "页面二", 5, now - 10 * 86400),  # 10 天前
            ],
            "Profile 1": [("https://c.com/3", "高频站", 99, now - 60)],
        })


def _search(**kw):
    return run_local_search(LocalSearchQuery.from_params(**kw))


# ── 参数解析 ───────────────────────────────────────────────

def test_parse_since_relative_and_date():
    sec = parse_since("1h")
    assert 3595 < time.time() - sec < 3605
    assert parse_since("2026-11-01") is not None


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        parse_since("3x")
    with pytest.raises(ValueError):
        parse_since("not-a-date")


def test_from_params_validates_only_sort_limit():
    with pytest.raises(ValueError):
        LocalSearchQuery.from_params(only="everything")
    with pytest.raises(ValueError):
        LocalSearchQuery.from_params(sort="oldest")
    with pytest.raises(ValueError):
        LocalSearchQuery.from_params(limit=-1)
    with pytest.raises(ValueError):
        LocalSearchQuery.from_params(keywords=42)
    # 字符串关键词按空格分词
    q = LocalSearchQuery.from_params(keywords="财务 平台")
    assert q.keywords == ("财务", "平台")


# ── 书签 ───────────────────────────────────────────────────

def test_bookmark_keyword_match_with_folder_trail(fake_browser):
    res = _search(keywords="财务")
    assert res["ok"]
    assert len(res["bookmarks"]) == 1
    b = res["bookmarks"][0]
    assert b["url"] == "https://fin.internal.corp/dashboard"
    assert b["browser"] == "MyChrome" and b["profile"] == "Default"


def test_bookmark_inside_folder_keeps_trail(fake_browser):
    res = _search(keywords="github")
    assert [b["folder"] for b in res["bookmarks"]] == ["工具"]


def test_bookmarks_need_keywords(fake_browser):
    res = _search()
    assert res["bookmarks"] == []
    assert len(res["history"]) > 0  # 历史支持浏览式列举


# ── 历史 ───────────────────────────────────────────────────

def test_history_keyword_filters(fake_browser):
    res = _search(keywords="页面")
    urls = [h["url"] for h in res["history"]]
    assert urls == ["https://a.com/1", "https://b.com/2"]  # 近期在前


def test_history_spans_profiles(fake_browser):
    res = _search(keywords="高频")
    assert [h["profile"] for h in res["history"]] == ["工作"]


def test_history_since_window(fake_browser):
    res = _search(keywords="页面", since="1d")
    assert [h["url"] for h in res["history"]] == ["https://a.com/1"]


def test_history_sort_by_visits(fake_browser):
    res = _search(sort="visits")
    assert res["history"][0]["url"] == "https://c.com/3"  # visit_count=99 居首


def test_history_webkit_timestamp_converted(fake_browser):
    now = time.time()
    res = _search(keywords="页面一")
    assert abs(res["history"][0]["visited_at"] - now) < 5


def test_only_limits_source(fake_browser):
    assert _search(only="bookmarks")["history"] == []
    assert _search(only="history")["bookmarks"] == []


def test_limit_slices_each_source(fake_browser):
    res = _search(limit=1)
    assert len(res["history"]) == 1
    assert res["counts"]["history"] == 1


# ── 浏览器选择 / 容错 ─────────────────────────────────────

def test_unknown_browser_name_rejected(fake_browser):
    res = _search(browser="Netscape")
    assert not res["ok"]
    assert "MyChrome" in res["error"]


def test_no_browsers_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "default_roots", lambda: [])
    res = _search(keywords="x")
    assert not res["ok"]
    assert "remedy" in res


def test_broken_bookmarks_file_tolerated(fake_browser):
    (fake_browser / "Default" / "Bookmarks").write_text("{not json", encoding="utf-8")
    res = _search(keywords="财务")
    assert res["ok"] and res["bookmarks"] == []


def test_missing_history_tolerated(fake_browser):
    os.remove(fake_browser / "Default" / "History")
    res = _search(keywords="页面一")
    assert res["ok"] and res["history"] == []


# ── 容错与安全（撕裂快照 / 越界 profile / 腐坏时间戳 / 非对象书签）──

def test_torn_history_snapshot_tolerated(fake_browser):
    """主库文件撕裂（浏览器写入中途复制到的常见形态）：该 profile 返回空，
    不炸整体检索。"""
    (fake_browser / "Default" / "History").write_bytes(b"garbage not a sqlite")
    res = _search(keywords="页面")
    assert res["ok"] and res["history"] == []


def test_profile_key_traversal_rejected(fake_browser, tmp_path):
    """Local State 里的越界 profile 键（绝对路径 / .. 逃逸）不进入检索。"""
    evil = tmp_path / "evil"
    (evil / "Default").mkdir(parents=True)
    (evil / "Default" / "History").write_bytes(b"x")
    (fake_browser / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"name": "Default"},
        "/abs": {"name": "abs"},
        "../evil": {"name": "rel"}}}}), encoding="utf-8")
    res = _search(keywords="页面")
    assert res["ok"]
    assert all(p["profile"] != "abs" and p["profile"] != "rel"
               for p in res["history"])


def test_corrupt_timestamp_row_skipped(fake_browser):
    """last_visit_time 腐坏（溢出/pre-1970）的行跳过，不炸检索。"""
    conn = sqlite3.connect(str(fake_browser / "Default" / "History"))
    conn.execute("INSERT INTO urls (url, title, visit_count, last_visit_time)"
                 " VALUES ('https://bad/1', '坏时间', 1, ?)", (2 ** 62,))
    conn.commit()
    conn.close()
    res = _search(keywords="坏时间")
    assert res["ok"] and res["history"] == []


def test_bookmarks_non_dict_json_tolerated(fake_browser):
    (fake_browser / "Default" / "Bookmarks").write_text("[1, 2, 3]", encoding="utf-8")
    res = _search(keywords="财务")
    assert res["ok"] and res["bookmarks"] == []
