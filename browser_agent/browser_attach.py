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

失败分类（resolve_endpoint 返回 class 字段，remedy 按类给出——不同失败该做的
事完全不同，一律套同一段「开调试开关」提示是误导）：
- config-error   配置本身有误（目录不是数据目录 / 端点格式不对）→ 改配置，与
                 服务是否在运行无关
- unavailable    目标浏览器连不上，reason 细分四类：
                 not-enabled   没有 DevToolsActivePort（浏览器未运行，或开关未开）
                 offline       端口文件在、端口不在监听（浏览器已退出/崩溃残留）
                 unreadable    端口文件无权读取（macOS TCC 等）→ 指向显式端点配置
                 invalid       端口文件内容损坏
- ambiguous      检测到多台浏览器且无偏好 → 必须明确选择，拒绝猜测
- not-found      没检测到任何可辨认的 Chromium 数据目录

unavailable 的处理顺序对 agent 友好：**能自动解决的不打扰用户**——先建议 agent
用系统命令代开浏览器再重试，仍失败才升级为需要用户开调试开关。

前置条件：用户在其浏览器打开 chrome://inspect/#remote-debugging（Edge 为
edge://inspect/#remote-debugging）勾选 "Allow remote debugging for this browser
instance"（可能需重启浏览器）。
"""
from __future__ import annotations

import collections
import concurrent.futures
import errno as _errno
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

# 代开浏览器的系统命令建议（按平台；None = 无已知命令，用通用提示）。
# 只用于错误提示里让 agent 代开浏览器——工具自己不启动 GUI 应用。
_LAUNCH_CMDS = {
    # name: (darwin, win32, linux)
    "Chrome":         ('open -a "Google Chrome"', "start chrome", "google-chrome"),
    "Chrome Canary":  ('open -a "Google Chrome Canary"', None, "google-chrome-canary"),
    "Edge":           ('open -a "Microsoft Edge"', "start msedge", "microsoft-edge"),
    "Brave":          ('open -a Brave', "start brave", "brave-browser"),
    "Vivaldi":        ('open -a Vivaldi', None, "vivaldi"),
    "Opera":          ('open -a Opera', None, "opera"),
    "Opera GX":       ('open -a "Opera GX"', None, "opera"),
    "Arc":            ('open -a Arc', None, None),
    "Dia":            ('open -a Dia', None, None),
    "Chromium":       ('open -a Chromium', "start chromium", "chromium"),
    "Chromium (snap)":  (None, None, "snap run chromium"),
    "Chrome (flatpak)": (None, None, "flatpak run com.google.Chrome"),
    "Edge (flatpak)":   (None, None, "flatpak run com.microsoft.Edge"),
    "Brave (flatpak)":  (None, None, "flatpak run com.brave.Browser"),
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
_QUOTED = re.compile(r'^(["\'])(.*)\1$')


def _expand(path: str) -> str:
    """展开 ~、Windows 环境变量（%LOCALAPPDATA% 等）与成对引号后归一路径。

    引用了未设置的环境变量时抛 ValueError（配置写错应显式报错，不静默保留字面量）。
    """
    v = str(path).strip()
    m = _QUOTED.match(v)
    if m:
        v = m.group(2).strip()
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
        return os.path.normpath(os.path.expanduser(_QUOTED.sub(r"\2", str(path).strip())))


def is_browser_data_dir(path: str) -> bool:
    """判定目录是否为 Chromium 系浏览器数据目录（标志文件 Local State）。"""
    try:
        return os.path.isdir(path) and os.path.isfile(os.path.join(path, "Local State"))
    except OSError:
        return False


# ── DevToolsActivePort 读取（四分类的唯一事实来源）──────────────

def read_devtools_info(path: str) -> dict:
    """读数据目录下的 DevToolsActivePort → 结构化结果。

    成功：{"ok": True, "port": int, "ws_path": str, "endpoint": str}
      首行端口号，次行（若有）是 browser WebSocket 路径。新式开关
      （chrome://inspect 勾选）会禁用 /json/version 等 HTTP 端点，只认文件里
      的 ws 路径——因此有次行时必须 ws:// 直连；无次行（旧式命令行启动）
      回退 http://。
    失败：{"ok": False, "reason": not-enabled|unreadable|invalid, "detail": str}
    """
    file = os.path.join(path, "DevToolsActivePort")
    try:
        with open(file, "r", encoding="ascii") as f:
            content = f.read()
    except OSError as e:
        if e.errno in (_errno.ENOENT, _errno.ENOTDIR):
            # 文件/目录不存在（浏览器没运行或开关没开是最常见成因）→ 未开启
            return {"ok": False, "reason": "not-enabled",
                    "detail": "没有 DevToolsActivePort（浏览器未运行，或远程调试开关未开启）"}
        return {"ok": False, "reason": "unreadable",
                "detail": f"无法读取 DevToolsActivePort（{type(e).__name__}"
                          f"{f' errno={e.errno}' if e.errno else ''}）"}
    except ValueError:
        # UnicodeDecodeError 是 ValueError 不是 OSError：撕裂写/崩溃残留的非 ASCII
        # 字节正是 invalid 分类存在的理由，不能裸抛（会炸掉 /attach/discover）
        return {"ok": False, "reason": "invalid",
                "detail": "DevToolsActivePort 含非 ASCII 字节（文件损坏，完全退出并"
                          "重启浏览器可重写该文件）"}
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "reason": "invalid", "detail": "DevToolsActivePort 为空文件"}
    try:
        port = int(lines[0])
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        return {"ok": False, "reason": "invalid",
                "detail": f"DevToolsActivePort 端口行无效: {lines[0]!r}"}
    ws_path = lines[1] if len(lines) > 1 else ""
    endpoint = f"ws://127.0.0.1:{port}{ws_path}" if ws_path else f"http://127.0.0.1:{port}"
    return {"ok": True, "port": port, "ws_path": ws_path, "endpoint": endpoint}


def read_devtools_endpoint(path: str) -> Optional[dict]:
    """兼容入口：成功返回 {"port", "endpoint"}，任何失败返回 None。"""
    info = read_devtools_info(path)
    if not info.get("ok"):
        return None
    return {"port": info["port"], "endpoint": info["endpoint"]}


def read_devtools_port(path: str) -> Optional[int]:
    """读端口号（仅展示用；连接与分类请用 read_devtools_info）。"""
    info = read_devtools_info(path)
    return info["port"] if info.get("ok") else None


# ── 浏览器发现（清单内）────────────────────────────────────

def default_roots() -> list:
    key = "win32" if os.name == "nt" else ("darwin" if os.path.exists("/System/Library/CoreServices") else "linux")
    return list(_BROWSER_ROOTS.get(key, []))


def display_name(data_dir: str) -> str:
    """数据目录路径里最有辨识度的文件夹名（跳过通用的 User Data 层）。"""
    parts = [p for p in re.split(r"[\\/]+", str(data_dir)) if p]
    for p in reversed(parts):
        if p.lower() != "user data":
            return p
    return str(data_dir)


def browser_launch_hint(name: str) -> str:
    """打开该浏览器的系统命令建议（供 agent 代开浏览器；无映射时通用提示）。"""
    cmds = _LAUNCH_CMDS.get(name)
    if cmds:
        if os.name == "nt":
            cmd = cmds[1]
        elif sys.platform == "darwin":
            cmd = cmds[0]
        else:
            cmd = cmds[2]
        if cmd:
            return f"`{cmd}`"
    return "用系统方式打开该浏览器（应用列表 / 开始菜单 / Dock）"


# offline 细节的唯一文案（describe 与各解析路径共用，防漂移）
_OFFLINE_DETAIL = "DevToolsActivePort 记录的端口 {port} 未在监听（浏览器未运行或开关已关闭）"


def _describe_browser_dir(data_dir: str, known_names: Optional[dict] = None) -> dict:
    """单个数据目录的探测结论：显示名 + 远程调试状态（端口文件 + TCP 探测）。

    这是「读端口文件 → TCP 探测 → 四分类」的**单点实现**：discover / probe /
    _resolve_dir / _auto_resolve 全部消费它的结论，不各自重探。成功时一并给出
    endpoint（调用方不必为拿 ws 路径重读文件）。TCP 探测区分「端口文件在但
    浏览器没在听」（崩溃残留的 stale 文件）——只看文件会把已退出的浏览器误报
    为可附着。显示名优先取已知清单规范名（与 find-url --browser 用同一套名字），
    清单外才退回路径推断名。
    """
    data_name = (known_names or {}).get(_realpath_or_self(data_dir)) or display_name(data_dir)
    info = read_devtools_info(data_dir)
    if info.get("ok"):
        port = info["port"]
        listening = port_listening(port)
        remote = {"enabled": listening, "port": port, "detail":
                  "" if listening else _OFFLINE_DETAIL.format(port=port)}
        if listening:
            # 端点随结论一并给出：消费方（resolve/auto）无需重读端口文件
            remote["ws_path"] = info["ws_path"]
            remote["endpoint"] = info["endpoint"]
        else:
            # offline 是四分类之一，但文件读取层判不了它（需要 TCP）——
            # 在这里补上 reason，保证 auto 解析路径不会把它误标成 not-enabled
            remote["reason"] = "offline"
    else:
        reason, detail = info.get("reason"), info.get("detail", "")
        if reason == "unreadable":
            detail = detail + "；可在偏好中配置 BROWSER_AGENT_CDP_ENDPOINT 显式指定已核对的端口"
        remote = {"enabled": False, "port": None, "reason": reason, "detail": detail}
    return {"name": data_name, "data_dir": data_dir,
            "remote_debugging": remote,
            "attach_hint": f'browser-agent-client attach "{data_dir}"'}


def discover_browsers(roots: Optional[list] = None) -> list:
    """列出本机已安装且数据目录可辨认的浏览器（含远程调试端口状态）。

    只在已知目录清单内探测；不做目录名模糊扫描、不读目录内容以外的任何文件。
    remote_debugging 以 TCP 探测为准（文件存在但端口不在监听 = False + detail 说明）。
    """
    entries = []
    for name, raw_dir in (roots if roots is not None else default_roots()):
        d = _expand_safe(raw_dir)
        if not is_browser_data_dir(d):
            continue
        entries.append(_describe_browser_dir(d, {_realpath_or_self(d): name}))
    return entries


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


# ── 端点解析（失败分类 + 分类 remedy）────────────────────────

def error_response(error: str, candidates: Optional[list] = None, *,
                   klass: str, reason: Optional[str] = None,
                   remedy: Optional[list] = None) -> dict:
    """附着失败的统一错误报文。

    klass 为必填关键字（config-error | unavailable | ambiguous | not-found）——
    分类决定 remedy 语义，缺省值会让调用方拿到「目标不可达」的指引去做
    「修配置」的事；remedy 为分步列表；candidates 为本机检测到的浏览器。
    """
    res = {"ok": False, "error": error, "class": klass, "remedy": remedy or [REMEDY_STEPS]}
    if reason:
        res["reason"] = reason
    if candidates:
        res["candidates"] = candidates
    return res


def _validate_endpoint_format(endpoint: str) -> Optional[str]:
    """端点格式校验；返回错误信息，None = 合法。"""
    try:
        u = urlparse(str(endpoint))
    except ValueError:
        return f"CDP 端点格式无效: {endpoint}"
    if u.scheme not in ("ws", "http") or not u.hostname:
        return (f"CDP 端点须为 ws:// 或 http:// 且带主机"
                f"（如 ws://127.0.0.1:9222/devtools/browser/xxx），当前: {endpoint}")
    return None


def _unavailable_remedy(reason: str, detail: str, browser_label: str) -> list:
    """unavailable 四分类各自的分步处理（agent 优先自动解，再升级找用户）。"""
    if reason == "unreadable":
        return [
            f"系统拒绝读取该浏览器的 DevToolsActivePort（{detail}）。",
            "出路：确认该浏览器真实监听的调试端口后，在偏好中配置 "
            "BROWSER_AGENT_CDP_ENDPOINT=ws://127.0.0.1:<端口>（browser-agent-client "
            "attach <ws://端点> 即可保存），不会猜测端口或切换浏览器。",
        ]
    if reason == "invalid":
        return [
            f"{browser_label} 的 DevToolsActivePort 内容损坏（{detail}）。",
            f"出路：完全退出 {browser_label} 再重新打开（让其重写端口文件），然后重试。",
        ]
    # not-enabled / offline：共同根因大概率是浏览器没在运行——agent 先代开
    return [
        f"{browser_label} 的远程调试端口不可用（{detail}）。",
        f"Agent 先代开浏览器再重试（系统命令示例：{browser_launch_hint(browser_label)}）"
        "——浏览器没在运行是最常见成因，打开即可自愈。",
        f"仍失败再请用户处理：{REMEDY_STEPS}",
    ]


def _unavailable(error_line: str, label: str, reason: str, detail: str,
                 candidates: Optional[list] = None) -> dict:
    """unavailable 类错误的统一构造：各调用点只贡献自己的错误行，其余接线一处定。"""
    return error_response(
        error_line, candidates=candidates, klass="unavailable", reason=reason,
        remedy=_unavailable_remedy(reason, detail, label))


_CONFIG_ERROR_REMEDY = [
    "该目录不是 Chromium 浏览器的用户数据目录（须为含 Local State 的那一层）。",
    "确认真实路径：browser-agent-client attach --discover 列出已知浏览器，"
    "清单外用 attach --probe <应用数据根目录下的文件夹> 探测。",
    "改正偏好（browser-agent-client attach <正确目录>）后重试——"
    "这是配置问题，不需要重启或停止服务即可改正。",
]


def validate_attach_dir(raw: str) -> dict:
    """attach 目录的规范校验（client.attach 与 POST /attach/select 共用的单一入口）。

    成功：{"ok": True, "dir": <展开后的绝对路径>}
    失败：标准 config-error 报文（klass/reason/remedy 在本模块单点定义）。
    """
    try:
        d = expand_path(raw)
    except ValueError as e:
        return error_response(f"attach 目录路径无效: {e}", klass="config-error",
                              remedy=["修正路径写法（~ 展开 / Windows 环境变量）后重试。"])
    if not is_browser_data_dir(d):
        return error_response(
            f"目录不是浏览器数据目录（缺少 Local State）: {d}",
            candidates=discover_browsers(), klass="config-error",
            remedy=_CONFIG_ERROR_REMEDY)
    return {"ok": True, "dir": d}


def _resolve_dir(target_dir: str) -> dict:
    """解析指定数据目录 → 端点（happy path 不做全盘扫描，失败才带 candidates）。

    探测走 _describe_browser_dir 单点实现（读文件 + TCP 各一次），不在本函数重探。
    """
    if not is_browser_data_dir(target_dir):
        return error_response(
            f"配置的浏览器数据目录无效（不含 Local State）: {target_dir}",
            candidates=discover_browsers(), klass="config-error",
            remedy=_CONFIG_ERROR_REMEDY)
    label = display_name(target_dir)
    rd = _describe_browser_dir(target_dir)["remote_debugging"]
    if rd["enabled"]:
        return {"ok": True, "endpoint": rd["endpoint"],
                "browser_dir": target_dir, "port": rd["port"], "source": "configured-dir"}
    return _unavailable(
        f"浏览器 {label}（{target_dir}）远程调试不可用：{rd['detail']}",
        label, rd.get("reason", "not-enabled"), rd["detail"],
        candidates=discover_browsers())


def _auto_resolve() -> dict:
    """无显式目标：仅当恰好一台可用时自动采用；多台要求明确选择。"""
    candidates = discover_browsers()
    with_debug = [c for c in candidates if c["remote_debugging"]["enabled"]]

    def _pick(c, source):
        rd = c["remote_debugging"]
        return {"ok": True, "endpoint": rd["endpoint"], "browser_dir": c["data_dir"],
                "port": rd["port"], "source": source}

    if len(candidates) == 1:
        c = candidates[0]
        rd = c["remote_debugging"]
        if not rd["enabled"]:
            return _unavailable(
                f"唯一检测到的浏览器 {c['name']} 远程调试不可用：{rd['detail']}",
                c["name"], rd.get("reason", "not-enabled"), rd["detail"], candidates)
        return _pick(c, "auto-single")
    if len(with_debug) == 1:
        return _pick(with_debug[0], "auto-single-debug")
    if len(candidates) > 1:
        return error_response(
            "检测到多台浏览器，未配置偏好，拒绝猜测目标",
            candidates=candidates, klass="ambiguous",
            remedy=[
                "必须明确选择其一（即使只开着一台调试，也建议固化偏好）：",
                *[f"browser-agent-client attach \"{c['data_dir']}\"    # {c['name']}"
                  for c in candidates],
                "或通过环境变量 BROWSER_AGENT_ATTACH_DIR / 配置 browser.attach_dir 指定。",
            ],
        )
    return error_response(
        "未找到任何可辨认的 Chromium 系浏览器数据目录",
        klass="not-found",
        remedy=[
            "browser-agent-client attach --discover 查看已知清单与应用数据根目录文件夹名；",
            "清单外浏览器：从文件夹名认出候选后 attach --probe <文件夹> 受控探测；",
            f"确认远程调试已开启（{REMEDY_STEPS}）。",
        ],
    )


def resolve_endpoint(attach_dir: Optional[str] = None,
                     cdp_endpoint: Optional[str] = None) -> dict:
    """解析附着端点。优先级：显式 cdp_endpoint > 显式 attach_dir > 已存偏好 > 唯一已装浏览器。

    返回 {"ok": True, "endpoint": ..., "browser_dir": ..., "source": ...}
    或 {"ok": False, "error", "class", "reason"?, "remedy": [...], "candidates"?}。
    绝不静默换目标浏览器。
    """
    pref = read_preference()
    explicit_dir = _expand_safe(attach_dir) if attach_dir else None

    # 显式 cdp_endpoint 最优先；其次（未显式给目录时）已保存的端点偏好
    endpoint = cdp_endpoint or ""
    if not endpoint and explicit_dir is None:
        endpoint = pref["cdp_endpoint"]
    if endpoint:
        fmt_err = _validate_endpoint_format(endpoint)
        if fmt_err:
            return error_response(fmt_err, klass="config-error",
                                  remedy=["修正端点写法（ws:// 或 http:// + 主机）后重试。"])
        return {"ok": True, "endpoint": endpoint, "browser_dir": None,
                "source": "explicit-endpoint"}

    target_dir = explicit_dir or (pref["attach_dir"] or None)
    if target_dir:
        return _resolve_dir(target_dir)
    return _auto_resolve()


def format_attach_error(info: dict) -> str:
    """把 resolve_endpoint 的失败结果格式化为给 AI/用户的可执行报错（分类分步）。"""
    msg = f"附着日常浏览器失败: {info.get('error')}"
    remedy = info.get("remedy") or []
    if remedy:
        msg += "\n处理步骤:"
        for i, step in enumerate(remedy, 1):
            msg += f"\n {i}. {step}"
    cands = info.get("candidates") or []
    if cands:
        lines = []
        for c in cands:
            rd = c.get("remote_debugging") or {}
            state = "已开（端口在监听）" if rd.get("enabled") else f"未就绪 — {rd.get('detail', '未开')}"
            lines.append(f"- {c['name']}: {c.get('data_dir', c.get('dir', ''))}（远程调试{state}）")
        msg += "\n本机检测到的浏览器:\n" + "\n".join(lines)
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


def _identity_path(p: str) -> str:
    """目录身份的归一比较键：真实路径 + 大小写归一。

    macOS（APFS 默认大小写不敏感卷）与 Windows 上，同一浏览器的目录写法
    大小写不同也应视为同一身份——否则会把「偏好重存一次」误判成换目标。
    """
    rp = _realpath_or_self(p)
    if sys.platform == "darwin" or os.name == "nt":
        return rp.casefold()
    return rp


def pin_mismatch_error(new_identity: Optional[str]) -> Optional[str]:
    """新身份与已 pin 身份不同 → 可直接展示的错误；相同/无法比较 → None。"""
    old = _PROCESS_PIN["dir"]
    if not old or not new_identity:
        return None
    new = str(new_identity)
    if old.startswith("endpoint:") or new.startswith("endpoint:"):
        if old == new:
            return None
    elif _identity_path(old) == _identity_path(new):
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


def discover_payload() -> dict:
    """/attach/discover（HTTP）与 client attach --discover（CLI）共用的单一构造：
    已知浏览器 + 应用数据根目录文件夹名 + Agent 下一步 + 处理步骤。"""
    return {
        "ok": True,
        "browsers": discover_browsers(),
        "app_data_folders": list_app_data_folders(),
        "next_steps": [
            "1. 从 browsers 里选目标（远程调试已开的即可直接附着）；清单外浏览器从"
            " app_data_folders 的文件夹名认出候选（如 Dia、BraveSoftware）",
            "2. 清单外候选用受控探测确认数据目录与调试状态："
            'browser-agent-client attach --probe <文件夹名>',
            "3. 保存偏好并启动：browser-agent-client attach <数据目录>"
            "（之后 start 自动按偏好附着）",
            "4. 远程调试未开的浏览器，先处理开关：见 remedy",
        ],
        "probe_hint": ("清单外的浏览器：从应用数据根目录的文件夹名里认出"
                       "候选（如 Dia、BraveSoftware），用 attach --probe <文件夹> 探测"),
        "remedy": REMEDY_STEPS,
    }


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
