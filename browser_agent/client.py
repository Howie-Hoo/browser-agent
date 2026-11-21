#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器代理客户端 (Browser Agent Client)
AI 代理与浏览器代理服务通信的标准入口。

CLI（pip 安装后为 browser-agent-client；未安装用 python -m browser_agent.client）：
  browser-agent-client status
  browser-agent-client start [--url=http://...] [--headless] [--port=7788]
  browser-agent-client run <json文件或JSON字符串>
  browser-agent-client check <类型>
  browser-agent-client reset
  browser-agent-client stop-session        关闭本客户端会话（多任务共享服务时用它代替 shutdown）
  browser-agent-client sites               列出已积累的站点经验
  browser-agent-client sites <域名或URL>   读取该站点经验
  browser-agent-client find-url [关键词…] [--only bookmarks|history] [--browser <名>]
                                [--limit N] [--since 1d|7h|YYYY-MM-DD] [--sort recent|visits]
                                          本地检索浏览器书签/历史（内部系统、之前看过的页面）
  browser-agent-client attach --discover   列出可附着的本机浏览器（含远程调试开关状态）
  browser-agent-client attach --probe <文件夹>  探测清单外浏览器（应用数据根目录下的文件夹名）
  browser-agent-client attach <数据目录>   保存附着偏好并以 attach 模式启动服务
  browser-agent-client attach --forget     清除附着偏好
  browser-agent-client shutdown

退出码（agent 可据此分支，不必解析输出文本）：
  0  成功
  1  硬失败（服务未起 / 命令批失败 / 参数错误）
  2  需要用户动作——服务已在但浏览器未连上（attach 等待授权弹窗、目标浏览器
     未开/未开调试开关）；处理完用户侧重试即可

Python API：
  from browser_agent.client import BrowserAgentClient
  client = BrowserAgentClient(port=7788, base_url="http://localhost:3000")
  client.ensure_service_running()
  report = client.run(commands)
  client.shutdown()
"""
import sys
import json
import os
import time
import uuid
import subprocess
from typing import Optional, Union

from . import SERVICE_NAME as _SERVICE_NAME
from .utils import SERVER_DRAIN_SEC, is_pid_alive

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _attach_pending(status: Optional[dict]) -> bool:
    """「attach 且浏览器未连上」谓词——/status 字段知识的单点（三个调用方共用）。"""
    return bool(status and status.get("browser_mode") == "attach"
                and not status.get("browser_active"))


def _server_env() -> dict:
    # 子进程经 -m 拉起服务，不继承本进程 sys.path——把包父目录注入 PYTHONPATH，
    # 仓库克隆直跑（未 pip install）时 server 模块才能被解析
    env = dict(os.environ)
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    pkg_parent = os.path.dirname(_PKG_DIR)
    if pkg_parent not in parts:
        parts.insert(0, pkg_parent)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env

try:
    import httpx
except ImportError:
    print("[错误] 缺少 httpx，请执行: pip install httpx", file=sys.stderr)
    sys.exit(1)


def _log(msg: str):
    print(f"[客户端] {msg}", flush=True)


def _err(msg: str):
    print(f"[错误] {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────
# PID 工具
# ─────────────────────────────────────────────────────────
def _read_pid() -> Optional[dict]:
    from . import user_data
    try:
        with open(user_data.pid_file()) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────
class BrowserAgentClient:
    """
    浏览器代理客户端。

    参数：
        port:       服务端口（默认 7788）
        base_url:   目标网页地址
        headless:   无头模式（默认 False）
        slow_mo:    操作间延迟 ms（默认 200）
        auto_start: 未运行时自动启动（默认 True）
        timeout:    HTTP 请求超时秒（默认 120）
        session_id: 会话标识（多任务共享服务时各占一个隔离 context；默认自动生成，
                    同一 client 实例的命令/会话操作都路由到同一 session）
    """

    def __init__(self, port: int = 7788, base_url: str = "http://localhost:3000",
                 headless: bool = False, slow_mo: int = 0, auto_start: bool = True,
                 timeout: int = 120, session_id: str = "", owner: str = ""):
        self.port = port
        self.base_url = base_url
        self.headless = headless
        self.slow_mo = slow_mo
        self.auto_start = auto_start
        self.session_id = session_id or f"cli-{uuid.uuid4().hex[:8]}"
        self.owner = owner
        self._base = f"http://127.0.0.1:{port}"
        # 增加大超时时间，防止无尽挂起，默认为 120s，并禁用代理以防本地网络被劫持
        self._http = httpx.Client(timeout=timeout, proxy=None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._http.close()

    # ── 状态 ──────────────────────────────────────────────

    def get_status(self) -> Optional[dict]:
        try:
            r = self._http.get(f"{self._base}/status", timeout=4)
            return r.json()
        except Exception:
            return None

    def is_running(self) -> bool:
        s = self.get_status()
        return s is not None and s.get("service") == _SERVICE_NAME

    # ── 启动 ──────────────────────────────────────────────

    def start_service(self) -> bool:
        # 单次拉取：is_running 语义（服务身份核对）内联展开，避免同路径打两次 GET
        s = self.get_status()
        if s is not None and s.get("service") == _SERVICE_NAME:
            _log("服务已在运行")
            # attach 且浏览器未连上（典型：上次在等授权点了超时）→ 补就绪等待，
            # 维持「退出码 0 = 浏览器真可用」的契约，不让重试 start 假成功
            if _attach_pending(s):
                return self._await_attach_ready()
            return True

        # 已保存附着偏好 → 以 attach 模式启动（附着用户日常浏览器，登录态免费）
        attach_args = []
        from . import browser_attach
        pref = browser_attach.read_preference()
        if os.getenv("BROWSER_AGENT_MODE") == "attach" or (pref["attach_dir"] or pref["cdp_endpoint"]):
            attach_args = ["--attach"]
            if pref["attach_dir"]:
                attach_args.append(f"--attach-dir={pref['attach_dir']}")
            if pref["cdp_endpoint"]:
                attach_args.append(f"--cdp-endpoint={pref['cdp_endpoint']}")
            _log(f"attach 模式：将附着 {pref['attach_dir'] or pref['cdp_endpoint']}")
        else:
            # 资源预检 fail-fast：低配机器余量不足时拒绝拉起 Chromium，防整机被压垮。
            # attach 模式不拉浏览器，跳过预检
            from .utils import check_system_resources

            ok, detail = check_system_resources()
            if not ok:
                _log(f"资源预检未通过：{detail}")
                return False

        _log(f"启动服务 → :{self.port} | {self.base_url} | {'无头' if self.headless else '有头'}")
        cmd = [
            sys.executable, "-m", "browser_agent.server",
            f"--port={self.port}",
            f"--url={self.base_url}",
            f"--slow-mo={self.slow_mo}",
        ] + attach_args
        if self.headless:
            cmd.append("--headless")

        # 独立进程组/会话：服务与启动它的终端解耦（关终端/退出 agent 会话不带走
        # 共享服务），也让「精确清理整棵进程树」（server → playwright driver →
        # Chromium）成为可能——服务日志由自身写入 ~/.browser-agent/logs/，不依赖 stdout
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

        subprocess.Popen(cmd, env=_server_env(), **kwargs)

        for i in range(60):
            time.sleep(0.5)
            if self.is_running():
                _log(f"服务就绪 ✓ ({(i+1)*0.5:.1f}s)")
                if attach_args:
                    # attach 的就绪 = 浏览器真连上（授权点过/目标可用），不是 HTTP 起来
                    return self._await_attach_ready()
                return True
        _err("服务启动超时（30s）")
        from . import logs
        _err(f"服务日志: {logs.log_path()}（启动/连接失败原因见此文件）")
        return False

    def _await_attach_ready(self) -> bool:
        """attach 模式等浏览器真正连上（lazy 连接经 session/start 触发）。

        新式开关下每条新连接都要用户在浏览器点「允许」：2s 未连上即打印授权
        提示（不让 agent 与用户盲等），窗口 BROWSER_AGENT_ATTACH_READY_SEC
        （默认 20s）。超时**不杀服务**（用户点完允许后下一个请求即连上），
        返回 False 由 CLI 以退出码 2 告知「需要用户动作」。
        """
        try:
            ready_sec = float(os.getenv("BROWSER_AGENT_ATTACH_READY_SEC", "20"))
        except ValueError:
            ready_sec = 20.0
        # 触发连接：default 会话的建立即发起 CDP 附着（最长 120s 授权窗口在服务侧）
        try:
            self._http.post(f"{self._base}/session/start",
                            json={"session_id": "default", "context": "user"},
                            timeout=5)
        except Exception:
            pass
        hint_shown = False
        deadline = time.monotonic() + max(1.0, ready_sec)
        while time.monotonic() < deadline:
            s = self.get_status()
            if s and s.get("browser_active"):
                _log("浏览器已附着 ✓（登录态可用）")
                return True
            if not hint_shown:
                # 消费 /status 的 browser_starting：连接尝试中 ≈ 授权弹窗待点；
                # 否则大概率是目标浏览器没开/开关没开——两种提示动作不同
                if s and s.get("browser_starting"):
                    _log("⚠️ 正在连接浏览器——若出现远程调试授权弹窗，请点击「允许」，"
                         "连接会随后自动完成")
                else:
                    _log("⚠️ 浏览器尚未连上——若浏览器出现授权弹窗请点击「允许」；"
                         "没有弹窗则检查目标浏览器是否在运行、调试开关是否开启")
                hint_shown = True
            time.sleep(0.5)
        _err(f"浏览器在 {ready_sec:.0f}s 内未连上（等待授权/目标浏览器不可用）。"
             "服务保持运行：完成授权或打开目标浏览器后，下一个请求会自动重试")
        from . import logs
        _err(f"排障: browser-agent-client attach --discover 查看状态；"
             f"服务日志 {logs.log_path()}")
        return False

    def ensure_service_running(self) -> bool:
        """检测 → 验证服务身份 → 按需自动启动（AI 代理发命令前的标准调用）。"""
        status = self.get_status()
        if status is not None:
            if status.get("service") == _SERVICE_NAME:
                _log(f"服务已就绪（会话: {status.get('session_id','-')}）")
                return True
            _err(f"端口 {self.port} 已被其他服务占用（service={status.get('service')}）")
            return False

        pid_info = _read_pid()
        if pid_info and pid_info.get("port") == self.port:
            if is_pid_alive(pid_info["pid"]):
                # 测试模式下不再试图自杀重启
                pass

        if not self.auto_start:
            return True # 在不自启动的调试模式下，始终放行进行 HTTP 尝试

        return self.start_service()

    # ── 执行 ──────────────────────────────────────────────

    def run(self, commands: Union[list, dict, str], compact: bool = False, report: bool = True,
            context: Optional[str] = None) -> dict:
        """
        发送命令批次，返回富报告。命令路由到 self.session_id 对应的会话。

        :param context: "isolated"（默认，隔离 context）或 "user"（共享 default
                        context；attach 模式=用户真实 profile，登录态免费）。
                        会话已存在时此参数被忽略（沿用建立时的模式）。
        返回字段：ok, session_id, executed_at, steps, screenshots, errors, summary, report_file
        compact=True 时精简响应（不含完整 data 和截图路径）。
        report=False 时跳过报告文件生成。
        """
        commands = self._parse_commands(commands)
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行", "steps": [], "screenshots": [], "errors": []}

        _log(f"发送 {len(commands)} 条命令...")
        payload = {"commands": commands, "compact": compact, "report": report,
                   "session_id": self.session_id, "owner": self.owner}
        if context:
            payload["context"] = context
        try:
            r = self._http.post(f"{self._base}/run", json=payload)
            try:
                result = r.json()
            except Exception as e_json:
                return {"ok": False, "error": f"JSON解析失败 [{r.status_code}]: {r.text[:200]} ({e_json})", "steps": [], "screenshots": [], "errors": []}
        except httpx.TimeoutException:
            return {"ok": False, "error": "客户端执行超时 (120s)，请检查是否有死循环或阻塞", "steps": [], "screenshots": [], "errors": []}
        except Exception as e:
            return {"ok": False, "error": f"服务通信异常: {e}", "steps": [], "screenshots": [], "errors": []}

        _log(result.get("summary", ""))
        if result.get("report_file"):
            _log(f"报告: {result['report_file']}")
        for p in result.get("screenshots", []):
            _log(f"截图: {p}")
        for s in result.get("steps", []):
            if not s.get("ok"):
                _err(f"  步骤 {s['step']} [{s['action']}] 失败: {s.get('error','')}")
        return result

    def check(self, check_type: str = "full") -> dict:
        """运行预设检验套件：full | panels | dom | perf | a11y | interact | all"""
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行"}

        _log(f"运行检验: {check_type}")
        try:
            r = self._http.post(
                f"{self._base}/check",
                json={"type": check_type, "session_id": self.session_id, "owner": self.owner},
            )
            result = r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        _log(result.get("summary", ""))
        if result.get("report_file"):
            _log(f"报告: {result['report_file']}")
        return result

    def session_start(self, url: str = "", context: Optional[str] = None) -> dict:
        """启动/获取本客户端会话。

        :param context: "isolated"（默认）或 "user"（共享 default context；
                        attach 模式=用户真实 profile，登录态免费）
        """
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行"}
        payload = {
            "session_id": self.session_id,
            "owner": self.owner,
            "base_url": (url or self.base_url).rstrip("/"),
            "headless": self.headless,
            "slow_mo": self.slow_mo,
        }
        if context:
            payload["context"] = context
        try:
            r = self._http.post(f"{self._base}/session/start", json=payload)
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def stop_session(self) -> dict:
        """关闭本客户端的会话（只关自己的 context；最后一个会话关闭时浏览器随之关闭）。

        多任务共享服务时用它代替 shutdown()——不影响其他会话与服务。
        """
        try:
            r = self._http.post(
                f"{self._base}/session/stop",
                json={"session_id": self.session_id},
            )
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_session(self) -> dict:
        """重置会话页面（当前会话 context 内新开 page，cookie/storage 一并清空）。"""
        try:
            r = self._http.post(
                f"{self._base}/session/reset",
                json={"session_id": self.session_id},
            )
            result = r.json()
            _log(f"会话页面已重置: {result.get('session_id','-')}")
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 站点经验 ──────────────────────────────────────────

    def sites(self) -> dict:
        """列出已积累的站点经验。"""
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行"}
        try:
            r = self._http.get(f"{self._base}/sites", timeout=10)
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def read_site(self, domain_or_url: str) -> dict:
        """读取目标站点经验（域名或 URL，自动匹配子域名/别名）。"""
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行"}
        try:
            r = self._http.get(f"{self._base}/site", params={"domain": domain_or_url}, timeout=10)
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 本地浏览器资源检索 ────────────────────────────────

    def find_url(self, keywords, only: Optional[str] = None,
                 browser: Optional[str] = None, limit=20,
                 since=None, sort: str = "recent") -> dict:
        """从本机浏览器书签/历史检索 URL（纯本地文件操作，无需服务在运行）。

        适用：公网搜不到的组织内部系统、用户之前访问过但不记得地址的页面、
        最近高频访问的网站（sort=visits）。参数校验/执行统一在
        local_search.search（与命令批共用一份错误形状）。
        """
        from .local_search import search
        return search(keywords=keywords, only=only, browser=browser,
                      limit=limit, since=since, sort=sort)

    # ── 附着日常浏览器 ────────────────────────────────────

    def attach_discover(self) -> dict:
        """列出本机可附着的浏览器（含远程调试开关状态；与 HTTP 端点共用构造）。"""
        from . import browser_attach
        return browser_attach.discover_payload()

    def attach_probe(self, folder: str) -> dict:
        """在应用数据根目录内受控探测 Chromium 数据目录（清单外浏览器）。"""
        from . import browser_attach
        return browser_attach.probe_app_data_folder(folder)

    def attach(self, target: str = "") -> bool:
        """保存附着偏好并以 attach 模式（重新）启动服务。

        :param target: 浏览器数据目录或 CDP 端点；空串 = 沿用已存偏好启动
        """
        from . import browser_attach
        if target:
            is_endpoint = "://" in target
            if not is_endpoint:
                # 目录校验/报错走 browser_attach 单点（与 POST /attach/select 同源）
                res = browser_attach.validate_attach_dir(target)
                if not res.get("ok"):
                    _err(browser_attach.format_attach_error(res))
                    return False
                target = res["dir"]
            browser_attach.save_preference(
                attach_dir="" if is_endpoint else target,
                cdp_endpoint=target if is_endpoint else "",
            )
            _log(f"附着偏好已保存: {target}")
        # 偏好已变 → 重启服务使其按新偏好连接（复用统一重启语义）
        return self.restart_service()

    def shutdown(self) -> bool:
        """优雅关闭服务（浏览器 + HTTP 服务）。

        关停是实例身份门控的维护操作：先 GET /status 取当前 instance_id，
        再随关停请求提交——服务被替换过（旧 id 失配）会得到 409 而不是误杀新实例。
        """
        status = self.get_status()
        if status is None:
            _log("服务未运行")
            return True
        _log("关闭服务...")
        try:
            r = self._http.post(f"{self._base}/shutdown",
                                json={"instance_id": status.get("instance_id", "")},
                                timeout=15)
            return r.json().get("ok", False)
        except Exception:
            return False

    def restart_service(self) -> bool:
        """关闭并重启整个服务（用于切换 headless/有头模式等实例级配置）。"""
        if not self.is_running():
            return self.start_service()
        _log("重启服务...")
        self.shutdown()
        # 等旧实例真正退出再启动：排水期间 /status 仍应答（维护态设计），
        # 固定 sleep 会把排水中的旧服务误判为「已在运行」→ 假成功、后继无人
        deadline = time.time() + SERVER_DRAIN_SEC + 15  # 服务排水上限 + 浏览器关闭余量
        while time.time() < deadline:
            if self.get_status() is None:
                break
            time.sleep(0.3)
        return self.start_service()

    def run_single(self, action: str, **kwargs) -> dict:
        """执行单条命令的快捷方法。"""
        cmd = {"action": action, **kwargs}
        return self.run(cmd)

    # ── 内部工具 ──────────────────────────────────────────

    def _parse_commands(self, commands) -> list:
        if isinstance(commands, dict):
            return [commands]
        if isinstance(commands, list):
            return commands
        if isinstance(commands, str):
            path = commands.strip()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = json.loads(path)
            return [data] if isinstance(data, dict) else data
        raise ValueError(f"无法解析命令类型: {type(commands)}")


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
# 取值型旗标：支持 "--flag 值" 与 "--flag=值" 两种写法（文档示例用空格式）
_VALUE_FLAGS = {"--port", "--url", "--slow-mo", "--only", "--browser",
                "--limit", "--since", "--sort", "--probe"}


def _parse_args(args):
    sub = args[0] if args else "status"
    pos, opts = [], {"port": 7788, "url": "http://localhost:3000", "headless": False, "slow_mo": 200}
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--headless":
            opts["headless"] = True; opts["slow_mo"] = 0
        elif a.startswith("--"):
            name, sep, val = a.partition("=")
            if not sep and name in _VALUE_FLAGS and i + 1 < len(args):
                val = args[i + 1]  # 空格式取值
                i += 1
            if sep or val:
                if name == "--port":
                    opts["port"] = int(val)
                elif name == "--slow-mo":
                    opts["slow_mo"] = int(val)
                elif name in ("--url", "--only", "--browser", "--since",
                              "--sort", "--probe", "--limit"):
                    # limit 保持字符串：int 校验统一交给 from_params（坏值干净报错）
                    opts[name.lstrip("-").replace("-", "_")] = val
        elif a.startswith("http://") or a.startswith("https://"):
            opts["url"] = a
        elif not a.startswith("-"):
            pos.append(a)
        i += 1
    return sub, pos, opts


def _print_find_url(result: dict, sort: str = "recent") -> None:
    """格式化输出本地检索结果（书签/历史分节，| 分隔字段）。"""
    if not result.get("ok"):
        _err(result.get("error", "检索失败"))
        if result.get("remedy"):
            _log(f"处理步骤: {result['remedy']}")
        return
    bookmarks = result.get("bookmarks", [])
    history = result.get("history", [])
    if bookmarks:
        print(f"[书签] {len(bookmarks)} 条")
        for b in bookmarks:
            segs = [b.get("name") or "(无标题)", b.get("url", "")]
            if b.get("folder"):
                segs.append(b["folder"])
            print("  " + " | ".join(segs))
    if bookmarks and history:
        print()
    if history:
        print(f"[历史] {len(history)} 条（{'按访问次数' if sort == 'visits' else '按最近访问'}）")
        for h in history:
            segs = [h.get("title") or "(无标题)", h.get("url", ""),
                    h.get("visited_at_iso", "")]
            if h.get("visit_count", 0) > 1:
                segs.append(f"visits={h['visit_count']}")
            print("  " + " | ".join(segs))
    if not bookmarks and not history:
        print("（无匹配结果）")


def _needs_user_action(client: "BrowserAgentClient") -> bool:
    """服务已在但浏览器未连上（attach 等授权/目标浏览器不可用）→ 退出码 2。"""
    s = client.get_status()
    return bool(s and s.get("service") == _SERVICE_NAME and _attach_pending(s))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    sub, pos, opts = _parse_args(args)

    with BrowserAgentClient(
        port=opts["port"], base_url=opts["url"],
        headless=opts["headless"], slow_mo=opts["slow_mo"],
    ) as client:
        if sub == "status":
            s = client.get_status()
            if s:
                print(json.dumps(s, ensure_ascii=False, indent=2))
            else:
                print(f"服务未运行（端口 {opts['port']}）")
                sys.exit(1)

        elif sub == "start":
            if client.start_service():
                sys.exit(0)
            sys.exit(2 if _needs_user_action(client) else 1)

        elif sub == "run":
            if not pos:
                _err("用法: browser-agent-client run <json文件或JSON字符串>")
                sys.exit(1)
            result = client.run(pos[0])
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "check":
            result = client.check(pos[0] if pos else "full")
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "shutdown":
            sys.exit(0 if client.shutdown() else 1)

        elif sub == "reset":
            result = client.reset_session()
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "stop-session":
            result = client.stop_session()
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "sites":
            if pos:
                result = client.read_site(pos[0])
                if result.get("matched"):
                    print(f"# {result['domain']}（更新于 {result.get('updated','-')}，匹配方式 {result.get('match_type')}）")
                    print(result.get("content", "（无内容）"))
                else:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                result = client.sites()
                print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "find-url":
            result = client.find_url(
                pos, only=opts.get("only"), browser=opts.get("browser"),
                limit=opts.get("limit", 20), since=opts.get("since"),
                sort=opts.get("sort", "recent"))
            _print_find_url(result, sort=opts.get("sort", "recent"))
            sys.exit(0 if result.get("ok") else 1)

        elif sub == "attach":
            # --discover 列出本机浏览器；--probe 探测清单外浏览器（空格/等值两种
            # 写法都经 _parse_args 归一为 opts["probe"]）；--forget 清除偏好；
            # 带参数 = 保存偏好并启动
            discover = "--discover" in args[1:]
            forget = "--forget" in args[1:]
            probe = opts.get("probe")  # 空格/等值两种写法已由 _parse_args 归一
            if discover:
                res = client.attach_discover()
                print(json.dumps(res, ensure_ascii=False, indent=2))
                sys.exit(0)
            if probe is not None or "--probe" in args[1:]:
                folder = probe or (pos[0] if pos else "")
                if not folder:
                    _err("用法: browser-agent-client attach --probe <应用数据根目录下的文件夹>")
                    sys.exit(1)
                res = client.attach_probe(folder)
                print(json.dumps(res, ensure_ascii=False, indent=2))
                sys.exit(0 if res.get("ok") else 1)
            if forget:
                from . import browser_attach
                browser_attach.save_preference(attach_dir="", cdp_endpoint="")
                _log("附着偏好已清除（下次 start 回到 managed 模式）")
                sys.exit(0)
            target = pos[0] if pos else ""
            if client.attach(target):
                sys.exit(0)
            sys.exit(2 if _needs_user_action(client) else 1)

        else:
            _err(f"未知子命令: {sub}")
            print("可用: status | start | run | check | reset | stop-session | sites | "
                  "find-url | attach | shutdown")
            sys.exit(1)


if __name__ == "__main__":
    main()
