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

import os
from typing import Optional

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


def _expand(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


expand_path = _expand  # 供 client/server 复用的公开名


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
                    pref["attach_dir"] = _expand(line.split("=", 1)[1])
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
        "attach_dir": _expand(attach_dir) if attach_dir else "",
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
    explicit_dir = _expand(attach_dir) if attach_dir else None

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
