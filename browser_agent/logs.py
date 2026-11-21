#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务全生命周期日志 — 滚动文件，覆盖启动/浏览器/会话/命令批/回收/关停全链路。

设计约束：
- **单写者**：只有服务进程 activate()；client 等其他进程里 log() 是 no-op
  （两个进程共写一个 RotatingFileHandler 会在滚动时互相截断）。
- **无副作用**：log_path() 只拼路径不建目录、不触发用户数据迁移
  （因此不能复用 user_data.data_dir()，路径规则与它保持一致）。
- **不落凭证**：日志只记事件与元数据（动作名/域名/会话名），凭证只落文件
  是全仓不变式，日志同样不得例外。
"""
from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler

_LOCK = threading.Lock()
_LOGGER: logging.Logger | None = None

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 7


def log_path() -> str:
    """日志文件路径（无副作用；根路径规则复用 user_data.home_root 单点）。"""
    # 惰性导入：模块级 import 会成环（user_data → utils → logs）
    from .user_data import home_root
    return os.path.join(home_root(), "logs", "server.log")


def activate() -> str:
    """服务进程专属：初始化滚动文件日志（幂等），返回日志文件路径。

    级别由 BROWSER_AGENT_LOG_LEVEL 控制（默认 INFO，DEBUG 可看全部细节）。
    失败不致命：目录不可写时退化为仅 stderr，服务照常运行。
    """
    global _LOGGER
    with _LOCK:
        if _LOGGER is not None:
            return log_path()
        logger = logging.getLogger("browser_agent")
        level = os.getenv("BROWSER_AGENT_LOG_LEVEL", "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"  # 坏值不致命（回溯 activate 的"失败不致命"承诺）
        logger.setLevel(level)
        logger.propagate = False
        # 只挂文件 handler：stdout 已由调用方各自的 print 覆盖（前台直跑可见，
        # detach 时由文件兜底），再加 stderr 会造成前台双份日志
        try:
            os.makedirs(os.path.dirname(log_path()), exist_ok=True)
            fh = RotatingFileHandler(log_path(), maxBytes=MAX_BYTES,
                                     backupCount=BACKUP_COUNT, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(fh)
        except OSError:
            pass  # 目录建不出来：日志退化，服务照常运行
        _LOGGER = logger
    # 锁外返回：路径与是否可写无关（目录不可写时也已退化，路径仍用于提示）
    return log_path()


def log(msg: str, level: str = "info") -> None:
    """写一条生命周期日志；未 activate（client 进程等）时静默跳过。"""
    logger = _LOGGER
    if logger is None:
        return
    getattr(logger, level, logger.info)(msg)
