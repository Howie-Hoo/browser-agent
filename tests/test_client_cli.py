"""client CLI 解析与输出单测 — find-url 旗标 / attach --probe / 结果格式化。
不启动服务、不触碰真实浏览器数据。
运行：pytest tests/test_client_cli.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import client as ba_client  # noqa: E402


def test_parse_args_find_url_flags():
    """旗标一律等值形式（--only=bookmarks）；多关键词按位置参数收集。"""
    sub, pos, opts = ba_client._parse_args([
        "find-url", "财务", "平台", "--only=bookmarks", "--browser=Chrome",
        "--limit=0", "--since=1d", "--sort=visits",
    ])
    assert sub == "find-url" and pos == ["财务", "平台"]
    assert opts["only"] == "bookmarks" and opts["browser"] == "Chrome"
    assert opts["limit"] == "0" and opts["since"] == "1d" and opts["sort"] == "visits"


def test_parse_args_probe_flag_swallowed_safely():
    sub, pos, opts = ba_client._parse_args(["attach", "--probe=Google"])
    assert sub == "attach" and pos == [] and "--probe=Google" not in pos


def test_print_find_url_bookmarks_and_history(capsys):
    ba_client._print_find_url({
        "ok": True,
        "bookmarks": [{"name": "内部门户", "url": "https://p.internal/x",
                       "folder": "工作"}],
        "history": [{"title": "页面", "url": "https://h.com/1",
                     "visited_at_iso": "2026-11-21T10:00:00", "visit_count": 3}],
    })
    out = capsys.readouterr().out
    assert "[书签] 1 条" in out and "内部门户 | https://p.internal/x | 工作" in out
    assert "[历史] 1 条（按最近访问）" in out and "visits=3" in out


def test_print_find_url_empty_and_sort_label(capsys):
    ba_client._print_find_url({"ok": True, "bookmarks": [], "history": []},
                              sort="visits")
    assert "（无匹配结果）" in capsys.readouterr().out


def test_print_find_url_error_path(capsys):
    ba_client._print_find_url({"ok": False, "error": "未找到任何可辨认的浏览器",
                               "remedy": "安装 Chrome"})
    captured = capsys.readouterr()
    assert "未找到任何可辨认" in captured.err
    assert "安装 Chrome" in captured.out


def test_parse_args_space_form_flags():
    """空格式（--only history）与等值式（--only=history）都解析——SKILL.md
    文档示例用的是空格式，解析器不能只认等值式把值吞进关键词。"""
    sub, pos, opts = ba_client._parse_args(
        ["find-url", "agent", "--only", "history", "--limit", "0"])
    assert sub == "find-url" and pos == ["agent"]
    assert opts["only"] == "history" and opts["limit"] == "0"
    sub2, pos2, opts2 = ba_client._parse_args(
        ["find-url", "--since=7d", "--sort=visits"])
    assert pos2 == [] and opts2["since"] == "7d" and opts2["sort"] == "visits"


def test_parse_args_probe_space_form():
    sub, pos, opts = ba_client._parse_args(["attach", "--probe", "Google"])
    assert sub == "attach" and opts.get("probe") == "Google" and pos == []


def test_find_url_bad_limit_isolated(monkeypatch):
    """测试纪律：不在单测里触碰真实浏览器数据——根清单指向空。"""
    from browser_agent import browser_attach as ba
    monkeypatch.setattr(ba, "default_roots", lambda: [])
    c = ba_client.BrowserAgentClient(auto_start=False)
    res = c.find_url(["x"], limit="abc")
    assert res["ok"] is False and "--limit" in res["error"]
    res2 = c.find_url(["x"], limit="0")
    assert res2["ok"] is False and "未找到任何可辨认" in res2["error"]
