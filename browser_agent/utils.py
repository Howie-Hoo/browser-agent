#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BrowserProxy 辅助工具 — log / emit / 系统资源预检。
独立模块，避免循环导入。
"""
from __future__ import annotations

import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str):
    print(f"[代理] {msg}", flush=True)


def emit(data: dict):
    print(f"[结果] {json.dumps(data, ensure_ascii=False)}", flush=True)


# ── 系统资源预检（启动浏览器前 fail-fast）──────────────────
# Chromium 单实例常驻数百 MB；低配机器上叠被测应用可能把内存打穿导致整机卡死。
# client（start_service）与 server（ensure_proxy）两侧都调用，防绕过。

DEFAULT_MIN_FREE_MB = 768


def _host_mem_available_mb() -> float | None:
    """宿主可用内存（MB）；Linux 走 /proc/meminfo，其余平台尝试 psutil，都不可得返回 None。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import psutil  # type: ignore

        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _cgroup_mem_available_mb() -> float | None:
    """cgroup v2 限额内余量（MB）；不在容器或读不到返回 None。宿主与容器取较小者才是真实余量。"""
    try:
        with open("/sys/fs/cgroup/memory.max", encoding="ascii") as f:
            limit = f.read().strip()
        if limit == "max":
            return None
        with open("/sys/fs/cgroup/memory.current", encoding="ascii") as f:
            used = int(f.read().strip())
        return (int(limit) - used) / (1024 * 1024)
    except (OSError, ValueError):
        return None


def check_system_resources() -> tuple[bool, str]:
    """是否允许启动浏览器。返回 (ok, 说明)。

    - 内存：宿主与 cgroup v2 可用量的较小者 < BROWSER_AGENT_MIN_FREE_MB（默认 768）即拒绝
    - 负载：设置 BROWSER_AGENT_MAX_LOAD 时检查 1min loadavg（默认不检查）
    - 平台不支持且无 psutil 时跳过（保持可用性优先）
    """
    try:
        min_free_mb = float(os.getenv("BROWSER_AGENT_MIN_FREE_MB", str(DEFAULT_MIN_FREE_MB)))
    except ValueError:
        min_free_mb = float(DEFAULT_MIN_FREE_MB)
    candidates = [v for v in (_host_mem_available_mb(), _cgroup_mem_available_mb()) if v is not None]
    if not candidates:
        return True, "资源预检跳过（无法读取内存信息）"
    available = min(candidates)
    if available < min_free_mb:
        return False, (
            f"可用内存 {available:.0f}MB 低于阈值 {min_free_mb:.0f}MB，拒绝启动浏览器"
            "（可用 BROWSER_AGENT_MIN_FREE_MB 调整阈值，或先腾出内存 / 改跑已部署环境）"
        )
    max_load = os.getenv("BROWSER_AGENT_MAX_LOAD", "")
    if max_load:
        try:
            if os.getloadavg()[0] > float(max_load):
                return False, f"系统负载 {os.getloadavg()[0]:.2f} 超过 BROWSER_AGENT_MAX_LOAD={max_load}，拒绝启动浏览器"
        except (OSError, ValueError):
            pass
    return True, f"资源预检通过（可用内存 {available:.0f}MB）"


# ── 报告产物目录 ──────────────────────────────────────────
# BROWSER_AGENT_REPORT_DIR 覆盖；默认落在仓库根 temp/（包随 pip 装进
# site-packages 时退回当前工作目录，不向 site-packages 写文件）
def report_dir() -> str:
    override = os.getenv("BROWSER_AGENT_REPORT_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    if "site-packages" in pkg_dir:
        d = os.path.join(os.getcwd(), "temp")
    else:
        d = os.path.join(os.path.dirname(pkg_dir), "temp")
    os.makedirs(d, exist_ok=True)
    return d
