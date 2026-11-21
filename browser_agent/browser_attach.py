#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
附着日常浏览器（attach 模式）— 经 CDP 连接用户正在使用的 Chromium 内核浏览器。

与 managed 模式（自拉隔离 Chromium）互补：
- **登录态免费**：用户日常 profile 的 cookies 直接可用，无需逐站构造 profile
- **指纹真实**：真实浏览器的指纹与环境天然可信，无 stealth 军备竞赛
- **最小侵入契约**：永不操作用户已有标签页；只在自建标签页里工作；
  会话结束只关自建标签页；绝不关闭用户的浏览器或 default context；
  attach 模式下不抢焦点（switch_tab 不 bring_to_front）、不清用户 storage

安全边界（硬约束）：
- **不扫描 9222 等固定端口**：只读所配置浏览器数据目录下的 DevToolsActivePort 文件
- **不静默降级**：配置/偏好的浏览器不可用 → 硬错并给出处理步骤，绝不连到别的浏览器
- **身份 pin**：偏好保存的是浏览器数据目录（端口随浏览器重启变化，每次连接重读）
- 探测范围限定在已知浏览器数据目录与显式给出的目录内，不做全盘扫描

前置条件：用户在其浏览器打开 chrome://inspect/#remote-debugging（Edge 为
edge://inspect/#remote-debugging）勾选 "Allow remote debugging for this browser
instance"（可能需重启浏览器）。
"""
from __future__ import annotations

import collections
import concurrent.futures
import os
import re
import socket
import sys
from typing import Optional
from urllib.parse import urlparse

from .user_data import data_dir

# 各平台常见 Chromium 内核浏览器的用户数据目录（含 Local State 的那一层）。
# 清单外的任意 Chromium 内核浏览器：把其用户数据目录填给 BROWSER_AGENT_ATTACH_DIR
# / browser.attach_dir / browser-agent-client attach <目录> 即可。
# Firefox / Safari 非 Chromium 内核、无 CDP，不在支持范围。
_BROWSER_ROOTS = {
    "darwin": [
        ("Chrome", "~/Library/Application Support/Google/Chrome"),
        ("Chrome Canary", "~/Library/Application Support/Google/Chrome Canary"),
        ("Edge", "~/Library/Application Support/Microsoft Edge"),
        ("Brave", "~/Library/Application Support/BraveSoftware/Brave-Browser"),
        ("Vivaldi", "~/Library/Application Support/Vivaldi"),
        ("Opera", "~/Library/Application Support/com.operasoftware.Opera"),
        ("Opera GX", "~/Library/Application Support/com.operasoftware.OperaGX"),
        ("Arc", "~/Library/Application Support/Arc/User Data"),
        ("Dia", "~/Library/Application Support/Dia/User Data"),
        ("Chromium", "~/Library/Application Support/Chromium"),
    ],
    "linux": [
        ("Chrome", "~/.config/google-chrome"),
        ("Edge", "~/.config/microsoft-edge"),
        ("Brave", "~/.config/brave"),
        ("Vivaldi", "~/.config/vivaldi"),
        ("Opera", "~/.config/opera"),
        ("Chromium", "~/.config/chromium"),
        ("Chromium (snap)", "~/snap/chromium/current/.config/chromium"),
        ("Chrome (flatpak)", "~/.var/app/com.google.Chrome/config/google-chrome"),
        ("Edge (flatpak)", "~/.var/app/com.microsoft.Edge/config/microsoft-edge"),
        ("Brave (flatpak)", "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"),
    ],
    "win32": [
        ("Chrome", "~/AppData/Local/Google/Chrome/User Data"),
        ("Chrome Canary", "~/AppData/Local/Google/Chrome SxS/User Data"),
        ("Edge", "~/AppData/Local/Microsoft/Edge/User Data"),
        ("Brave", "~/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
        ("Vivaldi", "~/AppData/Local/Vivaldi/User Data"),
        ("Opera", "~/AppData/Local/Opera Software/Opera Stable/User Data"),
        ("Arc", "~/AppData/Local/Packages/TheBrowserCompany.Arc/LocalCache/Local/Arc/User Data"),
        ("Chromium", "~/AppData/Local/Chromium/User Data"),
    ],
}

REMEDY_STEPS = (
    "在目标浏览器地址栏打开 chrome://inspect/#remote-debugging"
    "（Edge 为 edge://inspect/#remote-debugging），勾选"
    ' "Allow remote debugging for this browser instance"（可能需重启浏览器），'
    "然后重试。旧版本浏览器没有此开关时：完全退出浏览器后，用 "
    "--remote-debugging-port=9222 参数启动同一个浏览器（用户数据目录不变），"
    "服务同样能附着。不扫描固定端口，也不支持附着另起的空白调试浏览器。"
)


_WIN_ENV_RE = re.compile(r"%([A-Za-z0-9_]+)%")


def _expand(path: str) -> str:
    """展开 ~ 与 Windows 环境变量（%LOCALAPPDATA% 等）后归一路径。

    引用了未设置的环境变量时抛 ValueError（配置写错应显式报错，不静默保留字面量）。
    """
    v = str(path).strip()
    if os.name == "nt":
        def _sub(m: "re.Match[str]") -> str:
            val = os.environ.get(m.group(1).upper())
            if val is None:
                raise ValueError(f"环境变量 {m.group(1).upper()} 未设置，无法展开 {path}")
            return val
        v = _WIN_ENV_RE.sub(_sub, v)
    return os.path.normpath(os.path.expanduser(v))


expand_path = _expand  # 供 client/server 复用的公开名（严格版：坏引用即报错）


def _expand_safe(path: str) -> str:
    """偏好文件等持久值的展开：引用了未设置的环境变量时退化为 ~ 展开，
    不让一条历史坏值炸掉整个偏好读取。"""
    try:
        return _expand(path)
    except ValueError:
        return os.path.normpath(os.path.expanduser(str(path).strip()))


def is_browser_data_dir(path: str) -> bool:
    """判定目录是否为 Chromium 系浏览器数据目录（标志文件 Local State）。"""
    try:
        return os.path.isdir(path) and os.path.isfile(os.path.join(path, "Local State"))
    except OSError:
        return False


def read_devtools_endpoint(path: str) -> Optional[dict]:
    """读数据目录下 DevToolsActivePort → {"port": int, "endpoint": url}。

    首行端口号，次行（若有）是 browser WebSocket 路径。新式开关
    （chrome://inspect 勾选 "Allow remote debugging"）会禁用 /json/version
    等 HTTP 端点（连接 http:// 会 404 "not a DevTools server"），只认文件里
    的 ws 路径——因此有次行时必须用 ws:// 直连；无次行（旧式命令行启动）
    回退 http://。
    """
    try:
        with open(os.path.join(path, "DevToolsActivePort"), "r", encoding="ascii") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        if not lines:
            return None
        port = int(lines[0])
        ws_path = lines[1] if len(lines) > 1 else ""
        endpoint = f"ws://127.0.0.1:{port}{ws_path}" if ws_path else f"http://127.0.0.1:{port}"
        return {"port": port, "endpoint": endpoint}
    except (OSError, ValueError):
        return None


def read_devtools_port(path: str) -> Optional[int]:
    """读数据目录下 DevToolsActivePort 的端口号（仅展示用；连接请用 read_devtools_endpoint）。"""
    info = read_devtools_endpoint(path)
    return info["port"] if info else None


def default_roots() -> list:
    key = "win32" if os.name == "nt" else ("darwin" if os.path.exists("/System/Library/CoreServices") else "linux")
    return list(_BROWSER_ROOTS.get(key, []))


def discover_browsers(roots: Optional[list] = None) -> list:
    """列出本机已安装且数据目录可辨认的浏览器（含远程调试端口状态）。

    只在已知目录清单内探测；不做目录名模糊扫描、不读目录内容以外的任何文件。
    """
    found = []
    for name, raw_dir in (roots if roots is not None else default_roots()):
        d = _expand(raw_dir)
        if not is_browser_data_dir(d):
            continue
        port = read_devtools_port(d)
        found.append({
            "name": name,
            "dir": d,
            "debug_port": port,
            "remote_debugging": port is not None,
        })
    return found


# ── 偏好持久化（~/.browser-agent/attach.env）────────────────
def preference_file() -> str:
    return os.path.join(data_dir(), "attach.env")


def read_preference() -> dict:
    """已保存的附着偏好。优先级：显式参数 > 环境变量 > 本文件。"""
    pref = {"attach_dir": "", "cdp_endpoint": ""}
    try:
        with open(preference_file(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BROWSER_AGENT_ATTACH_DIR="):
                    pref["attach_dir"] = _expand_safe(line.split("=", 1)[1])
                elif line.startswith("BROWSER_AGENT_CDP_ENDPOINT="):
                    pref["cdp_endpoint"] = line.split("=", 1)[1]
    except OSError:
        pass
    # 环境变量覆盖文件
    pref["attach_dir"] = os.getenv("BROWSER_AGENT_ATTACH_DIR") or pref["attach_dir"]
    pref["cdp_endpoint"] = os.getenv("BROWSER_AGENT_CDP_ENDPOINT") or pref["cdp_endpoint"]
    return pref


def save_preference(attach_dir: Optional[str] = None,
                    cdp_endpoint: Optional[str] = None) -> dict:
    """保存/更新附着偏好（None 字段保持原值；空串 = 清除该字段）。"""
    current = read_preference()
    # 归一：显式入参 > 环境变量 > 既有值；空串 = 清除（_expand("") 是毒值 "."，须归空）
    if attach_dir is None:
        attach_dir = os.getenv("BROWSER_AGENT_ATTACH_DIR") or current["attach_dir"]
    if cdp_endpoint is None:
        cdp_endpoint = os.getenv("BROWSER_AGENT_CDP_ENDPOINT") or current["cdp_endpoint"]
    new = {
        "attach_dir": _expand_safe(attach_dir) if attach_dir else "",
        "cdp_endpoint": cdp_endpoint or "",
    }
    lines = []
    if new["attach_dir"]:
        lines.append(f"BROWSER_AGENT_ATTACH_DIR={new['attach_dir']}")
    if new["cdp_endpoint"]:
        lines.append(f"BROWSER_AGENT_CDP_ENDPOINT={new['cdp_endpoint']}")
    with open(preference_file(), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return {"ok": True, "preference_file": preference_file(), **new}


# ── 端点解析 ────────────────────────────────────────────────
def error_response(error: str, candidates: Optional[list] = None) -> dict:
    """附着失败的统一错误报文（error + remedy + 可选候选浏览器清单）。"""
    res = {"ok": False, "error": error, "remedy": REMEDY_STEPS}
    if candidates:
        res["candidates"] = candidates
    return res


def resolve_endpoint(attach_dir: Optional[str] = None,
                     cdp_endpoint: Optional[str] = None) -> dict:
    """解析附着端点。优先级：显式 cdp_endpoint > 显式 attach_dir > 已存偏好 > 唯一已装浏览器。

    返回 {"ok": True, "endpoint": "http://127.0.0.1:PORT", "browser_dir":..., "port":...}
    或 {"ok": False, "error", "remedy", "candidates"}。绝不静默换目标浏览器。
    """
    pref = read_preference()
    explicit_dir = _expand_safe(attach_dir) if attach_dir else None

    # 显式 cdp_endpoint 最优先；其次（未显式给目录时）已保存的端点偏好。
    # 显式端点无需发现候选——提前返回，省一次全根扫描
    endpoint = cdp_endpoint or ""
    if not endpoint and explicit_dir is None:
        endpoint = pref["cdp_endpoint"]
    if endpoint:
        return {"ok": True, "endpoint": endpoint, "browser_dir": None,
                "source": "explicit-endpoint"}

    dir_candidates = discover_browsers()

    target_dir = explicit_dir or pref["attach_dir"]
    if target_dir:
        if not is_browser_data_dir(target_dir):
            return _err(
                f"配置的浏览器数据目录无效（不含 Local State）: {target_dir}",
                candidates=dir_candidates,
            )
        ep = read_devtools_endpoint(target_dir)
        if ep is None:
            return _err(
                f"浏览器 {target_dir} 未开放远程调试（读不到 DevToolsActivePort）",
                candidates=dir_candidates,
            )
        return {"ok": True, "endpoint": ep["endpoint"],
                "browser_dir": target_dir, "port": ep["port"], "source": "configured-dir"}

    # 无显式配置、无偏好：仅当恰好一台可辨认浏览器时才自动采用；多台则要求明确选择
    with_debug = [c for c in dir_candidates if c["remote_debugging"]]
    if len(dir_candidates) == 1:
        c = dir_candidates[0]
        if not c["remote_debugging"]:
            return _err(f"唯一检测到的浏览器 {c['name']} 未开放远程调试", candidates=dir_candidates)
        ep = read_devtools_endpoint(c["dir"])
        return {"ok": True, "endpoint": ep["endpoint"],
                "browser_dir": c["dir"], "port": ep["port"], "source": "auto-single"}
    if len(with_debug) == 1:
        c = with_debug[0]
        ep = read_devtools_endpoint(c["dir"])
        return {"ok": True, "endpoint": ep["endpoint"],
                "browser_dir": c["dir"], "port": ep["port"], "source": "auto-single-debug"}
    if len(dir_candidates) > 1:
        return _err(
            "检测到多台浏览器，未配置偏好，拒绝猜测目标",
            candidates=dir_candidates,
        )
    return _err("未找到任何可辨认的 Chromium 系浏览器数据目录", candidates=dir_candidates)


_err = error_response  # 模块内沿用历史名


def format_attach_error(info: dict) -> str:
    """把 resolve_endpoint 的失败结果格式化为给 AI/用户的可执行报错。"""
    msg = f"附着日常浏览器失败: {info.get('error')}\n处理步骤: {info.get('remedy')}"
    cands = info.get("candidates") or []
    if cands:
        lines = [f"- {c['name']}: {c['dir']}（远程调试{'已开' if c['remote_debugging'] else '未开'}）"
                 for c in cands]
        msg += "\n本机检测到的浏览器:\n" + "\n".join(lines)
        msg += ("\n指定其一: browser-agent-client attach <数据目录>（保存偏好后重启服务），"
                "或配置 browser.attach_dir / 环境变量 BROWSER_AGENT_ATTACH_DIR")
    return msg


# ── 进程级浏览器身份 pin ───────────────────────────────────
# 同一服务进程内只附着同一台浏览器：断线重连（浏览器重启后自动恢复）时若偏好
# 已指向另一目标（别的数据目录**或别的 CDP 端点**），硬错提示先停服务再切换
# ——绝不静默换目标。身份令牌：数据目录优先；端点直连无目录可比时用端点本身
# （两种身份不互通，运行中从目录 A 换到端点 B 同样算换目标）。
_PROCESS_PIN: dict = {"dir": None}


def attach_identity(info: dict) -> Optional[str]:
    """resolve_endpoint 结果 → 身份令牌（数据目录路径，或 endpoint:<url>）。"""
    d = info.get("browser_dir")
    if d:
        return d
    ep = info.get("endpoint")
    return f"endpoint:{ep}" if ep else None


def pinned_dir() -> Optional[str]:
    """本进程已成功附着的浏览器身份令牌（未附着 → None）。"""
    return _PROCESS_PIN["dir"]


def set_pinned_dir(identity: Optional[str]) -> None:
    """记录进程级 pin（附着**完整成功**后调用）。目录身份在此归一展开；
    None 不清空既有 pin——清空只能经 reset_pin（防止端点直连路径把已 pin
    的目录身份抹掉）。"""
    if identity:
        token = str(identity)
        _PROCESS_PIN["dir"] = token if token.startswith("endpoint:") \
            else _expand_safe(token)


def reset_pin() -> None:
    """清空进程级 pin（测试隔离用）。"""
    _PROCESS_PIN["dir"] = None


def pin_mismatch_error(new_identity: Optional[str]) -> Optional[str]:
    """新身份与已 pin 身份不同 → 可直接展示的错误；相同/无法比较 → None。"""
    old = _PROCESS_PIN["dir"]
    if not old or not new_identity:
        return None
    new = str(new_identity)
    if old.startswith("endpoint:") or new.startswith("endpoint:"):
        if old == new:
            return None
    elif _realpath_or_self(old) == _realpath_or_self(new):
        return None
    return (f"本服务进程已附着浏览器 {old}，拒绝改连 {new}（不静默切换目标浏览器）。"
            "如确要切换：先 browser-agent-client shutdown 停止服务，"
            "再 browser-agent-client attach <数据目录或端点>")


# ── 端点/端口工具 ─────────────────────────────────────────

def endpoint_port(endpoint: str) -> Optional[int]:
    """从 CDP 端点 URL 解析端口号（解析失败 → None）。"""
    try:
        return urlparse(str(endpoint)).port
    except ValueError:
        return None


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """TCP 层探测端口在监听（只建 TCP 连接，不建 WebSocket——
    不触发浏览器的远程调试授权弹窗）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── 清单外浏览器探测（attach --probe）──────────────────────
# 边界由代码保证（不靠 AI 自觉）：
# - 只列应用数据根目录最外层文件夹名，不进入任何子文件夹
# - 探测范围严格限定在根目录内：拒绝 ~、越界相对路径、根目录之外/本身的
#   绝对路径；经软链接指向根目录之外的同样拒绝（根目录内的软链接允许，
#   如 snap 的 current → 修订目录）
# - 文件夹内向下最多 PROBE_MAX_DEPTH 层，以 Local State 识别 Chromium 数据
#   目录，识别后不再深入；目录数上限 PROBE_MAX_DIRS 防无界遍历
# - 无权限的目录跳过并注明；识别出的目录只读 DevToolsActivePort + TCP 探测
PROBE_MAX_DEPTH = 3
PROBE_MAX_DIRS = 500


def app_data_roots() -> list:
    """各平台浏览器数据目录通常所在的应用数据根目录（存在的才列出）。"""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Application Support")]
    elif os.name == "nt":
        roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")]
    else:
        # Linux 另含 snap（~/snap/<应用>/…）与 flatpak（~/.var/app/<应用 id>/…）
        roots = [os.path.join(home, ".config"),
                 os.path.join(home, "snap"),
                 os.path.join(home, ".var", "app")]
    return [r for r in roots if r]


def list_app_data_folders() -> list:
    """每个应用数据根目录的最外层文件夹名（只 readdir 根目录本身）。"""
    out = []
    for root in app_data_roots():
        try:
            names = sorted(
                e for e in os.listdir(root)
                if os.path.isdir(os.path.join(root, e)) or os.path.islink(os.path.join(root, e))
            )
            out.append({"root": root, "folders": names})
        except OSError as e:
            out.append({"root": root, "error": e.errno or "EIO", "detail": str(e)})
    return out


def display_name(data_dir: str) -> str:
    """数据目录路径里最有辨识度的文件夹名（跳过通用的 User Data 层）。"""
    parts = [p for p in re.split(r"[\\/]+", str(data_dir)) if p]
    for p in reversed(parts):
        if p.lower() != "user data":
            return p
    return str(data_dir)


def _strictly_inside(path: str, root: str) -> bool:
    """统一路径包含谓词的模块内别名（实现在 utils.strictly_inside）。"""
    from .utils import strictly_inside
    return strictly_inside(path, root)


def _realpath_or_self(p: str) -> str:
    try:
        return os.path.realpath(p)
    except OSError:
        return p


def _resolve_probe_targets(value: str) -> tuple:
    """文件夹参数 → (待探测绝对路径列表, 错误信息)。

    绝对/相对两分支统一为：先算候选（绝对名一个；相对名在每个根下各一个），
    再用同一谓词（存在 + 严格位于某根之内）过滤，最后按分支选错误信息。
    """
    v = value.strip()
    roots = app_data_roots()
    hint = (f"只接受应用数据根目录（{'、'.join(roots) or '当前平台未知'}）下的"
            "相对文件夹名，如 Google、BraveSoftware")
    if not v:
        return [], f"需要文件夹参数；{hint}"
    if v.startswith("~"):
        return [], f"不接受 ~ 路径；{hint}"
    is_abs = os.path.isabs(v)
    candidates = [os.path.normpath(v)] if is_abs \
        else [os.path.normpath(os.path.join(r, v)) for r in roots]
    valid = [c for c in candidates
             if os.path.exists(c) and any(
                 _strictly_inside(_realpath_or_self(c), _realpath_or_self(r))
                 for r in roots)]
    if is_abs and not valid:
        # 存在但在根外（过滤掉）与根本不存在，报不同的错误
        if os.path.exists(candidates[0]):
            return [], f"{candidates[0]} 在应用数据根目录之外，已拒绝；{hint}"
        return [], f"应用数据根目录下不存在 {candidates[0]}"
    if not valid:
        return [], f"应用数据根目录下没有文件夹 {v}；{hint}"
    return valid, None


def _known_browser_names() -> dict:
    """{realpath(数据目录): 规范名}——探测结果命名与已知清单互查用，每轮建一次。"""
    return {os.path.realpath(_expand_safe(raw)): name
            for name, raw in default_roots()}


def _describe_browser_dir(data_dir: str, known_names: dict) -> dict:
    """单个数据目录的探测结论：显示名 + 远程调试状态（只读端口文件 + TCP）。

    显示名优先取已知清单里的规范名（find-url --browser / default_roots 用同一
    套名字），清单外才退回路径推断名——两个入口的名字可互查。"""
    data_name = known_names.get(os.path.realpath(data_dir)) or display_name(data_dir)
    port = read_devtools_port(data_dir)
    if port is not None:
        listening = port_listening(port)
        remote = {"enabled": listening, "port": port, "detail":
                  "" if listening else
                  f"DevToolsActivePort 记录的端口 {port} 未在监听（浏览器未运行或开关已关闭）"}
    else:
        port_file = os.path.join(data_dir, "DevToolsActivePort")
        try:
            exists = os.path.isfile(port_file)
        except OSError:
            exists = False
        remote = {"enabled": False, "port": None, "detail":
                  ("无法读取 DevToolsActivePort（权限不足）" if exists
                   else "没有 DevToolsActivePort（浏览器未运行或远程调试开关未开启）")}
    return {"name": data_name, "data_dir": data_dir,
            "remote_debugging": remote,
            "attach_hint": f'browser-agent-client attach "{data_dir}"'}


def discover_payload() -> dict:
    """/attach/discover（HTTP）与 client attach --discover（CLI）共用的单一构造：
    已知浏览器 + 应用数据根目录文件夹名 + probe 提示 + 处理步骤。"""
    return {"ok": True, "browsers": discover_browsers(),
            "app_data_folders": list_app_data_folders(),
            "probe_hint": ("清单外的浏览器：从应用数据根目录的文件夹名里认出"
                           "候选（如 Dia、BraveSoftware），用 attach --probe <文件夹> 探测"),
            "remedy": REMEDY_STEPS}


def probe_app_data_folder(arg: str) -> dict:
    """在应用数据根目录内的指定文件夹里寻找 Chromium 数据目录（受控探测）。"""
    targets, err = _resolve_probe_targets(arg)
    if err:
        return {"ok": False, "error": err}
    real_roots = [_realpath_or_self(r) for r in app_data_roots()]
    known_names = _known_browser_names()
    results, found = [], 0
    for folder in targets:
        browsers: list = []
        skipped: list = []
        truncated = False
        seen = {_realpath_or_self(folder)}
        queue = collections.deque([(folder, 0)])
        visited = 0
        while queue:
            d, depth = queue.popleft()
            visited += 1
            if visited > PROBE_MAX_DIRS:
                truncated = True
                break
            try:
                if is_browser_data_dir(d):
                    browsers.append(d)
                    continue  # 识别为数据目录后不再深入
                if depth < PROBE_MAX_DEPTH:
                    for entry in os.listdir(d):
                        p = os.path.join(d, entry)
                        if not os.path.isdir(p):
                            continue
                        real = _realpath_or_self(p)
                        # 根目录内的软链接允许（snap current）；指向外部的拒绝
                        if any(_strictly_inside(real, r) for r in real_roots):
                            if real not in seen:
                                seen.add(real)
                                queue.append((p, depth + 1))
            except OSError as e:
                skipped.append({"dir": d, "error": e.errno or "EIO"})
        # 目录扫描先完成，TCP 探测并发做（每台浏览器最多 1s 连接超时）
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=4) as pool:
            described = list(pool.map(
                lambda d: _describe_browser_dir(d, known_names), browsers))
        found += len(described)
        results.append({"folder": folder, "browsers": described,
                        "skipped": skipped, "truncated": truncated})
    res = {"ok": found > 0, "folders": results}
    if not found:
        res["error"] = "探测范围内没有找到 Chromium 浏览器数据目录（Local State）"
        res["remedy"] = REMEDY_STEPS
    return res
