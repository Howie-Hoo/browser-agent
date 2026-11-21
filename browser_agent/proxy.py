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
import contextvars
import os
import sys
import time
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .utils import log, emit
from .proxy_ai import BrowserProxyAI
from .proxy_input import BrowserProxyInput
from .proxy_tabs import BrowserProxyTabs
from .proxy_auth import BrowserProxyAuth
from .site_patterns import BrowserProxySites
from .local_search import BrowserProxyLocalSearch
from . import user_data


class BrowserProxy(BrowserProxyAI, BrowserProxyTabs, BrowserProxyAuth,
                   BrowserProxySites, BrowserProxyLocalSearch, BrowserProxyInput):
    """Playwright 浏览器操作封装，由服务端持久托管。

    两种模式：
    - managed（默认）：自拉隔离 Chromium，配合 profile 注入登录态
    - attach：经 CDP 附着用户日常浏览器（登录态免费，最小侵入契约见 browser_attach.py）
    """

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
        mode: str = "managed",
        attach_dir: str = "",
        cdp_endpoint: str = "",
        stealth_enabled: bool = True,
        storage_guard=None,
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
        self.mode = mode
        self.attach_dir = attach_dir
        self.cdp_endpoint = cdp_endpoint
        # attach 模式禁用 stealth：真实用户浏览器即指纹，注入反而画蛇添足
        self.stealth_enabled = stealth_enabled and mode != "attach"
        self._playwright = None
        self._browser = None
        self._contexts = {}
        self._pages = {}
        # 当前标签页的权威来源是任务级 ContextVar，值是**可变 router 字典**
        # （{"tab": 键, "sid": 会话}）：asyncio.wait_for 在 Python < 3.12 会把协程
        # 包进子任务（复制上下文，ContextVar.set 不回传父任务），而子任务复制的是
        # router 的**引用**——批内命令（switch_tab 等，运行在 wait_for 之下）对
        # router 的写入对所有版本都对外层批任务可见。每个批任务入口 set 自己的
        # router，多会话批真并行互不串页；_active_tab 仅作无 router 时的回退默认。
        self._current_tab: contextvars.ContextVar = contextvars.ContextVar("ba_current_tab")
        self._active_tab = "default"
        # storage 清除类操作的服务器侧守卫（构造注入：按 sid 判共享上下文）
        self._storage_guard = storage_guard
        # adopt_tab 收养的用户标签页键集合：这些页属于用户，
        # 服务/会话关闭时只取消注册、绝不关闭它们
        self._adopted_tabs: set = set()
        # ── 以下运行态全部按标签页键隔离（并发批不串扰）──
        self._downloads = {}
        self._frames: dict = {}                      # tab_key -> 当前 iframe Frame
        self._routes: dict = {}                      # tab_key -> [url_pattern]
        self._console_errors: dict = {}              # tab_key -> 环形缓冲
        self._failed_requests: dict = {}             # tab_key -> 环形缓冲
        self._element_caches: dict = {}              # tab_key -> {url, time, data}
        self._active_profiles: dict = {}             # tab_key -> profile 名
        # default context 是否与他人共享（attach 模式的用户 profile context）：
        # 共享则绝不 close 该 context、不抢焦点、不清 storage
        self._shared_default_context = False
        self._attach_info: dict = {}
        # attach：本连接的 CDP 调试端口（页面级端口防护用；managed 模式为 None）
        self._attached_port: Optional[int] = None
        # 已挂 context 级端口防护的自建 context（页级挂载据此跳过，防重复注册）
        self._guarded_contexts: set = set()
        # 标签页空闲回收用的活动时间（monotonic）：批入口/切页两个路由写入点刷新
        self._tab_last_used: dict = {}

    # ── 标签页上下文（并发模型核心）──────────────────────
    # 键约定唯一属主："sid::name"。server 与 proxy 都只经这一对函数拼/拆。

    @staticmethod
    def tab_key(sid: str, name: str) -> str:
        return f"{sid}::{name}"

    @staticmethod
    def tab_owner(key: str) -> Optional[str]:
        return key.split("::", 1)[0] if "::" in key else None

    def _tab(self) -> Optional[str]:
        """当前任务上下文的标签页键（router 优先，回退全局默认）。"""
        router = self._current_tab.get(None)
        if router is not None:
            return router.get("tab") or self._active_tab
        return self._active_tab

    def _begin_batch(self, sid: str, tab: str) -> None:
        """命令批入口：为本任务装上带 sid 的路由上下文（唯一合法写入点）。"""
        self._current_tab.set({"tab": tab, "sid": sid})
        self._active_tab = tab
        # 标签页空闲回收的活动信号：批到达即视为该页活跃
        self._tab_last_used[tab] = time.monotonic()

    def _router_sid(self) -> Optional[str]:
        """当前批所属会话（server 在批入口写入 router；守卫钩子用）。"""
        router = self._current_tab.get(None)
        return router.get("sid") if router else None

    def _set_current_tab(self, key: Optional[str]) -> None:
        """切换当前任务上下文的操作目标页；同时更新回退默认值。

        写入 router（可变字典）：wait_for 子任务内调用也能被外层批任务看到
        （Python ≥3.10 一致）；本任务没有 router 时创建一个——此后该任务与
        其他任务完全隔离。
        """
        if key is None:
            return
        router = self._current_tab.get(None)
        if router is None:
            router = {}
            self._current_tab.set(router)
        router["tab"] = key
        self._active_tab = key
        # 切页即刷新活动时间：批中途 switch_tab 的目标页不能被当成陈旧页回收
        # （Session.active_tab 要到批结束才回写，期间全靠这里的触碰保活）
        self._tab_last_used[key] = time.monotonic()

    def _purge_tab_state(self, key: str) -> None:
        """清理某标签页键关联的全部运行态（页已关闭时调用）。"""
        self._frames.pop(key, None)
        self._routes.pop(key, None)
        self._console_errors.pop(key, None)
        self._failed_requests.pop(key, None)
        self._element_caches.pop(key, None)
        self._active_profiles.pop(key, None)
        self._tab_last_used.pop(key, None)

    def _cache_invalidate(self) -> None:
        """清除当前标签页的元素缓存（改变页面状态的操作后调用）。"""
        self._element_caches.pop(self._tab(), None)

    def _active_profile_name(self) -> Optional[str]:
        return self._active_profiles.get(self._tab())

    def _set_active_profile(self, name: str) -> None:
        self._active_profiles[self._tab()] = name

    @property
    def _page(self):
        key = self._tab()
        return self._frames.get(key) or self._pages.get(key)

    @property
    def _main_page(self):
        """始终返回主页面（绕过 iframe）。"""
        return self._pages.get(self._tab())

    def _in_user_context(self) -> bool:
        """当前活跃页是否位于共享的用户 default context。"""
        p = self._page
        return bool(
            self._shared_default_context
            and p is not None
            and getattr(p, "context", None) is self._contexts.get("default")
        )

    async def _apply_stealth(self, page) -> None:
        """按配置注入 stealth（attach 模式自动跳过）。"""
        if not self.stealth_enabled:
            return
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

    def _debug_port_patterns(self) -> list:
        """端口防护的拦截模式（无端口 → 空表）。只拦 http 探测；WebSocket
        握手不经 page.route，不在拦截范围（文档已注明）。"""
        port = self._attached_port
        return [f"http://127.0.0.1:{port}/**", f"http://localhost:{port}/**"] if port else []

    async def _guard_debug_port(self, target) -> None:
        """attach 端口防护：拦截页面脚本对本机 CDP 调试端口的探测（反风控）。

        页面可用 fetch 127.0.0.1:{调试端口} 探测浏览器是否开着自动化接口，
        这是常见的自动化检测面。只拦本连接的端口，不影响其他本地服务；
        挂载失败不阻断建页（防护尽力而为）。managed 模式无端口暴露，不调用。
        target 为 Page 或自建 Context——context 级挂载可覆盖弹窗与其后续页。
        """
        if not self._attached_port:
            return

        async def _abort(route) -> None:
            try:
                await route.abort()
            except Exception:
                pass

        for pattern in self._debug_port_patterns():
            try:
                await target.route(pattern, _abort)
            except Exception as e:
                log(f"⚠️ 调试端口防护未挂载（{pattern}）: {e}")

    async def _unguard_debug_port(self, page) -> None:
        """撤销页面上的端口防护路由（收养结束时调用——最小侵入不留残余拦截）。

        unroute(模式) 移除该模式全部 handler；页面从未挂过时静默。
        """
        if not self._attached_port:
            return
        for pattern in self._debug_port_patterns():
            try:
                await page.unroute(pattern)
            except Exception:
                pass

    async def _register_page(self, context, key: str, label: str = ""):
        """在指定 context 建页并注册：stealth / 超时 / 崩溃与下载监听统一在此。

        新页成为当前任务上下文的操作目标（contextvar），并初始化其按页隔离的运行态。
        """
        page = await context.new_page()
        await self._apply_stealth(page)
        page.set_default_timeout(self.default_timeout)
        page.on("crash", lambda p: log(f"⚠️ 页面 '{label or key}' 崩溃！状态已失效"))
        page.on("download", self._on_download)
        await self._register_existing_page(key, page, health_listeners=True)
        return page

    async def _register_existing_page(self, key: str, page,
                                      health_listeners: bool = False) -> None:
        """把已存在的页纳入服务管理——建页与 adopt_tab 收养的**唯一注册咽喉**：
        注册、切换操作目标（顺带刷新活动时间）、健康监听、端口防护。

        端口防护挂页级，但所在 context 已整体挂过（自建 context）则跳过——
        同一套模式重复注册只是死代码加一次 CDP 往返。
        """
        self._pages[key] = page
        if health_listeners:
            self._setup_health_listeners(page, key)
        self._set_current_tab(key)  # 切页即刷新 _tab_last_used（无需再显式 touch）
        if self._attached_port and \
                getattr(page, "context", None) not in self._guarded_contexts:
            await self._guard_debug_port(page)

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        if self.mode == "attach":
            await self._start_attached()
        else:
            await self._start_managed()

        context = self._contexts["default"]
        self._browser.on("disconnected", lambda browser: log("⚠️ 浏览器连接断开！"))
        await self._register_page(context, "default", label="default")
        if self.mode == "attach":
            # 完整启动成功（默认页注册完毕）才落 pin：半启动失败不留幽灵 pin，
            # 否则会把用户重试时换到正确浏览器的第一次连接也挡掉
            from .browser_attach import attach_identity, set_pinned_dir
            set_pinned_dir(attach_identity(self._attach_info))
            log(f"已附着用户浏览器 [{'无头' if self.headless else '有头'}] "
                f"{self._attach_info.get('browser_dir') or self._attach_info.get('endpoint')}")
        else:
            log(f"浏览器已启动 [{'无头' if self.headless else '有头'}] "
                f"{self.viewport['width']}x{self.viewport['height']}")

    async def _start_managed(self) -> None:
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

        state_path = user_data.state_file()
        if os.path.isfile(state_path) and not self.no_storage:
            context_args["storage_state"] = state_path
            log(f"自动复用持久化会话凭证: {state_path}")
        elif self.no_storage:
            log("沙箱模式已开启: 本次会话不加载任何持久化状态")

        self._contexts["default"] = await self._browser.new_context(**context_args)

    async def _start_attached(self) -> None:
        """CDP 附着用户日常浏览器（硬错不降级，契约见 browser_attach 模块头）。"""
        from .browser_attach import (resolve_endpoint, format_attach_error,
                                     attach_identity, pin_mismatch_error,
                                     endpoint_port)
        # 解析含同步文件 IO + TCP 探测（失败路径还会全盘重扫发现根，最坏秒级）：
        # 下线程执行，不阻塞事件循环上其他会话的批与 /status 轮询
        info = await asyncio.to_thread(resolve_endpoint,
                                       self.attach_dir, self.cdp_endpoint)
        if not info.get("ok"):
            raise RuntimeError(format_attach_error(info))
        # 进程级身份 pin：本进程已附着过别的浏览器/端点 → 硬错（不静默换目标）。
        # 身份令牌含端点直连（目录与端点两种身份不互通）
        identity = attach_identity(info)
        pin_err = pin_mismatch_error(identity)
        if pin_err:
            raise RuntimeError(pin_err)
        # 连接超时给足用户在浏览器里点「允许」的时间（Chrome 新式开关每条新连接
        # 都要授权一次；Playwright 默认 30s 太短，用户来不及点「允许」）。
        # BROWSER_AGENT_CONNECT_TIMEOUT_MS 可调。
        try:
            connect_timeout_ms = int(os.getenv("BROWSER_AGENT_CONNECT_TIMEOUT_MS", "120000"))
        except ValueError:
            connect_timeout_ms = 120000
        self._browser = await self._playwright.chromium.connect_over_cdp(
            info["endpoint"], timeout=connect_timeout_ms)
        contexts = self._browser.contexts
        # 有既有 context = 用户真实 profile（登录态即来自它）→ 共享，只加页不关它
        self._shared_default_context = bool(contexts)
        self._contexts["default"] = contexts[0] if contexts else await self._browser.new_context()
        self._attached_port = endpoint_port(info["endpoint"])
        self._attach_info = {
            "endpoint": info["endpoint"],
            "browser_dir": info.get("browser_dir"),
            "browser_version": self._browser.version,
            "source": info.get("source"),
            "debug_port": self._attached_port,
        }

    async def stop(self) -> None:
        if self._browser:
            if self._shared_default_context:
                # attach：只关自建标签页（adopt_tab 收养的用户页除外），
                # close() 对连接型浏览器仅断开连接——用户浏览器与其余标签页原样保留
                for key, page in list(self._pages.items()):
                    if key in self._adopted_tabs:
                        continue
                    try:
                        await page.close()
                    except Exception:
                        pass
                self._pages.clear()
                self._adopted_tabs.clear()
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log("已断开浏览器连接（用户浏览器保持运行）" if self._shared_default_context else "浏览器已关闭")

    async def close_idle_tabs(self, idle_sec: float, protected: set) -> list:
        """关闭超时无活动的**自建**标签页（收养页与 protected 集合除外）。

        protected 由 server 传入各会话的 active_tab——活跃操作目标永不回收；
        页面整体生命周期归 /session/stop 与全局空闲回收管，这里只做页级兜底
        （长会话开一堆 tab 只用其一的场景）。
        """
        now = time.monotonic()
        closed = []
        for key in list(self._pages):
            if key in self._adopted_tabs or key in protected:
                continue
            # 显式判空：monotonic 时间戳理论可为 0.0（falsy，不能用 or 链）
            last = self._tab_last_used.get(key)
            if last is None:
                last = now
            if now - last < idle_sec:
                continue
            try:
                await self._unregister_page(key, close=True)
                closed.append(key)
            except Exception as e:
                log(f"标签页空闲回收失败（{key}）: {e}")
        if closed:
            log(f"标签页空闲回收（>{int(idle_sec)}s 无活动）：{closed}")
        return closed

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

    def _setup_health_listeners(self, page, tab_key: Optional[str] = None) -> None:
        """为页面绑定运行时健康监控事件（缓冲按标签页键隔离，并发批不串扰）。"""
        key = tab_key or self._tab()

        def on_console(msg):
            if msg.type in ("error", "warning"):
                buf = self._console_errors.setdefault(key, [])
                buf.append({
                    "type": msg.type, "text": msg.text[:300],
                    "url": (msg.location or {}).get("url", "")
                })
                if len(buf) > 50:
                    buf.pop(0)

        def on_request_failed(req):
            buf = self._failed_requests.setdefault(key, [])
            buf.append({
                "url": req.url[:200],
                "reason": req.failure or "unknown"
            })
            if len(buf) > 50:
                buf.pop(0)

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
        image_type: Optional[str] = None,
        quality: int = 80,
        scale: Optional[str] = None,
    ) -> dict:
        """截图。省 token 优先级：clip/selector（元素）> viewport > full_page；
        内容型页面 image_type="jpeg"（quality 默认 80）通常比 PNG 小 3~5 倍；
        scale="css" 按页面 CSS 像素出图（devicePixelRatio>1 的机器上更小）。

        image_type 缺省时按 path 后缀推断（.jpg/.jpeg → jpeg，其余 png）；
        显式指定且与后缀不符时改写后缀以匹配实际格式（返回的 path 是最终路径）。
        """
        if image_type not in (None, "png", "jpeg"):
            return {"ok": False, "error": "image_type 仅支持 png | jpeg"}
        if image_type is None:
            ext = os.path.splitext(path or "")[1].lower()
            image_type = "jpeg" if ext in (".jpg", ".jpeg") else "png"
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if not path.lower().endswith(f".{image_type}"):
                path = f"{path.rsplit('.', 1)[0] if '.' in os.path.basename(path) else path}.{image_type}"

        # type/quality 规则单点构造（元素与页面两条分支共用）
        kwargs = {"type": image_type}
        if image_type == "jpeg":
            kwargs["quality"] = quality

        if selector:
            el = await self._page.wait_for_selector(selector)
            if path:
                await el.screenshot(path=path, **kwargs)
                emit({"type": "screenshot", "path": path, "selector": selector})
                return {"path": path}
            data = await el.screenshot(**kwargs)
            b64 = base64.b64encode(data).decode()
            emit({"type": "screenshot", "base64": b64, "selector": selector})
            return {"base64": b64}

        if clip:
            kwargs["clip"] = clip
        else:
            kwargs["full_page"] = full_page
        if scale in ("css", "device"):
            kwargs["scale"] = scale
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

    async def eval_js(self, script: str, selector: Optional[str] = None,
                      isolated: bool = False) -> dict:
        """执行 JS 表达式（底层万能工具）。

        isolated=True：在主文档的**隔离环境**（isolated world）执行——页面脚本
        对 document.querySelectorAll / elementFromPoint 等 DOM API 的改写不影响
        结果，反自动化页面也拿不到干净 DOM 视图。代价：看不到页面自己的 JS
        全局变量（window.__APP__ 之类）；隔离求值只作用于主文档，当前若已
        switch_frame 进 iframe，请用默认模式或先 switch_to_main_frame。
        """
        if isolated:
            if selector:
                return {"ok": False,
                        "error": "isolated 求值不支持 selector（隔离环境作用于主文档全局，"
                                 "拿不到元素作用域）。元素级求值请去掉 isolated，"
                                 "或在 script 内自行 querySelector"}
            return await self._eval_js_isolated(script)
        if selector:
            value = await self._page.locator(selector).first.evaluate(script)
        else:
            value = await self._page.evaluate(script)
        return self._wrap_eval_value(value)

    async def _eval_js_isolated(self, script: str) -> dict:
        """经 CDP 在主文档隔离环境求值（每次独立建/撤会话，无共享状态）。"""
        page = self._main_page
        if page is None:
            return {"ok": False, "error": "无可用页面"}
        try:
            session = await page.context.new_cdp_session(page)
        except Exception as e:
            return {"ok": False,
                    "error": f"隔离环境需要 CDP 会话（仅 Chromium）：{str(e).splitlines()[0]}"}
        try:
            tree = await session.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
            world = await session.send("Page.createIsolatedWorld",
                                       {"frameId": frame_id,
                                        "worldName": "browser-agent-isolated"})
            ctx_id = world.get("executionContextId")
            if not ctx_id:
                return {"ok": False, "error": "隔离执行环境创建失败"}
            resp = await session.send("Runtime.evaluate", {
                "expression": script, "returnByValue": True,
                "awaitPromise": True, "contextId": ctx_id})
        except Exception as e:
            return {"ok": False, "error": f"隔离求值失败: {str(e).splitlines()[0]}"}
        finally:
            try:
                await session.detach()
            except Exception:
                pass
        if resp.get("exceptionDetails"):
            text = (resp["exceptionDetails"].get("exception", {}).get("description")
                    or resp["exceptionDetails"].get("text", "JS 执行异常"))
            return {"ok": False, "error": text.splitlines()[0]}
        # 隔离环境结果没有 Python 侧对象，只有 CDP RemoteObject 描述
        result = resp.get("result", {})
        value = result.get("value")
        value_type = result.get("type", "undefined")
        emit({"type": "eval_js", "isolated": True, "value": value, "value_type": value_type})
        return {"value": value, "raw_value": value, "value_type": value_type}

    @staticmethod
    def _wrap_eval_value(value) -> dict:
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

    async def go_back(self, wait_until: str = "load") -> dict:
        """后退一页（无历史时不报错，went_back=false 如实回报）。

        导航是页级操作：当前在 iframe 上下文时也作用于主文档（Frame 没有
        go_back），且导航后原 iframe 上下文必然失效——顺带退出 frame 上下文。
        """
        page = self._main_page
        if page is None:
            return {"ok": False, "error": "无可用页面"}
        resp = await page.go_back(wait_until=wait_until)
        self._frames.pop(self._tab(), None)
        went = resp is not None
        emit({"type": "go_back", "went_back": went, "ok": True})
        if went:
            return {"ok": True, "went_back": True, "url": page.url}
        return {"ok": True, "went_back": False, "note": "没有可后退的历史"}

    # ── 命令分发 ──────────────────────────────────────────

    # 注册/查找标签页的命令及其键参数——命名空间化的唯一事实来源（server 不再维护清单）
    _TAB_KEY_ACTIONS = {"new_tab", "switch_tab", "close_tab",
                        "new_isolated_session", "new_shared_session",
                        "adopt_tab"}
    # 注意：load_profile 不在此列——其 name 是 profile 文件名而非标签页名，
    # 作用域化会破坏 profile 查找（profile::name 页键维持全局共享语义）

    async def _dispatch(self, cmd: dict) -> dict:
        action = cmd.get("action", cmd.get("type", ""))
        p = {k: v for k, v in cmd.items() if k not in ("action", "type", "stop_on_error", "comment")}

        # 标签页键按会话命名空间化（sid::name）：在 dispatch 咽喉点统一进行，
        # /run 主循环、loop 子命令、if_error_then 降级链、ensure_auth 的
        # login_commands——一切执行路径全部覆盖，多会话不撞键不误关。
        sid = self._router_sid()
        if sid and action in self._TAB_KEY_ACTIONS:
            if action == "new_tab" and not p.get("name"):
                p["name"] = f"tab_{len(self._pages)}"
            if "name" in p:
                p["name"] = self.tab_key(sid, p["name"])

        handlers = {
            "navigate":          lambda: self.navigate(p.get("url"), p.get("wait_until", "networkidle")),
            "screenshot":        lambda: self.screenshot(p.get("path"), p.get("full_page", True),
                                                         p.get("selector"), p.get("clip"), p.get("return_base64", False),
                                                         p.get("image_type", "png"), p.get("quality", 80), p.get("scale")),
            "click":             lambda: self.click(p["selector"], p.get("button", "left"),
                                                    p.get("click_count", 1), p.get("delay", 0), p.get("position")),
            "click_xy":          lambda: self.click_xy(p["x"], p["y"], p.get("button", "left"),
                                                       p.get("click_count", 1), p.get("delay", 0)),
            "click_text":        lambda: self.click_text(p["text"], p.get("exact", False)),
            "click_nth":         lambda: self.click_nth(p["selector"], p.get("index", 0)),
            "type":              lambda: self.type_text(p["selector"], p["text"], p.get("clear_first", True), p.get("delay", 0)),
            "fill":              lambda: self.fill(p["selector"], p["value"]),
            "clear":             lambda: self.clear(p["selector"]),
            "press_key":         lambda: self.press_key(p["key"], p.get("selector")),
            "select":            lambda: self.select_option(p["selector"], p.get("value"), p.get("label"), p.get("index")),
            "check":             lambda: self.check(p["selector"], p.get("force", False)),
            "uncheck":           lambda: self.uncheck(p["selector"]),
            "upload_file":       lambda: self.upload_file(p["selector"], p.get("files") or p.get("file_path")),
            "hover":             lambda: self.hover(p["selector"]),
            "scroll":            lambda: self.scroll(p.get("selector"), p.get("x", 0), p.get("y", 500),
                                                     p.get("direction")),
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
            "eval_js":           lambda: self.eval_js(p["script"], p.get("selector"), p.get("isolated", False)),
            "get_title":         lambda: self.get_title(),
            "get_url":           lambda: self.get_url(),
            "reload":            lambda: self.reload(p.get("wait_until", "networkidle")),
            "go_back":           lambda: self.go_back(p.get("wait_until", "load")),
            # 标签页/持久化/断言（来自 BrowserProxyTabs mixin）
            "new_tab":           lambda: self.new_tab(p.get("name", "tab_" + str(len(self._pages)))),
            "new_isolated_session": lambda: self.new_isolated_session(p["name"], p.get("profile")),
            "new_shared_session": lambda: self.new_shared_session(p["name"]),
            "switch_tab":        lambda: self.switch_tab(p["name"]),
            "close_tab":         lambda: self.close_tab(p["name"]),
            "list_user_tabs":    lambda: self.list_user_tabs(),
            "adopt_tab":         lambda: self.adopt_tab(p["index"], p.get("name", ""), p.get("activate", True)),
            "mock_route":        lambda: self.mock_route(p["url_pattern"], p.get("response_data", {})),
            "clear_routes":      lambda: self.clear_routes(),
            "analyze_network":   lambda: self.analyze_network(),
            "wait_for_download": lambda: self.wait_for_download(p["filename"]),
            "save_cookies":      lambda: self.save_cookies(p.get("path", user_data.cookies_file())),
            "load_cookies":      lambda: self.load_cookies(p.get("path", user_data.cookies_file())),
            "save_storage":      lambda: self.save_storage(p.get("path", user_data.state_file())),
            "clear_storage":     lambda: self.clear_storage(),
            # 站点经验（来自 BrowserProxySites mixin；纯文件操作，无需浏览器）
            "list_sites":        lambda: self.list_sites(),
            "read_site":         lambda: self.read_site(p.get("domain") or p.get("url")),
            "write_site":        lambda: self.write_site(p["domain"], p.get("aliases"), p.get("platform"), p.get("patterns"), p.get("pitfalls")),
            "delete_site":       lambda: self.delete_site(p["domain"]),
            # 本地浏览器资源检索（来自 BrowserProxyLocalSearch mixin；纯文件操作）
            "search_local":      lambda: self.search_local(p.get("keywords", p.get("keyword")),
                                                           p.get("only"), p.get("browser"),
                                                           p.get("limit", 20), p.get("since"),
                                                           p.get("sort", "recent")),
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
            "list_frames":       lambda: self.list_frames(),
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
                # success 缺省回落 ok：拒绝/失败类结果（如 clear_storage 守卫拒绝）
                # 只带 ok=false，若默认 success=True 会把失败步骤误判为成功
                res["success"] = res.get("success", res.get("ok", True))
            # O3: 缓存失效 — 改变页面状态的操作后清除元素缓存
            _CACHE_INVALIDATING_ACTIONS = {
                "navigate", "click", "click_xy", "click_text", "click_nth", "fill", "type",
                "select", "check", "uncheck", "scroll", "scroll_to_element",
                "reload", "switch_tab", "switch_frame", "switch_to_main_frame",
                "new_tab", "drag_and_drop", "drag_drop", "hover", "load_profile",
            }
            if action in _CACHE_INVALIDATING_ACTIONS:
                self._cache_invalidate()
            return res
        except Exception as e:
            err_msg = str(e).split('\n')[0]
            emit({"type": action, "success": False, "error": err_msg})
            return {"success": False, "error": err_msg}