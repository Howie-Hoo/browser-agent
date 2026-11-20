"""共享测试设施：用户数据目录隔离。

所有需要触碰 ~/.browser-agent 语义的用例统一从这里拿隔离 HOME，
绝不在测试里读写真实用户数据。"""
import os

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """隔离的 BROWSER_AGENT_HOME 根目录（返回路径；环境变量已指向它）。"""
    home = tmp_path / "home"
    monkeypatch.setenv("BROWSER_AGENT_HOME", str(home))
    return home
