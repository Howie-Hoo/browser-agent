#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户数据目录（~/.browser-agent/）— 与代码安装位置解耦的持久数据层。

profiles（登录态）、site-patterns（站点经验）、state.json（会话状态）、
downloads 全部落在用户目录：pip 重装 / 升级 / 卸载都不会触碰这些数据。
（数据放在包目录里，包管理器升级即丢——用户数据必须与代码安装位置解耦。）

BROWSER_AGENT_HOME 可覆盖根目录（测试隔离用）。

首次访问时自动执行一次性迁移：把旧版落在包目录 storage/ 里的数据
只复制、不覆盖地搬过来（完成后写 .migrated 标记，此后不再扫描旧位置；
旧位置只读保留，不删除——回滚安全）。
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime

from .utils import log

MIGRATED_MARKER = ".migrated"

# 旧版数据位置（包目录内 / 仓库根 storage/）——只作一次性迁移来源
_LEGACY_PKG_STORAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
_LEGACY_REPO_STORAGE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage")
)
# 旧版把 pid 文件写在包目录——运行态必须落用户目录，顺手清掉旧文件
_LEGACY_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_agent.pid")
_legacy_pid_checked = False


def data_dir() -> str:
    """用户数据根目录（确保存在 + 一次性迁移已执行）。"""
    root = os.getenv("BROWSER_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".browser-agent"
    )
    _ensure(root)
    return root


def profiles_dir() -> str:
    return os.path.join(data_dir(), "profiles")


def site_patterns_dir() -> str:
    return os.path.join(data_dir(), "site-patterns")


def downloads_dir() -> str:
    return os.path.join(data_dir(), "downloads")


def state_file() -> str:
    return os.path.join(data_dir(), "state.json")


def pid_file() -> str:
    """服务 pid 文件（运行态与用户数据同落 ~/.browser-agent/——永不写包目录，
    pip 重装不留 stale pid）。惰性语义由 data_dir() 保证：只在调用时解析，
    import 期不触碰用户目录（测试的 BROWSER_AGENT_HOME 隔离晚于模块导入）。"""
    return os.path.join(data_dir(), "browser_agent.pid")


def cookies_file() -> str:
    return os.path.join(data_dir(), "cookies.json")


# ── 一次性迁移 ──────────────────────────────────────────────
def _legacy_items() -> list:
    """旧位置 → 新名。独立函数便于测试注入。"""
    return [
        (os.path.join(_LEGACY_PKG_STORAGE, "profiles"), "profiles"),
        (os.path.join(_LEGACY_PKG_STORAGE, "state.json"), "state.json"),
        (os.path.join(_LEGACY_PKG_STORAGE, "cookies.json"), "cookies.json"),
        (os.path.join(_LEGACY_REPO_STORAGE, "downloads"), "downloads"),
    ]


def _copy_tree_no_overwrite(src: str, dst: str) -> int:
    """目录级「只复制、不覆盖」（cp -Rn 语义）。返回复制的文件数。"""
    copied = 0
    for base, _dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in files:
            target = os.path.join(target_dir, fn)
            if os.path.exists(target):
                continue
            shutil.copy2(os.path.join(base, fn), target)
            copied += 1
    return copied


def _migrate_legacy(root: str) -> list:
    moved = []
    for src, name in _legacy_items():
        if not os.path.exists(src):
            continue
        dst = os.path.join(root, name)
        try:
            if os.path.isdir(src):
                if _copy_tree_no_overwrite(src, dst) > 0:
                    moved.append(name + "/")
            elif not os.path.exists(dst):
                shutil.copy2(src, dst)
                moved.append(name)
        except OSError as e:
            # 迁移失败不阻断服务：新位置照常工作，旧数据原地保留可手动搬
            log(f"迁移 {name} 失败（跳过，旧数据保留在 {src}）: {e}")
    return moved


def _remove_legacy_pid_once() -> None:
    """清理包目录里的旧 pid 文件（新位置在用户数据目录；每次进程只查一次）。"""
    global _legacy_pid_checked
    if _legacy_pid_checked:
        return
    _legacy_pid_checked = True
    try:
        os.remove(_LEGACY_PID_FILE)
    except OSError:
        pass


def _ensure(root: str) -> None:
    _remove_legacy_pid_once()
    marker = os.path.join(root, MIGRATED_MARKER)
    if os.path.exists(marker):
        return
    try:
        os.makedirs(root, exist_ok=True)
        moved = _migrate_legacy(root)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"migrated: {datetime.now().isoformat(timespec='seconds')}\n")
        if moved:
            log(f"用户数据已一次性迁移到 {root}: {', '.join(moved)}（旧位置只读保留）")
    except OSError as e:
        # 根目录建不出来（如只读文件系统）：不让服务崩，调用方写文件时自然会报错
        log(f"用户数据目录初始化异常（继续运行）: {e}")
