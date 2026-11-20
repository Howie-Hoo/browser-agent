"""browser_attach 单测 — 浏览器发现 / DevToolsActivePort 读取 / 端点解析（硬错不降级）/ 偏好持久化。

用临时目录伪造浏览器数据目录（Local State + DevToolsActivePort），
不连接任何真实浏览器、不碰真实用户数据。
运行：pytest tests/test_browser_attach.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import browser_attach as ba  # noqa: E402


@pytest.fixture
def home(isolated_home, monkeypatch):
    monkeypatch.delenv("BROWSER_AGENT_ATTACH_DIR", raising=False)
    monkeypatch.delenv("BROWSER_AGENT_CDP_ENDPOINT", raising=False)
    # 隔离发现逻辑：默认根指向临时目录，绝不去读真机浏览器目录
    monkeypatch.setattr(ba, "default_roots", lambda: [])
    return isolated_home


def _install_fake_roots(monkeypatch, tmp_path, spec: dict):
    """在 tmp 下伪造若干浏览器并把发现根指过去。spec: {名: 端口或 None}"""
    roots = []
    for name, port in spec.items():
        _fake_browser(tmp_path, name, port=port)
        roots.append((name, str(tmp_path / name)))
    monkeypatch.setattr(ba, "default_roots", lambda: roots)


def _fake_browser(base: str, name: str, port=None, with_local_state=True):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if with_local_state:
        (d / "Local State").write_text("{}", encoding="utf-8")
    if port is not None:
        (d / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n", encoding="ascii")
    return d


# ── 发现与端口读取 ───────────────────────────────────────

def test_is_browser_data_dir(tmp_path):
    d = _fake_browser(tmp_path, "chrome")
    assert ba.is_browser_data_dir(str(d)) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ba.is_browser_data_dir(str(plain)) is False
    assert ba.is_browser_data_dir(str(tmp_path / "absent")) is False


def test_read_devtools_port(tmp_path):
    d = _fake_browser(tmp_path, "chrome", port=9333)
    assert ba.read_devtools_port(str(d)) == 9333
    d2 = _fake_browser(tmp_path, "chrome-noport")
    assert ba.read_devtools_port(str(d2)) is None


def test_endpoint_prefers_ws_path_and_falls_back_to_http(tmp_path):
    # 有第二行 ws 路径（新式开关）→ ws:// 直连（/json/version 已被禁用）
    d = _fake_browser(tmp_path, "new-style", port=9333)
    ep = ba.read_devtools_endpoint(str(d))
    assert ep["endpoint"] == "ws://127.0.0.1:9333/devtools/browser/x"
    # 只有端口行（旧式 --remote-debugging-port 启动）→ 回退 http://
    d2 = _fake_browser(tmp_path, "old-style")
    (d2 / "DevToolsActivePort").write_text("9444\n", encoding="ascii")
    ep2 = ba.read_devtools_endpoint(str(d2))
    assert ep2["endpoint"] == "http://127.0.0.1:9444"


def test_discover_finds_only_real_browsers(tmp_path):
    _fake_browser(tmp_path, "chrome", port=9333)
    _fake_browser(tmp_path, "edge")            # 未开远程调试
    (tmp_path / "not-a-browser").mkdir()       # 无 Local State，应被忽略
    roots = [("Chrome", str(tmp_path / "chrome")), ("Edge", str(tmp_path / "edge")),
             ("Ghost", str(tmp_path / "not-a-browser"))]
    found = ba.discover_browsers(roots)
    assert [f["name"] for f in found] == ["Chrome", "Edge"]
    assert found[0]["remote_debugging"] is True and found[0]["debug_port"] == 9333
    assert found[1]["remote_debugging"] is False


# ── 端点解析：优先级与硬错 ───────────────────────────────

def test_explicit_endpoint_wins(home, tmp_path):
    _fake_browser(tmp_path, "chrome", port=9333)
    res = ba.resolve_endpoint(attach_dir=str(tmp_path / "chrome"), cdp_endpoint="http://127.0.0.1:9999")
    assert res["ok"] and res["endpoint"] == "http://127.0.0.1:9999"


def test_configured_dir_resolves_port(home, tmp_path):
    d = _fake_browser(tmp_path, "brave", port=9444)
    res = ba.resolve_endpoint(attach_dir=str(d))
    assert res["ok"] and res["endpoint"] == "ws://127.0.0.1:9444/devtools/browser/x" and res["browser_dir"] == str(d)


def test_configured_dir_without_debug_port_hard_errors(home, tmp_path, monkeypatch):
    d = _fake_browser(tmp_path, "edge")
    other = _fake_browser(tmp_path, "chrome", port=9333)
    _install_fake_roots(monkeypatch, tmp_path, {"edge": None, "chrome": 9333})
    res = ba.resolve_endpoint(attach_dir=str(d))
    # 不静默降级到另一台已开调试的浏览器
    assert res["ok"] is False
    assert "DevToolsActivePort" in res["error"] or "远程调试" in res["error"]
    assert res["remedy"]
    assert any(c["dir"] == str(other) for c in res["candidates"])


def test_invalid_configured_dir_hard_errors(home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = ba.resolve_endpoint(attach_dir=str(plain))
    assert res["ok"] is False and "Local State" in res["error"]


def test_auto_single_browser(home, tmp_path, monkeypatch):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333})
    res = ba.resolve_endpoint()
    assert res["ok"] and res["endpoint"] == "ws://127.0.0.1:9333/devtools/browser/x" and res["source"] == "auto-single"


def test_multiple_browsers_without_pref_refuse_to_guess(home, tmp_path, monkeypatch):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333, "edge": 9444})
    res = ba.resolve_endpoint()
    assert res["ok"] is False and "拒绝猜测" in res["error"] and len(res["candidates"]) == 2


def test_preference_roundtrip_and_resolution(home, tmp_path):
    d = _fake_browser(tmp_path, "arc", port=9555)
    ba.save_preference(attach_dir=str(d))
    assert ba.read_preference()["attach_dir"] == str(d)
    # 已存偏好 → 无需显式配置即解析到该浏览器
    res = ba.resolve_endpoint()
    assert res["ok"] and res["browser_dir"] == str(d)
    # 环境变量覆盖偏好
    monkeypatch = pytest.MonkeyPatch()
    other = _fake_browser(tmp_path, "vivaldi", port=9666)
    try:
        monkeypatch.setenv("BROWSER_AGENT_ATTACH_DIR", str(other))
        res2 = ba.resolve_endpoint()
        assert res2["ok"] and res2["browser_dir"] == str(other)
    finally:
        monkeypatch.undo()


def test_forget_clears_preference_without_poison(home, tmp_path):
    """清除偏好写空（不是 _expand('') 的毒值 '.'）——下次 start 回 managed。"""
    d = _fake_browser(tmp_path, "arc", port=9555)
    ba.save_preference(attach_dir=str(d))
    res = ba.save_preference(attach_dir="", cdp_endpoint="")
    assert res["ok"] and res["attach_dir"] == "" and res["cdp_endpoint"] == ""
    content = open(ba.preference_file(), encoding="utf-8").read()
    assert "." not in content.split("=")[-1:] or "ATTACH_DIR=." not in content
    assert "ATTACH_DIR=." not in content and "ENDPOINT=." not in content
    pref = ba.read_preference()
    assert pref["attach_dir"] == "" and pref["cdp_endpoint"] == ""


def test_format_attach_error_includes_candidates(home, tmp_path, monkeypatch):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333, "edge": 9444})
    res = ba.resolve_endpoint()
    msg = ba.format_attach_error(res)
    assert "chrome" in msg and str(tmp_path / "chrome") in msg and "处理步骤" in msg
