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
  browser-agent-client shutdown

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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(_PKG_DIR, "browser_agent.pid")
_SERVICE_NAME = "browser_agent"


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
    try:
        with open(_PID_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


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
        if self.is_running():
            _log("服务已在运行")
            return True

        # 资源预检 fail-fast：低配机器余量不足时拒绝拉起 Chromium，防整机被压垮
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
        ]
        if self.headless:
            cmd.append("--headless")

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL

        subprocess.Popen(cmd, env=_server_env(), **kwargs)

        for i in range(60):
            time.sleep(0.5)
            if self.is_running():
                _log(f"服务就绪 ✓ ({(i+1)*0.5:.1f}s)")
                return True
        _err("服务启动超时（30s）")
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
            if _is_pid_alive(pid_info["pid"]):
                # 测试模式下不再试图自杀重启
                pass

        if not self.auto_start:
            return True # 在不自启动的调试模式下，始终放行进行 HTTP 尝试

        return self.start_service()

    # ── 执行 ──────────────────────────────────────────────

    def run(self, commands: Union[list, dict, str], compact: bool = False, report: bool = True) -> dict:
        """
        发送命令批次，返回富报告。命令路由到 self.session_id 对应的隔离会话。

        返回字段：ok, session_id, executed_at, steps, screenshots, errors, summary, report_file
        compact=True 时精简响应（不含完整 data 和截图路径）。
        report=False 时跳过报告文件生成。
        """
        commands = self._parse_commands(commands)
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行", "steps": [], "screenshots": [], "errors": []}

        _log(f"发送 {len(commands)} 条命令...")
        try:
            r = self._http.post(
                f"{self._base}/run",
                json={"commands": commands, "compact": compact, "report": report,
                      "session_id": self.session_id, "owner": self.owner},
            )
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

    def session_start(self, url: str = "") -> dict:
        """启动/获取本客户端的隔离会话（多任务共享服务时各占一个 context）。"""
        if not self.ensure_service_running():
            return {"ok": False, "error": "服务未运行"}
        payload = {
            "session_id": self.session_id,
            "owner": self.owner,
            "base_url": (url or self.base_url).rstrip("/"),
            "headless": self.headless,
            "slow_mo": self.slow_mo,
        }
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

    def shutdown(self) -> bool:
        """优雅关闭服务（浏览器 + HTTP 服务）。"""
        if not self.is_running():
            _log("服务未运行")
            return True
        _log("关闭服务...")
        try:
            r = self._http.post(f"{self._base}/shutdown", json={}, timeout=15)
            return r.json().get("ok", False)
        except Exception:
            return False

    def restart_service(self) -> bool:
        """关闭并重启整个服务（用于切换 headless/有头模式等实例级配置）。"""
        if not self.is_running():
            return self.start_service()
        _log("重启服务...")
        try:
            self._http.post(f"{self._base}/shutdown", json={}, timeout=15)
        except Exception:
            pass
        time.sleep(1)
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
def _parse_args(args):
    sub = args[0] if args else "status"
    pos, opts = [], {"port": 7788, "url": "http://localhost:3000", "headless": False, "slow_mo": 200}
    for a in args[1:]:
        if a == "--headless":
            opts["headless"] = True; opts["slow_mo"] = 0
        elif a.startswith("--port="):
            opts["port"] = int(a.split("=")[1])
        elif a.startswith("--url="):
            opts["url"] = a.split("=", 1)[1]
        elif a.startswith("--slow-mo="):
            opts["slow_mo"] = int(a.split("=")[1])
        elif a.startswith("http://") or a.startswith("https://"):
            opts["url"] = a
        elif not a.startswith("-"):
            pos.append(a)
    return sub, pos, opts


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
            sys.exit(0 if client.start_service() else 1)

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

        else:
            _err(f"未知子命令: {sub}")
            print("可用: status | start | run | check | shutdown | reset")
            sys.exit(1)


if __name__ == "__main__":
    main()
