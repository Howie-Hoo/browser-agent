#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BrowserProxy — Playwright 浏览器操作核心类
由 server.py 托管，通过 client.py 使用。

核心方法在本文件中，AI 高级方法在 proxy_ai.py，
标签页/持久化方法在 proxy_tabs.py，通过 mixin 继承组合。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .utils import log, emit
from .proxy_ai import BrowserProxyAI
from .proxy_tabs import BrowserProxyTabs
from .proxy_auth import BrowserProxyAuth


class BrowserProxy(BrowserProxyAI, BrowserProxyTabs, BrowserProxyAuth):
    """Playwright 浏览器操作封装，由服务端持久托管。"""

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        headless: bool = False,
        viewport_w: int = 1440,
        viewport_h: int = 900,
        timeout: int = 30000,
        slow_mo: int = 0,
        proxy_server: Optional[str] = None,
        record_video_dir: Optional[str] = None,
        record_har_path: Optional[str] = None,
        no_storage: bool = False,
        ignore_https_errors: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.viewport = {"width": viewport_w, "height": viewport_h}
        self.default_timeout = timeout
        self.slow_mo = slow_mo
        self.proxy_server = proxy_server
        self.record_video_dir = record_video_dir
        self.record_har_path = record_har_path
        self.no_storage = no_storage
        self.ignore_https_errors = ignore_https_errors
        self._playwright = None
        self._browser = None
        self._contexts = {}
        self._pages = {}
        self._active_tab = "default"
        self._routes = []
        self._downloads = {}
        self._frames: dict = {}
        self._console_errors: list = []
        self._failed_requests: list = []
        # O3: 元素缓存
        self._elements_cache: list = []
        self._elements_cache_url: str = ""
        self._elements_cache_time: float = 0

    @property
    def _page(self):
        return self._frames.get(self._active_tab) or self._pages.get(self._active_tab)

    @property
    def _main_page(self):
        """始终返回主页面（绕过 iframe）。"""
        return self._pages.get(self._active_tab)

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

        launch_args = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if self.proxy_server:
            launch_args["proxy"] = {"server": self.proxy_server}

        self._browser = await self._playwright.chromium.launch(**launch_args)

        context_args = {"viewport": self.viewport}
        if self.ignore_https_errors:
            context_args["ignore_https_errors"] = True
        if self.record_video_dir:
            os.makedirs(self.record_video_dir, exist_ok=True)
            context_args["record_video_dir"] = self.record_video_dir
        if self.record_har_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.record_har_path)) or ".", exist_ok=True)

        STORAGE_DIR = os.path.join(os.path.dirname(__file__), "storage")
        DEFAULT_STATE_FILE = os.path.join(STORAGE_DIR, "state.json")
        if os.path.isfile(DEFAULT_STATE_FILE) and not self.no_storage:
            context_args["storage_state"] = DEFAULT_STATE_FILE
            log(f"自动复用持久化会话凭证: {DEFAULT_STATE_FILE}")
        elif self.no_storage:
            log("沙箱模式已开启: 本次会话不加载任何持久化状态")

        context = await self._browser.new_context(**context_args)
        self._contexts["default"] = context

        page = await context.new_page()
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

        page.set_default_timeout(self.default_timeout)
        page.on("crash", lambda p: log("⚠️ 页面崩溃！状态已失效"))
        page.on("download", self._on_download)
        self._browser.on("disconnected", lambda browser: log("⚠️ 浏览器进程断开！"))
        self._pages["default"] = page
        self._active_tab = "default"
        self._setup_health_listeners(page)
        log(f"浏览器已启动 [{'无头' if self.headless else '有头'}] {self.viewport['width']}x{self.viewport['height']}")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log("浏览器已关闭")

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    def is_browser_open(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def _resolve_url(self, url: Optional[str]) -> str:
        if not url:
            return self.base_url
        if url.startswith(("http://", "https://", "data:", "file:")):
            return url
        return self.base_url + ("/" if not url.startswith("/") else "") + url

    def _setup_health_listeners(self, page) -> None:
        """为页面绑定运行时健康监控事件。"""
        def on_console(msg):
            if msg.type in ("error", "warning"):
                self._console_errors.append({
                    "type": msg.type, "text": msg.text[:300],
                    "url": (msg.location or {}).get("url", "")
                })
                if len(self._console_errors) > 50:
                    self._console_errors.pop(0)

        def on_request_failed(req):
            self._failed_requests.append({
                "url": req.url[:200],
                "reason": req.failure or "unknown"
            })
            if len(self._failed_requests) > 50:
                self._failed_requests.pop(0)

        page.on("console", on_console)
        page.on("requestfailed", on_request_failed)

    # ── 导航 ──────────────────────────────────────────────

    async def navigate(self, url: Optional[str] = None, wait_until: str = "networkidle") -> dict:
        target = self._resolve_url(url)
        log(f"导航: {target}")
        await self._page.goto(target, wait_until=wait_until)
        title = await self._page.title()
        emit({"type": "navigate", "url": target, "title": title})
        return {"url": target, "title": title}

    # ── 截图 ──────────────────────────────────────────────

    async def screenshot(
        self,
        path: Optional[str] = None,
        full_page: bool = True,
        selector: Optional[str] = None,
        clip: Optional[dict] = None,
        return_base64: bool = False,
    ) -> dict:
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        if selector:
            el = await self._page.wait_for_selector(selector)
            if path:
                await el.screenshot(path=path)
                emit({"type": "screenshot", "path": path, "selector": selector})
                return {"path": path}
            data = await el.screenshot()
            b64 = base64.b64encode(data).decode()
            emit({"type": "screenshot", "base64": b64, "selector": selector})
            return {"base64": b64}

        kwargs = {"clip": clip} if clip else {"full_page": full_page}
        if path:
            await self._page.screenshot(path=path, **kwargs)
            log(f"截图: {path}")
            if return_base64:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                emit({"type": "screenshot", "path": path, "base64": b64})
                return {"path": path, "base64": b64}
            emit({"type": "screenshot", "path": path})
            return {"path": path}

        data = await self._page.screenshot(**kwargs)
        b64 = base64.b64encode(data).decode()
        emit({"type": "screenshot", "base64": b64})
        return {"base64": b64}

    # ── 点击 ──────────────────────────────────────────────

    async def click(self, selector: str, button: str = "left", click_count: int = 1,
                    delay: int = 0, position: Optional[dict] = None) -> dict:
        kw = {"button": button, "click_count": click_count, "delay": delay}
        if position:
            kw["position"] = position
        await self._page.click(selector, **kw)
        emit({"type": "click", "selector": selector, "ok": True})
        return {"ok": True}

    async def click_text(self, text: str, exact: bool = False) -> dict:
        sel = f"text='{text}'" if exact else f"text={text}"
        await self._page.click(sel)
        emit({"type": "click_text", "text": text, "ok": True})
        return {"ok": True}

    async def click_nth(self, selector: str, index: int = 0) -> dict:
        await self._page.locator(selector).nth(index).click()
        emit({"type": "click_nth", "selector": selector, "index": index, "ok": True})
        return {"ok": True}

    # ── 输入 ──────────────────────────────────────────────

    async def type_text(self, selector: str, text: str, clear_first: bool = True, delay: int = 0) -> dict:
        if clear_first:
            await self._page.fill(selector, "")
        await self._page.type(selector, text, delay=delay)
        emit({"type": "type", "selector": selector, "ok": True})
        return {"ok": True}

    async def fill(self, selector: str, value: str) -> dict:
        await self._page.fill(selector, value)
        emit({"type": "fill", "selector": selector, "ok": True})
        return {"ok": True}

    async def press_key(self, key: str, selector: Optional[str] = None) -> dict:
        if selector:
            await self._page.press(selector, key)
        else:
            await self._page.keyboard.press(key)
        emit({"type": "press_key", "key": key, "ok": True})
        return {"ok": True}

    async def clear(self, selector: str) -> dict:
        await self._page.fill(selector, "")
        emit({"type": "clear", "selector": selector, "ok": True})
        return {"ok": True}

    # ── 表单 ──────────────────────────────────────────────

    async def select_option(self, selector: str, value: Optional[str] = None,
                            label: Optional[str] = None, index: Optional[int] = None) -> dict:
        kw = {}
        if value is not None: kw["value"] = value
        elif label is not None: kw["label"] = label
        elif index is not None: kw["index"] = index
        selected = await self._page.select_option(selector, **kw)
        emit({"type": "select", "selector": selector, "selected": selected, "ok": True})
        return {"selected": selected}

    async def check(self, selector: str, force: bool = False) -> dict:
        await self._page.check(selector, force=force)
        emit({"type": "check", "selector": selector, "ok": True})
        return {"ok": True}

    async def uncheck(self, selector: str) -> dict:
        await self._page.uncheck(selector)
        emit({"type": "uncheck", "selector": selector, "ok": True})
        return {"ok": True}

    async def upload_file(self, selector: str, file_path: str) -> dict:
        await self._page.set_input_files(selector, file_path)
        emit({"type": "upload_file", "selector": selector, "ok": True})
        return {"ok": True}

    # ── 鼠标/滚动 ─────────────────────────────────────────

    async def hover(self, selector: str) -> dict:
        await self._page.hover(selector)
        emit({"type": "hover", "selector": selector, "ok": True})
        return {"ok": True}

    async def scroll(self, selector: Optional[str] = None, x: int = 0, y: int = 500) -> dict:
        if selector:
            await self._page.locator(selector).evaluate(f"el => el.scrollBy({x}, {y})")
        else:
            await self._page.evaluate(f"window.scrollBy({x}, {y})")
        emit({"type": "scroll", "ok": True})
        return {"ok": True}

    async def scroll_to_element(self, selector: str) -> dict:
        await self._page.locator(selector).scroll_into_view_if_needed()
        emit({"type": "scroll_to_element", "selector": selector, "ok": True})
        return {"ok": True}

    async def drag_and_drop(self, source: str, target: str) -> dict:
        await self._page.drag_and_drop(source, target)
        emit({"type": "drag_and_drop", "ok": True})
        return {"ok": True}

    # ── 等待 ──────────────────────────────────────────────

    async def wait_for(self, selector: str, state: str = "visible", timeout: Optional[int] = None) -> dict:
        await self._page.wait_for_selector(selector, state=state, timeout=timeout or self.default_timeout)
        emit({"type": "wait_for", "selector": selector, "state": state, "ok": True})
        return {"ok": True}

    async def wait_for_text(self, text: str, timeout: Optional[int] = None) -> dict:
        await self._page.wait_for_selector(f"text={text}", timeout=timeout or self.default_timeout)
        emit({"type": "wait_for_text", "text": text, "ok": True})
        return {"ok": True}

    async def wait_ms(self, ms: int) -> dict:
        await asyncio.sleep(ms / 1000)
        emit({"type": "wait_ms", "ms": ms, "ok": True})
        return {"ok": True}

    # ── DOM 读取 ──────────────────────────────────────────

    async def get_text(self, selector: str) -> dict:
        text = await self._page.locator(selector).first.inner_text()
        emit({"type": "get_text", "selector": selector, "text": text})
        return {"text": text}

    async def get_all_text(self, selector: str) -> dict:
        texts = await self._page.locator(selector).all_inner_texts()
        emit({"type": "get_all_text", "selector": selector, "texts": texts})
        return {"texts": texts}

    async def get_attr(self, selector: str, attr: str) -> dict:
        value = await self._page.get_attribute(selector, attr)
        emit({"type": "get_attr", "selector": selector, "attr": attr, "value": value})
        return {"value": value}

    async def get_count(self, selector: str) -> dict:
        count = await self._page.locator(selector).count()
        emit({"type": "get_count", "selector": selector, "count": count})
        return {"count": count}

    async def get_html(self, selector: Optional[str] = None, outer: bool = False) -> dict:
        if selector:
            el = await self._page.wait_for_selector(selector)
            html = await el.outer_html() if outer else await el.inner_html()
        else:
            html = await self._page.content()
        emit({"type": "get_html", "selector": selector, "html": html[:8000]})
        return {"html": html}

    async def get_style(self, selector: str, prop: str) -> dict:
        value = await self._page.locator(selector).first.evaluate(
            f"el => getComputedStyle(el).getPropertyValue('{prop}')"
        )
        emit({"type": "get_style", "selector": selector, "prop": prop, "value": value})
        return {"value": value}

    async def get_bounding_box(self, selector: str) -> dict:
        box = await self._page.locator(selector).first.bounding_box()
        emit({"type": "get_bounding_box", "selector": selector, "box": box})
        return {"box": box}

    async def get_value(self, selector: str) -> dict:
        value = await self._page.locator(selector).first.input_value()
        emit({"type": "get_value", "selector": selector, "value": value})
        return {"value": value}

    async def is_visible(self, selector: str) -> dict:
        visible = await self._page.locator(selector).first.is_visible()
        emit({"type": "is_visible", "selector": selector, "visible": visible})
        return {"visible": visible}

    async def is_enabled(self, selector: str) -> dict:
        enabled = await self._page.locator(selector).first.is_enabled()
        emit({"type": "is_enabled", "selector": selector, "enabled": enabled})
        return {"enabled": enabled}

    async def is_checked(self, selector: str) -> dict:
        checked = await self._page.locator(selector).first.is_checked()
        emit({"type": "is_checked", "selector": selector, "checked": checked})
        return {"checked": checked}

    # ── JavaScript ────────────────────────────────────────

    async def eval_js(self, script: str, selector: Optional[str] = None) -> dict:
        if selector:
            value = await self._page.locator(selector).first.evaluate(script)
        else:
            value = await self._page.evaluate(script)
        # O6: 类型保留
        if isinstance(value, bool):
            value_type = "boolean"
        elif isinstance(value, (int, float)):
            value_type = "number"
        elif value is None:
            value_type = "null"
        elif isinstance(value, str):
            value_type = "string"
        elif isinstance(value, (list, tuple)):
            value_type = "array"
        elif isinstance(value, dict):
            value_type = "object"
        else:
            value_type = type(value).__name__
        emit({"type": "eval_js", "value": value, "value_type": value_type})
        return {"value": value, "raw_value": value, "value_type": value_type}

    # ── 页面信息 ──────────────────────────────────────────

    async def get_title(self) -> dict:
        title = await self._page.title()
        emit({"type": "get_title", "title": title})
        return {"title": title}

    async def get_url(self) -> dict:
        url = self._page.url
        emit({"type": "get_url", "url": url})
        return {"url": url}

    async def reload(self, wait_until: str = "networkidle") -> dict:
        await self._page.reload(wait_until=wait_until)
        emit({"type": "reload", "ok": True})
        return {"ok": True}

    # ── 命令分发 ──────────────────────────────────────────

    async def _dispatch(self, cmd: dict) -> dict:
        action = cmd.get("action", cmd.get("type", ""))
        p = {k: v for k, v in cmd.items() if k not in ("action", "type", "stop_on_error", "comment")}

        handlers = {
            "navigate":          lambda: self.navigate(p.get("url"), p.get("wait_until", "networkidle")),
            "screenshot":        lambda: self.screenshot(p.get("path"), p.get("full_page", True),
                                                         p.get("selector"), p.get("clip"), p.get("return_base64", False)),
            "click":             lambda: self.click(p["selector"], p.get("button", "left"),
                                                    p.get("click_count", 1), p.get("delay", 0), p.get("position")),
            "click_text":        lambda: self.click_text(p["text"], p.get("exact", False)),
            "click_nth":         lambda: self.click_nth(p["selector"], p.get("index", 0)),
            "type":              lambda: self.type_text(p["selector"], p["text"], p.get("clear_first", True), p.get("delay", 0)),
            "fill":              lambda: self.fill(p["selector"], p["value"]),
            "clear":             lambda: self.clear(p["selector"]),
            "press_key":         lambda: self.press_key(p["key"], p.get("selector")),
            "select":            lambda: self.select_option(p["selector"], p.get("value"), p.get("label"), p.get("index")),
            "check":             lambda: self.check(p["selector"], p.get("force", False)),
            "uncheck":           lambda: self.uncheck(p["selector"]),
            "upload_file":       lambda: self.upload_file(p["selector"], p["file_path"]),
            "hover":             lambda: self.hover(p["selector"]),
            "scroll":            lambda: self.scroll(p.get("selector"), p.get("x", 0), p.get("y", 500)),
            "scroll_to_element": lambda: self.scroll_to_element(p["selector"]),
            "drag_and_drop":     lambda: self.drag_and_drop(p["source"], p["target"]),
            "wait_for":          lambda: self.wait_for(p["selector"], p.get("state", "visible"), p.get("timeout")),
            "wait_for_text":     lambda: self.wait_for_text(p["text"], p.get("timeout")),
            "wait_ms":           lambda: self.wait_ms(p.get("ms", 1000)),
            "get_text":          lambda: self.get_text(p["selector"]),
            "get_all_text":      lambda: self.get_all_text(p["selector"]),
            "get_attr":          lambda: self.get_attr(p["selector"], p["attr"]),
            "get_count":         lambda: self.get_count(p["selector"]),
            "get_html":          lambda: self.get_html(p.get("selector"), p.get("outer", False)),
            "get_style":         lambda: self.get_style(p["selector"], p["prop"]),
            "get_bounding_box":  lambda: self.get_bounding_box(p["selector"]),
            "get_value":         lambda: self.get_value(p["selector"]),
            "is_visible":        lambda: self.is_visible(p["selector"]),
            "is_enabled":        lambda: self.is_enabled(p["selector"]),
            "is_checked":        lambda: self.is_checked(p["selector"]),
            "eval_js":           lambda: self.eval_js(p["script"], p.get("selector")),
            "get_title":         lambda: self.get_title(),
            "get_url":           lambda: self.get_url(),
            "reload":            lambda: self.reload(p.get("wait_until", "networkidle")),
            # 标签页/持久化/断言（来自 BrowserProxyTabs mixin）
            "new_tab":           lambda: self.new_tab(p.get("name", "tab_" + str(len(self._pages)))),
            "new_isolated_session": lambda: self.new_isolated_session(p["name"], p.get("profile")),
            "switch_tab":        lambda: self.switch_tab(p["name"]),
            "close_tab":         lambda: self.close_tab(p["name"]),
            "mock_route":        lambda: self.mock_route(p["url_pattern"], p.get("response_data", {})),
            "clear_routes":      lambda: self.clear_routes(),
            "analyze_network":   lambda: self.analyze_network(),
            "wait_for_download": lambda: self.wait_for_download(p["filename"]),
            "save_cookies":      lambda: self.save_cookies(p.get("path", os.path.join(os.path.dirname(__file__), "storage", "cookies.json"))),
            "load_cookies":      lambda: self.load_cookies(p.get("path", os.path.join(os.path.dirname(__file__), "storage", "cookies.json"))),
            "save_storage":      lambda: self.save_storage(p.get("path", os.path.join(os.path.dirname(__file__), "storage", "state.json"))),
            "clear_storage":     lambda: self.clear_storage(),
            # 认证会话（来自 BrowserProxyAuth mixin）
            "save_profile":      lambda: self.save_profile(p["name"], p.get("include_auth", True)),
            "load_profile":      lambda: self.load_profile(p["name"], p.get("into", "new"), p.get("navigate", True)),
            "has_profile":       lambda: self.has_profile(p["name"]),
            "list_profiles":     lambda: self.list_profiles(),
            "delete_profile":    lambda: self.delete_profile(p["name"]),
            "check_auth":        lambda: self.check_auth(p.get("name")),
            "ensure_auth":       lambda: self.ensure_auth(p.get("name"), p.get("login_commands"), p.get("into", "new")),
            "assert_text":       lambda: self.assert_text(p["selector"], p["expected"]),
            "assert_url":        lambda: self.assert_url(p["expected"]),
            "assert_visible":    lambda: self.assert_visible(p["selector"]),
            "assert_value":      lambda: self.assert_value(p["selector"], p["expected"]),
            "get_annotated_dom": lambda: self.get_annotated_dom(),
            # AI 高级方法（来自 BrowserProxyAI mixin）
            "get_page_summary":  lambda: self.get_page_summary(),
            "snapshot_dom":      lambda: self._snapshot_dom(),
            "get_dom_diff":      lambda: self.get_dom_diff(p.get("snapshot")),
            "fill_form":         lambda: self.fill_form(p["fields"]),
            "wait_for_stable":   lambda: self.wait_for_stable(p.get("timeout", 10000)),
            "click_by_id":       lambda: self.click_by_id(p["id"]),
            "extract":           lambda: self.extract(p["schema"], p.get("wait_timeout", 3000)),
            "get_session_context": lambda: self.get_session_context(),
            "get_interactive_elements": lambda: self.get_interactive_elements(p.get("limit", 50), p.get("_use_cache", True)),
            "get_a11y_tree":     lambda: self.get_a11y_tree(p.get("root_selector"), p.get("interesting_only", True)),
            "switch_frame":      lambda: self.switch_frame(p.get("selector"), p.get("name"), p.get("url_contains")),
            "switch_to_main_frame": lambda: self.switch_to_main_frame(),
            "get_page_health":   lambda: self.get_page_health(),
            "scroll_until_found": lambda: self.scroll_until_found(p["selector"], p.get("max_scrolls", 10), p.get("scroll_step", 800), p.get("timeout_per_scroll", 1000)),
            "find_and_act":      lambda: self.find_and_act(p["description"], p.get("act", "click"), p.get("value")),
            "detect_captcha":    lambda: self.detect_captcha(),
            "form_analyze":      lambda: self.form_analyze(p.get("form_selector", "form")),
            "watch_for_change":  lambda: self.watch_for_change(p["conditions"], p.get("timeout", 10000), p.get("poll_interval", 500)),
            "hover_submenu":     lambda: self.hover_submenu(p["hover_selector"], p.get("click_selector"), p.get("wait_after_hover", 600)),
            "smart_wait":        lambda: self.smart_wait(p["condition"], p.get("value"), p.get("timeout", 10000)),
            "drag_drop":         lambda: self.drag_drop(p["source_selector"], p.get("target_selector"), p.get("target_x"), p.get("target_y"), p.get("steps", 15)),
            "clip_screenshot":   lambda: self.clip_screenshot(p["selector"], p.get("path"), p.get("padding", 8)),
        }

        if action not in handlers:
            return {"success": False, "error": f"未知命令: '{action}'。支持: {sorted(handlers.keys())}"}

        try:
            res = await handlers[action]()
            if isinstance(res, dict):
                res["success"] = res.get("success", True)
            # O3: 缓存失效 — 改变页面状态的操作后清除元素缓存
            _CACHE_INVALIDATING_ACTIONS = {
                "navigate", "click", "click_text", "click_nth", "fill", "type",
                "select", "check", "uncheck", "scroll", "scroll_to_element",
                "reload", "switch_tab", "switch_frame", "switch_to_main_frame",
                "new_tab", "drag_and_drop", "drag_drop", "hover", "load_profile",
            }
            if action in _CACHE_INVALIDATING_ACTIONS:
                self._elements_cache = []
            return res
        except Exception as e:
            err_msg = str(e).split('\n')[0]
            emit({"type": action, "success": False, "error": err_msg})
            return {"success": False, "error": err_msg}