"""E2E 生命周期闭环 — CLI 全链路 / attach 就绪等待与退出码 / 精确清理真机（优雅+强杀）/
经验前置闭环 / 新命令面实战（go_back、JPEG 截图、多文件上传、list_frames、隔离求值）。

RUN_E2E=1 运行（需要真实浏览器与本机端口）；不打 RUN_E2E 自动跳过。
绝不触碰真实用户数据：BROWSER_AGENT_HOME 一律指向临时目录。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(not os.getenv("RUN_E2E"), reason="需要 RUN_E2E=1（真实浏览器）")

from e2e_helpers import (Service, fresh_home, cleanup_home, start_page_server,  # noqa: E402
                         ok_step, run_cli)

PAGE_PORT = 18801
SVC_PORT = 18802
CLI_PORT = 18803
ATTACH_CLI_PORT = 18804
CLEAN_PORT = 18805
FORCE_PORT = 18806

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pages():
    srv = start_page_server(PAGE_PORT)
    yield f"http://127.0.0.1:{PAGE_PORT}"
    srv.shutdown()


@pytest.fixture(scope="module")
def svc(pages):
    home = fresh_home("ba-life-")
    s = Service(SVC_PORT, home, extra_args=[f"--url={pages}"])
    s.start()
    yield s
    s.shutdown()
    cleanup_home(home)


def _cli(home, *args, port=CLI_PORT, timeout=90):
    """lifecycle 套件薄封装（CLI 启动契约的共享实现在 e2e_helpers.run_cli）。
    注意：端口经 port= 传，不要混在 args 里——run_cli 会追加默认端口，后写覆盖。"""
    return run_cli(home, *args, port=port, timeout=timeout)


# ── CLI 全链路（agent 实际用法：start → run → sites → stop-session → shutdown）──

def test_cli_full_lifecycle(pages):
    home = fresh_home("ba-cli-life-")
    try:
        code, out = _cli(home, "start", f"--url={pages}", f"--port={CLI_PORT}")
        assert code == 0, out

        code, out = _cli(home, "status", f"--port={CLI_PORT}")
        assert code == 0 and '"service": "browser_agent"' in out, out
        assert '"log_file"' in out  # 日志路径对 agent 可见

        code, out = _cli(home, "run",
                         json.dumps([
                             {"action": "navigate", "url": pages},
                             {"action": "get_title"},
                         ]), f"--port={CLI_PORT}")
        assert code == 0 and "执行了 2 步" in out, out

        # 站点经验闭环：写入带别名 → CLI 读回（自由文本入参）
        code, out = _cli(home, "run", json.dumps([
            {"action": "write_site", "domain": "127.0.0.1",
             "aliases": ["本地标的"], "patterns": ["入口固定为主页"]},
        ]), f"--port={CLI_PORT}")
        assert code == 0, out
        code, out = _cli(home, "sites", "去本地标的看看", f"--port={CLI_PORT}")
        assert code == 0 and "127.0.0.1" in out and "入口固定为主页" in out, out

        code, out = _cli(home, "stop-session", f"--port={CLI_PORT}")
        assert code == 0, out
        code, out = _cli(home, "shutdown", f"--port={CLI_PORT}")
        assert code == 0, out

        # 日志链完整：启动 → 批起止 → 关停受理 → 退出清理
        # （shutdown 响应先于排水返回，最后一条事件稍后落盘 → 轮询等待）
        log_path = os.path.join(home, "logs", "server.log")
        deadline = time.time() + 15
        log = ""
        while time.time() < deadline:
            log = open(log_path, encoding="utf-8").read()
            if "服务退出清理完成" in log:
                break
            time.sleep(0.3)
        for marker in ("服务启动", "命令批开始", "命令批结束",
                       "关停请求受理", "服务退出清理完成"):
            assert marker in log, f"日志缺生命周期事件: {marker}"
        assert "站点经验已更新" in log
    finally:
        cleanup_home(home)


def test_cli_attach_unreachable_target_exits_2():
    """attach 目标端口不在监听（offline 分类）→ CLI 退出码 2（需要用户动作）。"""
    import httpx
    home = fresh_home("ba-cli-att-")
    fake = tempfile.mkdtemp(prefix="ba-fake-offline-")
    open(os.path.join(fake, "Local State"), "w").write("{}")
    open(os.path.join(fake, "DevToolsActivePort"), "w").write("1\n")  # 端口 1 不会有监听
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "attach.env"), "w") as f:
            f.write(f"BROWSER_AGENT_ATTACH_DIR={fake}\n")
        code, out = _cli(home, "start", port=ATTACH_CLI_PORT, timeout=120)
        assert code == 2, f"期望退出码 2（需要用户动作），实际 {code}: {out}"
        assert "未连上" in out or "授权" in out
        # 服务保持运行（不是被杀掉）——用户处理完，下一个请求自动重试
        c = httpx.Client(timeout=5, proxy=None)
        st = c.get(f"http://127.0.0.1:{ATTACH_CLI_PORT}/status").json()
        assert st["service"] == "browser_agent" and st["browser_mode"] == "attach"
        assert st["browser_active"] is False
        c.post(f"http://127.0.0.1:{ATTACH_CLI_PORT}/shutdown",
               json={"instance_id": st["instance_id"]})
        c.close()
    finally:
        import shutil
        shutil.rmtree(fake, ignore_errors=True)
        cleanup_home(home)


# ── 新命令面实战（真实 Chromium）──────────────────────────

def test_go_back_and_jpeg_screenshot(svc, pages):
    svc.run([{"action": "navigate", "url": pages},
             {"action": "navigate", "url": f"{pages}/second"}])
    r = svc.run([{"action": "go_back"}, {"action": "get_url"}])
    d = ok_step(r, 1)
    assert "/second" not in d["url"], d["url"]

    tmp = tempfile.mkdtemp(prefix="ba-shot-")
    png = os.path.join(tmp, "s.png")
    jpg = os.path.join(tmp, "s.jpeg")
    svc.run([{"action": "screenshot", "path": png, "full_page": False}])
    svc.run([{"action": "screenshot", "path": jpg, "full_page": False,
              "image_type": "jpeg", "quality": 70}])
    assert open(png, "rb").read(8).startswith(b"\x89PNG")
    jpeg = open(jpg, "rb").read()
    assert jpeg[:2] == b"\xff\xd8", "JPEG 魔数校验"
    assert len(jpeg) < os.path.getsize(png), "JPEG 应显著小于 PNG（省 token）"


def test_upload_file_multi(svc, pages):
    tmp = tempfile.mkdtemp(prefix="ba-up-")
    a = os.path.join(tmp, "a.txt"); open(a, "w").write("A")
    b = os.path.join(tmp, "b.txt"); open(b, "w").write("B")
    svc.run([{"action": "navigate", "url": f"{pages}/upload"}])
    r = svc.run([
        {"action": "upload_file", "selector": "#files", "files": [a, b]},
        {"action": "upload_file", "selector": "#single", "file_path": a},
        {"action": "eval_js",
         "script": "JSON.stringify({m: document.querySelector('#files').files.length,"
                   " s: document.querySelector('#single').files.length})"},
    ])
    data = json.loads(ok_step(r, 2)["value"])
    assert data == {"m": 2, "s": 1}, data


def test_list_frames_switch_and_operate(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([{"action": "list_frames"}])
    d = ok_step(r, 0)
    assert d["count"] == 1 and d["in_frame"] is False
    f = d["frames"][0]
    assert f["id"] == "embed" and "/iframe" in f["url"]
    r2 = svc.run([
        {"action": "switch_frame", "selector": "iframe#embed"},
        {"action": "click", "selector": "#if-btn"},
        {"action": "get_text", "selector": "#if-btn"},
        {"action": "switch_to_main_frame"},
        {"action": "list_frames"},
    ])
    assert "iframe 已点" in ok_step(r2, 2)["text"]
    assert ok_step(r2, 4)["in_frame"] is False


def test_eval_isolated_sees_clean_dom(svc, pages):
    """主世界读到被篡改的 DOM；isolated 求值读到真实 DOM（反自动化页实战）。"""
    svc.run([{"action": "navigate", "url": f"{pages}/tamper"}])
    expr = ("JSON.stringify(Array.from(document.querySelectorAll('#list li'))"
            ".map(e => e.textContent))")
    r = svc.run([
        {"action": "eval_js", "script": expr},
        {"action": "eval_js", "script": expr, "isolated": True},
    ])
    main_world = json.loads(ok_step(r, 0)["value"])
    isolated = json.loads(ok_step(r, 1)["value"])
    assert main_world == ["假条目"], main_world          # 篡改生效
    assert isolated == ["真实条目 A", "真实条目 B"], isolated  # 隔离环境免疫


# ── 站点经验前置闭环（真实服务：写入 → status 带 aliases → 原话命中 → navigate 自动提示）──

def test_site_experience_front_load_loop(svc, pages):
    svc.run([{"action": "write_site", "domain": "127.0.0.1",
              "aliases": ["本地标的"], "patterns": ["入口固定为主页"]}])
    st = svc.get("/status")
    entry = next(p for p in st["site_patterns"] if p["domain"] == "127.0.0.1")
    assert "本地标的" in entry["aliases"]
    # 用户原话自由文本匹配
    hit = svc.get("/site", params={"domain": "去本地标的看看"})
    assert hit["matched"] and hit["match_type"] == "text" and hit["domain"] == "127.0.0.1"
    # navigate 自动附经验提示（兜底层）
    r = svc.run([{"action": "navigate", "url": pages}])
    assert ok_step(r, 0).get("site_experience", {}).get("domain") == "127.0.0.1"
    svc.run([{"action": "delete_site", "domain": "127.0.0.1"}])


# ── 精确进程清理真机（优雅路径 + 挂死强杀路径）──────────────

def _descendant_pids(root_pid: int) -> list:
    """root_pid 的全部后代 pid（ps 实测，只读本机进程表）。"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                         text=True, timeout=10).stdout
    edges = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            pid, ppid = int(parts[0]), int(parts[1])
            edges.setdefault(ppid, []).append(pid)
    result, queue = [], [root_pid]
    while queue:
        cur = queue.pop()
        for child in edges.get(cur, []):
            result.append(child)
            queue.append(child)
    return result


def _wait_pid_gone(pid: int, timeout: float = 20.0) -> bool:
    """等进程退出。pytest 是这些服务的父进程：退出后成 zombie，kill(pid,0)
    仍成功——须顺手 waitpid 收尸才算真消失。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return True   # 已收尸
        except (ChildProcessError, OSError):
            return True       # 不是本进程子进程且 kill 失败 → 已退出
        time.sleep(0.3)
    return False


def test_cleanup_graceful_reaps_whole_tree(pages):
    """优雅路径：真实 Chromium 进程树（server→driver→Chrome）随服务一并退出。"""
    home = fresh_home("ba-clean-gr-")
    s = Service(CLEAN_PORT, home, extra_args=[f"--url={pages}"])
    s.start()
    deadline = time.time() + 30
    while time.time() < deadline:   # managed 启动即拉浏览器
        if s.get("/status").get("browser_active"):
            break
        time.sleep(0.3)
    chrome_kids = _descendant_pids(s.proc.pid)
    assert chrome_kids, "应有 playwright/Chromium 子进程树"
    server_pid = s.proc.pid
    env = dict(os.environ, BROWSER_AGENT_HOME=home, PYTHONPATH=REPO)
    r = subprocess.run([sys.executable, "-m", "browser_agent.cleanup"],
                       capture_output=True, text=True, timeout=60, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "优雅关停" in r.stdout
    assert _wait_pid_gone(server_pid), "服务进程应退出"
    for p in chrome_kids:
        assert _wait_pid_gone(p, 5), f"子进程 {p} 未随进程树退出（孤儿残留）"
    assert not os.path.exists(os.path.join(home, "browser_agent.pid"))
    s.proc = None   # 已由清理收尾，避免 fixture 再 kill 报噪声
    s.http.close()
    cleanup_home(home)


def test_cleanup_force_kills_unresponsive_server(pages):
    """挂死路径：SIGSTOP 冻结服务（优雅关停无响应）→ SIGTERM 无效 → SIGKILL 收尸。"""
    home = fresh_home("ba-clean-fk-")
    s = Service(FORCE_PORT, home, extra_args=[f"--url={pages}"])
    s.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        if s.get("/status").get("browser_active"):
            break
        time.sleep(0.3)
    server_pid = s.proc.pid
    os.kill(server_pid, 19)  # SIGSTOP：HTTP 不再应答，模拟挂死
    time.sleep(0.5)
    env = dict(os.environ, BROWSER_AGENT_HOME=home, PYTHONPATH=REPO)
    r = subprocess.run([sys.executable, "-m", "browser_agent.cleanup"],
                       capture_output=True, text=True, timeout=90, env=env, cwd=REPO)
    os.kill(server_pid, 18)  # SIGCONT（若 SIGKILL 已生效则忽略 ENOENT）
    assert r.returncode == 0, r.stdout + r.stderr
    assert "进程树终止" in r.stdout, r.stdout
    assert _wait_pid_gone(server_pid, 10), "挂死的服务应被强制终止"
    s.proc = None
    s.http.close()
    cleanup_home(home)
