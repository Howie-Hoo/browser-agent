"""检索类测试共用的伪造浏览器数据工厂（绝不触真实用户数据）。

test_local_search / test_proxy_commands 共用：Local State + Bookmarks + History
三件套与 default_roots 隔离都只写一份。
"""
import json
import os
import sqlite3
import time

from browser_agent.local_search import _WEBKIT_EPOCH_DIFF_US as WEBKIT_EPOCH


def write_history(path, rows):
    """rows: [(url, title, visit_count, unix_sec)] → 伪造 Chrome History。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url VARCHAR, title VARCHAR,"
        " visit_count INTEGER, last_visit_time INTEGER)")
    conn.executemany(
        "INSERT INTO urls (url, title, visit_count, last_visit_time)"
        " VALUES (?, ?, ?, ?)",
        [(u, t, c, int(sec * 1_000_000) + WEBKIT_EPOCH) for u, t, c, sec in rows])
    conn.commit()
    conn.close()


def make_fake_browser(tmp_path, monkeypatch, name="Fake",
                      profiles=None, bookmarks=None, history=None):
    """在 tmp_path 下伪造浏览器数据目录并把 default_roots 指过去。

    profiles: [(目录名, 显示名)]；bookmarks: 嵌套书签树（写入 Default 的
    Bookmarks）；history: {profile目录名: [(url,title,visits,unix_sec)]}。
    返回数据目录路径。
    """
    data = tmp_path / name
    profiles = profiles or [("Default", "Default")]
    for p_dir, _ in profiles:
        (data / p_dir).mkdir(parents=True, exist_ok=True)
    (data / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        p_dir: {"name": p_name} for p_dir, p_name in profiles}}}), encoding="utf-8")
    if bookmarks is not None:
        (data / "Default" / "Bookmarks").write_text(
            json.dumps({"roots": {"bookmark_bar": {"children": bookmarks}}}),
            encoding="utf-8")
    for p_dir, rows in (history or {}).items():
        write_history(data / p_dir / "History", rows)

    from browser_agent import browser_attach as ba
    monkeypatch.setattr(ba, "default_roots", lambda: [(name, str(data))])
    return data


def recent_us():
    """当前时刻的 webkit 微秒时间戳（伪造新鲜历史行用）。"""
    return int(time.time() * 1_000_000) + WEBKIT_EPOCH
