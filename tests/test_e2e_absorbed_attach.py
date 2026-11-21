"""E2E 实战（attach 模式）— 本次能力升级的附着侧真机验证。

覆盖：调试端口防护真实拦截（页面内 fetch 探测被 route 挡下、服务自身端口不受
影响）、运行中浏览器身份 pin（偏好改成另一台 → 重连硬错不静默切换）、
probe 真机探测。全部使用临时 profile 的独立 Chrome，不碰用户日常浏览器。
运行：RUN_E2E=1 pytest tests/test_e2e_absorbed_attach.py -v
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(not os.getenv("RUN_E2E"), reason="需要 RUN_E2E=1（真实浏览器）")

from e2e_helpers import (Service, fresh_home, cleanup_home, start_page_server,  # noqa: E402
                         ok_step, find_chrome, spawn_chrome, stop_chrome)

PAGE_PORT = 18731
SVC_PORT = 18732
PIN_SVC_PORT = 18733


@pytest.fixture(scope="module")
def pages():
    srv = start_page_server(PAGE_PORT)
    yield f"http://127.0.0.1:{PAGE_PORT}"
    srv.shutdown()


@pytest.fixture(scope="module")
def chrome():
    if not find_chrome():
        pytest.skip("本机无可用 Chromium 系浏览器")
    c = spawn_chrome("ba-absorbed-attach-")
    yield c
    stop_chrome(c)


@pytest.fixture(scope="module")
def svc(chrome, pages):
    home = fresh_home("ba-absorbed-attach-home-")
    s = Service(SVC_PORT, home,
                extra_args=["--attach", f"--attach-dir={chrome['dir']}", f"--url={pages}"])
    s.start()
    yield s
    s.shutdown()
    cleanup_home(home)


# ── 调试端口防护：页面内探测被拦截 ────────────────────────

def test_debug_port_guard_blocks_page_probes(svc, pages):
    """页面脚本 fetch 本机调试端口应被 route 挡下；同页 fetch 服务自身端口
    （非调试端口）应正常——证明拦截是选择性的，不是全网络都被断。"""
    # 预热：触发按需附着（/status 的 attach 块只在浏览器已连接时输出）
    svc.run([{"action": "navigate", "url": f"{pages}/second"}], sid="warmup")
    st = svc.get("/status")
    dbg_port = st["attach"]["debug_port"]
    assert dbg_port, "/status 应暴露 attach.debug_port"
    script = f"""
    (async () => {{
        const out = {{}};
        try {{ await fetch('http://127.0.0.1:{dbg_port}/json/version', {{mode: 'no-cors'}});
               out.debug = 'ok'; }}
        catch (e) {{ out.debug = 'blocked'; }}
        try {{ await fetch('http://127.0.0.1:{SVC_PORT}/status', {{mode: 'no-cors'}});
               out.service = 'ok'; }}
        catch (e) {{ out.service = 'blocked'; }}
        return out; }})()"""
    r = svc.run([{"action": "navigate", "url": f"{pages}/second"},
                {"action": "eval_js", "script": script}], sid="guard")
    val = ok_step(r, 1)["value"]
    assert val["debug"] == "blocked", "调试端口探测应被防护拦截"
    assert val["service"] == "ok", "非调试端口的本地请求不应被误伤"


# ── 运行中浏览器身份 pin ──────────────────────────────────

def test_pin_blocks_silent_browser_switch(pages):
    """服务已附着 Chrome A；偏好改成 Chrome B 后连接断开 → 重连硬错，
    绝不静默连到 B。"""
    if not find_chrome():
        pytest.skip("本机无可用 Chromium 系浏览器")
    chrome_a, chrome_b = spawn_chrome("ba-pin-a-"), spawn_chrome("ba-pin-b-")
    dir_a, dir_b = chrome_a["dir"], chrome_b["dir"]
    home = fresh_home("ba-pin-home-")
    # 偏好直接落 attach.env（服务启动时读的就是这个文件；不带显式
    # --attach-dir 启动——显式参数优先于偏好，带着它就测不到改偏好路径）
    with open(os.path.join(home, "attach.env"), "w", encoding="utf-8") as f:
        f.write(f"BROWSER_AGENT_ATTACH_DIR={dir_a}\n")
    s = Service(PIN_SVC_PORT, home,
                extra_args=["--attach", f"--url={pages}"])
    s.start()
    try:
        r = s.run([{"action": "navigate", "url": f"{pages}/second"}], sid="pin")
        assert r["ok"], "附着 A 后应能正常操作"

        # 偏好改成 B（只落盘，不重启服务）；随后 A 退出断开连接
        sel = s.post("/attach/select", {"attach_dir": dir_b})
        assert sel["ok"]
        stop_chrome(chrome_a)
        time.sleep(1.0)  # 让服务感知断连

        r2 = s.run([{"action": "get_title"}], sid="pin")
        assert r2["ok"] is False, "重连到另一台浏览器应被拒绝"
        err_text = json.dumps(r2, ensure_ascii=False)
        assert "拒绝改连" in err_text and "shutdown" in err_text, err_text
    finally:
        s.shutdown()
        cleanup_home(home)
        stop_chrome(chrome_a)
        stop_chrome(chrome_b)


# ── probe 真机 ────────────────────────────────────────────

def test_probe_finds_real_installed_chrome():
    if not find_chrome():
        pytest.skip("本机无 Chrome")
    from browser_agent import browser_attach as ba
    folders = [f for listing in ba.list_app_data_folders()
               for f in listing.get("folders", [])]
    if "Google" not in folders:
        pytest.skip("应用数据根目录下无 Google 文件夹")
    res = ba.probe_app_data_folder("Google")
    assert res["ok"], res.get("error")
    browsers = res["folders"][0]["browsers"]
    assert browsers, "应探测到 Chrome 数据目录"
    assert browsers[0]["remote_debugging"]["enabled"] in (True, False)  # 结构合法即可
