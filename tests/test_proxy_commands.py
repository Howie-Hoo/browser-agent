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


# ── go_back / upload_file 多文件 / screenshot 参数 / list_frames / 隔离求值 ──

class FakeNavPage(FakePage):
    def __init__(self, has_history=True):
        super().__init__()
        self.has_history = has_history
        self.back_calls = []
        self.url = "https://a.example/prev" if has_history else "https://a.example/cur"

    async def go_back(self, wait_until=None):
        self.back_calls.append(wait_until)
        return True if self.has_history else None


class FakeUploadPage(FakePage):
    def __init__(self):
        super().__init__()
        self.uploads = []

    async def set_input_files(self, selector, files):
        self.uploads.append((selector, files))


class FakeShotPage(FakePage):
    def __init__(self):
        super().__init__()
        self.shots = []

    async def screenshot(self, **kwargs):
        self.shots.append(kwargs)
        return b"img-bytes"


def test_go_back_with_and_without_history():
    p = BrowserProxy()
    page = FakeNavPage(has_history=True)
    p._pages = {"default": page}
    res = asyncio.run(p.go_back())
    assert res["ok"] and res["went_back"] and page.back_calls == ["load"]
    p2 = BrowserProxy()
    page2 = FakeNavPage(has_history=False)
    p2._pages = {"default": page2}
    res2 = asyncio.run(p2.go_back())
    assert res2["ok"] and res2["went_back"] is False and "没有可后退" in res2["note"]


def test_dispatch_wires_go_back():
    p = BrowserProxy()
    page = FakeNavPage()
    p._pages = {"default": page}
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "go_back"}))
    assert res["ok"] and page.back_calls == ["load"]


def test_upload_file_accepts_single_and_list():
    p = BrowserProxy()
    page = FakeUploadPage()
    p._pages = {"default": page}
    assert asyncio.run(p.upload_file("input[type=file]", "/a.png"))["files"] == 1
    res = asyncio.run(p.upload_file("input[type=file]", ["/a.png", "/b.png"]))
    assert res["ok"] and res["files"] == 2
    assert page.uploads[1] == ("input[type=file]", ["/a.png", "/b.png"])


def test_upload_file_rejects_bad_input():
    p = BrowserProxy()
    page = FakeUploadPage()
    p._pages = {"default": page}
    for bad in ([], True, ["/a.png", 5]):
        res = asyncio.run(p.upload_file("input", bad))
        assert res["ok"] is False and "files" in res["error"]
    assert page.uploads == []


def test_screenshot_passes_type_quality_scale():
    p = BrowserProxy()
    page = FakeShotPage()
    p._pages = {"default": page}
    asyncio.run(p.screenshot(image_type="jpeg", quality=60, scale="css"))
    kw = page.shots[-1]
    assert kw["type"] == "jpeg" and kw["quality"] == 60 and kw["scale"] == "css"
    assert kw["full_page"] is True  # 无 clip 时默认全页
    asyncio.run(p.screenshot(clip={"x": 0, "y": 0, "width": 10, "height": 10}))
    kw2 = page.shots[-1]
    assert "full_page" not in kw2 and kw2["clip"]["width"] == 10
    assert kw2["type"] == "png" and "quality" not in kw2


def test_screenshot_rejects_bad_image_type():
    p = BrowserProxy()
    p._pages = {"default": FakeShotPage()}
    res = asyncio.run(p.screenshot(image_type="webp"))
    assert res["ok"] is False


class _FakeFrameEl:
    def __init__(self, attrs):
        self._attrs = attrs

    async def get_attribute(self, name):
        return self._attrs.get(name)


class FakeFrame:
    def __init__(self, name, url, parent, attrs=None):
        self.name, self.url, self.parent_frame = name, url, parent
        self._attrs = attrs or {}

    async def frame_element(self):
        return _FakeFrameEl(self._attrs)


class FakeFramesPage(FakePage):
    def __init__(self, frames):
        super().__init__()
        self._main = FakeFrame("main", "https://top.example", None)
        self.frames = [self._main] + frames

    @property
    def main_frame(self):
        return self._main


def test_list_frames_lists_direct_children_only():
    inner = FakeFrame("inner", "https://nested.example", "not-main-marker")
    child = FakeFrame("pay", "https://pay.example", None,
                      {"id": "pay", "src": "https://pay.example/x"})
    child.parent_frame = "MAIN"  # 占位，构造后替换
    page = FakeFramesPage([])
    child.parent_frame = page.main_frame
    inner.parent_frame = child           # 嵌套：不是主文档直系，不应出现
    page.frames.append(child)
    page.frames.append(inner)
    p = BrowserProxy()
    p._pages = {"default": page}
    res = asyncio.run(p.list_frames())
    assert res["ok"] and res["count"] == 1
    f = res["frames"][0]
    assert f["index"] == 0 and f["name"] == "pay" and f["url"].startswith("https://pay")
    assert f["id"] == "pay" and f["src"] == "https://pay.example/x"
    assert res["in_frame"] is False


def test_list_frames_marks_active_frame_context():
    page = FakeFramesPage([])
    p = BrowserProxy()
    p._pages = {"default": page}
    frame = FakeFrame("f", "https://inframe.example", page.main_frame)
    p._frames["default"] = frame
    res = asyncio.run(p.list_frames())
    assert res["in_frame"] is True and res["current_frame_url"] == "https://inframe.example"


def test_dispatch_wires_list_frames():
    p = BrowserProxy()
    p._pages = {"default": FakeFramesPage([])}
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "list_frames"}))
    assert res["ok"] and "主文档没有直接 iframe" in res["ai_hint"]


class FakeCdpSession:
    def __init__(self, script_result=None, exception=None):
        self.sent = []
        self.detached = False
        self._script_result = script_result or {"result": {"type": "string", "value": "clean"}}
        self._exception = exception

    async def send(self, method, params=None):
        self.sent.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 77}
        if method == "Runtime.evaluate":
            assert params.get("contextId") == 77 and params.get("awaitPromise") is True
            if self._exception:
                return {"exceptionDetails": {"text": "boom",
                                             "exception": {"description": "boom\n at x"}}}
            return self._script_result
        raise AssertionError(f"unexpected {method}")

    async def detach(self):
        self.detached = True


class FakeCdpContext:
    def __init__(self, session):
        self._session = session

    async def new_cdp_session(self, page):
        return self._session


class FakeEvalPage(FakePage):
    def __init__(self, session):
        super().__init__()
        self.context = FakeCdpContext(session)


def test_eval_js_isolated_roundtrip_and_detach():
    session = FakeCdpSession()
    p = BrowserProxy()
    p._pages = {"default": FakeEvalPage(session)}
    res = asyncio.run(p.eval_js("document.title", isolated=True))
    assert res["value"] == "clean" and res["value_type"] == "string"
    assert session.detached is True
    assert session.sent == ["Page.getFrameTree", "Page.createIsolatedWorld",
                            "Runtime.evaluate"]


def test_eval_js_isolated_reports_js_exception():
    session = FakeCdpSession(exception=True)
    p = BrowserProxy()
    p._pages = {"default": FakeEvalPage(session)}
    res = asyncio.run(p.eval_js("throw 1", isolated=True))
    assert res["ok"] is False and res["error"] == "boom"
    assert session.detached is True


def test_dispatch_wires_eval_isolated_flag():
    session = FakeCdpSession()
    p = BrowserProxy()
    p._pages = {"default": FakeEvalPage(session)}
    p._begin_batch("s", "default")
    res = asyncio.run(p._dispatch({"action": "eval_js", "script": "1", "isolated": True}))
    assert res["value"] == "clean" and session.detached


def test_go_back_operates_main_page_and_exits_frame_context():
    """switch_frame 后 go_back：作用于主文档（Frame 无 go_back）并清除 frame 上下文。"""
    p = BrowserProxy()
    page = FakeNavPage()
    p._pages = {"default": page}
    p._begin_batch("s", "default")

    class _Frame:
        url = "https://frame.example"

    p._frames["default"] = _Frame()
    res = asyncio.run(p._dispatch({"action": "go_back"}))
    assert res["ok"] and res["went_back"]
    assert "default" not in p._frames, "导航后 iframe 上下文必须失效清除"


def test_eval_isolated_rejects_selector():
    p = BrowserProxy()
    p._pages = {"default": FakeEvalPage(FakeCdpSession())}
    res = asyncio.run(p.eval_js("this.value", selector="#x", isolated=True))
    assert res["ok"] is False and "selector" in res["error"]


def test_screenshot_infers_type_from_path(tmp_path):
    """image_type 缺省时按后缀推断：.jpeg → jpeg，无后缀 → png。"""
    p = BrowserProxy()
    page = FakeShotPage()
    p._pages = {"default": page}
    asyncio.run(p.screenshot(path=str(tmp_path / "shot.jpeg"), full_page=False))
    assert page.shots[-1]["type"] == "jpeg"
    asyncio.run(p.screenshot(path=str(tmp_path / "plain"), full_page=False))
    assert page.shots[-1]["type"] == "png"
