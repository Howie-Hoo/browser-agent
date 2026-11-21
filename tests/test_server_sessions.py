"""browser_agent 会话模型单测 — 单 Chromium 多会话隔离 / 批级排队 / 空闲回收。

用 FakeProxy 注入 AppState（不启动真浏览器/Playwright），覆盖 server 层会话管理逻辑。
零额外依赖：async 用例经 asyncio.run 自驱（无需 pytest-asyncio）。
运行：pytest tests/test_server_sessions.py -q
"""
import asyncio
import json
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
        # 与真实 BrowserProxy 对齐的属性（server 直连访问，不再 getattr 兜底）
        self.mode = "managed"
        self._shared_default_context = False
        self._adopted_tabs = set()

    def _tab(self):
        return self._active_tab

    def _set_current_tab(self, key):
        if key is not None:
            self._active_tab = key

    def _begin_batch(self, sid, tab):
        self._active_tab = tab

    def _purge_tab_state(self, key):
        pass

    def is_browser_open(self):
        return not self.stopped

    async def new_isolated_session(self, name):
        if name in self._contexts:
            return {"ok": False, "error": f"名称 '{name}' 已被占用"}
        ctx = FakeContext(name)
        self._contexts[name] = ctx
        # 与真实 proxy 一致：隔离会话初始页用裸会话名做键（后续 tab 才带 :: 前缀）
        self._pages[name] = FakePage(ctx)
        self._active_tab = name
        return {"ok": True, "name": name}

    async def new_shared_session(self, name):
        ctx = self._contexts.get("default")
        self._pages[name] = FakePage(ctx)
        self._active_tab = name
        return {"ok": True, "name": name}

    async def _register_page(self, context, key, label=""):
        page = FakePage(context)
        self._pages[key] = page
        self._active_tab = key
        return page

    def _setup_health_listeners(self, page):
        pass

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
    # 与生产路径一致：DEFAULT 会话落在共享 default context（user 模式）
    s._register_session(DEFAULT_SESSION, context_mode="user")
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
    assert state.proxy._pages["default"].context is not state.proxy._pages["task-a"].context
    # 浏览器已在运行（被 default 会话共享）→ 实例级配置未生效的 warning 如约返回
    assert warning and "未生效" in warning


def test_switch_routes_active_tab_per_session():
    state = _make_state()
    run(state.ensure_session("task-a"))
    run(state.ensure_session("task-b"))
    assert state.switch_to_session("task-a")
    assert state.proxy._active_tab == "task-a"
    assert state.switch_to_session("task-b")
    assert state.proxy._active_tab == "task-b"
    assert state.switch_to_session(DEFAULT_SESSION)
    assert state.proxy._active_tab == "default"


def test_switch_falls_back_within_same_context():
    state = _make_state()
    run(state.ensure_session("task-a"))
    # 模拟会话 active tab 被外部关闭：同 context 再开一个 tab，旧键移除
    state.proxy._pages["task-a::tab2"] = FakePage(state.proxy._contexts["task-a"])
    del state.proxy._pages["task-a"]
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
    assert "task-a" not in state.proxy._pages
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


def test_close_default_session_only_clears_own_pages():
    state = _make_state()
    run(state.ensure_session("task-a"))
    res = run(state.close_session(DEFAULT_SESSION))
    assert res["ok"] and "browser_shutdown" not in res
    # default 是 user 模式会话：只关自建页面，共享 context 保留（他人可能仍在用）
    assert "default" not in state.proxy._pages
    assert "default" in state.proxy._contexts
    # task-a 的 context/page 不受影响
    assert "task-a" in state.proxy._pages
    assert state.switch_to_session("task-a")


# ── 空闲回收 ───────────────────────────────────────────


def test_attach_idle_sweep_keeps_connection(monkeypatch):
    """attach 空闲回收：关超时会话的自建标签页，但 CDP 连接保持不断。

    断连需要用户在浏览器里重新授权（Chrome 新式开关），所以回收 ≠ 断连；
    孤儿标签页照样被清理。
    """
    state = _make_state()
    state.config["mode"] = "attach"
    state.proxy.mode = "attach"
    run(state.ensure_session("task-a"))
    state.proxy._pages["task-a::tab1"] = FakePage(state.proxy._contexts["task-a"])
    old = time.time() - browser_server.IDLE_SHUTDOWN_SEC - 1
    for s in state.sessions.values():
        s.last_active = old
    run(state.idle_recycle())
    # 连接保持：proxy 未 stop、未置空
    assert state.proxy is not None and state.proxy.is_browser_open()
    # 会话与自建标签页已清（孤儿标签页不残留）
    assert not state.sessions
    assert "task-a::tab1" not in state.proxy._pages
    assert "task-a" not in state.proxy._pages


def test_idle_recycle_disabled_by_env_zero(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_IDLE_SHUTDOWN_SEC", "0")
    state = _make_state()
    state.config["mode"] = "attach"
    state.proxy.mode = "attach"
    run(state.ensure_session("task-a"))
    for s in state.sessions.values():
        s.last_active = time.time() - 10 ** 6
    run(state.idle_recycle())
    assert state.sessions and state.proxy.is_browser_open()


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


# ── 会话锁：同会话串行 / 跨会话并行 ─────────────────────


def test_session_lock_times_out_when_held(monkeypatch):
    """锁被占 → 超时返回 None；释放 → 可获取（失败路径不持有锁）。"""
    monkeypatch.setattr(browser_server, "QUEUE_TIMEOUT_SEC", 0.1)

    async def _scenario():
        state = browser_server._state
        lock = state.session_lock("busy-sid")
        await lock.acquire()  # 模拟该会话的命令批正在执行
        try:
            assert await state._acquire_session_lock("busy-sid") is None
        finally:
            lock.release()
        acquired = await state._acquire_session_lock("busy-sid")
        assert acquired is not None
        acquired.release()

    run(_scenario())


def test_session_locks_are_per_session():
    """不同会话各持各的锁：A 持锁不阻塞 B（跨会话并行的前提）。"""

    async def _scenario():
        state = browser_server._state
        a = await state._acquire_session_lock("sid-a")
        try:
            b = await state._acquire_session_lock("sid-b", timeout_sec=0.2)
            assert b is not None, "A 会话持锁不应阻塞 B 会话"
            b.release()
        finally:
            a.release()

    run(_scenario())


def test_session_lock_zero_timeout_means_direct(monkeypatch):
    """QUEUE_TIMEOUT_SEC=0 → 直取模式（不做超时控制），空闲锁立即获取。"""
    monkeypatch.setattr(browser_server, "QUEUE_TIMEOUT_SEC", 0)

    async def _scenario():
        state = browser_server._state
        acquired = await state._acquire_session_lock("direct-sid")
        assert acquired is not None
        acquired.release()

    run(_scenario())


# ── 兼容性：不带 session_id 的旧客户端 ──────────────────


def test_sid_from_body_defaults_to_default():
    assert browser_server._sid_from_body({}) == DEFAULT_SESSION
    assert browser_server._sid_from_body({"session_id": ""}) == DEFAULT_SESSION
    assert browser_server._sid_from_body({"session_id": "task-a"}) == "task-a"


# ── 路由 ContextVar：wait_for 子任务写必须回传（Python ≥3.10 一致）──


def test_router_survives_wait_for_child_task():
    """asyncio.wait_for 在 py<3.12 把协程包进子任务（复制上下文，ContextVar.set
    不回传）——router 是可变字典，子任务复制的是引用，写必须对外层可见。"""
    import asyncio
    from browser_agent.proxy import BrowserProxy

    proxy = BrowserProxy()

    async def _switch_inside_wait_for():
        async def _do():
            proxy._set_current_tab("new-tab")
        await asyncio.wait_for(_do(), timeout=1)

    async def _scenario():
        proxy._current_tab.set({"tab": "old-tab", "sid": "s1"})
        assert proxy._tab() == "old-tab"
        await _switch_inside_wait_for()
        assert proxy._tab() == "new-tab", "wait_for 子任务内的切页未回传外层批任务"
        assert proxy._router_sid() == "s1"

    run(_scenario())


def test_dispatch_namespaces_tab_commands():
    """标签页命名空间化在 dispatch 咽喉点：主循环/loop/降级链/login_commands 全覆盖。
    switch_tab 查不到时错误信息里的键必须是 sid::name——证明改写已生效。"""
    from browser_agent.proxy import BrowserProxy

    proxy = BrowserProxy()
    proxy._begin_batch("s1", "default")
    res = run(proxy._dispatch({"action": "switch_tab", "name": "x"}))
    assert res["ok"] is False and "s1::x" in res["error"], res
    # 无会话上下文（router 无 sid）时不改写——直连 API 行为不变
    proxy2 = BrowserProxy()
    res2 = run(proxy2._dispatch({"action": "switch_tab", "name": "x"}))
    assert res2["ok"] is False and "s1::" not in res2["error"]


def test_batch_isolated_context_reclaimed_on_session_close():
    """批内 new_isolated_session 建的 context（sid::name 键）随会话关闭回收。"""
    state = _make_state()
    run(state.ensure_session("iso-batch"))
    # 模拟批内建立的隔离 context + 页
    state.proxy._contexts["iso-batch::acct"] = FakeContext("iso-batch::acct")
    state.proxy._pages["iso-batch::acct"] = FakePage(state.proxy._contexts["iso-batch::acct"])
    run(state.close_session("iso-batch"))
    assert "iso-batch::acct" not in state.proxy._contexts
    assert "iso-batch::acct" not in state.proxy._pages


# ── 共享上下文守卫（storage 清除类操作）──────────────────


def test_shared_context_refusal_rules():
    state = _make_state()
    run(state.ensure_session("task-a"))  # isolated
    # isolated 会话：独占 context，允许
    assert state._shared_context_refusal("task-a") is None
    # default 会话（user 模式）单占 default context：legacy 行为，允许
    assert state._shared_context_refusal(DEFAULT_SESSION) is None
    # 出现第二个 user 模式会话 → 共享，双方都被拒
    run(state.ensure_session("shared-1", context_mode="user"))
    assert state._shared_context_refusal("shared-1") is not None
    assert state._shared_context_refusal(DEFAULT_SESSION) is not None
    # attach（共享 default context 标记）→ 即便只有一个 user 会话也拒绝
    state2 = _make_state()
    state2.proxy._shared_default_context = True
    assert state2._shared_context_refusal(DEFAULT_SESSION) is not None


# ── 标签页键归属（多会话不撞键）─────────────────────────


def test_tab_key_convention_single_owner():
    """键约定唯一属主：tab_key/tab_owner 互逆，server 与 proxy 共用这一对。"""
    from browser_agent.proxy import BrowserProxy

    assert BrowserProxy.tab_key("task-a", "x") == "task-a::x"
    assert BrowserProxy.tab_owner("task-a::x") == "task-a"
    assert BrowserProxy.tab_owner("default") is None


def test_session_pages_ownership():
    state = _make_state()
    run(state.ensure_session("task-a"))
    pages = state.proxy._pages
    pages["task-a::tab1"] = FakePage(state.proxy._contexts["task-a"])
    pages["other::tab1"] = FakePage(state.proxy._contexts["default"])
    own = state._session_pages("task-a")
    assert "task-a" in own and "task-a::tab1" in own
    assert "other::tab1" not in own and "default" not in own


# ── user 模式会话：只关自建标签页，不 close 共享 context ──


def test_close_user_session_closes_own_pages_only():
    state = _make_state()
    state.proxy._shared_default_context = True  # 模拟 attach
    run(state.ensure_session("shared-1", context_mode="user"))
    pages = state.proxy._pages
    assert "shared-1" in pages  # 初始页（裸键）
    pages["shared-1::tab1"] = FakePage(state.proxy._contexts["default"])
    res = run(state.close_session("shared-1"))
    assert res["ok"]
    # 自建页已关
    assert "shared-1" not in state.proxy._pages
    assert "shared-1::tab1" not in state.proxy._pages
    # default context（用户浏览器 profile）绝不被 close
    assert "default" in state.proxy._contexts
    assert not state.proxy._contexts["default"].closed
    # default 会话与页面完好
    assert "default" in state.proxy._pages
    assert state.switch_to_session(DEFAULT_SESSION)


# ── 关停维护态（排水拒绝新批）──────────────────────────────

class _FakeReq:
    path = "/run"
    method = "POST"

    def __init__(self, body=None):
        self._body = body
        self.can_read_body = body is not None

    async def json(self):
        return self._body or {}


def test_shutdown_requires_matching_instance_id(monkeypatch):
    """实例身份门：不带/带错 instance_id → 409 拒绝且不进维护态。"""
    s = AppState()
    monkeypatch.setattr(browser_server, "_state", s)
    resp = asyncio.run(browser_server.handle_shutdown(_FakeReq({})))
    body = json.loads(resp.text)
    assert body["ok"] is False and resp.status == 409
    assert body["instance_id"] == browser_server._INSTANCE_ID
    assert s.maintenance is False


def test_shutdown_sets_maintenance_flag(monkeypatch):
    s = AppState()
    monkeypatch.setattr(browser_server, "_state", s)
    resp = asyncio.run(browser_server.handle_shutdown(
        _FakeReq({"instance_id": browser_server._INSTANCE_ID})))
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert s.maintenance is True


def test_run_refused_while_maintaining(monkeypatch):
    """维护拒绝由中间件统一实施——经中间件调用 handler 验证。"""
    s = AppState()
    s.maintenance = True
    monkeypatch.setattr(browser_server, "_state", s)
    resp = asyncio.run(browser_server._maintenance_middleware(
        _FakeReq(), browser_server.handle_run))
    body = json.loads(resp.text)
    assert body["ok"] is False and body["maintenance"] is True
    assert "关停" in body["error"]


def test_maintenance_open_routes_still_served(monkeypatch):
    """白名单路由（/status /sites 等）在维护期照常应答。"""
    s = AppState()
    s.maintenance = True
    monkeypatch.setattr(browser_server, "_state", s)
    for path in ("/status", "/sites", "/macro/list", "/attach/discover"):
        req = _FakeReq()
        req.path = path
        served = {}

        async def _handler(request):
            served["hit"] = True
            return browser_server._json({"ok": True})

        asyncio.run(browser_server._maintenance_middleware(req, _handler))
        assert served.get("hit") is True, path


# ── 页级空闲回收 ───────────────────────────────────────────

class _LiveBrowser:
    def is_connected(self):
        return True


def _proxy_with_pages(pages: dict, adopted: set, times: dict):
    """真实 BrowserProxy + 假页面（close_idle_tabs / _unregister_page 是真实现）。"""
    from browser_agent.proxy import BrowserProxy
    p = BrowserProxy()
    p._browser = _LiveBrowser()
    ctx = FakeContext("c")
    for key in pages:
        p._pages[key] = pages[key] or FakePage(ctx)
    p._adopted_tabs = set(adopted)
    p._tab_last_used = dict(times)
    return p


def test_close_idle_tabs_closes_stale_self_created_only():
    now = time.monotonic()
    p = _proxy_with_pages(
        pages={"a": None, "b": None, "c": None},
        adopted={"c"},
        times={"a": now, "b": now - 999})
    p._page_registered_at = {"c": now - 999}
    closed = asyncio.run(p.close_idle_tabs(100, protected={"a"}))
    assert closed == ["b"]
    assert "b" not in p._pages and "a" in p._pages and "c" in p._pages
    assert "b" not in p._tab_last_used  # 运行态随页清理


def test_recycle_idle_tabs_uses_session_active_tabs_as_protected(monkeypatch):
    from browser_agent.server import Session
    monkeypatch.setattr(browser_server, "TAB_IDLE_SEC", 60)
    now = time.monotonic()
    p = _proxy_with_pages(
        pages={"s1::extra": None, "s1::t1": None, "user-tab": None},
        adopted={"user-tab"},
        times={"s1::extra": now - 999, "s1::t1": now, "user-tab": now})
    s = AppState()
    s.proxy = p
    s.sessions = {"s1": Session("s1", tab="s1::t1")}
    closed = asyncio.run(s.recycle_idle_tabs())
    assert closed == ["s1::extra"]


def test_recycle_idle_tabs_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(browser_server, "TAB_IDLE_SEC", 0)
    now = time.monotonic()
    p = _proxy_with_pages(pages={"b": None}, adopted=set(), times={"b": now - 9999})
    s = AppState()
    s.proxy = p
    assert asyncio.run(s.recycle_idle_tabs()) == []


def test_recycle_idle_tabs_skips_inflight_session_pages(monkeypatch):
    """锁被持有（批在途）的会话名下页面整组跳过——批中途 switch_tab 的
    目标页要等批结束才回写 active_tab，期间不能被回收（并发竞态守卫）。"""
    from browser_agent.server import Session
    monkeypatch.setattr(browser_server, "TAB_IDLE_SEC", 60)
    now = time.monotonic()
    p = _proxy_with_pages(pages={"s2::stale": None, "s2": None},
                          adopted=set(),
                          times={"s2::stale": now - 999, "s2": now - 999})
    s = AppState()
    s.proxy = p
    s.sessions = {"s2": Session("s2", tab="s2::other")}
    lock = s.session_lock("s2")
    asyncio.run(lock.acquire())  # 模拟在途批持锁
    try:
        # 由别的会话触发：在途会话的页（含裸 sid 初始页）全部受保护
        assert asyncio.run(s.recycle_idle_tabs(exclude_sid="s1")) == []
        # 在途会话自身触发（刚完成 active_tab 回写）：不受自持锁误跳过
        assert asyncio.run(s.recycle_idle_tabs(exclude_sid="s2")) == ["s2::stale", "s2"]
    finally:
        lock.release()


def test_maintenance_refuses_refresh_stop_and_auth_profiles(monkeypatch):
    """维护中间件覆盖会话变更类入口：refresh（防排水中清库+auto_save 落盘空态）、
    stop（防与关停并发 stop）、auth/profiles（防排水期拉起浏览器）。"""
    s = AppState()
    s.maintenance = True
    monkeypatch.setattr(browser_server, "_state", s)
    for handler in (browser_server.handle_session_refresh,
                    browser_server.handle_session_stop,
                    browser_server.handle_auth_profiles):
        resp = asyncio.run(browser_server._maintenance_middleware(
            _FakeReq(), handler))
        body = json.loads(resp.text)
        assert body["ok"] is False and body["maintenance"] is True, handler
