"""E2E 公共设施 — 本地测试页 + 服务进程编排。

只在 RUN_E2E=1 时由 test_e2e_*.py 使用（需要真实浏览器环境）。
绝不触碰真实用户数据：BROWSER_AGENT_HOME 一律指向临时目录。
"""
from contextlib import contextmanager
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

# 坐标点击标的页：目标区内点击计数并记录命中坐标（click_xy 实战验证用）
XY_PAGE = """<!doctype html><html><head><title>坐标页</title></head>
<body style="margin:0">
<div id="xy-target" style="position:absolute;left:100px;top:100px;width:200px;height:100px;
  background:#4a7">点击目标（命中会计数并记录坐标）</div>
<span id="hits" style="position:fixed;left:2px;top:2px;font-size:12px">0</span>
<script>
window.lastClick = null;
document.addEventListener('click', e => {
  if (e.target.closest('#xy-target')) {
    document.getElementById('hits').textContent =
      parseInt(document.getElementById('hits').textContent || '0') + 1;
    window.lastClick = {x: e.clientX, y: e.clientY};
  }
});
</script></body></html>"""

# 超长页：direction=top/bottom 滚动验证用
TALL_PAGE = """<!doctype html><html><head><title>长页</title></head>
<body style="margin:0;height:6000px"><div style="height:6000px">tall</div></body></html>"""

IFRAME_PAGE = """<!doctype html><html><head><title>iframe 内部</title></head><body>
<h1 id="iframe-title">iframe 内部标题</h1>
<button id="if-btn" onclick="this.textContent='iframe 已点'">iframe 按钮</button>
</body></html>"""

# 多文件上传页：upload_file 数组实战验证用
UPLOAD_PAGE = """<!doctype html><html><head><title>上传页</title></head><body>
<input id="files" type="file" multiple>
<input id="single" type="file">
<div id="report"></div>
</body></html>"""

# DOM 篡改页：主世界改写 querySelectorAll（反自动化常见手法）。
# 隔离环境（isolated world）有自己的一套 DOM 原型——eval_js isolated 应读到真实 DOM
TAMPER_PAGE = """<!doctype html><html><head><title>篡改页</title></head><body>
<ul id="list"><li>真实条目 A</li><li>真实条目 B</li></ul>
<script>
Document.prototype.querySelectorAll = function () {
  return [{ textContent: "假条目" }];
};
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/iframe"):
            body = IFRAME_PAGE
        elif self.path.startswith("/second"):
            body = SECOND_PAGE
        elif self.path.startswith("/xy"):
            body = XY_PAGE
        elif self.path.startswith("/tall"):
            body = TALL_PAGE
        elif self.path.startswith("/upload"):
            body = UPLOAD_PAGE
        elif self.path.startswith("/tamper"):
            body = TAMPER_PAGE
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
        # 独立进程组：与生产 client 拉起语义一致（终端解耦 + 可整树终止），
        # 强杀类用例的 killpg 不会波及 pytest 自身进程组
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "browser_agent.server",
             f"--port={self.port}", "--headless"] + self.extra_args,
            cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
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
        # 有意不复用 client.shutdown（测试骨架与被测代码保持独立），
        # 手写同一关停协议：GET /status 取 instance_id → POST /shutdown
        try:
            status = self.http.get(f"{self.base}/status", timeout=3).json()
            self.http.post(f"{self.base}/shutdown",
                           json={"instance_id": status.get("instance_id", "")},
                           timeout=10)
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.http.close()


def run_cli(home: str, *args, port: int = None, timeout: float = 90) -> tuple:
    """真实 CLI 子进程（agent 的实际用法）：隔离 HOME + PYTHONPATH + 仓库 cwd。

    返回 (exit_code, stdout+stderr)。CLI 启动契约（环境注入/解释器/cwd）的
    唯一实现——lifecycle 与 attach 两套 E2E 共用，不各抄一份。
    """
    cmd = [sys.executable, "-m", "browser_agent.client", *args]
    if port is not None:
        cmd.append(f"--port={port}")
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ, BROWSER_AGENT_HOME=home, PYTHONPATH=REPO), cwd=REPO)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


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


# ── 真实 Chrome 编排（临时 profile，自管端口）────────────────

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def wait_devtools_port(profile_dir: str, timeout=25) -> int:
    """轮询等待 Chrome 写出 DevToolsActivePort（解析复用 browser_attach）。"""
    from browser_agent import browser_attach as ba
    deadline = time.time() + timeout
    while time.time() < deadline:
        port = ba.read_devtools_port(profile_dir)
        if port is not None:
            return port
        time.sleep(0.3)
    raise RuntimeError("Chrome 未在超时内写出 DevToolsActivePort")


def spawn_chrome(prefix: str = "ba-e2e-chrome-") -> dict:
    """启动一台临时 profile 的真实 Chrome（自管端口）；返回
    {proc, dir, port}。调用方负责 proc 终结与目录清理（stop_chrome）。"""
    import subprocess
    import tempfile
    binary = find_chrome()
    if not binary:
        raise RuntimeError("本机无可用 Chromium 系浏览器")
    profile = tempfile.mkdtemp(prefix=prefix)
    proc = subprocess.Popen([
        binary, "--headless=new", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"proc": proc, "dir": profile, "port": wait_devtools_port(profile)}


def stop_chrome(chrome: dict) -> None:
    chrome["proc"].terminate()
    try:
        chrome["proc"].wait(timeout=10)
    except Exception:
        chrome["proc"].kill()
    shutil.rmtree(chrome["dir"], ignore_errors=True)


@contextmanager
def launched_service(port: int, url: str, prefix: str = "ba-e2e-svc-", env: dict = None):
    """函数级服务编排：临时 HOME + 起服务 + 收尾（关停/兜底 kill + 清目录）。

    env 里的变量在服务进程启动期间临时注入（如 BROWSER_AGENT_TAB_IDLE_SEC）。
    """
    saved_env = {k: os.environ.get(k) for k in (env or {})}
    try:
        for k, v in (env or {}).items():
            os.environ[k] = str(v)
        home = fresh_home(prefix)
        s = Service(port, home, extra_args=[f"--url={url}"])
        s.start()
        try:
            yield s
        finally:
            s.shutdown()
            cleanup_home(home)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
