"""proxy 命令面单测（吸收能力）— click_xy / scroll direction / 调试端口防护 /
dispatch 接线。用假页面替身直调真实 BrowserProxy 方法，不起服务不开浏览器。
运行：pytest tests/test_proxy_commands.py -q
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import browser_attach as ba  # noqa: E402
from browser_agent.proxy import BrowserProxy  # noqa: E402
from fakes import make_fake_browser  # noqa: E402


class FakeMouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y, steps=None):
        self.calls.append(("move", x, y))

    async def down(self, button="left"):
        self.calls.append(("down", button))

    async def up(self, button="left"):
        self.calls.append(("up", button))


class FakeRoute:
    """route.abort() 替身：记录被拦截。"""

    def __init__(self):
        self.aborted = 0

    async def abort(self):
        self.aborted += 1


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.routes = {}
        self.eval_scripts = []

    async def route(self, pattern, handler):
        self.routes[pattern] = handler

    async def evaluate(self, script, *args, **kwargs):
        self.eval_scripts.append(script)
        return 0

    async def wait_for_timeout(self, ms):
        pass


def _proxy_with_page() -> tuple:
    p = BrowserProxy()
    page = FakePage()
    p._pages = {"default": page}
    return p, page


# ── click_xy ───────────────────────────────────────────────

def test_click_xy_sends_move_down_up_sequence():
    p, page = _proxy_with_page()
    res = asyncio.run(p.click_xy(120, 80))
    assert res["ok"] and res["clicked"] and res["click_count"] == 1
    assert page.mouse.calls == [("move", 120, 80), ("down", "left"), ("up", "left")]


def test_click_xy_multi_click_and_button():
    p, page = _proxy_with_page()
    res = asyncio.run(p.click_xy(10, 20, button="right", click_count=2))
    assert res["click_count"] == 2 and res["button"] == "right"
    kinds = [c[0] for c in page.mouse.calls]
    assert kinds == ["move", "down", "up", "down", "up"]


def test_click_xy_rejects_bad_coordinates():
    p, page = _proxy_with_page()
    for bad in (-1, 5), ("a", 5), (float("nan"), 5), (5, None):
        res = asyncio.run(p.click_xy(*bad))
        assert res["ok"] is False and "坐标" in res["error"]
    assert page.mouse.calls == []  # 校验失败不发事件


def test_dispatch_wires_click_xy():
    p, page = _proxy_with_page()
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "click_xy", "x": 33, "y": 44}))
    assert res["ok"]
    assert page.mouse.calls[0] == ("move", 33, 44)


# ── scroll direction ───────────────────────────────────────

def test_scroll_direction_bottom_and_top():
    p, page = _proxy_with_page()
    asyncio.run(p.scroll(direction="bottom"))
    bottom = next(s for s in page.eval_scripts if "scrollHeight" in s)
    assert "documentElement" in bottom, "standards 页面 body 可能不是滚动容器，须取两者较大值"
    page.eval_scripts.clear()
    asyncio.run(p.scroll(direction="top"))
    assert any("scrollTo(0, 0)" in s for s in page.eval_scripts)


def test_scroll_direction_invalid_rejected_without_eval():
    p, page = _proxy_with_page()
    res = asyncio.run(p.scroll(direction="middle"))
    assert res["ok"] is False and "direction" in res["error"]
    assert page.eval_scripts == []


def test_scroll_default_still_uses_scroll_by():
    p, page = _proxy_with_page()
    asyncio.run(p.scroll(y=300))
    assert any("scrollBy(0, 300)" in s for s in page.eval_scripts)


def test_dispatch_wires_scroll_direction():
    p, page = _proxy_with_page()
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "scroll", "direction": "bottom"}))
    assert res["ok"] and res["direction"] == "bottom"
    assert any("scrollHeight" in s for s in page.eval_scripts)


# ── 调试端口防护 ──────────────────────────────────────────

def test_guard_debug_port_registers_both_hosts_and_aborts():
    p, page = _proxy_with_page()
    p._attached_port = 39999
    asyncio.run(p._guard_debug_port(page))
    assert set(page.routes) == {"http://127.0.0.1:39999/**",
                                "http://localhost:39999/**"}
    route = FakeRoute()
    for handler in page.routes.values():
        asyncio.run(handler(route))
    assert route.aborted == 2  # 两条模式都真实拦截


def test_guard_debug_port_noop_without_port():
    p, page = _proxy_with_page()
    p._attached_port = None
    asyncio.run(p._guard_debug_port(page))
    assert page.routes == {}


def test_guard_mount_failure_does_not_raise():
    class BadPage(FakePage):
        async def route(self, pattern, handler):
            raise RuntimeError("route unavailable")

    p = BrowserProxy()
    bad = BadPage()
    p._attached_port = 12345
    asyncio.run(p._guard_debug_port(bad))  # 不抛 = 防护尽力而为


# ── search_local 经 dispatch 全链接线 ─────────────────────

@pytest.fixture
def fake_browser(tmp_path, monkeypatch):
    return make_fake_browser(
        tmp_path, monkeypatch, name="Fake",
        bookmarks=[{"type": "url", "name": "内部门户",
                    "url": "https://portal.internal/x"}],
        history={"Default": [("https://h.com/1", "门户日志", 3, time.time())]})


def test_dispatch_search_local_full_chain(fake_browser):
    p = BrowserProxy()
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "search_local",
                                   "keywords": "门户", "limit": 5}))
    assert res["ok"], res
    assert res["counts"]["bookmarks"] == 1 and res["counts"]["history"] == 1
    assert res["searched"] == [{"browser": "Fake", "profiles": ["Default"]}]


def test_dispatch_search_local_bad_params_reported(fake_browser):
    p = BrowserProxy()
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "search_local", "only": "web"}))
    assert res["ok"] is False and "only" in res["error"]


def test_dispatch_search_local_keyword_alias_param(fake_browser):
    p = BrowserProxy()
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "search_local", "keyword": "门户日志"}))
    assert res["ok"] and res["counts"]["history"] == 1


def test_click_xy_rejects_bad_delay_and_count():
    p, page = _proxy_with_page()
    for bad in [{"delay": -1}, {"delay": "x"}, {"click_count": True}]:
        res = asyncio.run(p.click_xy(5, 5, **bad))
        assert res["ok"] is False and "非负整数" in res["error"]
    assert page.mouse.calls == []
