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


@pytest.fixture
def always_listening(monkeypatch):
    """TCP 探测恒真（用例只关心解析链路，不关心端口是否真的在监听）。"""
    monkeypatch.setattr(ba, "port_listening",
                        lambda port, host="127.0.0.1", timeout=1.0: True)


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


def test_discover_finds_only_real_browsers(tmp_path, always_listening):
    _fake_browser(tmp_path, "chrome", port=9333)
    _fake_browser(tmp_path, "edge")            # 未开远程调试
    (tmp_path / "not-a-browser").mkdir()       # 无 Local State，应被忽略
    roots = [("Chrome", str(tmp_path / "chrome")), ("Edge", str(tmp_path / "edge")),
             ("Ghost", str(tmp_path / "not-a-browser"))]
    found = ba.discover_browsers(roots)
    assert [f["name"] for f in found] == ["Chrome", "Edge"]
    rd = found[0]["remote_debugging"]
    assert rd["enabled"] is True and rd["port"] == 9333 and rd["detail"] == ""
    assert rd["endpoint"] == "ws://127.0.0.1:9333/devtools/browser/x"  # 端点随结论给出，无需重读
    assert found[1]["remote_debugging"]["enabled"] is False
    assert "DevToolsActivePort" in found[1]["remote_debugging"]["detail"]


def test_discover_flags_stale_port_file(tmp_path):
    """端口文件在但端口不在监听（浏览器崩溃残留）→ 不误报为可附着。"""
    _fake_browser(tmp_path, "chrome", port=9333)   # 9333 无监听
    found = ba.discover_browsers([("Chrome", str(tmp_path / "chrome"))])
    rd = found[0]["remote_debugging"]
    assert rd["enabled"] is False and rd["port"] == 9333
    assert "未在监听" in rd["detail"]


# ── 端点解析：优先级与硬错 ───────────────────────────────

def test_explicit_endpoint_wins(home, tmp_path):
    _fake_browser(tmp_path, "chrome", port=9333)
    res = ba.resolve_endpoint(attach_dir=str(tmp_path / "chrome"), cdp_endpoint="http://127.0.0.1:9999")
    assert res["ok"] and res["endpoint"] == "http://127.0.0.1:9999"


def test_configured_dir_resolves_port(home, tmp_path, always_listening):
    d = _fake_browser(tmp_path, "brave", port=9444)
    res = ba.resolve_endpoint(attach_dir=str(d))
    assert res["ok"] and res["endpoint"] == "ws://127.0.0.1:9444/devtools/browser/x" and res["browser_dir"] == str(d)


def test_configured_dir_without_debug_port_hard_errors(home, tmp_path, monkeypatch, always_listening):
    d = _fake_browser(tmp_path, "edge")
    other = _fake_browser(tmp_path, "chrome", port=9333)
    _install_fake_roots(monkeypatch, tmp_path, {"edge": None, "chrome": 9333})
    res = ba.resolve_endpoint(attach_dir=str(d))
    # 不静默降级到另一台已开调试的浏览器
    assert res["ok"] is False
    assert "DevToolsActivePort" in res["error"] or "远程调试" in res["error"]
    assert res["remedy"] and res["class"] == "unavailable"
    assert res["reason"] == "not-enabled"
    assert any(c["data_dir"] == str(other) for c in res["candidates"])


def test_configured_dir_offline_reports_agent_first_remedy(home, tmp_path, monkeypatch):
    """端口文件在但不在监听 → offline 分类，remedy 第一步是 agent 代开浏览器。"""
    d = _fake_browser(tmp_path, "Chrome", port=9333)   # 9333 无监听；目录名可对上启动命令映射
    monkeypatch.setattr(ba, "port_listening",
                        lambda port, host="127.0.0.1", timeout=1.0: False)
    res = ba.resolve_endpoint(attach_dir=str(d))
    assert res["ok"] is False and res["class"] == "unavailable" and res["reason"] == "offline"
    assert "代开" in res["remedy"][1] and "chrome" in res["remedy"][1].lower()


def test_invalid_configured_dir_is_config_error(home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = ba.resolve_endpoint(attach_dir=str(plain))
    assert res["ok"] is False and "Local State" in res["error"]
    assert res["class"] == "config-error"
    assert any("不需要重启" in step for step in res["remedy"])


def test_invalid_endpoint_format_is_config_error(home):
    res = ba.resolve_endpoint(cdp_endpoint="not-a-url")
    assert res["ok"] is False and res["class"] == "config-error"


def test_auto_single_browser(home, tmp_path, monkeypatch, always_listening):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333})
    res = ba.resolve_endpoint()
    assert res["ok"] and res["endpoint"] == "ws://127.0.0.1:9333/devtools/browser/x" and res["source"] == "auto-single"


def test_multiple_browsers_without_pref_refuse_to_guess(home, tmp_path, monkeypatch, always_listening):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333, "edge": 9444})
    res = ba.resolve_endpoint()
    assert res["ok"] is False and "拒绝猜测" in res["error"] and len(res["candidates"]) == 2
    assert res["class"] == "ambiguous"
    # remedy 里直接给出可执行的 attach 命令
    assert any("attach --" in step or "attach \"" in step for step in res["remedy"])


def test_preference_roundtrip_and_resolution(home, tmp_path, always_listening):
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


# ── 进程级浏览器身份 pin ───────────────────────────────────

def test_pin_lifecycle_and_mismatch(home, tmp_path):
    ba.reset_pin()
    assert ba.pinned_dir() is None
    a = tmp_path / "chrome"
    b = tmp_path / "edge"
    a.mkdir(); b.mkdir()
    # 未 pin / 目标为空 → 永不报 mismatch
    assert ba.pin_mismatch_error(str(b)) is None
    ba.set_pinned_dir(str(a))
    assert ba.pin_mismatch_error(str(a)) is None            # 同一浏览器
    assert ba.pin_mismatch_error(str(tmp_path / "chrome/")) is None  # 尾斜杠归一
    err = ba.pin_mismatch_error(str(b))
    assert err and "拒绝改连" in err and "shutdown" in err
    assert ba.pin_mismatch_error(None) is None              # 端点直连无目录可比
    ba.reset_pin()
    assert ba.pinned_dir() is None


# ── 端点/端口工具 ─────────────────────────────────────────

def test_endpoint_port():
    assert ba.endpoint_port("ws://127.0.0.1:52323/devtools/browser/abc") == 52323
    assert ba.endpoint_port("http://127.0.0.1:9222") == 9222
    assert ba.endpoint_port("not a url") is None
    assert ba.endpoint_port("ws://127.0.0.1:notaport/x") is None


def test_port_listening_true_then_false():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert ba.port_listening(port, timeout=0.5) is True
    # 端口已关闭（bind 后立刻关）：再连大概率被拒；连一个几乎不可能在听的端口
    assert ba.port_listening(1, timeout=0.2) is False


# ── 清单外浏览器受控探测 ──────────────────────────────────

def _fake_probe_root(tmp_path, monkeypatch):
    root = tmp_path / "AppSupport"
    root.mkdir()
    monkeypatch.setattr(ba, "app_data_roots", lambda: [str(root)])
    return root


def test_probe_finds_browser_with_debug_state(tmp_path, monkeypatch):
    root = _fake_probe_root(tmp_path, monkeypatch)
    data = root / "Dia" / "User Data"
    data.mkdir(parents=True)
    (data / "Local State").write_text("{}", encoding="utf-8")
    res = ba.probe_app_data_folder("Dia")
    assert res["ok"]
    browsers = res["folders"][0]["browsers"]
    assert len(browsers) == 1
    assert browsers[0]["name"] == "Dia"
    assert browsers[0]["data_dir"] == str(data)
    assert browsers[0]["remote_debugging"]["enabled"] is False  # 无端口文件 → 未开启


def test_probe_reports_listening_port(tmp_path, monkeypatch):
    import socket
    root = _fake_probe_root(tmp_path, monkeypatch)
    data = root / "Chrome"
    data.mkdir(parents=True)
    (data / "Local State").write_text("{}", encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        (data / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n")
        res = ba.probe_app_data_folder("Chrome")
    b = res["folders"][0]["browsers"][0]
    assert b["remote_debugging"]["enabled"] is True and b["remote_debugging"]["port"] == port
    assert b["remote_debugging"]["detail"] == ""


def test_probe_rejects_tilde_and_outside(tmp_path, monkeypatch):
    _fake_probe_root(tmp_path, monkeypatch)
    assert not ba.probe_app_data_folder("~")["ok"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    res = ba.probe_app_data_folder(str(outside))
    assert not res["ok"] and "已拒绝" in res["error"]


def test_probe_rejects_parent_escape(tmp_path, monkeypatch):
    _fake_probe_root(tmp_path, monkeypatch)
    res = ba.probe_app_data_folder("../" + tmp_path.name)
    assert not res["ok"]


def test_probe_symlink_escape_rejected(tmp_path, monkeypatch):
    root = _fake_probe_root(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Local State").write_text("{}", encoding="utf-8")
    (root / "evil").symlink_to(outside)
    res = ba.probe_app_data_folder("evil")
    # 软链接指向根目录之外：不当作浏览器发现
    assert not res["ok"] or all(
        b["data_dir"] != str(outside) for f in res["folders"] for b in f["browsers"])


def test_probe_in_root_symlink_allowed(tmp_path, monkeypatch):
    """根目录内的软链接（snap current 布局）照常穿透。"""
    import os as _os
    root = _fake_probe_root(tmp_path, monkeypatch)
    snap_app = root / "chromium-snap"
    revision = snap_app / "1234" / ".config" / "chromium"
    revision.mkdir(parents=True)
    (revision / "Local State").write_text("{}", encoding="utf-8")
    _os.symlink("1234", snap_app / "current")
    res = ba.probe_app_data_folder("chromium-snap")
    assert res["ok"]
    assert res["folders"][0]["browsers"][0]["name"] == "chromium"


def test_probe_missing_folder(tmp_path, monkeypatch):
    _fake_probe_root(tmp_path, monkeypatch)
    res = ba.probe_app_data_folder("Nope")
    assert not res["ok"] and "没有文件夹" in res["error"]


def test_display_name_skips_user_data_layer():
    assert ba.display_name("/x/y/Dia/User Data") == "Dia"
    assert ba.display_name("/x/y/Google/Chrome") == "Chrome"
    assert ba.display_name("C:\\a\\Microsoft\\Edge\\User Data") == "Edge"


# ── Windows 环境变量展开 ───────────────────────────────────

def test_windows_env_var_expansion(home, monkeypatch):
    monkeypatch.setattr(ba.os, "name", "nt")
    monkeypatch.setenv("MYROOT", "/Users/x/AppData/Local")
    assert ba._expand("%MYROOT%\\Google\\Chrome\\User Data").endswith("Google/Chrome/User Data") \
        or "Google" in ba._expand("%MYROOT%\\Google\\Chrome\\User Data")
    with pytest.raises(ValueError):
        ba._expand("%MISSING_VAR%\\Chrome")


def test_expand_safe_falls_back_on_missing_var(home, monkeypatch):
    monkeypatch.setattr(ba.os, "name", "nt")
    v = ba._expand_safe("%MISSING_VAR%\\Chrome")
    assert "MISSING_VAR" in v  # 不炸，保留字面量供报错定位


def test_pin_identity_endpoint_token(home):
    """端点直连也是身份：目录 ↔ 端点、端点 ↔ 端点' 都算换目标。"""
    ba.reset_pin()
    try:
        ba.set_pinned_dir("/data/chrome")
        err = ba.pin_mismatch_error("endpoint:http://127.0.0.1:9999")
        assert err and "拒绝改连" in err
        assert ba.pin_mismatch_error("endpoint:http://127.0.0.1:9999") is not None
        ba.set_pinned_dir("endpoint:http://127.0.0.1:9999")
        assert ba.pin_mismatch_error("endpoint:http://127.0.0.1:9999") is None
        assert ba.pin_mismatch_error("endpoint:http://127.0.0.1:8888") is not None
        # set_pinned_dir(None) 不清空既有 pin（清空只能经 reset_pin）
        ba.set_pinned_dir(None)
        assert ba.pinned_dir() == "endpoint:http://127.0.0.1:9999"
        # attach_identity：目录优先，端点兜底
        assert ba.attach_identity({"browser_dir": "/d", "endpoint": "ws://x"}) == "/d"
        assert ba.attach_identity({"endpoint": "ws://x"}) == "endpoint:ws://x"
        assert ba.attach_identity({}) is None
    finally:
        ba.reset_pin()


def test_strictly_inside_rejects_root_itself(tmp_path):
    assert ba._strictly_inside(str(tmp_path), str(tmp_path)) is False  # relpath '.'


def test_probe_rejects_root_itself(tmp_path, monkeypatch):
    _fake_probe_root(tmp_path, monkeypatch)
    res = ba.probe_app_data_folder(".")
    assert not res["ok"]


def test_strictly_inside_cross_drive_valueerror(monkeypatch, tmp_path):
    """Windows 跨盘 relpath 抛 ValueError → 按越界处理而非裸崩。"""
    def _raise(path, root):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")
    monkeypatch.setattr(ba.os.path, "relpath", _raise)
    assert ba._strictly_inside("D:\\x", "C:\\Users") is False


# ── 端口文件四分类 / 提示 / 身份归一 ────────────────────────

def test_read_devtools_info_invalid_content(tmp_path):
    d = _fake_browser(tmp_path, "chrome")
    (d / "DevToolsActivePort").write_text("not-a-port\n", encoding="ascii")
    info = ba.read_devtools_info(str(d))
    assert info["ok"] is False and info["reason"] == "invalid"
    empty = _fake_browser(tmp_path, "empty")
    (empty / "DevToolsActivePort").write_text("\n", encoding="ascii")
    assert ba.read_devtools_info(str(empty))["reason"] == "invalid"


def test_read_devtools_info_unreadable_classified(tmp_path, monkeypatch):
    import builtins
    real_open = builtins.open

    def _perm_denied(file, *a, **k):
        if str(file).endswith("DevToolsActivePort"):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", _perm_denied)
    d = _fake_browser(tmp_path, "chrome", port=9333)
    info = ba.read_devtools_info(str(d))
    assert info["ok"] is False and info["reason"] == "unreadable"
    # unreadable 的 remedy 出路指向显式端点配置
    remedy = ba._unavailable_remedy("unreadable", info["detail"], "Chrome")
    assert any("BROWSER_AGENT_CDP_ENDPOINT" in s for s in remedy)


def test_launch_hint_per_platform():
    hint = ba.browser_launch_hint("Chrome")
    assert hint.startswith("`") and hint.endswith("`")   # 有映射 → 给出可执行命令
    assert ba.browser_launch_hint("未知浏览器").startswith("用系统方式")


def test_discover_payload_has_agent_next_steps(home, monkeypatch, tmp_path, always_listening):
    _install_fake_roots(monkeypatch, tmp_path, {"chrome": 9333})
    payload = ba.discover_payload()
    assert payload["ok"] and len(payload["next_steps"]) == 4
    assert any("--probe" in s for s in payload["next_steps"])


@pytest.mark.skipif(sys.platform != "darwin" and os.name != "nt",
                    reason="大小写不敏感卷仅 macOS/Windows")
def test_pin_identity_case_insensitive(tmp_path):
    """同一浏览器目录大小写写法不同 ≠ 换目标（macOS/Windows 卷不区分大小写）。"""
    ba.reset_pin()
    try:
        d = tmp_path / "ChromeData"
        d.mkdir()
        ba.set_pinned_dir(str(d))
        assert ba.pin_mismatch_error(str(d).swapcase()) is None
    finally:
        ba.reset_pin()


def test_expand_strips_paired_quotes(home):
    v = str(home / "Google Chrome")
    assert ba._expand(f'"{v}"') == ba._expand(v)
    assert ba._expand(f"'{v}'") == ba._expand(v)


def test_discover_enabled_only_when_port_really_listens(tmp_path):
    """discover 的 TCP 探测真值：端口真实在监听才报 enabled（非 monkeypatch）。"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        d = _fake_browser(tmp_path, "Chrome", port=port)
        found = ba.discover_browsers([("Chrome", str(d))])
        rd = found[0]["remote_debugging"]
        assert rd["enabled"] is True and rd["port"] == port and rd["detail"] == ""
        assert rd["endpoint"].startswith(f"ws://127.0.0.1:{port}")


def test_read_devtools_info_non_ascii_is_invalid(tmp_path):
    """撕裂写残留的非 ASCII 字节 → invalid 分类，不裸抛（旧码只捕 OSError）。"""
    d = _fake_browser(tmp_path, "chrome")
    (d / "DevToolsActivePort").write_bytes(b"\xff\xfe\x80bad\n")
    info = ba.read_devtools_info(str(d))
    assert info["ok"] is False and info["reason"] == "invalid"


def test_discover_stale_port_reports_offline_reason(tmp_path):
    """端口文件在但不在监听 → remote_debugging 带 reason=offline（auto 解析据此分诊）。"""
    _fake_browser(tmp_path, "Chrome", port=9333)
    found = ba.discover_browsers([("Chrome", str(tmp_path / "Chrome"))])
    assert found[0]["remote_debugging"]["reason"] == "offline"


def test_error_response_requires_klass():
    """klass 是必填关键字：漏传是 TypeError（编译期失败），不再是静默误分类。"""
    with pytest.raises(TypeError):
        ba.error_response("x", ["c"])
