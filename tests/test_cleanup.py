"""精确进程清理单测 — 身份核对 / 优雅优先 / 树杀兜底 / PID 复用防护。

全部用 monkeypatch 替身（不起真进程），覆盖 cleanup 决策链的每个分支。
运行：pytest tests/test_cleanup.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import cleanup  # noqa: E402


@pytest.fixture
def pid_file(isolated_home, tmp_path):
    isolated_home.mkdir(parents=True, exist_ok=True)
    path = isolated_home / "browser_agent.pid"

    def _write(pid=4321, port=7788):
        path.write_text(json.dumps({"pid": pid, "port": port,
                                    "service": "browser_agent"}), encoding="utf-8")
        return path
    return _write


def _no_http(monkeypatch):
    monkeypatch.setattr(cleanup, "_http_get_json", lambda *a, **k: None)


def _no_orphans(monkeypatch):
    """孤儿回收打桩：防测试真扫宿主 ps（副作用 + 不确定性）。"""
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans", lambda: [])


def test_no_pid_file_is_noop(isolated_home, monkeypatch, capsys):
    # isolated_home 必须：否则 cleanup 会打到真实 ~/.browser-agent，
    # 把开发者正在跑的服务连同浏览器杀掉（AGENTS.md 测试纪律）
    _no_orphans(monkeypatch)
    assert cleanup.cleanup() == 0
    assert "无 pid 记录" in capsys.readouterr().out


def test_dead_pid_just_removes_stale_file(pid_file, monkeypatch):
    path = pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: False)
    _no_orphans(monkeypatch)
    assert cleanup.cleanup() == 0
    assert not path.exists()


def test_pid_reuse_guard_refuses_foreign_process(pid_file, monkeypatch, capsys):
    """服务无响应且命令行不含本服务标记（PID 被复用）→ 宁可放过不可错杀。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    _no_http(monkeypatch)
    monkeypatch.setattr(cleanup, "_cmdline",
                        lambda pid: "/usr/sbin/some_daemon --pid 4321")
    assert cleanup.cleanup() == 0
    out = capsys.readouterr().out
    assert "不动它" in out


def test_graceful_path_terminates_nothing(pid_file, monkeypatch):
    """HTTP 身份确认 + 优雅关停成功 → 不发任何信号。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cleanup, "_http_get_json",
                        lambda port, path, timeout=2.0:
                        {"service": "browser_agent", "pid": 4321,
                         "instance_id": "X"})
    terminated = []
    monkeypatch.setattr(cleanup, "_terminate_tree",
                        lambda pid, force=False: terminated.append((pid, force)))
    monkeypatch.setattr(cleanup, "_graceful_shutdown", lambda port, status=None: True)
    monkeypatch.setattr(cleanup, "_wait_gone",
                        lambda pid, timeout: True)
    _no_orphans(monkeypatch)
    assert cleanup.cleanup() == 0
    assert terminated == []


def test_http_identity_mismatch_refuses(pid_file, monkeypatch, capsys):
    """端口上的服务 pid 与 pid 文件不符（实例已替换）→ 不动它。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cleanup, "_http_get_json",
                        lambda port, path, timeout=2.0:
                        {"service": "browser_agent", "pid": 9999})
    assert cleanup.cleanup() == 0
    assert "不动它" in capsys.readouterr().out


def test_unresponsive_server_gets_tree_termination_then_force(pid_file, monkeypatch):
    """挂死（优雅关停无响应）→ 进程组 SIGTERM → 仍活 → SIGKILL。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    _no_http(monkeypatch)
    monkeypatch.setattr(cleanup, "_cmdline",
                        lambda pid: "python -m browser_agent.server --port=7788")
    monkeypatch.setattr(cleanup, "_graceful_shutdown", lambda port, status=None: False)
    _no_orphans(monkeypatch)
    calls = []
    monkeypatch.setattr(cleanup, "_terminate_tree",
                        lambda pid, force=False: calls.append(force))
    monkeypatch.setattr(cleanup, "_wait_gone", lambda pid, timeout: False)
    assert cleanup.cleanup() == 0
    assert calls == [False, True]   # 先 TERM 后 KILL


def test_cmdline_marks_match():
    assert "browser_agent.server" in "python -m browser_agent.server --port=7788"
    assert any(m in "x/browser-agent-server --port=1" for m in cleanup._SERVER_MARKS)


# ── code-review 修复回归 ─────────────────────────────────

def test_corrupt_pid_file_never_kills(isolated_home, monkeypatch, capsys):
    """pid/port 不可解析的坏文件：删除文件退出，绝不据此杀进程。"""
    import os as _os
    bad = cleanup._pid_file()
    _os.makedirs(_os.path.dirname(bad), exist_ok=True)
    open(bad, "w").write('{"pid": "not-a-number"}')
    killed = []
    monkeypatch.setattr(cleanup, "_terminate_tree",
                        lambda pid, force=False: killed.append(pid))
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans", lambda: [])
    assert cleanup.cleanup() == 0
    assert killed == [] and "损坏" in capsys.readouterr().out
    assert not _os.path.exists(bad)


def test_missing_port_refuses_to_kill(pid_file, monkeypatch, capsys):
    """pid 活着但文件缺 port：无法做 HTTP 身份核对 → 拒绝杀（宁可放过）。"""
    pid_file(pid=4321)
    # 去掉 port 字段
    import json as _json
    path = cleanup._pid_file()
    info = _json.loads(open(path).read())
    del info["port"]
    open(path, "w").write(_json.dumps(info))
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(cleanup, "_terminate_tree",
                        lambda pid, force=False: killed.append(pid))
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans", lambda: [])
    assert cleanup.cleanup() == 0
    assert killed == []
    assert "宁可放过" in capsys.readouterr().out


def test_local_http_bypasses_proxy_env(pid_file, monkeypatch):
    """http_proxy 环境下 127.0.0.1 探测必须绕过代理（否则身份核对被代理劫持）。"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        assert cleanup._http_get_json(port, "/nope") is None  # 连上但立刻断开，不超时


def test_reap_playwright_orphans_targets_only_ppid1(monkeypatch):
    """孤儿回收只动「ppid==1 + playwright 特征」：活项目的实例（有父）绝不碰。"""
    fake_ps = "\n".join([
        "  100     1 /var/folders/xx/playwright_chromiumdev_profile-1/Chrome --user-data-dir=/var/folders/xx/playwright_chromiumdev_profile-1 --type=  # 孤儿 Chromium → 回收",
        "  101    99 node /x/playwright/driver/package/lib/cli.js run-driver      # 有父 driver（活项目）→ 不碰",
        "  102     1 node /y/playwright/driver/package/lib/server.js run-driver   # 孤儿 driver → 回收",
        "  103     1 /usr/sbin/some_daemon --pid 1                                 # init 之子但无特征 → 不碰",
    ])
    import subprocess as _sp
    monkeypatch.setattr(cleanup, "os", __import__("os"))  # 保持 os.kill 可打桩
    killed = []
    monkeypatch.setattr(cleanup.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(_sp, "run", lambda *a, **k: type("R", (), {"stdout": fake_ps})())
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": fake_ps})())
    reaped = cleanup._reap_playwright_orphans()
    assert sorted(pid for pid, _ in reaped) == [100, 102]
    assert killed == [100, 102]


# ── 孤儿回收时机（覆盖 driver 先死于 server 的漏网窗口）─────────

def test_no_pid_file_still_reaps_orphans(isolated_home, monkeypatch, capsys):
    """无 pid 记录（服务本就没跑）：孤儿只可能是历史残留，同样回收。
    isolated_home 必须：否则读到真实 pid 文件会去关停开发者正在跑的服务。"""
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans",
                        lambda: [(555, "chromium --user-data-dir=/x/playwright_y")])
    assert cleanup.cleanup() == 0
    out = capsys.readouterr().out
    assert "顺带回收 1 个孤儿" in out


def test_graceful_path_reaps_orphans(pid_file, monkeypatch, capsys):
    """优雅关停成功出口回收孤儿：driver 先死于 server（OOM/-9）时，孤儿
    Chromium 脱离 server 关闭管辖——此前该路径从不回收，孤儿永久堆积。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cleanup, "_http_get_json",
                        lambda port, path, timeout=2.0:
                        {"service": "browser_agent", "pid": 4321, "instance_id": "X"})
    monkeypatch.setattr(cleanup, "_graceful_shutdown", lambda port, status=None: True)
    monkeypatch.setattr(cleanup, "_wait_gone", lambda pid, timeout: True)
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans",
                        lambda: [(777, "node /p/driver/cli.js run-driver")])
    assert cleanup.cleanup() == 0
    assert "顺带回收 1 个孤儿" in capsys.readouterr().out


def test_identity_mismatch_skips_orphan_reap(pid_file, monkeypatch):
    """端口上是别人的服务（身份不符不动它）：别人的进程树连孤儿归别人管，不扫。"""
    pid_file(pid=4321)
    monkeypatch.setattr(cleanup, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cleanup, "_http_get_json",
                        lambda port, path, timeout=2.0:
                        {"service": "browser_agent", "pid": 9999})
    reaps = []
    monkeypatch.setattr(cleanup, "_reap_playwright_orphans",
                        lambda: reaps.append(1) or [])
    assert cleanup.cleanup() == 0
    assert reaps == []
