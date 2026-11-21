"""user_data 单测 — 用户数据目录解析 / 一次性迁移（只复制不覆盖）/ 标记幂等。

全部用例在临时 BROWSER_AGENT_HOME 下运行，绝不触碰真实 ~/.browser-agent 与包内数据。
运行：pytest tests/test_user_data.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import user_data  # noqa: E402





@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """伪造的旧版包内 storage/（迁移来源）。"""
    pkg_storage = tmp_path / "pkg" / "storage"
    repo_storage = tmp_path / "pkg" / "repo-storage"
    (pkg_storage / "profiles").mkdir(parents=True)
    (pkg_storage / "profiles" / "a.json").write_text('{"name":"a"}', encoding="utf-8")
    (pkg_storage / "state.json").write_text("{}", encoding="utf-8")
    (repo_storage / "downloads").mkdir(parents=True)
    (repo_storage / "downloads" / "f.bin").write_bytes(b"x")
    monkeypatch.setattr(user_data, "_LEGACY_PKG_STORAGE", str(pkg_storage))
    monkeypatch.setattr(user_data, "_LEGACY_REPO_STORAGE", str(repo_storage))
    return pkg_storage


def test_data_dir_respects_env(isolated_home):
    assert user_data.data_dir() == str(isolated_home)
    assert os.path.isdir(str(isolated_home))


def test_subdirs_under_home(isolated_home):
    assert user_data.profiles_dir() == os.path.join(str(isolated_home), "profiles")
    assert user_data.site_patterns_dir() == os.path.join(str(isolated_home), "site-patterns")
    assert user_data.downloads_dir() == os.path.join(str(isolated_home), "downloads")
    assert user_data.state_file() == os.path.join(str(isolated_home), "state.json")
    assert user_data.cookies_file() == os.path.join(str(isolated_home), "cookies.json")


def test_migration_copies_legacy_data(isolated_home, legacy):
    user_data.data_dir()  # 触发一次性迁移
    assert (isolated_home / "profiles" / "a.json").read_text(encoding="utf-8") == '{"name":"a"}'
    assert (isolated_home / "state.json").exists()
    assert (isolated_home / "downloads" / "f.bin").exists()
    # 旧位置只读保留（只复制、不删除）
    assert (legacy / "profiles" / "a.json").exists()
    # 标记已写
    assert (isolated_home / user_data.MIGRATED_MARKER).exists()


def test_migration_never_overwrites(isolated_home, legacy):
    (isolated_home / "profiles").mkdir(parents=True)
    (isolated_home / "profiles" / "a.json").write_text('{"name":"newer"}', encoding="utf-8")
    user_data.data_dir()
    assert (isolated_home / "profiles" / "a.json").read_text(encoding="utf-8") == '{"name":"newer"}'


def test_migration_runs_once(isolated_home, legacy):
    user_data.data_dir()
    marker_mtime = os.path.getmtime(str(isolated_home / user_data.MIGRATED_MARKER))
    # 迁移后旧位置新增文件不再被搬（标记幂等，不再扫描）
    (legacy / "profiles" / "b.json").write_text('{"name":"b"}', encoding="utf-8")
    user_data.data_dir()
    assert not (isolated_home / "profiles" / "b.json").exists()
    assert os.path.getmtime(str(isolated_home / user_data.MIGRATED_MARKER)) == marker_mtime


def test_no_legacy_sources_is_noop(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setattr(user_data, "_LEGACY_PKG_STORAGE", str(tmp_path / "absent-a"))
    monkeypatch.setattr(user_data, "_LEGACY_REPO_STORAGE", str(tmp_path / "absent-b"))
    user_data.data_dir()
    assert (isolated_home / user_data.MIGRATED_MARKER).exists()
    assert not (isolated_home / "profiles").exists()


# ── pid 文件落位（运行态不写包目录）─────────────────────────

def test_legacy_pid_file_removed_once(isolated_home, monkeypatch, tmp_path):
    """包目录里的旧 pid 文件在首次 data_dir() 时被清理，且只查一次。"""
    legacy = tmp_path / "browser_agent.pid"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(user_data, "_LEGACY_PID_FILE", str(legacy))
    monkeypatch.setattr(user_data, "_legacy_pid_checked", False)
    user_data.data_dir()
    assert not legacy.exists()
    # 二次调用不再探测：重新放一个文件不会被删
    legacy.write_text("{}", encoding="utf-8")
    user_data.data_dir()
    assert legacy.exists()


def test_pid_file_respects_isolated_home(isolated_home):
    """pid_file() 惰性求值——只在调用时才解析（且跟随 BROWSER_AGENT_HOME）。"""
    path = user_data.pid_file()
    assert path.startswith(str(isolated_home))
