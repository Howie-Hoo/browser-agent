"""browser_agent 会话模型单测 — 单 Chromium 多会话隔离 / 批级排队 / 空闲回收。

用 FakeProxy 注入 AppState（不启动真浏览器/Playwright），覆盖 server 层会话管理逻辑。
零额外依赖：async 用例经 asyncio.run 自驱（无需 pytest-asyncio）。
运行：pytest tests/test_server_sessions.py -q
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import server as browser_server  # noqa: E402
from browser_agent.server import AppState, DEFAULT_SESSION  # noqa: E402


class FakePage:
    def __init__(self, ctx):
        self.context = ctx

    async def close(self):
        pass


class FakeContext:
    def __init__(self, name):
        self.name = name
        self.closed = False

    async def new_page(self):
        return FakePage(self)

    async def close(self):
        self.closed = True


class FakeProxy:
    """BrowserProxy 最小替身：只实现会话管理用到的接口。"""

    def __init__(self):
        self._contexts = {}
        self._pages = {}
        self._active_tab = None
        self.stopped = False

    def is_browser_open(self):
        return not self.stopped

    async def new_isolated_session(self, name):
        if name in self._contexts:
            return {"ok": False, "error": f"名称 '{name}' 已被占用"}
        ctx = FakeContext(name)
        self._contexts[name] = ctx
        self._pages[f"{name}::default"] = FakePage(ctx)
        self._active_tab = f"{name}::default"
        return {"ok": True, "name": name}

    async def stop(self):
        self.stopped = True


def _make_state(monkeypatch=None):
    """已启动浏览器（FakeProxy + default 会话）的 AppState。"""
    s = AppState()
    proxy = FakeProxy()
    default_ctx = FakeContext("default")
    proxy._contexts["default"] = default_ctx
    proxy._pages["default"] = FakePage(default_ctx)
    proxy._active_tab = "default"
    s.proxy = proxy

    async def _noop_ensure(cfg=None):
        return

    if monkeypatch is not None:
        monkeypatch.setattr(s, "ensure_proxy", _noop_ensure)
    else:
        s.ensure_proxy = _noop_ensure
    s._register_session(DEFAULT_SESSION)
    return s


def run(coro):
    return asyncio.run(coro)


# ── 会话注册与切换 ───────────────────────────────────────


def test_new_session_gets_isolated_context():
    state = _make_state()
    sess, created, warning = run(state.ensure_session("task-a", owner="pytest-a"))
    assert created and sess.sid == "task-a"
    assert "task-a" in state.proxy._contexts
    # default 会话 page 与 task-a 的 page 分属不同 context
    assert state.proxy._pages["default"].context is not state.proxy._pages["task-a::default"].context
    # 浏览器已在运行（被 default 会话共享）→ 实例级配置未生效的 warning 如约返回
    assert warning and "未生效" in warning


def test_switch_routes_active_tab_per_session():
    state = _make_state()
    run(state.ensure_session("task-a"))
    run(state.ensure_session("task-b"))
    assert state.switch_to_session("task-a")
    assert state.proxy._active_tab == "task-a::default"
    assert state.switch_to_session("task-b")
    assert state.proxy._active_tab == "task-b::default"
    assert state.switch_to_session(DEFAULT_SESSION)
    assert state.proxy._active_tab == "default"


def test_switch_falls_back_within_same_context():
    state = _make_state()
    run(state.ensure_session("task-a"))
    # 模拟会话 active tab 被外部关闭：同 context 再开一个 tab，旧键移除
    state.proxy._pages["task-a::tab2"] = FakePage(state.proxy._contexts["task-a"])
    del state.proxy._pages["task-a::default"]
    assert state.switch_to_session("task-a")
    assert state.proxy._active_tab == "task-a::tab2"


def test_switch_unknown_session_fails():
    state = _make_state()
    assert not state.switch_to_session("no-such-session")


# ── 会话关闭：只关自己，不动他人 ──────────────────────────


def test_close_session_keeps_browser_for_others():
    state = _make_state()
    run(state.ensure_session("task-a"))
    run(state.ensure_session("task-b"))
    res = run(state.close_session("task-a"))
    assert res["ok"] and "browser_shutdown" not in res
    assert state.proxy is not None and state.proxy.is_browser_open()
    assert "task-a" not in state.proxy._contexts
    assert "task-a::default" not in state.proxy._pages
    # task-b 完好且仍可切换
    assert state.switch_to_session("task-b")


def test_close_last_session_shuts_down_browser():
    state = _make_state()
    run(state.ensure_session("task-a"))
    # 关 task-a 时 default 会话仍在 → 浏览器保持运行
    res = run(state.close_session("task-a"))
    assert res["ok"] and "browser_shutdown" not in res
    # 关掉最后的 default 会话 → 浏览器随之关闭
    res2 = run(state.close_session(DEFAULT_SESSION))
    assert res2.get("browser_shutdown") is True
    assert state.proxy is None


def test_close_default_session_only_clears_default_context():
    state = _make_state()
    run(state.ensure_session("task-a"))
    res = run(state.close_session(DEFAULT_SESSION))
    assert res["ok"] and "browser_shutdown" not in res
    assert "default" not in state.proxy._contexts
    assert "default" not in state.proxy._pages
    # task-a 的 context/page 不受影响
    assert "task-a::default" in state.proxy._pages
    assert state.switch_to_session("task-a")


# ── 空闲回收 ───────────────────────────────────────────


def test_idle_recycle_closes_browser_when_all_stale():
    state = _make_state()
    run(state.ensure_session("task-a"))
    old = time.time() - browser_server.IDLE_SHUTDOWN_SEC - 1
    for s in state.sessions.values():
        s.last_active = old
    run(state.idle_recycle())
    assert state.proxy is None and not state.sessions


def test_idle_recycle_skipped_when_any_active():
    state = _make_state()
    run(state.ensure_session("task-a"))
    sess_b, _, _ = run(state.ensure_session("task-b"))
    old = time.time() - browser_server.IDLE_SHUTDOWN_SEC - 1
    state.sessions["task-a"].last_active = old
    sess_b.touch()  # task-b 刚活动过
    run(state.idle_recycle())
    assert state.proxy is not None


# ── 排队超时 ───────────────────────────────────────────


def test_acquire_run_lock_times_out(monkeypatch):
    """锁被占 → 超时返回 False；释放 → 可获取。

    注意 _acquire_run_lock 操作的是模块级 _state 的锁（全局），必须占/放同一把，
    且失败路径也释放——否则污染全局状态会连锁卡死后续用例。
    """
    monkeypatch.setattr(browser_server, "QUEUE_TIMEOUT_SEC", 0.1)

    async def _scenario():
        lock = browser_server._state._run_lock
        await lock.acquire()  # 模拟其他会话的命令批正在执行
        try:
            assert await browser_server._acquire_run_lock() is False
        finally:
            lock.release()
        assert await browser_server._acquire_run_lock() is True
        lock.release()

    run(_scenario())


def test_acquire_run_lock_zero_means_no_timeout(monkeypatch):
    """QUEUE_TIMEOUT_SEC=0 → 直取模式（不做超时控制），空闲锁立即获取。"""
    monkeypatch.setattr(browser_server, "QUEUE_TIMEOUT_SEC", 0)
    # browser_server._acquire_run_lock 引用的是全局 _state 的锁；借它验证直取路径
    async def _scenario():
        assert await browser_server._acquire_run_lock() is True
        browser_server._state._run_lock.release()

    run(_scenario())


# ── 兼容性：不带 session_id 的旧客户端 ──────────────────


def test_sid_from_body_defaults_to_default():
    assert browser_server._sid_from_body({}) == DEFAULT_SESSION
    assert browser_server._sid_from_body({"session_id": ""}) == DEFAULT_SESSION
    assert browser_server._sid_from_body({"session_id": "task-a"}) == "task-a"
