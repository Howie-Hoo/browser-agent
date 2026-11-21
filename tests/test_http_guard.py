"""HTTP 来源校验与关停实例门 — 纯判定函数 + build_app 真实中间件链集成测试。

覆盖两类本机攻击面（实测可命中后才加的防线，回归防退化）：
- 跨站简单请求（CSRF）：text/plain POST + Origin
- DNS rebinding：伪造 Host
以及 /shutdown 的 instance_id 门。
运行：pytest tests/test_http_guard.py -q
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from browser_agent import server as browser_server  # noqa: E402
from browser_agent.http_guard import guard_rejection  # noqa: E402

# ── 纯判定函数 ───────────────────────────────────────────

def test_pure_function_rejects_foreign_host():
    assert guard_rejection("evil.example:7788", "", "", False)[2] == "bad-host"
    assert guard_rejection("evil.example", "", "", False) is not None


def test_pure_function_allows_loopback_hosts_with_or_without_port():
    for host in ("127.0.0.1:7788", "127.0.0.1", "localhost:7788", "localhost",
                 "[::1]:7788", "[::1]"):
        assert guard_rejection(host, "", "", False) is None


def test_pure_function_rejects_any_origin():
    for origin in ("https://evil.example", "http://localhost:3000", "null"):
        assert guard_rejection("127.0.0.1:7788", origin, "", False)[2] == "browser-origin"


def test_pure_function_requires_json_content_type_for_body():
    assert guard_rejection("127.0.0.1", "", "text/plain;charset=UTF-8", True)[2] == "content-type"
    assert guard_rejection("127.0.0.1", "", "", True)[2] == "content-type"
    assert guard_rejection("127.0.0.1", "", "application/json", True) is None
    assert guard_rejection("127.0.0.1", "", "application/json; charset=utf-8", True) is None
    # 无 body 的 GET 不受 content-type 约束
    assert guard_rejection("127.0.0.1", "", "", False) is None


# ── 真实应用集成（guard → maintenance → handler 全链）────────

@pytest.fixture
def app(monkeypatch, isolated_home):
    async def _noop_shutdown():
        return
    # 集成测试不真关停进程（_do_shutdown 里有 SIGTERM）
    monkeypatch.setattr(browser_server, "_do_shutdown", _noop_shutdown)
    browser_server._state.maintenance = False
    return browser_server.build_app()


def _run(app, coro_fn):
    async def _inner():
        async with TestClient(TestServer(app)) as client:
            return await coro_fn(client)
    return asyncio.run(_inner())


def test_get_status_passes_guard(app):
    async def check(client):
        r = await client.get("/status")
        return r.status, (await r.json()).get("service")
    status, service = _run(app, check)
    assert status == 200 and service == "browser_agent"


def test_browser_origin_get_rejected(app):
    async def check(client):
        r = await client.get("/status", headers={"Origin": "https://evil.example"})
        return r.status, (await r.json()).get("rejected")
    status, reason = _run(app, check)
    assert status == 403 and reason == "browser-origin"


def test_spoofed_host_rejected(app):
    async def check(client):
        r = await client.get("/status", headers={"Host": "evil.example:7788"})
        return r.status, (await r.json()).get("rejected")
    status, reason = _run(app, check)
    assert status == 403 and reason == "bad-host"


def test_csrf_style_simple_post_rejected(app, isolated_home):
    """no-cors 简单请求（text/plain body + Origin）必须在中间件层被拦。"""
    async def check(client):
        r = await client.post(
            "/site", data='{"domain":"csrf-probe.example","patterns":["p"]}',
            headers={"Content-Type": "text/plain;charset=UTF-8",
                     "Origin": "https://evil.example"})
        body = await r.json()
        written = (isolated_home / "site-patterns" / "csrf-probe.example.md").exists()
        return r.status, body.get("rejected"), written
    status, reason, written = _run(app, check)
    assert status == 403 and reason == "browser-origin" and written is False


def test_text_plain_without_origin_still_rejected(app):
    async def check(client):
        r = await client.post("/site", data="{}",
                              headers={"Content-Type": "text/plain"})
        return r.status, (await r.json()).get("rejected")
    status, reason = _run(app, check)
    assert status == 415 and reason == "content-type"


def test_proper_json_post_passes(app, isolated_home):
    async def check(client):
        r = await client.post("/site", json={"domain": "ok.example",
                                             "patterns": ["p1"]})
        return r.status, (await r.json()).get("ok")
    status, ok = _run(app, check)
    assert status == 200 and ok is True
    assert (isolated_home / "site-patterns" / "ok.example.md").exists()


def test_shutdown_gate_rejects_wrong_or_missing_instance_id(app):
    async def check(client):
        r1 = await client.post("/shutdown", json={})
        b1 = await r1.json()
        r2 = await client.post("/shutdown", json={"instance_id": "stale-id"})
        b2 = await r2.json()
        return (r1.status, b1.get("ok"), b1.get("instance_id"),
                r2.status, b2.get("ok"))
    s1, ok1, cur_id, s2, ok2 = _run(app, check)
    assert s1 == 409 and ok1 is False and cur_id == browser_server._INSTANCE_ID
    assert s2 == 409 and ok2 is False
    assert browser_server._state.maintenance is False  # 拒绝不动维护态


def test_shutdown_gate_accepts_current_instance_id(app):
    async def check(client):
        r = await client.post("/shutdown",
                              json={"instance_id": browser_server._INSTANCE_ID})
        return r.status, (await r.json()).get("ok")
    status, ok = _run(app, check)
    assert status == 200 and ok is True
    assert browser_server._state.maintenance is True
    browser_server._state.maintenance = False


# ── 全路由攻防矩阵：每个端点 × {正常, 伪 Origin, 伪 Host, text/plain} ────

_POST_ROUTES = [
    ("/session/start", {}),
    ("/session/reset", {}),
    ("/session/refresh", {}),
    ("/session/stop", {}),
    ("/run", {"commands": [{"action": "get_title"}]}),
    ("/check", {"type": "dom"}),
    ("/auth/ensure", {}),
    ("/site", {"domain": "matrix.example", "patterns": ["p"]}),
    ("/attach/select", {"forget": True}),
    ("/macro/save", {"name": "m", "commands": [{"action": "get_title"}]}),
    ("/macro/run", {"name": "m"}),
    ("/shutdown", {}),
]

_GET_ROUTES = ["/status", "/", "/sites", "/site?domain=example.com",
               "/macro/list", "/auth/profiles", "/attach/discover"]


def _bogus_attach_state(monkeypatch):
    """attach 指向不存在的目录：ensure 快速失败，不启动任何浏览器。"""
    monkeypatch.setitem(browser_server._state.config, "mode", "attach")
    monkeypatch.setitem(browser_server._state.config, "attach_dir",
                        "/nonexistent-ba-guard-matrix")
    # 封闭性：discover 端点打桩，矩阵测试不读真机浏览器目录
    from browser_agent import browser_attach
    monkeypatch.setattr(browser_attach, "discover_payload",
                        lambda: {"ok": True, "browsers": [], "app_data_folders": [],
                                 "next_steps": [], "probe_hint": "", "remedy": ""})


def test_route_matrix_all_post_routes_guarded(app, monkeypatch):
    """三条攻击面（Origin / Host / text/plain）在**每一个** POST 端点上一致拦截；
    正常 JSON 请求全部放行（业务成败 200+ok 字段，绝不因防护误伤）。"""
    _bogus_attach_state(monkeypatch)

    async def sweep(client):
        results = {}
        for path, payload in _POST_ROUTES:
            r_ok = await client.post(path, json=payload)
            r_ct = await client.post(path, json=payload,
                                     headers={"Content-Type": "text/plain"})
            r_or = await client.post(path, json=payload,
                                     headers={"Origin": "https://evil.example"})
            r_host = await client.post(path, json=payload,
                                       headers={"Host": "evil.example"})
            results[path] = (r_ok.status, r_ct.status, r_or.status, r_host.status)
        return results
    res = _run(app, sweep)
    for path, (s_ok, s_ct, s_or, s_host) in res.items():
        expect_ok = 409 if path == "/shutdown" else 200  # 空instance_id → 409（门自身）
        assert s_ok == expect_ok, f"{path} 正常请求被误伤: {s_ok}"
        assert s_ct == 415, f"{path} text/plain 未拦"
        assert s_or == 403, f"{path} Origin 未拦"
        assert s_host == 403, f"{path} 伪 Host 未拦"


def test_route_matrix_all_get_routes_guarded(app, monkeypatch):
    _bogus_attach_state(monkeypatch)

    async def sweep(client):
        out = {}
        for path in _GET_ROUTES:
            r_ok = await client.get(path)
            r_or = await client.get(path, headers={"Origin": "https://evil.example"})
            out[path] = (r_ok.status, r_or.status)
        # /stream 是 SSE：正常请求会挂住，只验证防护面
        r_stream = await client.get("/stream", headers={"Origin": "https://evil.example"})
        out["/stream"] = (None, r_stream.status)
        return out
    res = _run(app, sweep)
    for path, (s_ok, s_or) in res.items():
        if s_ok is not None:
            assert s_ok == 200, f"{path} 正常请求被误伤: {s_ok}"
        assert s_or == 403, f"{path} Origin 未拦"
