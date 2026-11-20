from __future__ import annotations

"""
BrowserProxyTabs — 多标签页/iframe/网络拦截/状态持久化/断言 Mixin
由 BrowserProxy 通过多继承组合使用，不独立实例化。
"""
import asyncio
import json
import os
from typing import Optional

from .utils import log, emit
from . import user_data


class BrowserProxyTabs:
    """多标签页、iframe、网络拦截、状态持久化、断言相关方法。"""

    # ── 多标签页管理 ──────────────────────────────────────────
    async def new_tab(self, name: str) -> dict:
        if name in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 已存在"}
        # 新 tab 落在当前活跃页所属 context（多会话隔离下不串到别的会话）
        context = self._page.context if self._page else self._contexts.get("default")
        if not context: return {"ok": False, "error": "当前上下文不存在"}
        await self._register_page(context, name)
        emit({"type": "new_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    async def new_isolated_session(self, name: str, profile: Optional[str] = None) -> dict:
        if name in self._contexts or name in self._pages:
            return {"ok": False, "error": f"名称 '{name}' 已被占用"}
        context_args = {"viewport": self.viewport}
        if getattr(self, "ignore_https_errors", False):
            context_args["ignore_https_errors"] = True
        if self.record_video_dir: context_args["record_video_dir"] = self.record_video_dir
        # 注入命名 profile 的登录态（cookies + localStorage，原子且含 httpOnly/Secure）
        if profile:
            from .proxy_auth import validate_profile_name, _read_profile
            err = validate_profile_name(profile)
            if err:
                return {"ok": False, "error": err}
            data = _read_profile(profile)
            if data is None:
                return {"ok": False, "error": f"profile '{profile}' 不存在"}
            context_args["storage_state"] = data.get("storage_state") or {"cookies": [], "origins": []}
            # 不继承 record_har_path：多 context 抢同一 HAR 文件会损坏
        elif self.record_har_path:
            context_args["record_har_path"] = self.record_har_path
        context = await self._browser.new_context(**context_args)
        self._contexts[name] = context
        await self._register_page(context, name)
        if profile:
            self._set_active_profile(profile)
        emit({"type": "new_isolated_session", "name": name, "profile": profile, "ok": True})
        return {"ok": True, "name": name, "profile": profile}

    async def new_shared_session(self, name: str) -> dict:
        """在 default context（managed=启动 context；attach=用户真实 profile）内建会话页。

        共享 cookie/storage：attach 模式下即「带着用户登录态操作」；
        会话关闭时只关自建标签页，绝不 close 共享 context。
        """
        if name in self._contexts:
            return {"ok": False, "error": f"名称 '{name}' 已被占用（isolated 会话同名）"}
        if any(k == name or k.startswith(self.tab_key(name, "")) for k in self._pages):
            return {"ok": False, "error": f"会话 '{name}' 已有标签页"}
        context = self._contexts.get("default")
        if not context:
            return {"ok": False, "error": "default context 不存在（浏览器未启动）"}
        await self._register_page(context, name)
        emit({"type": "new_shared_session", "name": name, "ok": True})
        return {"ok": True, "name": name, "context": "shared"}

    async def switch_tab(self, name: str) -> dict:
        if name not in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 不存在"}
        self._set_current_tab(name)
        # attach 模式不抢用户焦点：只切换本服务的操作目标，不把标签页拉到前台。
        # 例外：adopt_tab 收养的页是用户明确要求操作的——切到它时照常激活
        if not self._shared_default_context or name in self._adopted_tabs:
            await self._page.bring_to_front()
        emit({"type": "switch_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    async def _unregister_page(self, key: str, close: bool) -> None:
        """注销一个已注册页面。close=False 或页面属于用户（adopt_tab 收养）时
        只取消注册、绝不关闭浏览器里的页——最小侵入契约的唯一实现点。"""
        page = self._pages.pop(key, None)
        if page is not None and close and key not in self._adopted_tabs:
            try:
                await page.close()
            except Exception:
                pass
        self._adopted_tabs.discard(key)
        self._purge_tab_state(key)

    def _owned_fallback_tab(self, closed_key: str, closed_ctx) -> Optional[str]:
        """关闭页后的回落目标：只在本会话名下（键前缀归属）且非收养的页里选。

        共享 default context 混着多个会话的页与收养的用户页——按 context 猜会
        落到别人的页上。找不到本会话的页时返回 None（后续命令会得到明确的
        「目标已关闭」失败，而不是静默污染他会有话）。
        """
        owner = self.tab_owner(closed_key)
        same_ctx = [k for k, p in self._pages.items()
                    if p.context is closed_ctx and k not in self._adopted_tabs]
        own = [k for k in same_ctx if owner and (k == owner or k.startswith(owner + "::"))]
        return own[0] if own else (same_ctx[0] if owner is None and same_ctx else None)

    async def close_tab(self, name: str) -> dict:
        if name not in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 不存在"}
        closed_ctx = self._pages[name].context
        adopted = name in self._adopted_tabs
        await self._unregister_page(name, close=not adopted)
        if self._tab() == name:
            # 只回落到本会话名下的其余 tab（绝不落到他会有话/收养页）
            fallback = self._owned_fallback_tab(name, closed_ctx)
            if fallback is not None:
                self._set_current_tab(fallback)
        if adopted:
            emit({"type": "close_tab", "name": name, "adopted": True, "ok": True})
            return {"ok": True, "name": name, "note": "已取消注册用户标签页（浏览器中保持打开）"}
        emit({"type": "close_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    # ── 用户标签页收养（attach 模式，需用户明确要求）─────────
    async def list_user_tabs(self) -> dict:
        """列出 default context 里的全部标签页（含用户已打开的）。

        最小侵入契约的默认是不碰用户标签页；本命令与 adopt_tab 仅供
        **用户明确要求**操作其某个标签页时使用。
        """
        ctx = self._contexts.get("default")
        if not ctx:
            return {"ok": False, "error": "default context 不存在（浏览器未启动/未附着）"}
        tabs = []
        pages = ctx.pages
        known = {p: k for k, p in self._pages.items()}

        async def _title(p):
            try:
                return await p.title()
            except Exception:
                return ""

        titles = await asyncio.gather(*(_title(p) for p in pages))  # 并发采集，挂起页不拖垮全表
        for i, (p, title) in enumerate(zip(pages, titles)):
            tabs.append({
                "index": i,
                "title": (title or "")[:60],
                "url": p.url[:120],
                "managed": p in known,       # 是否已在服务注册（自建或已收养）
                "key": known.get(p, ""),
            })
        return {"ok": True, "tabs": tabs, "count": len(tabs)}

    async def adopt_tab(self, index: int, name: str = "", activate: bool = True) -> dict:
        """把 default context 中第 index 个已有标签页纳入服务操作。

        用户明确要求操作其标签页时的唯一入口（如「切到我的第一个标签页」）。
        收养的页：切换时会真正激活（bring_to_front）；服务/会话关闭时只取消
        注册、绝不关闭该页。
        """
        ctx = self._contexts.get("default")
        if not ctx:
            return {"ok": False, "error": "default context 不存在（浏览器未启动/未附着）"}
        pages = ctx.pages
        if not isinstance(index, int) or index < 0 or index >= len(pages):
            return {"ok": False, "error": f"index 越界（共 {len(pages)} 个标签页）",
                    "ai_hint": "先执行 list_user_tabs 查看有效 index"}
        page = pages[index]
        known = {p: k for k, p in self._pages.items()}
        if page in known:
            self._set_current_tab(known[page])
            if activate:
                await page.bring_to_front()
            return {"ok": True, "name": known[page], "adopted": False,
                    "note": "该标签页已注册，直接切换"}
        key = name or f"user-tab-{index}"
        if key in self._pages:
            return {"ok": False, "error": f"键 '{key}' 已被占用，换一个 name"}
        self._pages[key] = page
        self._adopted_tabs.add(key)
        self._set_current_tab(key)
        if activate:
            try:
                await page.bring_to_front()
            except Exception:
                pass
        emit({"type": "adopt_tab", "index": index, "name": key, "ok": True})
        return {"ok": True, "name": key, "index": index, "url": page.url[:120]}

    # ── 网络拦截 ──────────────────────────────────────────
    async def mock_route(self, url_pattern: str, response_data: dict) -> dict:
        async def handle_route(route, request):
            await route.fulfill(
                status=response_data.get("status", 200),
                body=json.dumps(response_data.get("body", {})),
                content_type="application/json"
            )
        await self._page.route(url_pattern, handle_route)
        self._routes.setdefault(self._tab(), []).append(url_pattern)
        emit({"type": "mock_route", "pattern": url_pattern, "ok": True})
        return {"ok": True, "pattern": url_pattern}

    async def clear_routes(self) -> dict:
        for p in self._routes.pop(self._tab(), []):
            try:
                await self._page.unroute(p)
            except Exception:
                pass
        emit({"type": "clear_routes", "ok": True})
        return {"ok": True}

    async def analyze_network(self) -> dict:
        if not self.record_har_path or not os.path.exists(self.record_har_path):
            return {"ok": False, "error": "HAR 文件未配置或抓包尚未落盘生成"}
        try:
            with open(self.record_har_path, "r", encoding="utf-8") as f:
                har_data = json.load(f)
            api_calls = []
            for entry in har_data.get("log", {}).get("entries", []):
                req = entry.get("request", {})
                res = entry.get("response", {})
                url = req.get("url", "")
                mime = res.get("content", {}).get("mimeType", "")
                if "api" in url.lower() or "json" in url.lower() or mime.startswith("application/json"):
                    api_calls.append({
                        "url": url,
                        "method": req.get("method"),
                        "status": res.get("status"),
                        "req_body": req.get("postData", {}).get("text", "")[:500],
                        "res_body": res.get("content", {}).get("text", "")[:500]
                    })
            return {"ok": True, "api_calls": api_calls, "total_apis": len(api_calls)}
        except Exception as e:
            return {"ok": False, "error": f"解析 HAR 失败: {e}"}

    # ── 状态持久化 ──────────────────────────────────────────
    async def save_cookies(self, path: str) -> dict:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        cookies = await self._page.context.cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        emit({"type": "save_cookies", "path": path, "count": len(cookies), "ok": True})
        return {"ok": True, "path": path, "count": len(cookies)}

    async def load_cookies(self, path: str) -> dict:
        if not os.path.exists(path):
            return {"ok": False, "error": f"文件不存在: {path}"}
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await self._page.context.add_cookies(cookies)
        emit({"type": "load_cookies", "path": path, "count": len(cookies), "ok": True})
        return {"ok": True, "loaded": len(cookies)}

    async def save_storage(self, path: str) -> dict:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        await self._page.context.storage_state(path=path)
        emit({"type": "save_storage", "path": path, "ok": True})
        return {"ok": True, "path": path}

    async def clear_storage(self) -> dict:
        # 安全边界（咽喉点拦截：/run、ensure_auth 的 login_commands、loop 子命令、
        # 宏——全部经 _dispatch 走到这里）：
        # 1) attach 的用户日常浏览器上下文 → 拒绝（不清用户真实登录态）
        # 2) server 注册的守卫钩子按 sid 判定（managed 下 default context 多会话共享 → 拒绝）
        if self._in_user_context():
            return {
                "ok": False,
                "error": "当前会话运行在共享用户上下文（attach/user 模式 default context），拒绝清除",
                "ai_hint": "需要干净环境时用隔离会话（new_isolated_session / context=isolated）",
            }
        if self._storage_guard is not None:
            refusal = self._storage_guard(self._router_sid())
            if refusal:
                return {
                    "ok": False,
                    "error": refusal,
                    "ai_hint": "需要干净环境时改用隔离会话（session/start 传 context=isolated）",
                }
        cleared_files = []
        for name, path in [("cookies.json", user_data.cookies_file()), ("state.json", user_data.state_file())]:
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    cleared_files.append(name)
                except OSError:
                    pass
        await self._clear_live_storage()
        emit({"type": "clear_storage", "cleared_files": cleared_files, "ok": True})
        return {"ok": True, "cleared_files": cleared_files}

    async def _clear_live_storage(self) -> None:
        """清除当前页的内存态（cookies + localStorage/sessionStorage）。

        clear_storage 命令与 /session/refresh 端点共用；守卫在各入口做。"""
        if self._page and self._page.context:
            await self._page.context.clear_cookies()
        try:
            await self._page.evaluate("try { localStorage.clear(); sessionStorage.clear(); } catch(e){}")
        except Exception:
            pass
        self._cache_invalidate()

    # ── 断言 ──────────────────────────────────────────
    async def assert_text(self, selector: str, expected: str) -> dict:
        actual = await self._page.locator(selector).first.inner_text()
        passed = expected in actual
        res = {"ok": passed, "passed": passed, "expected": expected, "actual": actual}
        if not passed: res["error"] = f"断言失败: 期望包含 '{expected}'，实际为 '{actual}'"
        emit({**res, "type": "assert_text", "selector": selector})
        return res

    async def assert_url(self, expected: str) -> dict:
        actual = self._page.url
        passed = expected in actual
        res = {"ok": passed, "passed": passed, "expected": expected, "actual": actual}
        if not passed: res["error"] = f"断言失败: 期望 URL 包含 '{expected}'，实际为 '{actual}'"
        emit({**res, "type": "assert_url"})
        return res

    async def assert_visible(self, selector: str) -> dict:
        passed = await self._page.locator(selector).first.is_visible()
        res = {"ok": passed, "passed": passed, "selector": selector}
        if not passed: res["error"] = f"断言失败: 元素 '{selector}' 不可见"
        emit({**res, "type": "assert_visible"})
        return res

    async def assert_value(self, selector: str, expected: str) -> dict:
        actual = await self._page.locator(selector).first.input_value()
        passed = expected == actual
        res = {"ok": passed, "passed": passed, "expected": expected, "actual": actual}
        if not passed: res["error"] = f"断言失败: 期望值 '{expected}'，实际为 '{actual}'"
        emit({**res, "type": "assert_value", "selector": selector})
        return res

    # ── iframe 切换 ──────────────────────────────────────────
    async def switch_frame(self, selector: str = None, name: str = None, url_contains: str = None) -> dict:
        """
        切换到指定 iframe/frame 上下文。切换后所有 click/fill 等操作均在该 frame 内执行。
        三种定位方式：CSS selector(包含 iframe 标签) / frame name / URL 包含字符串。
        找不到时返回所有可用 frame 清单供 AI 选择。
        """
        page = self._main_page
        target_frame = None

        try:
            if selector:
                el = await page.query_selector(selector)
                if el:
                    target_frame = await el.content_frame()
            elif name:
                target_frame = page.frame(name=name)
            elif url_contains:
                target_frame = next((f for f in page.frames if url_contains in f.url), None)
        except Exception as e:
            return {"ok": False, "error": str(e).split(chr(10))[0]}

        if not target_frame:
            frames_info = [{"name": f.name or "(unnamed)", "url": f.url} for f in page.frames]
            emit({"type": "switch_frame", "ok": False})
            return {
                "ok": False,
                "error": "未找到指定 frame",
                "available_frames": frames_info,
                "ai_hint": (
                    "请从 available_frames 中选择目标 frame："
                    " 用 url_contains 匹配 URL 片段，或用 name 匹配 frame name，"
                    "或用 selector 定位 <iframe> 元素（如 'iframe#payment'）"
                )
            }

        self._frames[self._tab()] = target_frame
        emit({"type": "switch_frame", "url": target_frame.url, "ok": True})
        return {"ok": True, "frame_url": target_frame.url, "frame_name": target_frame.name or "(unnamed)"}

    async def switch_to_main_frame(self) -> dict:
        """退出 iframe 上下文，切回主页面。所有后续操作回归主页面执行。"""
        key = self._tab()
        if key in self._frames:
            del self._frames[key]
        emit({"type": "switch_to_main_frame", "ok": True})
        return {"ok": True, "note": "已切回主页面上下文"}

    # ── 下载等待 ──────────────────────────────────────────
    async def wait_for_download(self, filename: str) -> dict:
        for _ in range(30):
            if filename in self._downloads:
                return {"ok": True, "path": self._downloads[filename]}
            import asyncio
            await asyncio.sleep(1)
        return {"ok": False, "error": f"等待文件 '{filename}' 下载超时"}

    async def _on_download(self, download) -> None:
        path = os.path.join(user_data.downloads_dir(), download.suggested_filename)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        await download.save_as(path)
        self._downloads[download.suggested_filename] = path
        log(f"文件已下载至: {path}")
