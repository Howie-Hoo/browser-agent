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


# ── 退出码契约（0 成功 / 1 硬失败 / 2 需用户动作）──────────

class _StubClient:
    """CLI 层测试替身：只提供 main() 用到的表面（start_service/get_status）。"""

    def __init__(self, status=None):
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def start_service(self):
        return False

    def get_status(self):
        return self._status


def test_exit_code_2_when_attach_browser_not_connected(monkeypatch, capsys):
    """服务在但 attach 浏览器未连上（等授权/目标不可用）→ CLI 退出码 2。"""
    from browser_agent import client as cli

    monkeypatch.setattr(cli, "BrowserAgentClient", lambda **kw: _StubClient(
        status={"service": "browser_agent", "browser_mode": "attach",
                "browser_active": False}))
    with pytest.raises(SystemExit) as e:
        _run_main(cli, ["start"])
    assert e.value.code == 2


def _run_main(cli, argv):
    import sys as _sys
    old = _sys.argv
    _sys.argv = ["browser-agent-client"] + argv
    try:
        return cli.main()
    finally:
        _sys.argv = old


def test_exit_code_1_on_plain_failure(monkeypatch):
    from browser_agent import client as cli

    monkeypatch.setattr(cli, "BrowserAgentClient", lambda **kw: _StubClient(status=None))
    with pytest.raises(SystemExit) as e:
        _run_main(cli, ["start"])
    assert e.value.code == 1


# ── attach 就绪等待（授权提示 + 超时返回 False）────────────

def test_await_attach_ready_success_then_timeout(monkeypatch, isolated_home):
    from types import SimpleNamespace
    from browser_agent.client import BrowserAgentClient

    monkeypatch.setenv("BROWSER_AGENT_ATTACH_READY_SEC", "1")
    monkeypatch.setattr("time.sleep", lambda s: None)  # 纯逻辑测试不吃真实等待
    c = BrowserAgentClient.__new__(BrowserAgentClient)
    c._http = SimpleNamespace(post=lambda *a, **k: None)

    seq = [{"browser_active": False}, {"browser_active": True}]
    monkeypatch.setattr(c, "get_status",
                        lambda: seq.pop(0) if seq else {"browser_active": True})
    assert c._await_attach_ready() is True

    seq2 = [{"browser_active": False}] * 50
    monkeypatch.setattr(c, "get_status",
                        lambda: seq2.pop(0) if seq2 else {"browser_active": False})
    assert c._await_attach_ready() is False


def test_start_service_waits_when_attach_not_connected(monkeypatch):
    """服务已在但 attach 未连上：start 补就绪等待（退出码契约），不假成功。"""
    from types import SimpleNamespace
    from browser_agent.client import BrowserAgentClient

    c = BrowserAgentClient.__new__(BrowserAgentClient)
    c._http = SimpleNamespace(post=lambda *a, **k: None)
    monkeypatch.setattr(c, "is_running", lambda: True)
    waited = []
    monkeypatch.setattr(c, "_await_attach_ready", lambda: waited.append(1) or True)

    monkeypatch.setattr(c, "get_status",
                        lambda: {"service": "browser_agent", "browser_mode": "attach", "browser_active": False})
    assert c.start_service() is True and waited == [1]

    monkeypatch.setattr(c, "get_status",
                        lambda: {"service": "browser_agent", "browser_mode": "managed", "browser_active": True})
    waited.clear()
    assert c.start_service() is True and waited == []


# ── started_by_me（谁拉起谁清理的事实源）──────────────────────

def test_started_by_me_defaults_false_and_stays_false_when_already_running(monkeypatch):
    """默认 False；服务已在运行（start_service 短路分支）不置位——那是别人拉起的。"""
    from browser_agent.client import BrowserAgentClient

    c = BrowserAgentClient(auto_start=False)
    assert c.started_by_me is False
    monkeypatch.setattr(c, "get_status",
                        lambda: {"service": "browser_agent", "pid": 1})
    assert c.start_service() is True
    assert c.started_by_me is False


def test_started_by_me_set_only_after_popen(monkeypatch):
    """真正 Popen 出服务进程才置 True（服务确系本进程拉起）。"""
    import browser_agent.utils as bau
    from browser_agent.client import BrowserAgentClient

    c = BrowserAgentClient(auto_start=False)
    monkeypatch.setattr(c, "get_status", lambda: None)
    monkeypatch.setattr(bau, "check_system_resources", lambda: (True, ""))
    popened = []
    monkeypatch.setattr(ba_client.subprocess, "Popen", lambda *a, **k: popened.append(1))
    monkeypatch.setattr(c, "is_running", lambda: True)   # 就绪轮询立即成功
    assert c.start_service() is True
    assert popened == [1] and c.started_by_me is True
