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


class BrowserProxyTabs:
    """多标签页、iframe、网络拦截、状态持久化、断言相关方法。"""

    # ── 多标签页管理 ──────────────────────────────────────────
    async def new_tab(self, name: str) -> dict:
        if name in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 已存在"}
        from playwright_stealth import Stealth
        # 新 tab 落在当前活跃页所属 context（多会话隔离下不串到别的会话）
        context = self._page.context if self._page else self._contexts.get("default")
        if not context: return {"ok": False, "error": "当前上下文不存在"}
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        page.set_default_timeout(self.default_timeout)
        page.on("crash", lambda p: log(f"⚠️ 页面 '{name}' 崩溃！状态已失效"))
        page.on("download", self._on_download)
        self._pages[name] = page
        self._active_tab = name
        emit({"type": "new_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    async def new_isolated_session(self, name: str, profile: Optional[str] = None) -> dict:
        if name in self._contexts or name in self._pages:
            return {"ok": False, "error": f"名称 '{name}' 已被占用"}
        from playwright_stealth import Stealth
        context_args = {"viewport": self.viewport}
        if getattr(self, "ignore_https_errors", False):
            context_args["ignore_https_errors"] = True
        if self.record_video_dir: context_args["record_video_dir"] = self.record_video_dir
        # 注入命名 profile 的登录态（cookies + localStorage，原子且含 httpOnly/Secure）
        if profile:
            from .proxy_auth import validate_profile_name, profile_path, _read_profile
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
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        page.set_default_timeout(self.default_timeout)
        page.on("crash", lambda p: log(f"⚠️ 页面 '{name}' 崩溃！状态已失效"))
        page.on("download", self._on_download)
        self._pages[name] = page
        self._active_tab = name
        if profile:
            self._active_profile = profile
        emit({"type": "new_isolated_session", "name": name, "profile": profile, "ok": True})
        return {"ok": True, "name": name, "profile": profile}

    async def switch_tab(self, name: str) -> dict:
        if name not in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 不存在"}
        self._active_tab = name
        await self._page.bring_to_front()
        emit({"type": "switch_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    async def close_tab(self, name: str) -> dict:
        if name not in self._pages:
            return {"ok": False, "error": f"标签页 '{name}' 不存在"}
        closed_ctx = self._pages[name].context
        await self._pages[name].close()
        del self._pages[name]
        if self._active_tab == name:
            # 回落到同一 context 内的其余 tab（多会话隔离下不跨会话乱切）
            fallback = next((k for k, p in self._pages.items() if p.context is closed_ctx), None)
            self._active_tab = fallback
        emit({"type": "close_tab", "name": name, "ok": True})
        return {"ok": True, "name": name}

    # ── 网络拦截 ──────────────────────────────────────────
    async def mock_route(self, url_pattern: str, response_data: dict) -> dict:
        async def handle_route(route, request):
            await route.fulfill(
                status=response_data.get("status", 200),
                body=json.dumps(response_data.get("body", {})),
                content_type="application/json"
            )
        await self._page.route(url_pattern, handle_route)
        self._routes.append(url_pattern)
        emit({"type": "mock_route", "pattern": url_pattern, "ok": True})
        return {"ok": True, "pattern": url_pattern}

    async def clear_routes(self) -> dict:
        for p in self._routes:
            try:
                await self._page.unroute(p)
            except Exception:
                pass
        self._routes.clear()
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
        storage_dir = os.path.join(os.path.dirname(__file__), "..", "storage")
        cleared_files = []
        for file in ["cookies.json", "state.json"]:
            path = os.path.join(storage_dir, file)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    cleared_files.append(file)
                except Exception as e:
                    pass
        # 清求内存态
        if self._page and self._page.context:
            await self._page.context.clear_cookies()
        try:
            await self._page.evaluate("try { localStorage.clear(); sessionStorage.clear(); } catch(e){}")
        except:
            pass
        emit({"type": "clear_storage", "cleared_files": cleared_files, "ok": True})
        return {"ok": True, "cleared_files": cleared_files}

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

        self._frames[self._active_tab] = target_frame
        emit({"type": "switch_frame", "url": target_frame.url, "ok": True})
        return {"ok": True, "frame_url": target_frame.url, "frame_name": target_frame.name or "(unnamed)"}

    async def switch_to_main_frame(self) -> dict:
        """退出 iframe 上下文，切回主页面。所有后续操作回归主页面执行。"""
        if self._active_tab in self._frames:
            del self._frames[self._active_tab]
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
        path = os.path.join(os.path.dirname(__file__), "..", "storage", "downloads", download.suggested_filename)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        await download.save_as(path)
        self._downloads[download.suggested_filename] = path
        log(f"文件已下载至: {path}")
