"""E2E 公共设施 — 本地测试页 + 服务进程编排。

只在 RUN_E2E=1 时由 test_e2e_*.py 使用（需要真实浏览器环境）。
绝不触碰真实用户数据：BROWSER_AGENT_HOME 一律指向临时目录。
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAIN_PAGE = """<!doctype html><html><head><title>E2E 标的页</title></head><body>
<h1 id="main-title">E2E 标的页</h1>
<button id="counter" onclick="document.getElementById('count').textContent=
  (parseInt(document.getElementById('count').textContent||'0')+1)">计数</button>
<span id="count">0</span>
<button id="semantic-target">语义目标按钮</button>
<a id="next" href="/second">去第二页</a>
<form id="signup" onsubmit="event.preventDefault()">
  <input id="phone" name="phone" placeholder="手机号">
  <input id="pwd" name="pwd" type="password">
  <select id="city"><option value="sh">上海</option><option value="bj">北京</option></select>
  <label><input type="checkbox" id="agree"> 同意条款</label>
  <button type="submit" id="submit-btn">提交</button>
</form>
<iframe id="embed" src="/iframe" title="内嵌页" width="300" height="120"></iframe>
<div id="lazy"></div>
<script>setTimeout(()=>{document.getElementById('lazy').textContent='异步内容出现'},400)</script>
<ul id="items"><li>条目一</li><li>条目二</li><li>条目三</li><li>条目四</li><li>条目五</li></ul>
</body></html>"""

SECOND_PAGE = """<!doctype html><html><head><title>第二页</title></head><body>
<h1 id="second-title">第二页内容</h1></body></html>"""

IFRAME_PAGE = """<!doctype html><html><head><title>iframe 内部</title></head><body>
<h1 id="iframe-title">iframe 内部标题</h1>
<button id="if-btn" onclick="this.textContent='iframe 已点'">iframe 按钮</button>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/iframe"):
            body = IFRAME_PAGE
        elif self.path.startswith("/second"):
            body = SECOND_PAGE
        else:
            body = MAIN_PAGE
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_page_server(port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Service:
    """browser-agent 服务进程包装（隔离 HOME，托管生命周期）。"""

    def __init__(self, port: int, home: str, extra_args: list = None):
        self.port = port
        self.home = home
        self.base = f"http://127.0.0.1:{port}"
        self.extra_args = extra_args or []
        self.proc = None
        self.http = httpx.Client(timeout=90, proxy=None)

    def start(self, timeout=45) -> None:
        env = dict(os.environ, BROWSER_AGENT_HOME=self.home)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "browser_agent.server",
             f"--port={self.port}", "--headless"] + self.extra_args,
            cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.http.get(f"{self.base}/status", timeout=2)
                if r.status_code == 200 and r.json().get("service") == "browser_agent":
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"服务 {self.port} 启动超时")

    def get(self, path, **kw):
        return self.http.get(f"{self.base}{path}", **kw).json()

    def post(self, path, payload=None, **kw):
        return self.http.post(f"{self.base}{path}", json=payload or {}, **kw).json()

    def run(self, commands, sid="", **kw):
        body = {"commands": commands, **kw}
        if sid:
            body["session_id"] = sid
        return self.post("/run", body)

    def shutdown(self):
        try:
            self.http.post(f"{self.base}/shutdown", json={}, timeout=10)
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.http.close()


def ok_step(report, idx):
    """断言第 idx 步成功并返回其 data。"""
    assert report["steps"][idx]["ok"], report["steps"][idx]
    return report["steps"][idx]["data"]


def wait_active_batches(get_status, n=2, timeout=6.0, poll=0.05):
    """轮询 /status 直到 active_batches >= n（并行证明的结构性判据）。"""
    import time as _t
    deadline = _t.monotonic() + timeout
    best = 0
    while _t.monotonic() < deadline:
        try:
            best = max(best, get_status().get("active_batches", 0))
            if best >= n:
                return best
        except Exception:
            pass
        _t.sleep(poll)
    return best


def fresh_home(prefix="ba-e2e") -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    return d


def cleanup_home(home: str) -> None:
    shutil.rmtree(home, ignore_errors=True)
