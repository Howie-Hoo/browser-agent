from __future__ import annotations

"""
BrowserProxyAuth — 认证会话管理 Mixin（跨项目通用）

提供「命名 profile」抽象：把一组浏览器登录态（cookies + localStorage）按名字落盘到
storage/profiles/{name}.json，可跨会话/跨重启恢复，支持多账号隔离。

设计原则：**本 mixin 零项目知识**。它不知道 JWT、不知道 localStorage 键名、不知道任何
登录端点。项目特定的「登录态长什么样」「怎么判定已登录」由 profile 文件里的 `auth.check`
规则描述（js / url_pattern / cookie_exists 三种）。项目侧负责生成 profile 文件（profile 格式
与接入步骤见仓库 SKILL.md「认证会话管理」）。

由 BrowserProxy 通过多继承组合使用，不独立实例化。
"""
import json
import os
import re
import time
from typing import Optional

from .utils import log, emit

# profile 名校验：字母/数字/下划线/连字符/点，首字符不能是点或斜杠类，限 63 字符。
# 拒绝前导点（.bashrc / ../）、斜杠（路径穿越）、空字节等。
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,62}$")

# storage/profiles 目录基准（包目录下的 storage/profiles）
_PROFILES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "profiles"))


def profiles_dir() -> str:
    return _PROFILES_DIR


def validate_profile_name(name: str) -> Optional[str]:
    """返回错误信息；None 表示合法。"""
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        return "profile 名只能含字母数字、下划线、连字符、点，且以字母数字开头（限 63 字符）"
    return None


def profile_path(name: str) -> str:
    """计算 profile 文件绝对路径，带路径穿越二次校验。"""
    err = validate_profile_name(name)
    if err:
        raise ValueError(err)
    full = os.path.normpath(os.path.join(_PROFILES_DIR, name + ".json"))
    # 纵深防御：即便正则被绕过，也要确保最终路径仍在 profiles/ 目录内
    if os.path.commonpath([_PROFILES_DIR, full]) != _PROFILES_DIR:
        raise ValueError(f"非法 profile 名（路径穿越）: {name!r}")
    return full


def _read_profile(name: str) -> Optional[dict]:
    path = profile_path(name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"读取 profile '{name}' 失败: {e}")
        return None


class BrowserProxyAuth:
    """认证会话 / 命名 profile 管理方法。"""

    # ── profile 元信息 ────────────────────────────────────
    async def has_profile(self, name: str) -> dict:
        err = validate_profile_name(name)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "name": name, "exists": os.path.isfile(profile_path(name))}

    async def list_profiles(self) -> dict:
        """列出所有 profile 元数据。**绝不返回 storage_state 内容**（防凭证泄露 / 爆破响应体积）。"""
        if not os.path.isdir(_PROFILES_DIR):
            return {"ok": True, "profiles": [], "count": 0}
        items = []
        for fn in os.listdir(_PROFILES_DIR):
            if not fn.endswith(".json"):
                continue
            data = _read_profile(fn[:-5])
            if data is None:
                items.append({"name": fn[:-5], "has_auth": False, "error": "parse_failed"})
                continue
            auth = data.get("auth") or {}
            items.append({
                "name": data.get("name", fn[:-5]),
                "has_auth": bool(auth.get("check")),
                "origin": auth.get("origin"),
                "updated_at": data.get("updated_at"),
            })
        items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return {"ok": True, "profiles": items, "count": len(items)}

    async def delete_profile(self, name: str) -> dict:
        err = validate_profile_name(name)
        if err:
            return {"ok": False, "error": err}
        path = profile_path(name)
        if not os.path.exists(path):
            return {"ok": False, "error": f"profile '{name}' 不存在"}
        os.remove(path)
        emit({"type": "delete_profile", "name": name, "ok": True})
        return {"ok": True, "name": name}

    # ── profile 保存 / 加载 ────────────────────────────────
    async def save_profile(self, name: str, include_auth: bool = True) -> dict:
        """把当前活跃 context 的完整 storage_state 落盘为命名 profile。"""
        err = validate_profile_name(name)
        if err:
            return {"ok": False, "error": err}
        if not self._page or not getattr(self._page, "context", None):
            return {"ok": False, "error": "无活跃页面/context，无法导出会话状态"}
        try:
            state = await self._page.context.storage_state()
        except Exception as e:
            return {"ok": False, "error": f"导出 storage_state 失败: {str(e).splitlines()[0]}"}

        os.makedirs(_PROFILES_DIR, exist_ok=True)
        path = profile_path(name)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        # 保留旧 profile 的 created_at 与（include_auth=False 时）auth 块 → 支持「只刷新 token 不动判定规则」
        created_at = now
        auth_block = None
        old = _read_profile(name)
        if old:
            created_at = old.get("created_at", now)
            auth_block = old.get("auth")
        if include_auth and auth_block is None:
            # 尝试沿用当前活跃 profile 的 auth 规则
            active = getattr(self, "_active_profile", None)
            if active:
                ap = _read_profile(active)
                if ap:
                    auth_block = ap.get("auth")

        profile = {
            "name": name,
            "created_at": created_at,
            "updated_at": now,
            "auth": auth_block,
            "storage_state": {
                "cookies": state.get("cookies", []),
                "origins": state.get("origins", []),
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        self._active_profile = name
        emit({"type": "save_profile", "name": name, "ok": True,
              "cookies": len(state.get("cookies", [])), "origins": len(state.get("origins", []))})
        return {"ok": True, "name": name,
                "cookies": len(state.get("cookies", [])), "origins": len(state.get("origins", []))}

    async def load_profile(self, name: str, into: str = "new", navigate: bool = True) -> dict:
        """
        加载命名 profile 的登录态。

        :param into: "new"（默认，推荐）= 用 storage_state 新建隔离 context，原子注入
                     cookies+localStorage（含 httpOnly/Secure，最可靠）；
                     "current" = 注入当前活跃 page（add_cookies + goto(origin) + setItem + reload，
                     较慢且需同源，仅在不能丢当前 context 时用）。
        :param navigate: 加载后是否导航到 profile.auth.origin。
        """
        if into not in ("new", "current"):
            return {"ok": False, "error": "into 必须是 'new' 或 'current'"}
        data = _read_profile(name)
        if data is None:
            return {"ok": False, "error": f"profile '{name}' 不存在"}
        state = data.get("storage_state") or {"cookies": [], "origins": []}
        origin = (data.get("auth") or {}).get("origin")

        if into == "new":
            ctx_name = f"profile::{name}"
            if ctx_name in self._contexts or ctx_name in self._pages:
                return {"ok": False, "error": f"已存在隔离 profile 会话 '{ctx_name}'，请先 close_tab 再加载"}
            from playwright_stealth import Stealth
            context_args = {"viewport": self.viewport, "storage_state": state}
            if getattr(self, "ignore_https_errors", False):
                context_args["ignore_https_errors"] = True
            if self.record_video_dir:
                context_args["record_video_dir"] = self.record_video_dir
            # 不继承 record_har_path：多 context 抢同一 HAR 文件会损坏
            try:
                context = await self._browser.new_context(**context_args)
            except Exception as e:
                return {"ok": False, "error": f"创建 context 失败: {str(e).splitlines()[0]}"}
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
            page.set_default_timeout(self.default_timeout)
            page.on("crash", lambda p: log(f"⚠️ profile 会话 '{name}' 页面崩溃"))
            page.on("download", self._on_download)
            self._setup_health_listeners(page)
            self._contexts[ctx_name] = context
            self._pages[ctx_name] = page
            self._active_tab = ctx_name
            self._active_profile = name
            self._elements_cache = []
            if navigate and origin:
                try:
                    await page.goto(origin, wait_until="domcontentloaded")
                except Exception as e:
                    log(f"load_profile 导航到 {origin} 失败（登录态已注入）: {e}")
            emit({"type": "load_profile", "name": name, "into": "new", "ok": True})
            return {"ok": True, "name": name, "into": "new", "context": ctx_name, "origin": origin}

        # into == "current"
        if not self._page:
            return {"ok": False, "error": "无活跃页面"}
        ctx = self._page.context
        try:
            await ctx.add_cookies(state.get("cookies", []))  # httpOnly / Secure 在此生效
            if origin:
                await self._page.goto(origin, wait_until="domcontentloaded")  # 必须同源才能写 localStorage
                for o in state.get("origins", []):
                    if o.get("origin") == origin:
                        for item in o.get("localStorage", []):
                            await self._page.evaluate(
                                "(a)=>localStorage.setItem(a[0],a[1])",
                                [item["name"], item["value"]],
                            )
                await self._page.reload(wait_until="networkidle")
        except Exception as e:
            return {"ok": False, "error": f"注入当前 context 失败: {str(e).splitlines()[0]}"}
        self._active_profile = name
        self._elements_cache = []
        emit({"type": "load_profile", "name": name, "into": "current", "ok": True})
        return {"ok": True, "name": name, "into": "current", "origin": origin}

    # ── 登录态检测 ────────────────────────────────────────
    async def check_auth(self, name: Optional[str] = None) -> dict:
        """
        判定登录态是否有效。

        :param name: 给定时，先把它 load 到隔离 context 再检测（不破坏当前活跃 page）；
                     不给时检测当前活跃 page（按其 profile 的 auth.check，或返回无判定规则）。
        """
        if name:
            err = validate_profile_name(name)
            if err:
                return {"ok": False, "error": err}
            data = _read_profile(name)
            if data is None:
                return {"ok": False, "error": f"profile '{name}' 不存在"}
            ctx_name = f"profile::{name}"
            if ctx_name in self._pages:
                # profile context 已存在：切过去判定，避免重复 load 触发"已存在会话"报错
                # _page 是按 _active_tab 取的只读 property，切换 tab 即可
                self._active_tab = ctx_name
                self._active_profile = name
                self._elements_cache = []
            else:
                lp = await self.load_profile(name, into="new", navigate=False)
                if not lp.get("ok"):
                    return lp
        else:
            active = getattr(self, "_active_profile", None)
            data = _read_profile(active) if active else None

        auth = (data or {}).get("auth") or {}
        check = auth.get("check")
        origin = auth.get("origin")
        if not check:
            return {"ok": True, "authenticated": False,
                    "reason": "profile 未定义 auth.check（仅是存储快照，无法判定登录态）"}
        if not self._page:
            return {"ok": False, "error": "无活跃页面"}

        # 确保在 origin 上，JS / URL / cookie 读取才一致
        if origin and not self._page.url.startswith(origin.rstrip("/")):
            try:
                await self._page.goto(origin, wait_until="domcontentloaded")
            except Exception:
                pass

        ctype = check.get("type")
        try:
            if ctype == "js":
                authed = bool(await self._page.evaluate(check["script"]))
            elif ctype == "url_pattern":
                hit = bool(re.search(check["pattern"], self._page.url))
                authed = (not hit) if check.get("negative") else hit
            elif ctype == "cookie_exists":
                cookies = await self._page.context.cookies()
                nm = check["name"]
                dom = check.get("domain")
                now = time.time()
                hit = any(
                    c.get("name") == nm
                    and (not dom or dom in c.get("domain", ""))
                    # 过滤已过期 cookie（expires=-1 表示会话 cookie，视为有效）
                    and (c.get("expires", -1) == -1 or c.get("expires", 0) >= now)
                    for c in cookies
                )
                authed = (not hit) if check.get("negative") else hit
            else:
                return {"ok": False, "error": f"未知 auth.check.type: {ctype}（支持 js / url_pattern / cookie_exists）"}
        except Exception as e:
            return {"ok": False, "error": f"check_auth 执行失败: {str(e).splitlines()[0]}"}

        emit({"type": "check_auth", "name": name, "authenticated": authed, "ok": True})
        return {"ok": True, "name": name, "authenticated": authed, "method": ctype}

    async def ensure_auth(self, name: Optional[str] = None,
                          login_commands: Optional[list] = None, into: str = "new") -> dict:
        """
        确保处于已登录状态：
        1. profile 存在且 check_auth 通过 → 直接复用；
        2. 否则执行 login_commands（复用 _dispatch，所以 navigate/fill_form/detect_captcha
           等任何现有命令都能当登录流程）；
        3. 再 check；若提供了 name 则 save_profile 落盘新登录态。
        """
        if name:
            err = validate_profile_name(name)
            if err:
                return {"ok": False, "error": err}
            if _read_profile(name) is not None:
                res = await self.check_auth(name)
                if res.get("authenticated"):
                    return {"ok": True, "name": name, "result": "reused", "detail": res}

        if login_commands:
            for cmd in login_commands:
                await self._dispatch(cmd)

        res = await self.check_auth()
        if name:
            if res.get("authenticated"):
                await self.save_profile(name)
                return {"ok": True, "name": name, "result": "fresh", "detail": res}
            return {"ok": False, "name": name, "result": "unauthenticated", "detail": res}
        return {"ok": True, "result": "login_executed", "login_steps": len(login_commands or []),
                "detail": res}
