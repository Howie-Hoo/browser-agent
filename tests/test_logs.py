"""全生命周期日志单测 — 落文件 / 幂等 / 未激活 no-op / 路径无副作用。
运行：pytest tests/test_logs.py -q
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import logs  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_logger():
    """日志器是模块级单例：每个用例后摘除全部 handler 并还原，避免跨用例串写。"""
    saved = logs._LOGGER
    yield
    logger = logging.getLogger("browser_agent")
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)
    logs._LOGGER = saved


def test_log_path_under_isolated_home(isolated_home):
    assert logs.log_path() == str(isolated_home / "logs" / "server.log")


def test_log_path_has_no_side_effects(isolated_home):
    logs.log_path()
    assert not (isolated_home / "logs").exists()   # 不建目录不迁移


def test_log_before_activate_is_noop(isolated_home):
    logs._LOGGER = None
    logs.log("不应落盘")
    assert not (isolated_home / "logs").exists()


def test_activate_creates_file_and_writes(isolated_home):
    logs._LOGGER = None
    path = logs.activate()
    assert path == str(isolated_home / "logs" / "server.log")
    logs.log("启动事件", "info")
    logs.log("警告事件", "warning")
    for h in logs._LOGGER.handlers:
        h.flush()
    content = open(path, encoding="utf-8").read()
    assert "启动事件" in content and "WARNING" in content and "警告事件" in content


def test_activate_idempotent(isolated_home):
    from logging.handlers import RotatingFileHandler
    logs._LOGGER = None
    first = logs.activate()
    second = logs.activate()
    assert first == second == logs.log_path()
    # 不叠加文件 handler（重复 activate 不产生第二个滚动文件）
    file_handlers = [h for h in logs._LOGGER.handlers
                     if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1 and file_handlers[0].baseFilename == logs.log_path()


def test_level_from_env(isolated_home, monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_LOG_LEVEL", "debug")
    logs._LOGGER = None
    logs.activate()
    assert logs._LOGGER.level == logging.DEBUG


def test_bad_level_env_falls_back_info(isolated_home, monkeypatch):
    """坏日志级别不致命（回溯 activate 承诺）：未知值回落 INFO 而非炸启动。"""
    monkeypatch.setenv("BROWSER_AGENT_LOG_LEVEL", "VERBOSE")
    logs._LOGGER = None
    logs.activate()   # 不抛异常即通过
    assert logs._LOGGER.level == logging.INFO
