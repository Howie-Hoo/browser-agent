#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""残留进程精确清理（跨平台）— 只清本服务自拉的进程树，绝不做进程名全局查杀。

为什么禁用 `pkill -f` 类全局查杀：
- `playwright/driver` 这样的模式会命中机器上**所有项目**的 Playwright 实例；
- 本服务的定位是多会话共享，按名查杀会杀掉其他 agent 会话正在用的服务与在途命令批；
- attach 模式断连后重连需用户在浏览器重新授权，误杀的代价远不止重启。

清理对象按**身份三重核对**锁定（防 PID 复用误杀无辜进程）：
1. pid 文件（~/.browser-agent/browser_agent.pid）记录的 pid + port；
2. HTTP /status 确认 service=browser_agent 且 pid 一致（服务还响应时）；
3. 进程命令行包含 browser_agent.server / browser-agent-server（服务无响应时的兜底；
   Windows 取不到命令行，退化为 pid 存在性核对）。

顺序：优雅关停（带 instance_id 的 POST /shutdown）→ 等待退出 → 仍在则对
进程组 SIGTERM（服务以独立进程组启动：server → playwright driver → Chromium
整棵树同组）→ 仍在则 SIGKILL。Windows 用 `taskkill /T` 树杀。
幂等：无残留即空操作；清理入口始终退出 0（它只是兜底，不该成为新的失败源）。

孤儿回收时机（判据见 _reap_playwright_orphans）：所有「机器上已无本服务」的
出口（无 pid 记录 / 记录已死 / 刚关停成功 / pid 文件损坏）统一扫——残留的
孤儿化 playwright 只能是历史遗留或本次关停漏网；「不动它」类出口（端口上是
别人的服务）不扫，别人的进程树归别人管。

--orphans-only：兜底模式（会话收尾 hook 用）——绝不关停活服务（服务是机器级
多会话共享单例，hook 无「谁拉起」事实），只回收孤儿 + 清已死的 pid 文件。

用法：browser-agent-cleanup（pip 安装后）或 python -m browser_agent.cleanup
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

from .utils import SERVER_DRAIN_SEC, is_pid_alive, read_json
from .user_data import PID_FILE_NAME, home_root
from . import SERVICE_NAME as _SERVICE_NAME

_SERVER_MARKS = ("browser_agent.server", "browser-agent-server")
# 优雅关停等待 = 服务排水上限 + 落盘/退出余量（与 server._do_shutdown 同源推导）
_GRACEFUL_WAIT_SEC = SERVER_DRAIN_SEC + 2.0
_TERM_WAIT_SEC = 5.0

# 本机回环请求必须绕过任何代理环境变量（http_proxy 等）：走代理探测 127.0.0.1
# 会把身份核对降级成命令行核对、把优雅关停降级成强杀
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _pid_file() -> str:
    # 纯读路径（home_root 无副作用）：清理工具不得触发目录创建/迁移
    return os.path.join(home_root(), PID_FILE_NAME)


def _read_pid_info() -> dict | None:
    info = read_json(_pid_file(), None)
    return info if info and info.get("pid") else None


def _http_get_json(port: int, path: str, timeout: float = 2.0):
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _cmdline(pid: int) -> str:
    """进程完整命令行（拿不到返回空串）。Linux 读 /proc，其余走 ps。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _is_our_server(pid: int, port: int) -> tuple[bool, str]:
    """身份核对：返回 (是否本服务, 结论说明)。宁可放过不可错杀。"""
    status = _http_get_json(port, "/status")
    if status is not None:
        if status.get("service") == _SERVICE_NAME and int(status.get("pid") or 0) == pid:
            return True, "HTTP /status 身份确认", status
        return False, f"端口 {port} 上的服务与 pid 文件不符（已被替换？），不动它", None
    # 服务不响应（挂死/半死）——退回命令行核对
    if os.name == "nt":
        return is_pid_alive(pid), "Windows 无法核对命令行，按 pid 存在性处理", None
    cmdline = _cmdline(pid)
    if any(mark in cmdline for mark in _SERVER_MARKS):
        return True, "进程命令行确认", None
    return False, f"pid {pid} 的命令行不含本服务标记（PID 可能已被复用），不动它", None


def _graceful_shutdown(port: int, status: dict | None = None) -> bool:
    """带 instance_id 的优雅关停（与 client.shutdown 同一语义）。

    status：身份核对阶段已取到的 /status 响应（同一 pid 刚验证过，重取只会
    引入换实例的歧义）；缺省（无响应路径）时才自己取。
    """
    if status is None:
        status = _http_get_json(port, "/status")
    if status is None:
        return False
    body = json.dumps({"instance_id": status.get("instance_id", "")}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/shutdown", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _OPENER.open(req, timeout=5):
            return True
    except Exception:
        return False


def _terminate_tree(pid: int, force: bool = False) -> None:
    """对整棵进程树发终止信号。POSIX 用进程组（服务以独立进程组启动）。"""
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        pgid = os.getpgid(pid)
        if pgid == pid:
            os.killpg(pgid, sig)   # 独立进程组：整树一次带走
            return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.3)
    return not is_pid_alive(pid)


def _drop_stale_pid_file() -> None:
    try:
        os.remove(_pid_file())
    except OSError:
        pass


def _reap_and_report() -> None:
    """扫孤儿 playwright 进程并汇报（无孤儿静默；判据宁可放过不可错杀）。"""
    orphans = _reap_playwright_orphans()
    if orphans:
        _say(f"顺带回收 {len(orphans)} 个孤儿 playwright 进程（父链已断）：")
        for opid, cmd in orphans:
            _say(f"  - {opid}: {cmd}")


def _reap_and_drop() -> int:
    """「机器上已无本服务」出口的统一收尾：回收孤儿 + 清 pid 文件。"""
    _reap_and_report()
    _drop_stale_pid_file()
    return 0


def _reap_playwright_orphans() -> list:
    """回收孤儿化的 playwright 子进程（服务被 SIGKILL 后残留的 Chromium/driver）。

    判定收窄到「**父进程已是 init**（ppid==1）+ playwright 特征」：活着的
    playwright 一定有活的祖先链（driver → 其调用方），被 init 收养只可能是
    父链已断的残留——不会命中其他项目正在跑的实例（与全局 pkill 的本质
    区别）。仅 POSIX；识别不了的就留着（宁可放过）。
    """
    if os.name == "nt":
        return []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    reaped = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or parts[1] != "1":
            continue
        cmd = parts[2]
        orphan_chromium = "playwright" in cmd and "--user-data-dir=" in cmd
        orphan_driver = "playwright" in cmd and ("/driver" in cmd or "driver" in cmd.split(" ")[0])
        if not (orphan_chromium or orphan_driver):
            continue
        try:
            pid = int(parts[0])
            os.kill(pid, signal.SIGTERM)
            reaped.append((pid, cmd[:80]))
        except OSError:
            pass
    return reaped


def _say(msg: str) -> None:
    print(f"[清理] {msg}", flush=True)


def cleanup(orphans_only: bool = False) -> int:
    info = _read_pid_info()
    if orphans_only:
        # 兜底模式：绝不关停活服务；服务状态不明时同样只动确定的孤儿
        if info:
            try:
                pid = int(info["pid"])
            except (KeyError, TypeError, ValueError):
                pid = 0
            if not (pid > 0 and is_pid_alive(pid)):
                _say("无活服务记录，回收孤儿并清理 pid 文件")
                return _reap_and_drop()
        _say("服务在跑（或状态不明）：只回收孤儿，不动服务")
        _reap_and_report()
        return 0

    if not info:
        _say("无 pid 记录，无需清理")
        return _reap_and_drop()  # 机器无本服务：孤儿只可能是历史残留
    try:
        pid = int(info["pid"])
        port = int(info.get("port") or 0)
    except (KeyError, TypeError, ValueError):
        _say("pid 文件损坏（pid/port 不可解析）——删除后退出，不据此杀任何进程")
        return _reap_and_drop()

    if not is_pid_alive(pid):
        # 服务本体已死（SIGKILL/OOM 崩溃）：pid 文件之外，它的 playwright
        # 子进程可能成了孤儿（父链断、被 init 收养）——顺带回收，这正是
        # 清理工具存在的理由；识别不了的残留宁可留着
        _say(f"pid {pid} 已退出，清理 stale pid 文件")
        return _reap_and_drop()

    if not port:
        # 我们自己写的 pid 文件必有 port；缺 port = 手改/旧版/半写坏文件。
        # 没有端口就无法做 HTTP 身份核对——绝不凭裸 pid 杀整组（PID 复用
        # 会误杀无关进程），宁可放过
        _say("pid 文件缺 port，无法核对服务身份，跳过（宁可放过不可错杀）；"
             "确认无残留后可手动删除该 pid 文件")
        return 0

    ours, why, status = _is_our_server(pid, port)
    if not ours:
        _say(f"跳过：{why}")
        return 0

    _say(f"目标: pid {pid}（端口 {port}，{why}）")
    if _graceful_shutdown(port, status):
        _say("已请求优雅关停（排水 → 关浏览器 → 退出），等待退出…")
        if _wait_gone(pid, _GRACEFUL_WAIT_SEC):
            _say("已退出 ✓")
            # 优雅路径无进程组信号兜底：driver 先死于 server（OOM/-9）时，
            # 孤儿 Chromium 脱离 server 关闭管辖，收尾统一扫
            return _reap_and_drop()
        _say("优雅关停未在期限内完成，升级为进程树终止")
    else:
        _say("服务无响应，直接进程树终止")

    _terminate_tree(pid)
    if _wait_gone(pid, _TERM_WAIT_SEC):
        _say("已退出 ✓")
        return _reap_and_drop()
    _terminate_tree(pid, force=True)
    _say("已强制终止（SIGKILL / taskkill /F）")
    return _reap_and_drop()


def main(argv: list | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    return cleanup(orphans_only="--orphans-only" in args)


if __name__ == "__main__":
    sys.exit(main())
