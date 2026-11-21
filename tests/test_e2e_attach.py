"""E2E — attach 模式真机验证（CDP 附着真实 Chrome）。

运行：
    RUN_E2E=1 pytest tests/test_e2e_attach.py -v

方案：用真实 Chrome 二进制 + 临时 user-data-dir + --remote-debugging-port=0
（Chrome 自动分配端口并把 DevToolsActivePort 写进该目录——正是 attach 解析
所读的机制），完整走「发现 → 偏好 → 连接 → user/isolated 会话 → 守卫 →
断开不杀浏览器 → 重连」。不触碰用户日常 Chrome 的真实 profile。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(not os.getenv("RUN_E2E"), reason="需要 RUN_E2E=1（真实浏览器）")

from e2e_helpers import (Service, fresh_home, cleanup_home, start_page_server,  # noqa: E402
                         ok_step, wait_active_batches, find_chrome, wait_devtools_port,
                         spawn_chrome, stop_chrome)

@pytest.fixture(scope="module")
def pages():
    srv = start_page_server(18711)
    yield "http://127.0.0.1:18711"
    srv.shutdown()


@pytest.fixture(scope="module")
def chrome():
    """临时 profile 的真实 Chrome（自管端口，DevToolsActivePort 落在 profile 目录）。"""
    if not find_chrome():
        pytest.skip("本机无可用 Chromium 系浏览器")
    c = spawn_chrome("ba-attach-profile-")
    yield c
    stop_chrome(c)


@pytest.fixture(scope="module")
def svc(chrome, pages):
    home = fresh_home("ba-attach-home-")
    s = Service(18712, home,
                extra_args=["--attach", f"--attach-dir={chrome['dir']}", f"--url={pages}"])
    s.start()
    yield s
    s.shutdown()
    cleanup_home(home)


# ── 连接与状态 ────────────────────────────────────────────

def test_attach_mode_status(svc, chrome, pages):
    # attach 惰性连接：首个请求才真正连浏览器
    assert svc.run([{"action": "get_title"}], sid="att-boot")["ok"]
    st = svc.get("/status")
    assert st["browser_mode"] == "attach"
    assert st["browser_active"] is True
    att = st.get("attach") or {}
    assert att.get("browser_dir") == chrome["dir"]
    assert str(chrome["port"]) in (att.get("endpoint") or "")


# ── user 会话：真实 profile 上下文 + 登录态共享语义 ────────

def test_user_session_shares_context_like_login_state(svc, pages):
    # 会话 A 在 user context 写 cookie（context=user → 共享 default context）
    r = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "eval_js",
         "script": "document.cookie='attach_e2e=yes;path=/'"},
    ], sid="att-a", context="user")
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    # 会话 B（另一 user 会话）在同一 default context → 能读到（= 登录态免费的本质）
    r2 = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "eval_js", "script": "document.cookie"},
    ], sid="att-b", context="user")
    assert r2["ok"]
    assert "attach_e2e=yes" in ok_step(r2, 1)["value"]
    # isolated 会话（incognito）读不到 → 隔离双向成立
    svc.post("/session/start", {"session_id": "att-iso", "context": "isolated"})
    r3 = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "eval_js", "script": "document.cookie"},
    ], sid="att-iso")
    assert r3["ok"]
    assert "attach_e2e" not in ok_step(r3, 1)["value"]


def test_user_session_full_flow(svc, pages):
    r = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "get_page_summary"},
        {"action": "find_and_act", "description": "计数", "act": "click"},
        {"action": "get_text", "selector": "#count"},
        {"action": "new_tab", "name": "t1"},
        {"action": "navigate", "url": pages + "/second"},
        {"action": "switch_tab", "name": "t1"},
        {"action": "close_tab", "name": "t1"},
    ], sid="att-flow")
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 3)["text"] == "1"
    svc.post("/session/stop", {"session_id": "att-flow"})


# ── 守卫：用户上下文拒绝清除 ───────────────────────────────

def test_guards_in_user_context(svc, pages):
    svc.post("/session/start", {"session_id": "att-g", "context": "user"})
    svc.run([{"action": "navigate", "url": pages}], sid="att-g")
    ref = svc.post("/session/refresh", {"session_id": "att-g"})
    assert ref["ok"] is False and ("用户" in ref.get("error", "") or "attach" in ref.get("error", ""))
    r = svc.run([{"action": "clear_storage"}], sid="att-g")
    assert r["ok"] is False
    svc.post("/session/stop", {"session_id": "att-g"})


# ── 并行：共享 default context 的两会话并发 ──────────────

def test_concurrent_user_sessions_share_context(svc, pages):
    """attach 下两个 user 会话（同一真实 profile context）并发执行——
    并行以 active_batches ≥ 2 为确定性判据；登录态共享（cookie 互通）。"""
    import threading, time as _t
    results = {}

    def _batch(sid, marker):
        results[sid] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "eval_js", "script": f"document.cookie='c_{marker}=1;path=/'"},
            {"action": "wait_ms", "ms": 1200},
            {"action": "eval_js", "script": "document.cookie"},
        ], sid=sid, context="user")

    t0 = _t.monotonic()
    observed = {}
    obs = threading.Thread(target=lambda: observed.update(n=wait_active_batches(lambda: svc.get("/status"), n=2)),
                           daemon=True)
    obs.start()
    t1 = threading.Thread(target=_batch, args=("cu-a", "a"))
    t2 = threading.Thread(target=_batch, args=("cu-b", "b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    obs.join(timeout=8)
    assert observed.get("n", 0) >= 2, "未观测到两会话批同时执行（退化为串行）"
    for sid in ("cu-a", "cu-b"):
        r = results[sid]
        assert r["ok"], (sid, [s.get("error") for s in r.get("steps", []) if not s.get("ok")])
        # 共享 context：能看到对方写的 cookie
        vals = " ".join(str(s["data"].get("value")) for s in r["steps"] if s["action"] == "eval_js")
        assert "c_a=1" in vals and "c_b=1" in vals, f"{sid} 未看到双方 cookie（共享登录态失效）"
        svc.post("/session/stop", {"session_id": sid})


# ── 用户标签页收养（明确要求时的例外通道）────────────────

def _step_by_action(report, action):
    return next(s for s in report["steps"] if s["action"] == action)


def test_adopt_user_tab_flow(svc, chrome, pages):
    r = svc.run([{"action": "list_user_tabs"}])
    assert r["ok"]
    tabs = _step_by_action(r, "list_user_tabs")["data"]["tabs"]
    assert tabs, "Chrome 初始 about:blank 页应可见"
    # 收养一个非服务自建的页（Chrome 自启的 about:blank）
    idx = next(i for i, t in enumerate(tabs) if not t["managed"])
    r2 = svc.run([
        {"action": "adopt_tab", "index": idx, "name": "my-tab"},
        {"action": "eval_js", "script": "1+1"},
        {"action": "get_url"},
    ], sid="adopt-e2e")
    assert r2["ok"], [s.get("error") for s in r2["steps"] if not s["ok"]]
    assert _step_by_action(r2, "eval_js")["data"]["value"] == 2
    # 会话关闭 → 收养页只注销、绝不关闭（Chrome 进程与该页都健在）
    svc.post("/session/stop", {"session_id": "adopt-e2e"})
    assert chrome["proc"].poll() is None
    r3 = svc.run([{"action": "list_user_tabs"}])
    assert r3["ok"]
    urls = [t["url"] for t in _step_by_action(r3, "list_user_tabs")["data"]["tabs"]]
    assert any(u.startswith("about:blank") for u in urls), "用户标签页不应被关闭"


# ── 断开连接不杀用户浏览器 + 自动重连 ─────────────────────

def test_disconnect_keeps_browser_and_reconnects(svc, chrome, pages):
    # 清掉现有会话 → 最后一个会话关闭 → 断开 CDP
    st = svc.get("/status")
    for s in st.get("sessions", []):
        svc.post("/session/stop", {"session_id": s["session_id"]})
    assert chrome["proc"].poll() is None, "attach 断开不得杀死用户浏览器"
    assert svc.get("/status")["browser_active"] is False
    # 下一个请求自动重连（端口文件可能因浏览器重启变化，重读解析）
    r = svc.run([{"action": "navigate", "url": pages}, {"action": "get_title"}])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert svc.get("/status")["browser_active"] is True


def test_shutdown_service_keeps_browser(svc, chrome):
    svc.post("/shutdown", {"instance_id": svc.get("/status")["instance_id"]})
    time.sleep(1.5)
    assert chrome["proc"].poll() is None, "服务退出不得杀死用户浏览器"


# ── 错误路径（无需真实可连浏览器）────────────────────────

def test_attach_dir_without_debug_port_hard_errors(pages):
    """有 Local State、无 DevToolsActivePort → 首个会话请求硬错并给出处理步骤。"""
    home = fresh_home("ba-attach-nodbg-")
    fake = tempfile.mkdtemp(prefix="ba-fake-browser-")
    open(os.path.join(fake, "Local State"), "w").write("{}")
    s = Service(18713, home, extra_args=["--attach", f"--attach-dir={fake}"])
    s.start()  # attach 惰性连接：HTTP 服务应正常就绪
    try:
        r = s.post("/session/start", {"session_id": "x"})
        assert r["ok"] is False
        assert "DevToolsActivePort" in r.get("error", "") or "远程调试" in r.get("error", "")
    finally:
        s.shutdown()
        cleanup_home(home)


def test_attach_dead_port_fails_with_clear_error(pages):
    """端口文件存在但无人监听 → 连接失败信息可读（连接拒绝，快速失败）。"""
    home = fresh_home("ba-attach-dead-")
    fake = tempfile.mkdtemp(prefix="ba-fake-dead-")
    open(os.path.join(fake, "Local State"), "w").write("{}")
    # 端口 9：特权端口几乎必然无人监听
    open(os.path.join(fake, "DevToolsActivePort"), "w").write("9\n/devtools/browser/x\n")
    s = Service(18714, home, extra_args=["--attach", f"--attach-dir={fake}"])
    s.start()
    try:
        r = s.post("/session/start", {"session_id": "x"})
        assert r["ok"] is False
        err = r.get("error", "")
        # offline 分类：端口文件在但不在监听，错误点名成因
        assert "未在监听" in err, err
        # remedy 先让 agent 代开浏览器，再升级到用户开调试开关
        assert "代开" in err and "chrome://inspect" in err
    finally:
        s.shutdown()
        cleanup_home(home)


def test_cli_attach_rejects_non_browser_dir():
    """CLI 对非浏览器目录立即拒绝（不启动服务）。"""
    r = subprocess.run(
        [sys.executable, "-m", "browser_agent.client", "attach", "/tmp",
         "--port=18715"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=dict(os.environ, BROWSER_AGENT_HOME=fresh_home("ba-attach-cli-"),
                 PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode != 0
    assert "Local State" in (r.stdout + r.stderr)


# ── CLI attach 成功全链路（真实可连浏览器 → 就绪等待 → 真实操作 → 退出码 0）──

CLI_ATTACH_PORT = 18716


def _cli(home, *args, timeout=90):
    """attach 套件薄封装：固定 CLI_ATTACH_PORT（共享实现在 e2e_helpers.run_cli）。"""
    from e2e_helpers import run_cli
    return run_cli(home, *args, port=CLI_ATTACH_PORT, timeout=timeout)


def test_cli_attach_success_full_chain(chrome, pages):
    """attach 就绪等待的成功路径：`browser-agent-client attach <目录>` →
    保存偏好 → 服务以 attach 启动 → 等浏览器真连上（退出码 0）→ 真实操作。"""
    if not find_chrome():
        pytest.skip("本机无可用 Chromium 系浏览器")
    home = fresh_home("ba-attach-cli-ok-")
    try:
        # 1) attach 命令：保存偏好 + 启动 + 等就绪
        code, out = _cli(home, "attach", chrome["dir"], timeout=120)
        assert code == 0, f"期望退出码 0，实际 {code}: {out}"
        assert "浏览器已附着 ✓" in out, out
        # 偏好已落盘
        pref = open(os.path.join(home, "attach.env"), encoding="utf-8").read()
        assert chrome["dir"] in pref

        # 2) 状态：attach 且浏览器在连
        import httpx
        c = httpx.Client(timeout=5, proxy=None)
        st = c.get(f"http://127.0.0.1:{CLI_ATTACH_PORT}/status").json()
        assert st["browser_mode"] == "attach" and st["browser_active"] is True
        assert (st.get("attach") or {}).get("browser_dir") == chrome["dir"]
        c.close()

        # 3) CLI 真实操作（附着浏览器上导航本地页）
        code, out = _cli(home, "run", json.dumps([
            {"action": "navigate", "url": pages},
            {"action": "get_title"},
        ]), timeout=120)
        assert code == 0 and "执行了 2 步" in out, out

        # 4) shutdown（实例门）→ 退出码 0；Chrome 不受影响
        code, out = _cli(home, "shutdown")
        assert code == 0, out
        assert chrome["proc"].poll() is None, "关服务不得杀用户浏览器"
    finally:
        # 兜底：测试中途失败也要关掉残留服务（Chrome 由 fixture 管）
        import httpx
        try:
            c = httpx.Client(timeout=5, proxy=None)
            st = c.get(f"http://127.0.0.1:{CLI_ATTACH_PORT}/status").json()
            c.post(f"http://127.0.0.1:{CLI_ATTACH_PORT}/shutdown",
                   json={"instance_id": st.get("instance_id", "")})
            c.close()
        except Exception:
            pass
        cleanup_home(home)


def test_attach_connect_timeout_against_stalling_endpoint(pages, monkeypatch):
    """连接等待超时可调且被遵守：假端点收下 TCP 但永不说 WebSocket →
    connect_over_cdp 挂到 BROWSER_AGENT_CONNECT_TIMEOUT_MS 超时硬错（不静默降级）。
    （授权弹框路径需真人点击，见手工清单；这里固化的是等待/超时机制本身。）"""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    held = []

    def _stall():
        while True:
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                return

    threading.Thread(target=_stall, daemon=True).start()
    monkeypatch.setenv("BROWSER_AGENT_CONNECT_TIMEOUT_MS", "3000")
    home = fresh_home("ba-attach-stall-")
    s = Service(18717, home, extra_args=[
        "--attach", f"--cdp-endpoint=ws://127.0.0.1:{port}/devtools/browser/x"])
    s.start()
    try:
        t0 = time.monotonic()
        r = s.post("/session/start", {"session_id": "stall"})
        elapsed = time.monotonic() - t0
        assert r["ok"] is False, "挂死端点不得连上"
        assert "Timeout" in r.get("error", "") or "超时" in r.get("error", ""), r
        assert elapsed < 20, f"连接超时未生效（应 ~3s 失败而非默认 120s）: {elapsed:.1f}s"
    finally:
        s.shutdown()
        cleanup_home(home)
        for c in held:
            try:
                c.close()
            except Exception:
                pass
        srv.close()
