#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器代理服务端 (Browser Agent Server)
基于 aiohttp 的纯异步 HTTP 微服务，持久运行 Playwright 浏览器实例。

端点：
  GET  /status          健康检查（含浏览器模式 / 站点经验列表 / 用户数据目录）
  POST /session/start   启动/重配会话（context=isolated|user）
  POST /session/reset   重置页面（保留浏览器）
  POST /run             执行命令批次
  POST /check           预设检验套件
  GET  /sites           站点经验列表
  GET  /site            读取站点经验（?domain= 或 ?url=）
  POST /site            追加站点经验
  GET  /attach/discover 列出可附着的本机浏览器
  POST /attach/select   保存附着偏好
  POST /shutdown        优雅关闭

启动：
  python -m browser_agent.server [--port=7788] [--url=http://...] [--headless] [--slow-mo=N]
                                 [--attach [--attach-dir=<浏览器数据目录> | --cdp-endpoint=<端点>]]
  # pip 安装后等价：browser-agent-server [--port=7788] ...
"""
import asyncio
import hashlib
import sys
import uuid
from contextlib import asynccontextmanager
import json
import os
import time
import signal
from datetime import datetime
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from aiohttp import web
except ImportError:
    print("[错误] 缺少 aiohttp，请执行: pip install aiohttp", file=sys.stderr)
    sys.exit(1)

from .proxy import BrowserProxy
from .utils import report_dir
from . import site_patterns
from . import user_data
from . import __version__ as _VERSION
_SERVICE_NAME = "browser_agent"

# ── 服务实例身份 ───────────────────────────────────────────
# 客户端/维护操作据此确认连的是同一个实例；revision 是核心源码指纹，
# 代码升级后旧服务仍在跑可被识别（升级需重启服务生效）。
_INSTANCE_ID = uuid.uuid4().hex
_STARTED_AT = datetime.now().isoformat(timespec="seconds")


def _running_revision() -> str:
    """核心源码指纹（sha256 前 12 位）；读不到的文件以文件名占位。"""
    h = hashlib.sha256()
    pkg = os.path.dirname(os.path.abspath(__file__))
    for name in ("proxy.py", "proxy_ai.py", "proxy_tabs.py", "proxy_auth.py",
                 "site_patterns.py", "browser_attach.py", "local_search.py",
                 "user_data.py", "server.py", "client.py"):
        try:
            with open(os.path.join(pkg, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(name.encode("utf-8"))
    return h.hexdigest()[:12]


_RUNNING_REVISION = _running_revision()

# ─────────────────────────────────────────────────────────
# 应用状态
# ─────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# 命令批排队等待上限：多任务共享服务时，后来的批等待锁的最长时间
QUEUE_TIMEOUT_SEC = _env_int("BROWSER_AGENT_QUEUE_TIMEOUT", 600)
# 空闲自动回收：所有会话超过该秒数无活动 → 自动关闭浏览器释放内存（0=禁用）
IDLE_SHUTDOWN_SEC = _env_int("BROWSER_AGENT_IDLE_SHUTDOWN_SEC", 900)
# 页级空闲回收：自建标签页超过该秒数无活动自动关闭（0=禁用；
# 收养页、各会话 active 页、在途批会话名下的页除外——会话整体归
# /session/stop 与全局空闲回收管）。批完成后事件驱动扫描（节流），心跳兜底。
TAB_IDLE_SEC = _env_int("BROWSER_AGENT_TAB_IDLE_SEC", 1800)

DEFAULT_SESSION = "default"


class Session:
    """一个会话 = page 集 + 自己的 active tab + context 归属模式。

    context_mode：
    - "isolated"：独占一个 browser context（cookie/storage 与他人互不干扰）
    - "user"：落在共享 default context（managed=启动 context；attach=用户真实
      profile——登录态免费）。关闭时只关自建标签页，绝不 close 共享 context。

    多任务共享同一浏览器实例：命令批**按会话加锁**——不同会话的批真并行
    （proxy 的 contextvar 标签上下文保证各批路由到各自的页），同一会话内
    串行（页面状态有序变更）。
    """

    def __init__(self, sid: str, owner: str = "", tab: str = "default",
                 context_mode: str = "isolated"):
        self.sid = sid
        self.owner = owner
        self.active_tab = tab      # proxy._pages 中的键（含 sid 前缀）
        self.context_mode = context_mode
        self.created_at = time.time()
        self.last_active = time.time()

    def touch(self):
        self.last_active = time.time()


class AppState:
    def __init__(self):
        self.proxy: Optional[BrowserProxy] = None
        self.session_id: Optional[str] = None
        # 多会话注册表：sid -> Session。旧客户端不带 session_id 的请求归入 DEFAULT_SESSION
        self.sessions: dict = {}
        self.config = {
            "port": 7788,
            "base_url": "http://localhost:3000",
            "headless": False,
            "slow_mo": 0,
            "viewport_w": 1440,
            "viewport_h": 900,
            "mode": "managed",          # managed=自拉隔离 Chromium | attach=附着用户日常浏览器
            "attach_dir": "",           # attach：浏览器用户数据目录（含 Local State 那层）
            "cdp_endpoint": "",         # attach：显式 CDP 端点（高级，优先于 attach_dir）
            "stealth_enabled": True,
        }
        self.macros = {}
        self.stream_clients = set()
        self._broker_lock = asyncio.Lock()
        # O2（升级）：命令批按**会话**排队——不同会话的批真并行（contextvar 标签
        # 上下文隔离操作目标页），同一会话内仍串行（页面状态有序变更）
        self._session_locks: dict = {}
        self._registry_lock = asyncio.Lock()  # 会话注册的 check-and-create 互斥
        self._inflight = 0                    # 正在执行的命令批数（关停排水用）
        # 关停维护态：/shutdown 触发后置位，排水期间拒绝新命令批/新会话，
        # 防止排水永远等不完、或新批刚起步就被 SIGTERM 腰斩
        self.maintenance = False

    def session_lock(self, sid: str) -> asyncio.Lock:
        """会话级命令锁（按需创建；创建互斥防两个请求各造一把）。"""
        lock = self._session_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[sid] = lock
        return lock

    async def _acquire_session_lock(self, sid: str,
                                     timeout_sec: Optional[int] = None) -> Optional[asyncio.Lock]:
        """带超时获取会话锁。timeout None/0 = 无限等待；超时返回 None（排队失败）。

        命令执行路径用 QUEUE_TIMEOUT_SEC；控制路径（stop/reset/refresh）用短超时——
        会话忙（有命令批在跑）时拒绝而不是干等，防止控制请求被长批卡死。
        返回已持有的锁或 None；调用方负责 release。
        """
        lock = self.session_lock(sid)
        timeout = QUEUE_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        if timeout <= 0:
            await lock.acquire()
            return lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return lock
        except asyncio.TimeoutError:
            return None

    def _resolve_active_tab(self, sess: Session) -> Optional[str]:
        """会话当前应操作的页键：active_tab 有效则用之，否则回落到名下任一存留页。"""
        if sess.active_tab in self.proxy._pages:
            return sess.active_tab
        return next(iter(self._session_pages(sess.sid)), None)

    def _session_pages(self, sid: str) -> list:
        """会话名下的页面键：sid 前缀键 + 初始页（default 会话的 'default' / 其余会话的裸 sid 键）。"""
        if not self.proxy:
            return []
        own = [k for k in self.proxy._pages if k.startswith(f"{sid}::")]
        if sid in self.proxy._pages and sid not in own:
            own.append(sid)
        return own

    @asynccontextmanager
    async def session_batch(self, sid: str, control: bool = False):
        """命令批/控制操作的会话锁协议：获取 → 锁内重钉 → 保证释放。

        control=True 用短时限（忙时拒绝，不被长批卡死）；否则用 QUEUE_TIMEOUT。
        yield True（持锁）或 False（忙/超时，调用方返回自己的错误报文）。
        """
        timeout = CONTROL_LOCK_TIMEOUT_SEC if control else None
        acquired = await self._acquire_session_lock(sid, timeout_sec=timeout)
        try:
            if acquired and sid in self.sessions:
                # 锁内重钉：同会话的前一批可能换了页（new_tab 等），必须以拿锁后
                # 的会话 active_tab 为准；router 带 sid → dispatch 命名空间生效
                tab = self._resolve_active_tab(self.sessions[sid])
                if tab is not None:
                    self.proxy._begin_batch(sid, tab)
            yield acquired is not None
        finally:
            if acquired is not None:
                acquired.release()

    def _session_context(self, sid: str):
        """会话对应的 browser context（user 模式 / default 会话 = 共享 default context）。"""
        if not self.proxy:
            return None
        sess = self.sessions.get(sid)
        if sid == DEFAULT_SESSION or (sess and sess.context_mode == "user"):
            return self.proxy._contexts.get("default")
        return self.proxy._contexts.get(sid)

    async def ensure_proxy(self, cfg: Optional[dict] = None):
        """启动浏览器（幂等）。锁保护整个创建流程，防止并发启动。"""
        async with self._broker_lock:
            if self.proxy is not None and self.proxy.is_browser_open():
                return
            c = {**self.config, **(cfg or {})}
            attach_mode = c.get("mode") == "attach"
            if not attach_mode:
                # 资源预检兜底（client 侧已查，此处防直接 HTTP 调 server 绕过）：
                # 低配机器余量不足时拒绝拉起 Chromium，防整机被压垮。
                # attach 模式不拉浏览器、几乎零内存开销，跳过预检
                from .utils import check_system_resources

                ok, detail = check_system_resources()
                if not ok:
                    raise RuntimeError(f"资源预检未通过：{detail}")
            self.proxy = BrowserProxy(
                base_url=c["base_url"],
                headless=c["headless"],
                slow_mo=c["slow_mo"],
                viewport_w=c["viewport_w"],
                viewport_h=c["viewport_h"],
                proxy_server=c.get("http_proxy"),
                record_video_dir=c.get("record_video_dir"),
                record_har_path=c.get("record_har_path"),
                no_storage=c.get("no_storage", False),
                ignore_https_errors=c.get("ignore_https_errors", False),
                mode="attach" if attach_mode else "managed",
                attach_dir=c.get("attach_dir", ""),
                cdp_endpoint=c.get("cdp_endpoint", ""),
                stealth_enabled=c.get("stealth_enabled", True),
                storage_guard=self._shared_context_refusal,
            )
            try:
                await self.proxy.start()
            except BaseException:
                # 启动失败（附着被拒/端点死/资源不足…）：清掉半启动实例，
                # 防泄漏 playwright driver 子进程；下次请求重建
                try:
                    await self.proxy.stop()
                except Exception:
                    pass
                self.proxy = None
                raise
            self.sessions.clear()
            # 首个（通常是 default）会话直接落在浏览器自带的 default context 上
            self._register_session(DEFAULT_SESSION, context_mode="user")
            _log(f"浏览器已就绪（{self.proxy.mode}），初始会话: {self.session_id}")

    def _register_session(self, sid: str, owner: str = "", tab: Optional[str] = None,
                          context_mode: str = "isolated") -> Session:
        """登记会话。tab 缺省按 context 归属自动探测（歧义场景务必显式传入）。"""
        ctx = self._session_context(sid)
        if tab is None:
            tab = next((k for k, p in self.proxy._pages.items()
                        if p.context is ctx and self._page_owner(k) in (None, sid)), "default")
        sess = Session(sid, owner, tab=tab, context_mode=context_mode)
        self.sessions[sid] = sess
        if not self.session_id:
            self.session_id = sid
        return sess

    def _page_owner(self, page_key: str) -> Optional[str]:
        """页面键归属的会话：'sid::tab' → sid；裸键（default / 会话初始页）→ None（歧义）。"""
        if "::" in page_key:
            return page_key.split("::", 1)[0]
        return None



    def _shared_context_refusal(self, sid: str) -> Optional[str]:
        """storage 清除类操作（session/refresh、clear_storage）的共享上下文守卫。

        返回拒绝原因；None = 允许。规则：
        - isolated 会话独占 context → 允许
        - user 模式（default context）在 attach 下 = 用户真实浏览器 → 拒绝
        - user 模式在 managed 下与他人（default 会话或其他 user 会话）共享 → 拒绝
        - user 模式独占（仅 default 会话自身，legacy 单会话用法）→ 允许
        """
        sess = self.sessions.get(sid)
        if not sess or sess.context_mode != "user":
            return None
        if self.proxy is not None and self.proxy._shared_default_context:
            return ("当前会话运行在用户日常浏览器上下文（attach/user），拒绝清除——"
                    "会波及用户真实登录态")
        sharers = sorted(s.sid for s in self.sessions.values() if s.context_mode == "user")
        if len(sharers) > 1:
            return f"default context 由多个会话共享 {sharers}，清除会波及他人"
        return None

    async def _heal_session_page(self, sess: Session) -> None:
        """user 模式会话的初始页已被关闭时，在共享 default context 内重开（无断点自愈）。"""
        if sess.context_mode != "user" or not self.proxy:
            return
        if sess.active_tab in self.proxy._pages:
            return
        ctx = self.proxy._contexts.get("default")
        if not ctx:
            return
        await self.proxy._register_page(ctx, sess.active_tab, label=f"session:{sess.sid}")
        _log(f"会话 {sess.sid} 初始页已失效，已在 default context 重开")

    async def ensure_session(self, sid: str, owner: str = "",
                             context_mode: str = "isolated") -> tuple:
        """确保会话存在。

        :param context_mode: "isolated"（默认，独占 context，cookie/storage 隔离）；
                             "user"（共享 default context——attach 模式即用户真实
                             profile，登录态免费；关闭时只关自建标签页）。
                             DEFAULT_SESSION 恒为 "user"。
        返回 (session, created, warning)。浏览器级配置（headless/viewport 等）只在
        浏览器冷启动时生效——浏览器已在运行时新会话沿用现有实例，返回 warning 说明。

        整个 ensure 阶段计入 inflight：CDP 授权等待可达 120s，关停排水必须看得见
        （所有调用方——run/check/session_start/macro/auth_ensure——由此统一覆盖）。
        """
        self._inflight += 1
        try:
            return await self._ensure_session_inner(sid, owner, context_mode)
        finally:
            self._inflight -= 1

    async def _ensure_session_inner(self, sid: str, owner: str = "",
                                    context_mode: str = "isolated") -> tuple:
        warnings = None
        if self.proxy is not None and self.proxy.is_browser_open() and sid not in self.sessions:
            warnings = "浏览器已在运行（被其他会话共享）：本次传入的实例级配置（headless/viewport/代理等）未生效，会话沿用现有实例"
        await self.ensure_proxy()
        if sid in self.sessions:
            sess = self.sessions[sid]
            await self._heal_session_page(sess)
            sess.touch()
            return sess, False, None

        # check-and-create 互斥：同 sid 并发首请求只建一次会话
        async with self._registry_lock:
            if sid in self.sessions:  # 双检：等锁期间可能已被并发请求创建
                sess = self.sessions[sid]
                sess.touch()
                return sess, True, warnings
            if sid == DEFAULT_SESSION:
                # default 的 context 由浏览器启动时创建，这里只补登记（浏览器刚重启等场景）
                sess = self._register_session(sid, owner, context_mode="user")
                await self._heal_session_page(sess)
                return sess, True, warnings

            if context_mode == "user":
                res = await self.proxy.new_shared_session(sid)
                if not res.get("ok", True):
                    return None, False, res.get("error", "创建共享会话失败")
                sess = self._register_session(sid, owner, tab=sid, context_mode="user")
                return sess, True, warnings

            # 新会话：在共享浏览器上开隔离 context（独立 cookie/storage）
            res = await self.proxy.new_isolated_session(sid)
            if not res.get("ok", True):
                return None, False, res.get("error", "创建会话上下文失败")
            sess = self._register_session(sid, owner, tab=sid, context_mode="isolated")
            return sess, True, warnings

    def switch_to_session(self, sid: str) -> bool:
        """把本任务上下文的操作目标切到该会话的 tab（命令执行前调用）。

        实际页路由由 proxy 的 contextvar 完成（每个命令批任务独立），这里只做
        校验与会话回落；并发批互不影响。
        """
        sess = self.sessions.get(sid)
        if not sess or not self.proxy:
            return False
        tab = self._resolve_active_tab(sess)
        if tab is None:
            return False
        sess.active_tab = tab
        self.proxy._begin_batch(sid, tab)
        sess.touch()
        self.session_id = sid
        return True

    async def close_session(self, sid: str, shutdown_if_last: bool = True) -> dict:
        """关闭单个会话（只关自己名下的页面/context，不动其他会话）。

        - isolated 会话：close 自己的 context（其下页面随之关闭）
        - user 会话 / default 会话：只关自建标签页，绝不 close 共享 default context；
          attach 模式下它就是用户的日常浏览器 profile
        - 最后一个会话关闭且 shutdown_if_last：managed 关浏览器；attach 断开连接
          （用户浏览器保持运行）。attach 的空闲回收传 False——清标签页但连接永驻
        """
        sess = self.sessions.pop(sid, None)
        if not self.proxy or not sess:
            return {"ok": True, "closed": sid, "note": "会话不存在或已关闭"}
        shared = sess.context_mode == "user"
        pages = self.proxy._pages
        adopted = self.proxy._adopted_tabs
        if shared:
            for key in self._session_pages(sid):
                if key in adopted:
                    # 用户收养的标签页：只取消注册，绝不当用户的页关掉
                    pages.pop(key, None)
                    adopted.discard(key)
                    self.proxy._purge_tab_state(key)
                    continue
                page = pages.pop(key, None)
                self.proxy._purge_tab_state(key)
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
        else:
            ctx = self.proxy._contexts.get(sid)
            if ctx:
                # context.close() 自动关闭其下全部 page；注册表按 context 归属清理
                for key in [k for k, p in list(pages.items()) if p.context is ctx]:
                    pages.pop(key, None)
                    self.proxy._purge_tab_state(key)
                try:
                    await ctx.close()
                except Exception:
                    pass
                self.proxy._contexts.pop(sid, None)
        # 批内 new_isolated_session 建的 context（键为 sid::name）一并回收，防泄漏
        for ck in [k for k in list(self.proxy._contexts)
                   if k.startswith(BrowserProxy.tab_key(sid, ""))]:
            ctx = self.proxy._contexts.pop(ck)
            for key in [k for k, p in list(pages.items()) if p.context is ctx]:
                pages.pop(key, None)
                self.proxy._purge_tab_state(key)
            try:
                await ctx.close()
            except Exception:
                pass
        if not self.sessions and self.proxy and shutdown_if_last:
            await self.shutdown_browser()
            return {"ok": True, "closed": sid, "browser_shutdown": True}
        if self.session_id == sid:
            self.session_id = next(iter(self.sessions), None)
        return {"ok": True, "closed": sid}

    async def reset_session_page(self, sid: str) -> str:
        """重置会话页面：当前会话 context 内新开 page（替换 active tab）。"""
        sess = self.sessions.get(sid)
        if not self.proxy or not sess:
            return "会话不存在"
        ctx = self.proxy._contexts.get(
            "default" if sess.context_mode == "user" else sid)
        if not ctx:
            return "会话上下文不存在"
        old = self.proxy._pages.get(sess.active_tab)
        await self.proxy._register_page(ctx, sess.active_tab, label=f"session:{sid}")
        if old:
            try:
                await old.close()
            except Exception:
                pass
        sess.touch()
        _log(f"会话 {sid} 页面已重置")
        return ""

    def _effective_idle_sec(self) -> int:
        """空闲回收阈值（两模式共用，BROWSER_AGENT_IDLE_SHUTDOWN_SEC 控制，0=禁用）。

        调用时动态读 env（改环境变量即时生效，无需重启服务）。回收的语义按模式
        区分（见 idle_recycle）：managed 关浏览器；attach 只关超时会话的自建
        标签页、**连接保持**——新式开关下每条新连接都要用户在浏览器里重新授权。
        """
        return _env_int("BROWSER_AGENT_IDLE_SHUTDOWN_SEC", IDLE_SHUTDOWN_SEC)

    def sessions_info(self) -> list:
        now = time.time()
        return [
            {
                "session_id": s.sid,
                "owner": s.owner,
                "active_tab": s.active_tab,
                "context_mode": s.context_mode,
                "idle_sec": int(now - s.last_active),
                "age_sec": int(now - s.created_at),
            }
            for s in self.sessions.values()
        ]

    async def idle_recycle(self):
        """所有会话空闲超阈值 → 回收（HTTP 服务保留）。

        - managed：关闭 Chromium 释放内存（下次请求自动重启）
        - attach：只关超时会话的自建标签页与隔离 context，**CDP 连接保持不断**——
          重连需要用户在浏览器里再次授权；同时防 agent 异常退出留下的孤儿标签页
        """
        idle_sec = self._effective_idle_sec()
        if idle_sec <= 0 or not self.sessions:
            return
        # 在途命令批存在（含单步长命令如 watch_for_change 数分钟）→ 不回收，
        # 防止把正在执行的批腰斩（显式 /shutdown 走排水路径）
        if self._inflight > 0:
            return
        now = time.time()
        # 任一会话仍在活跃期即不回收（须所有会话全部超时才回收）
        if any(now - s.last_active < idle_sec for s in self.sessions.values()):
            return
        idle = {s.sid: int(now - s.last_active) for s in self.sessions.values()}
        if self.proxy.mode == "attach":
            _log(f"空闲回收（attach）：会话 {list(idle)} 的自建标签页已关闭，连接保持"
                 f"（断连需用户重新授权；{idle_sec}s 阈值）")
            for sid in list(self.sessions):
                await self.close_session(sid, shutdown_if_last=False)
            self.session_id = None
            return
        _log(f"空闲回收：所有会话超时无活动 {idle}，关闭浏览器释放内存（{idle_sec}s 阈值）")
        await self.shutdown_browser()

    async def recycle_idle_tabs(self, exclude_sid: Optional[str] = None) -> list:
        """页级空闲回收：自建标签页超时无活动自动关闭。

        保护集 = 各会话 active_tab ∪ **在途批会话**名下的页。批中途
        switch_tab 的目标页要等批结束才回写 Session.active_tab——期间该页
        只被 _set_current_tab 的触碰保活，锁被持有即视为在途，整组跳过，
        回收由此在并发批下天然安全（不需要"全局 inflight 就不扫"的粗退避）。
        exclude_sid：触发本次扫描的批所属会话——它刚完成 active_tab 回写，
        自身按 active_tab 保护即可，不因自持锁被误跳过。
        """
        if TAB_IDLE_SEC <= 0 or not self.proxy or not self.proxy.is_browser_open():
            return []
        protected = {s.active_tab for s in self.sessions.values()}
        for sid, lock in self._session_locks.items():
            if sid != exclude_sid and lock.locked():
                # 裸 sid 键（会话初始页）与 sid:: 前缀键都算该会话名下
                protected.update(k for k in self.proxy._pages
                                 if k == sid or self.proxy.tab_owner(k) == sid)
        return await self.proxy.close_idle_tabs(TAB_IDLE_SEC, protected)

    async def shutdown_browser(self):
        attached = self.proxy.mode == "attach"
        if self.proxy:
            # auto_save：在 stop() 之前保存活跃会话（storage_state 需活跃 context）。
            # 安全边界：活跃页位于共享用户上下文（attach/user）时跳过——
            # 不把用户日常浏览器的全部登录态悄悄落盘
            if self.config.get("auto_save_state") and self.proxy._page:
                if self.proxy._in_user_context():
                    _log("auto_save 跳过：活跃页在共享用户上下文（attach/user），不导出用户全部登录态")
                else:
                    try:
                        state_path = user_data.state_file()
                        await self.proxy.save_storage(state_path)
                        ap = self.proxy._active_profile_name()
                        if ap:
                            await self.proxy.save_profile(ap)
                        _log(f"auto_save 已落盘 state.json{' + profile ' + ap if ap else ''}")
                    except Exception as e:
                        _log(f"auto_save 失败: {e}")
            try:
                await self.proxy.stop()
            except Exception:
                pass
            self.proxy = None
            self.session_id = None
            self.sessions.clear()
        _log("已断开浏览器连接（用户浏览器保持运行）" if attached else "浏览器已关闭")


_state = AppState()


def _log(msg: str):
    print(f"[服务] {datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ─────────────────────────────────────────────────────────
# 执行工具
# ─────────────────────────────────────────────────────────
async def _build_error_hint(proxy, action: str, err_msg: str, selector: str = None) -> dict:
    """根据错误类型和当前页面状态生成面向 AI 的结构化诊断提示。"""
    hint = {}
    try:
        current_url = proxy._page.url if proxy and proxy._page else ""
        page_title = await proxy._page.title() if proxy and proxy._page else ""
        hint["current_url"] = current_url
        hint["page_title"] = page_title

        err_lower = err_msg.lower()
        if "timeout" in err_lower or "超时" in err_lower:
            hint["possible_cause"] = "选择器失效或页面尚未完成加载"
            hints = ["先调用 wait_for_stable 确认页面就绪"]
            if selector:
                hints.append(f"尝试用 get_page_summary 查看当前页面结构后重新定位 '{selector}'")
            if current_url != (proxy.base_url if proxy else ""):
                hints.append(f"页面已跳转到 {current_url}，原选择器可能不再存在")
            hint["suggestion"] = "；".join(hints)
        elif "net::err" in err_lower or "connection" in err_lower:
            hint["possible_cause"] = "网络连接失败或目标服务未启动"
            hint["suggestion"] = f"检查目标地址 {current_url} 是否可访问，或是否需要 VPN/代理"
        elif "security" in err_lower or "cors" in err_lower:
            hint["possible_cause"] = "跨域安全限制"
            hint["suggestion"] = "eval_js 操作被同源策略阻止，尝试直接操作 DOM 元素而非通过 JS"
        elif "not found" in err_lower or "未知命令" in err_lower:
            hint["possible_cause"] = "使用了不存在的命令名称"
            hint["suggestion"] = "调用 get_page_summary 查看当前状态，检查命令拼写是否正确"
        else:
            hint["possible_cause"] = "未知错误"
            hint["suggestion"] = "调用 get_page_summary 了解当前页面状态后重新规划下一步"

        # 自动截图，让 AI 直接看到失败现场（仅截 viewport，排除全页以控制大小）
        try:
            import base64
            screenshot_bytes = await proxy._page.screenshot(full_page=False)
            hint["screenshot_base64"] = base64.b64encode(screenshot_bytes).decode()
        except Exception:
            pass
    except Exception:
        pass
    return hint


def _apply_token_budget(step_record: dict, max_chars: int) -> dict:
    """截断过大的响应数据，保护 AI 上下文窗口。截断时添加 truncation_notice 通知。"""
    import json as _json
    try:
        data = step_record.get("data")
        if not isinstance(data, dict):
            return step_record
        serialized = _json.dumps(data, ensure_ascii=False)
        if len(serialized) <= max_chars:
            return step_record
        # 找最大的列表字段进行截断
        truncated = {}
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 3:
                original_len = len(val)
                kept = val[:3]
                truncated[key] = {
                    "original_count": original_len,
                    "shown": kept,
                    "truncated": True,
                    "ai_hint": f"原始 {original_len} 条，已截断至 3 条以控制响应大小。设置更大的 max_response_chars 参数可获取更多数据。"
                }
            elif isinstance(val, dict) and val.get("items") and isinstance(val["items"], list) and len(val["items"]) > 3:
                original_len = len(val["items"])
                val_copy = dict(val)
                val_copy["items"] = val["items"][:3]
                val_copy["truncated"] = True
                val_copy["original_count"] = original_len
                val_copy["ai_hint"] = f"原始 {original_len} 条，已截断至 3 条。增大 max_response_chars 获取完整数据。"
                truncated[key] = val_copy
            else:
                truncated[key] = val
        step_record = dict(step_record)
        step_record["data"] = truncated
        step_record["token_budget_applied"] = True
        step_record["token_budget_chars"] = max_chars
    except Exception:
        pass
    return step_record


async def execute_commands(commands: list, out_dir: str, compact: bool = False,
                           generate_report: bool = True, session_id: str = "unknown") -> dict:
    _state._inflight += 1
    try:
        return await _execute_commands_inner(commands, out_dir, compact, generate_report, session_id)
    finally:
        # 批完成后顺手做页级空闲回收（事件驱动、节流；活跃会话场景比纯心跳
        # 轮询更及时，零流量场景由全局空闲回收兜底）。先扫后减：扫描（含
        # page.close）期间仍计在途，关停排水不会扫到一半被 SIGTERM 打断
        await _sweep_idle_tabs(session_id)
        _state._inflight -= 1


# 页级回收扫描节流：默认每 ≤30s 搭一次批的便车；阈值越小扫描越勤
_TAB_SWEEP = {"last": 0.0}


async def _sweep_idle_tabs(session_id: str) -> None:
    if TAB_IDLE_SEC <= 0:
        return
    throttle = max(1.0, min(30.0, TAB_IDLE_SEC / 10))
    now = time.monotonic()
    if now - _TAB_SWEEP["last"] < throttle:
        return
    _TAB_SWEEP["last"] = now
    try:
        await _state.recycle_idle_tabs(exclude_sid=session_id)
    except Exception as e:
        _log(f"页级空闲回收异常（忽略）: {e}")


async def _execute_commands_inner(commands, out_dir, compact, generate_report,
                                   session_id) -> dict:
    proxy = _state.proxy
    sess = _state.sessions.get(session_id)
    steps, screenshots, errors = [], [], []

    # 会话页失效时回落到名下任一存留页（与 switch_to_session 同一解析逻辑）
    if sess is not None and _state._resolve_active_tab(sess) is None:
        return {"ok": False, "session_id": session_id,
                "error": f"会话 '{session_id}' 无可用页面", "steps": [], "errors": []}
    for i, cmd in enumerate(commands):
        action = cmd.get("action", cmd.get("type", ""))
        comment = cmd.get("comment", "")
        # 每步开始即刷新活跃时间：单步长命令（如 watch_for_change 数分钟）期间
        # 会话仍算活跃，不会被空闲回收腰斩正在执行的批
        if sess is not None:
            sess.touch()
        _log(f"步骤 {i+1}/{len(commands)}: {action}" + (f" ({comment})" if comment else ""))
        t0 = time.monotonic()

        # ── V6: loop 批量循环宏指令（在 _dispatch 层外处理，支持迭代验证）──
        if action == "loop":
            selector = cmd.get("selector", "")
            loop_cmds = cmd.get("commands", [])
            limit = min(cmd.get("limit", 20), 100)
            try:
                count = await proxy._page.locator(selector).count()
                actual = min(count, limit)
                _log(f"  🔁 loop: 对 {actual}/{count} 个 '{selector}' 执行 {len(loop_cmds)} 条指令")
                loop_results, loop_errors = [], 0
                for idx in range(actual):
                    iter_steps = []
                    for tmpl in loop_cmds:
                        iter_cmd = json.loads(
                            json.dumps(tmpl, ensure_ascii=False)
                            .replace("{{index}}", str(idx))
                            .replace("{{index_1based}}", str(idx + 1))
                            .replace("{{count}}", str(actual))
                        )
                        try:
                            iter_res = await asyncio.wait_for(proxy._dispatch(iter_cmd), timeout=30)
                            iter_ok = iter_res.get("ok", iter_res.get("success", True)) if isinstance(iter_res, dict) else True
                            iter_steps.append({"action": iter_cmd.get("action"), "ok": iter_ok, "data": iter_res})
                            if not iter_ok:
                                loop_errors += 1
                        except Exception as e:
                            iter_steps.append({"action": iter_cmd.get("action"), "ok": False, "error": str(e).split(chr(10))[0]})
                            loop_errors += 1
                    loop_results.append({"index": idx, "steps": iter_steps})
                dur = int((time.monotonic() - t0) * 1000)
                loop_ok = loop_errors == 0
                loop_data = {
                    "ok": loop_ok, "total_selectors": count, "iterations": actual,
                    "error_count": loop_errors, "results": loop_results,
                    "ai_hint": (
                        None if loop_ok else
                        f"loop 执行了 {actual} 次迭代，其中 {loop_errors} 次出现错误。"
                        f"请查看 results 数组中 ok=false 的迭代详情，考虑增加容错或减少迭代次数。"
                    )
                }
                step_record = {"step": i+1, "action": "loop", "comment": comment, "ok": loop_ok, "data": loop_data, "duration_ms": dur}
                steps.append(step_record)
                await _broadcast("step", step_record)
            except Exception as e:
                dur = int((time.monotonic() - t0) * 1000)
                err = str(e).split(chr(10))[0]
                _log(f"  ✗ loop 错误: {err}")
                step_record = {"step": i+1, "action": "loop", "comment": comment, "ok": False, "error": err, "duration_ms": dur,
                               "ai_hint": f"loop 失败: {err}。请先确认 selector '{selector}' 是否能匹配到元素。"}
                steps.append(step_record)
                errors.append({"step": i+1, "action": "loop", "error": err})
                await _broadcast("step", step_record)
            continue  # 跳过下方正常 _dispatch 流程 (loop)

        # ── V7: multi_page_batch 跨页批量提取宏指令 ──
        if action == "multi_page_batch":
            urls = cmd.get("urls", [])
            extract_schema = cmd.get("schema", {})
            limit = min(len(urls), cmd.get("limit", 10))
            inter_delay = cmd.get("delay_ms", 800)
            batch_results = []
            success_count = 0
            _log(f"  🌐 multi_page_batch: 共 {len(urls)} 个 URL，处理前 {limit} 个")
            for b_idx, url in enumerate(urls[:limit]):
                try:
                    await asyncio.wait_for(proxy._dispatch({"action": "navigate", "url": url, "wait_until": "load"}), timeout=30)
                    await asyncio.sleep(inter_delay / 1000.0)
                    extracted = {}
                    if extract_schema:
                        ex_res = await asyncio.wait_for(proxy._dispatch({"action": "extract", "schema": extract_schema}), timeout=15)
                        extracted = ex_res.get("data", {})
                    batch_results.append({"url": url, "ok": True, "index": b_idx, "data": extracted})
                    success_count += 1
                except Exception as be:
                    batch_results.append({"url": url, "ok": False, "index": b_idx, "error": str(be).split(chr(10))[0]})
                    _log(f"    ✗ URL [{b_idx+1}] {url[:50]} 失败: {str(be).split(chr(10))[0]}")
            batch_ok = success_count == limit
            dur = int((time.monotonic() - t0) * 1000)
            step_record = {
                "step": i+1, "action": "multi_page_batch", "comment": comment, "ok": batch_ok,
                "data": {
                    "urls_total": len(urls), "processed": limit,
                    "success": success_count, "failed": limit - success_count,
                    "results": batch_results,
                    "ai_hint": (
                        None if batch_ok else
                        f"批量处理 {limit} 个 URL，{limit - success_count} 个失败。"
                        f"请查看 results 中 ok=false 的项目详情。"
                        f"建议：单独对失败的 URL 执行 navigate+extract 重试"
                    )
                },
                "duration_ms": dur,
            }
            steps.append(step_record)
            await _broadcast("step", step_record)
            continue  # 跳过下方正常 _dispatch 流程 (multi_page_batch)

        try:
            # 引入单步执行的指令超时熔断(P0，防止底层抛不出错误无限死锁)，默认 30000ms
            # watch_for_change / smart_wait 等内部有自己的等待逻辑，步骤超时须 > 命令 timeout + 缓冲
            WAIT_ACTIONS = {"watch_for_change", "smart_wait", "wait_for", "wait_for_stable"}
            cmd_timeout_ms = cmd.get("timeout", 0)
            if action in WAIT_ACTIONS and cmd_timeout_ms > 0:
                timeout_ms = cmd_timeout_ms + 8000  # 命令 timeout + 8s 安全缓冲
            else:
                timeout_ms = cmd.get("timeout", 60000 if action == "wait_for" else 30000)
            result = await asyncio.wait_for(proxy._dispatch(cmd), timeout=timeout_ms / 1000.0)
            dur = int((time.monotonic() - t0) * 1000)
            # 命令可能切换了操作目标页（new_tab/switch_tab/adopt_tab/load_profile…）：
            # 同步回会话，让该会话的下一个批从离开的地方继续
            if sess is not None:
                current = proxy._tab()
                if current:
                    sess.active_tab = current
                sess.touch()
            
            # 支持业务层面的 False（如断言失败，或者常规 action 的 success: False）
            is_ok = True
            if isinstance(result, dict):
                is_ok = result.get("success", result.get("ok", True))
                if action == "screenshot" and result.get("path"):
                    screenshots.append(result["path"])
            
            if is_ok:
                step_record = {"step": i+1, "action": action, "comment": comment, "ok": True, "data": result, "duration_ms": dur, "session_id": session_id}
                # navigate 后自动附加页面摘要，让 AI 就地知道当前在哪里
                if action == "navigate" and proxy and proxy._page:
                    try:
                        summary_data = await proxy.get_page_summary()
                        step_record["data"]["page_summary"] = summary_data
                    except Exception:
                        pass
                    # 站点经验提示：目标域名已有积累的经验 → 指引先读取先验
                    try:
                        hit = site_patterns.match_site(result.get("url") or "")
                        if hit.get("matched"):
                            step_record["data"]["site_experience"] = {
                                "domain": hit["domain"],
                                "match_type": hit.get("match_type"),
                                "updated": hit.get("updated"),
                                "ai_hint": "该站点已有经验记录，建议先执行 read_site 获取先验（平台特征/有效模式/已知陷阱）",
                            }
                    except Exception:
                        pass
                # V6: Token 预算控制 — 截断超大响应以保护 AI 上下文窗口
                budget = cmd.get("max_response_chars", 0)
                if budget > 0:
                    step_record = _apply_token_budget(step_record, budget)
                steps.append(step_record)
                await _broadcast("step", step_record)
            else:
                err_msg = result.get("error", "操作返回了失败状态但未提供详细信息")
                hint = await _build_error_hint(proxy, action, err_msg, cmd.get("selector"))
                _log(f"  ✗ {err_msg}")
                errors.append({"step": i+1, "action": action, "error": err_msg})
                step_record = {"step": i+1, "action": action, "comment": comment, "ok": False, "error": err_msg, "hint": hint, "duration_ms": dur}
                steps.append(step_record)
                await _broadcast("step", step_record)
                if cmd.get("stop_on_error", False):
                    break
        except (asyncio.TimeoutError, Exception) as e:
            err_msg = str(e) if not isinstance(e, asyncio.TimeoutError) else f"操作超时 (>{cmd.get('timeout', 30000)}ms)"
            healed = False
            if action in ("click", "type", "fill", "hover") and "selector" in cmd and ("Timeout" in err_msg or "超时" in err_msg):
                import re
                old_sel = cmd["selector"]
                txt_match = re.search(r'text=([\'"]?)(.+?)\1', old_sel) or re.search(r'has-text=([\'"]?)(.+?)\1', old_sel)
                if txt_match:
                    fuzzy = txt_match.group(2)
                    _log(f"  ✗ {err_msg} - 尝试 AI 自愈: 降级搜索文本 '{fuzzy}'")
                    try:
                        loc = proxy._page.get_by_text(fuzzy)
                        if await loc.count() > 0:
                            cmd["selector"] = f"text={fuzzy}"
                            _log(f"  ✓ 【AI 自愈】已将其纠正为 text={fuzzy} 并重试")
                            result = await asyncio.wait_for(proxy._dispatch(cmd), timeout=timeout_ms / 1000.0)
                            dur = int((time.monotonic() - t0) * 1000)
                            is_ok = result.get("success", result.get("ok", True)) if isinstance(result, dict) else True
                            if is_ok:
                                step_record = {"step": i+1, "action": action, "comment": "[AI自愈] " + str(comment), "ok": True, "data": result, "duration_ms": dur, "healed_selector": cmd["selector"]}
                                steps.append(step_record)
                                await _broadcast("step", step_record)
                                healed = True
                    except Exception:
                        pass

            if not healed:
                dur = int((time.monotonic() - t0) * 1000)
                _log(f"  ✗ {err_msg}")
                hint = await _build_error_hint(proxy, action, err_msg, cmd.get("selector"))
                errors.append({"step": i+1, "action": action, "error": err_msg})
                step_record = {"step": i+1, "action": action, "comment": comment, "ok": False, "error": err_msg, "hint": hint, "duration_ms": dur}
                steps.append(step_record)
                await _broadcast("step", step_record)

                # ── if_error_then 条件降级跳转 (V5) ──────────────────────────
                fallback_cmds = cmd.get("if_error_then")
                if fallback_cmds and isinstance(fallback_cmds, list):
                    _log(f"  ↩️ 触发 if_error_then，执行 {len(fallback_cmds)} 条备用指令")
                    errors.pop()  # 已走降级，从 errors 里移除原失败记录避免重复计
                    fb_ok_all = True
                    for j, fb_cmd in enumerate(fallback_cmds):
                        fb_action = fb_cmd.get("action", "")
                        fb_comment = "[降级] " + fb_cmd.get("comment", fb_action)
                        try:
                            fb_result = await asyncio.wait_for(proxy._dispatch(fb_cmd), timeout=30)
                            fb_ok = fb_result.get("ok", fb_result.get("success", True)) if isinstance(fb_result, dict) else True
                            fb_rec = {"step": f"{i+1}fb{j+1}", "action": fb_action, "comment": fb_comment, "ok": fb_ok, "data": fb_result, "duration_ms": 0}
                            steps.append(fb_rec)
                            await _broadcast("step", fb_rec)
                            if not fb_ok:
                                fb_ok_all = False
                        except Exception as fb_e:
                            fb_err = str(fb_e).split("\n")[0]
                            fb_rec = {"step": f"{i+1}fb{j+1}", "action": fb_action, "comment": fb_comment, "ok": False, "error": fb_err, "duration_ms": 0}
                            steps.append(fb_rec)
                            await _broadcast("step", fb_rec)
                            fb_ok_all = False
                    if not fb_ok_all:
                        errors.append({"step": i+1, "action": action, "error": f"原步骤失败且 if_error_then 降级未完全成功: {err_msg}"})

                elif cmd.get("stop_on_error", False):
                    break


    video_path = ""
    if proxy and proxy.record_video_dir and proxy._page and proxy._page.video:
        try:
            video_path = await asyncio.wait_for(proxy._page.video.path(), timeout=0.01)
        except Exception:
            pass

    ok_count = sum(1 for s in steps if s["ok"])
    summary = f"执行了 {len(steps)} 步，{ok_count} 步成功" + (f"，{len(errors)} 步失败。" if errors else "。")
    report_path = ""
    if generate_report:
        report_path = write_run_report(session_id, steps, screenshots, errors, summary, out_dir, video_path)
    result = {
        "ok": not errors,
        "session_id": session_id,
        "executed_at": datetime.now().isoformat(),
        "steps": steps,
        "screenshots": screenshots,
        "errors": errors,
        "summary": summary,
    }
    if generate_report:
        result["report_file"] = report_path
    if compact:
        result["steps"] = [
            {"step": s["step"], "action": s["action"], "ok": s["ok"], "duration_ms": s.get("duration_ms")}
            for s in steps
        ]
        result["screenshots"] = []
    return result


async def execute_check(check_type: str, out_dir: str, session_id: str = "unknown") -> dict:
    from .run_check import (
        check_full_screenshot, check_panels, check_dom,
        check_perf, check_a11y, check_interact,
    )
    proxy = _state.proxy
    screenshots, data = [], {}
    valid = {"full", "panels", "dom", "perf", "a11y", "interact", "all"}
    if check_type not in valid:
        return {"ok": False, "error": f"未知类型: {check_type}，可用: {sorted(valid)}"}

    if check_type in ("full", "all"):
        p = await check_full_screenshot(proxy, out_dir)
        screenshots.append(p); data["full_screenshot"] = p
    if check_type in ("panels", "all"):
        paths = await check_panels(proxy, out_dir)
        screenshots.extend(paths.values()); data["panels"] = paths
    if check_type in ("dom", "all"):
        data["dom"] = await check_dom(proxy)
    if check_type in ("perf", "all"):
        data["perf"] = await check_perf(proxy)
    if check_type in ("a11y", "all"):
        data["a11y"] = await check_a11y(proxy)
    if check_type in ("interact", "all"):
        data["interact"] = await check_interact(proxy)

    summary = f"检验 [{check_type}] 完成，截图 {len(screenshots)} 张。"
    report_path = write_check_report(session_id, check_type, screenshots, data, out_dir)
    return {
        "ok": True, "session_id": session_id, "check_type": check_type,
        "executed_at": datetime.now().isoformat(),
        "screenshots": screenshots, "data": data,
        "summary": summary, "report_file": report_path,
    }


# ─────────────────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────────────────
MAX_REPORT_FILES = 200


def _cleanup_reports(out_dir: str, max_files: int = MAX_REPORT_FILES):
    """O4: 清理旧报告文件，保留最新的 max_files 个。"""
    try:
        files = sorted(
            [f for f in os.listdir(out_dir) if f.endswith('.md')],
            key=lambda f: os.path.getmtime(os.path.join(out_dir, f))
        )
        if len(files) > max_files:
            for f in files[:len(files) - max_files]:
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
    except Exception:
        pass


def write_run_report(session_id, steps, screenshots, errors, summary, out_dir, video_path="") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"report_{session_id}_{ts}.md")
    lines = [
        "# 浏览器代理执行报告", "",
        f"- **会话**: `{session_id}`",
        f"- **时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **摘要**: {summary}", "",
        "## 步骤", "",
        "| # | 操作 | 状态 | 耗时(ms) | 备注 |",
        "|---|---|---|---|---|",
    ]
    for s in steps:
        status = "✅" if s["ok"] else "❌"
        note = s.get("comment", "")
        if not s["ok"]:
            note += (" — " if note else "") + s.get("error", "")
        elif s.get("healed_selector"):
            note = f"🤖 [AI已愈] {s.get('healed_selector')} (原: {note})"
        lines.append(f"| {s['step']} | `{s['action']}` | {status} | {s.get('duration_ms','-')} | {note} |")

    if video_path and os.path.exists(video_path):
        lines += ["", "## 录屏记录", ""]
        lines.append(f'<video controls src="file://{os.path.abspath(video_path).replace(os.path.sep, "/")}" width="800"></video>')

    if screenshots:
        lines += ["", "## 截图", ""]
        import base64
        for p in screenshots:
            try:
                with open(p, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                lines.append(f"![截图]({p})")
                lines.append(f'<img src="data:image/png;base64,{b64}" width="800" />\n')
            except Exception:
                lines.append(f"- `{p}`")
    if errors:
        lines += ["", "## 错误", ""]
        lines += [f"- **步骤 {e['step']}** `{e['action']}`: {e['error']}" for e in errors]
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    _log(f"报告: {path}")
    _cleanup_reports(out_dir)
    return path


def write_check_report(session_id, check_type, screenshots, data, out_dir) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"check_{check_type}_{session_id}_{ts}.md")
    lines = [f"# 检验报告 [{check_type}]", "", f"- **会话**: `{session_id}`",
             f"- **时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    if screenshots:
        lines += ["## 截图", ""] + [f"- `{p}`" for p in screenshots] + [""]
    if "dom" in data:
        lines += ["## DOM", "", "| 元素 | 数量 |", "|---|---|"]
        lines += [f"| {i['label']} | {i['count']} |" for i in data["dom"].get("dom_report", [])]
        lines.append("")
    if "perf" in data:
        lines += ["## 性能", "", "| 指标 | 值 |", "|---|---|"]
        for k, v in data["perf"].get("perf_report", {}).items():
            unit = "ms" if "loaded" in k.lower() or "complete" in k.lower() else ""
            lines.append(f"| {k} | {v}{unit} |")
        lines.append("")
    if "a11y" in data:
        cnt = data["a11y"].get("a11y_report", {}).get("issueCount", 0)
        lines += [f"## 可访问性", "", f"发现 **{cnt}** 个问题。", ""]
    if "interact" in data:
        ir = data["interact"]
        lines += [f"## 交互", "", f"通过 **{ir.get('passed',0)}/{ir.get('total',0)}**。", ""]
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    _log(f"检验报告: {path}")
    _cleanup_reports(out_dir)
    return path


# ─────────────────────────────────────────────────────────
# aiohttp 路由处理器
# ─────────────────────────────────────────────────────────
async def _broadcast(event: str, data: dict):
    if not _state.stream_clients: return
    disconnected = set()
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    for client in list(_state.stream_clients):
        try:
            await client.write(msg.encode("utf-8"))
        except Exception:
            disconnected.add(client)
    for c in disconnected:
        _state.stream_clients.discard(c)

async def handle_stream(request: web.Request) -> web.Response:
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })
    await response.prepare(request)
    _state.stream_clients.add(response)
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        _state.stream_clients.discard(response)
    return response

async def handle_macro_save(request: web.Request) -> web.Response:
    body = await _read_body(request)
    name = body.get("name")
    commands = body.get("commands", [])
    if not name or not commands:
        return _json({"ok": False, "error": "需要 name 和 commands 字段"}, 400)
    _state.macros[name] = commands
    return _json({"ok": True, "name": name, "steps_count": len(commands)})

async def handle_macro_list(request: web.Request) -> web.Response:
    return _json({"ok": True, "macros": {k: len(v) for k, v in _state.macros.items()}})

async def handle_macro_run(request: web.Request) -> web.Response:
    body = await _read_body(request)
    name = body.get("name")
    if name not in _state.macros:
        return _json({"ok": False, "error": f"宏 '{name}' 不存在"}, 404)
    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""),
                                   context_mode=body.get("context") or "isolated")
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    async with _state.session_batch(sid) as ok:
        if not ok:
            return _json({"ok": False, "error": f"排队超时：等待会话 '{sid}' 的前一个命令批（{QUEUE_TIMEOUT_SEC}s）"})
        report = await execute_commands(_state.macros[name], report_dir(), session_id=sid)
    return _json(report)

async def handle_status(request: web.Request) -> web.Response:
    browser_active = _state.proxy is not None and _state.proxy.is_browser_open()
    page_url = _state.proxy.current_url if browser_active else None
    try:
        sites = site_patterns.list_patterns()
    except Exception:
        sites = {"patterns": []}
    resp = {
        "service": _SERVICE_NAME,
        "version": _VERSION,
        "status": "running",
        # 服务实例身份：客户端/维护操作据此确认连的是同一实例；revision 为
        # 核心源码指纹——升级代码后旧服务仍在跑可被识别（升级需重启生效）
        "instance_id": _INSTANCE_ID,
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "revision": _RUNNING_REVISION,
        "maintenance": _state.maintenance,
        "browser_active": browser_active,
        "browser_mode": _state.config.get("mode", "managed"),
        "session_id": _state.session_id,
        "current_url": page_url,
        "base_url": _state.config["base_url"],
        "headless": _state.config["headless"],
        "port": _state.config["port"],
        "sessions": _state.sessions_info(),
        "active_batches": _state._inflight,
        "queue_timeout_sec": QUEUE_TIMEOUT_SEC,
        "idle_shutdown_sec": _state._effective_idle_sec(),
        "tab_idle_sec": TAB_IDLE_SEC,
        # 前置检查输出站点经验列表：确定目标网站后先读对应先验（SKILL.md 契约）
        "site_patterns": [p["domain"] for p in sites.get("patterns", [])],
        "user_data_dir": user_data.data_dir(),
    }
    if browser_active and _state.proxy is not None:
        attach_info = getattr(_state.proxy, "_attach_info", None)
        if attach_info:
            resp["attach"] = attach_info
    return _json(resp)


async def handle_session_start(request: web.Request) -> web.Response:
    """启动/获取会话 — 单 Chromium 多会话模型。

    浏览器未运行则拉起（实例级配置生效）；已在运行则在其上为本会话开
    context。context=isolated（默认，cookie/storage 互不干扰）或
    context=user（共享 default context；attach 模式即用户真实 profile，
    登录态免费，关闭时只关自建标签页）。多任务命令批按会话并行（同会话内串行保序）。
    """
    body = await _read_body(request)
    sid = str(body.get("session_id") or DEFAULT_SESSION)
    for k in ("base_url", "headless", "slow_mo", "viewport_w", "viewport_h"):
        if k in body:
            _state.config[k] = body[k]
    context_mode = body.get("context") or "isolated"
    if context_mode not in ("isolated", "user"):
        return _json({"ok": False, "error": "context 必须是 'isolated' 或 'user'"})
    try:
        sess, created, warning = await _state.ensure_session(
            sid, str(body.get("owner") or ""), context_mode=context_mode)
        if sess is None:
            return _json({"ok": False, "error": warning or "会话创建失败"})
        _state.switch_to_session(sid)
        resp = {
            "ok": True,
            "session_id": sid,
            "created": created,
            "context_mode": sess.context_mode,
            "base_url": _state.config["base_url"],
            "headless": _state.config["headless"],
            "active_sessions": len(_state.sessions),
        }
        if warning:
            resp["warning"] = warning
        return _json(resp)
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


# 控制操作（reset/stop/refresh）等待会话锁的短时限：会话忙（有命令批在跑）时
# 拒绝而不是干等，防止控制请求被长批卡死
CONTROL_LOCK_TIMEOUT_SEC = 10


async def handle_session_reset(request: web.Request) -> web.Response:
    """重置会话页面（当前会话 context 内新开 page，cookie/storage 一并清空）。"""
    body = await _read_body(request)
    sid = str(body.get("session_id") or DEFAULT_SESSION)
    async with _state.session_batch(sid, control=True) as ok:
        if not ok:
            return _json({"ok": False,
                          "error": f"会话 '{sid}' 忙（有命令批在执行），稍后重试"})
        try:
            err = await _state.reset_session_page(sid)
            if err:
                return _json({"ok": False, "error": err})
            return _json({"ok": True, "session_id": sid})
        except Exception as e:
            return _json({"ok": False, "error": str(e)}, 500)


async def handle_session_stop(request: web.Request) -> web.Response:
    """关闭单个会话（只关自己的 context；最后一个会话关闭时顺带关浏览器，HTTP 服务保留）。"""
    body = await _read_body(request)
    sid = str(body.get("session_id") or DEFAULT_SESSION)
    async with _state.session_batch(sid, control=True) as ok:
        if not ok:
            return _json({"ok": False,
                          "error": f"会话 '{sid}' 忙（有命令批在执行），稍后重试"})
        try:
            res = await _state.close_session(sid)
            res["message"] = "会话已关闭" + ("，浏览器随之关闭（最后一个会话）" if res.get("browser_shutdown") else "，浏览器仍在运行（其他会话活跃）")
            return _json(res)
        except Exception as e:
            return _json({"ok": False, "error": str(e)}, 500)


async def handle_session_refresh(request: web.Request) -> web.Response:
    """O5: 清除当前会话的 cookies + localStorage/sessionStorage，保留浏览器实例。"""
    body = await _read_body(request)
    sid = _sid_from_body(body)
    try:
        proxy = _state.proxy
        if not proxy or sid not in _state.sessions or not _state.switch_to_session(sid):
            return _json({"ok": False, "error": "无活跃页面"}, 400)
        refusal = _state._shared_context_refusal(sid)
        if refusal:
            return _json({
                "ok": False,
                "error": refusal,
                "ai_hint": "需要干净环境时改用隔离会话（session/start 传 context=isolated）",
            })
        async with _state.session_batch(sid, control=True) as ok:
            if not ok:
                return _json({"ok": False,
                              "error": f"会话 '{sid}' 忙（有命令批在执行），稍后重试"})
            await proxy._clear_live_storage()
            _state.sessions[sid].touch()
            _log(f"会话 {sid} 已刷新（cookies + storage 已清除）")
            return _json({"ok": True, "session_id": sid})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


# 维护态统一拒绝：关停排水期不收变更类请求；开放路由白名单与
# AGENTS.md / command-reference 契约一致（/status /sites /macro/list /attach/*）。
# 策略在此编码一次——新端点默认受保护，无需逐处粘贴守卫
_MAINTENANCE_OPEN = {"/", "/status", "/stream", "/sites", "/macro/list",
                     "/attach/discover", "/attach/select"}


@web.middleware
async def _maintenance_middleware(request, handler):
    if _state.maintenance and request.path not in _MAINTENANCE_OPEN:
        return _json({"ok": False, "maintenance": True,
                      "error": "服务正在关停（排水排除在途命令批），请稍后重试"})
    return await handler(request)


async def handle_run(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if not body:
        return _json({"success": False, "ok": False, "error": "缺少 commands 字段或请求体为空"}, 400)

    commands = body.get("commands", body.get("steps", []))
    if not commands:
        return _json({"success": False, "ok": False, "error": "缺少 commands 字段"}, 400)
    if isinstance(commands, dict):
        commands = [commands]

    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""),
                                   context_mode=body.get("context") or "isolated")
    except Exception as e:
        return _json({"success": False, "ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    async with _state.session_batch(sid) as ok:
        if not ok:
            return _json({
                "success": False, "ok": False,
                "error": f"排队超时：等待会话 '{sid}' 的前一个命令批完成超过 {QUEUE_TIMEOUT_SEC}s"
                         f"（同一会话串行执行；可用 BROWSER_AGENT_QUEUE_TIMEOUT 调整）",
            })
        # 锁内重钉已由 session_batch 完成（前一批可能换了页）；
        # 页面全失效时报无可用页面
        if sid in _state.sessions and _state.sessions[sid].active_tab not in _state.proxy._pages:
            return _json({"success": False, "ok": False, "error": f"会话 '{sid}' 无可用页面"}, 400)
        # 若页面空白且无 navigate 指令则自动导航
        try:
            has_nav = any(c.get("action", c.get("type", "")) == "navigate" for c in commands)
            if not has_nav and _state.proxy.current_url in ("about:blank", ""):
                commands = [{"action": "navigate", "url": _state.config["base_url"]}] + commands

            report = await execute_commands(
                commands, report_dir(),
                compact=body.get("compact", False),
                generate_report=body.get("report", True),
                session_id=sid,
            )
            return _json(report)
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc().split('\n')[-2]
            return _json({"success": False, "ok": False, "error": f"执行崩溃: {err_msg}"}, 500)


async def handle_check(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if not body:
        body = {}
    check_type = body.get("type", "full")

    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""),
                                   context_mode=body.get("context") or "isolated")
    except Exception as e:
        return _json({"success": False, "ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    async with _state.session_batch(sid) as ok:
        if not ok:
            return _json({"success": False, "ok": False,
                          "error": f"排队超时：等待会话 '{sid}' 的前一个命令批（{QUEUE_TIMEOUT_SEC}s）"})
        if sid in _state.sessions and _state.sessions[sid].active_tab not in _state.proxy._pages:
            return _json({"success": False, "ok": False, "error": f"会话 '{sid}' 无可用页面"}, 400)
        try:
            if _state.proxy.current_url in ("about:blank", ""):
                await _state.proxy.navigate(_state.config["base_url"])
            report = await execute_check(check_type, report_dir(), session_id=sid)
            return _json(report)
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc().split('\n')[-2]
            return _json({"success": False, "ok": False, "error": f"检查崩溃: {err_msg}"}, 500)


async def handle_shutdown(request: web.Request) -> web.Response:
    # 维护态先行：排水期间拒绝新命令批/新会话（/status 仍可用，供观察退出）
    _state.maintenance = True
    resp = _json({"ok": True, "message": "正在关闭...", "instance_id": _INSTANCE_ID})
    asyncio.get_event_loop().call_later(0.3, lambda: asyncio.ensure_future(_do_shutdown()))
    return resp


async def _do_shutdown():
    """优雅关停：排水（等在途命令批收尾）→ 浏览器落盘/关闭 → SIGTERM 退出。

    不能在 handler task 里调 app.shutdown()/app.cleanup()（自等死锁，进程不退——
    旧版即此 bug，一直靠 cleanup.sh 的 pkill 兜底）；也不能直接 loop.stop()——
    新版 aiohttp 的 run_app 用 run_until_complete 包着 main_task，强停会抛
    RuntimeError。SIGTERM 是 run_app 注册的官方优雅退出通道。
    全程异常兜底：任何一步失败也必须走到 SIGTERM——否则进程僵死且
    maintenance 永久为真，所有请求被拒。
    """
    try:
        # 排水：在途命令批最多等 10s；超时则如实记录并继续（shutdown 是显式意图）
        deadline = time.time() + 10
        while _state._inflight > 0 and time.time() < deadline:
            await asyncio.sleep(0.2)
        if _state._inflight > 0:
            _log(f"关停排水超时：仍有 {_state._inflight} 个命令批在执行，继续关停")
        try:
            await _state.shutdown_browser()
        except Exception as e:
            _log(f"关停时浏览器落盘/关闭异常（继续退出）: {e}")
        _remove_pid()
    finally:
        # Windows 无 add_signal_handler；os.kill(pid, SIGTERM) 在 Windows 即
        # TerminateProcess——浏览器落盘/关闭已在上方尽力完成，此处终止进程本体
        os.kill(os.getpid(), signal.SIGTERM)


def _json(data: dict, status: int = 200) -> web.Response:
    # 强制所有业务报文走 200 避免 HTTP 层代理由于 500 甚至超时导致的连接中断或网关错误 (502)
    # 通过 data.get("success", True) 来让客户端识别逻辑是否正常
    if "ok" not in data:
        data["ok"] = data.get("success", True)
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, indent=2),
        status=200,
        content_type="application/json",
    )


async def _read_body(request: web.Request) -> dict:
    if not request.has_body:
        return {}
    try:
        return await request.json()
    except Exception as e:
        _log(f"解析请求体 JSON 失败: {e}")
        return {}


def _sid_from_body(body: dict) -> str:
    """请求归属的会话；不带 session_id 的旧客户端归入 default 会话（向后兼容）。"""
    sid = str(body.get("session_id") or "").strip()
    return sid or DEFAULT_SESSION


# ─────────────────────────────────────────────────────────
# 认证会话端点（profile 管理）
# ─────────────────────────────────────────────────────────
async def handle_auth_profiles(request: web.Request) -> web.Response:
    """GET /auth/profiles — 列出所有 profile 元数据（纯文件读，无需浏览器与锁）。"""
    try:
        await _state.ensure_proxy()
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"})
    res = await _state.proxy.list_profiles()
    return _json(res)


async def handle_auth_ensure(request: web.Request) -> web.Response:
    """POST /auth/ensure {profile, login_commands, into} — 确保已登录，必要时执行登录命令并落盘。"""
    body = await _read_body(request)
    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""),
                                   context_mode=body.get("context") or "isolated")
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"})
    async with _state.session_batch(sid) as ok:
        if not ok:
            return _json({"ok": False, "error": f"排队超时：等待会话 '{sid}' 的前一个命令批（{QUEUE_TIMEOUT_SEC}s）"})
        if sid in _state.sessions and _state.sessions[sid].active_tab not in _state.proxy._pages:
            return _json({"ok": False, "error": f"会话 '{sid}' 无可用页面"})
        res = await _state.proxy.ensure_auth(
            body.get("profile"), body.get("login_commands"), body.get("into", "new")
        )
    return _json(res)


# ─────────────────────────────────────────────────────────
# 站点经验端点（纯文件操作，无需浏览器）
# ─────────────────────────────────────────────────────────
async def handle_sites_list(request: web.Request) -> web.Response:
    """GET /sites — 已积累的站点经验列表。"""
    return _json(site_patterns.list_patterns())


async def handle_site_get(request: web.Request) -> web.Response:
    """GET /site?domain=（或 ?url=）— 匹配并返回站点经验全文。"""
    target = request.query.get("domain") or request.query.get("url") or ""
    if not target:
        return _json({"ok": False, "error": "需要 domain 或 url 查询参数"})
    return _json(site_patterns.match_site(target))


async def handle_site_write(request: web.Request) -> web.Response:
    """POST /site {domain, aliases?, platform?, patterns?, pitfalls?} — 追加站点经验（带日期）。"""
    body = await _read_body(request)
    domain = body.get("domain") or body.get("url")
    if not domain:
        return _json({"ok": False, "error": "需要 domain 字段"})
    try:
        res = site_patterns.write_pattern(
            domain,
            aliases=body.get("aliases"),
            platform=body.get("platform"),
            patterns=body.get("patterns"),
            pitfalls=body.get("pitfalls"),
        )
    except Exception as e:
        # 恒 200 契约：任何异常转为结构化失败（如 aliases 传了不可迭代对象）
        return _json({"ok": False, "error": f"写入失败: {str(e).splitlines()[0]}"})
    if res.get("ok"):
        _log(f"站点经验已更新: {res.get('domain')}（+{res.get('appended_entries')} 条）")
    return _json(res)


# ─────────────────────────────────────────────────────────
# 附着偏好端点（attach 模式）
# ─────────────────────────────────────────────────────────
async def handle_attach_select(request: web.Request) -> web.Response:
    """POST /attach/select {attach_dir | cdp_endpoint | forget} — 保存/清除附着偏好。

    偏好落 ~/.browser-agent/attach.env；服务需重启（shutdown 后自动拉起）才按新偏好连接。
    """
    from . import browser_attach
    body = await _read_body(request)
    if body.get("forget"):
        browser_attach.save_preference(attach_dir="", cdp_endpoint="")
        return _json({"ok": True, "message": "附着偏好已清除"})
    attach_dir = body.get("attach_dir") or ""
    cdp_endpoint = body.get("cdp_endpoint") or ""
    if not attach_dir and not cdp_endpoint:
        return _json({
            "ok": False,
            "error": "需要 attach_dir 或 cdp_endpoint（或 forget: true 清除偏好）",
            "candidates": browser_attach.discover_browsers(),
        })
    if attach_dir:
        try:
            attach_dir = browser_attach.expand_path(attach_dir)
        except ValueError as e:
            return _json({"ok": False, "error": f"attach_dir 路径无效: {e}"})
        if not browser_attach.is_browser_data_dir(attach_dir):
            return _json(browser_attach.error_response(
                f"目录不是浏览器数据目录（缺少 Local State）: {attach_dir}",
                candidates=browser_attach.discover_browsers()))
    res = browser_attach.save_preference(attach_dir=attach_dir, cdp_endpoint=cdp_endpoint)
    res["message"] = "偏好已保存；重启服务后按新偏好连接（POST /shutdown 后下次请求自动拉起）"
    return _json(res)


async def handle_attach_discover(request: web.Request) -> web.Response:
    """GET /attach/discover — 列出本机可附着的浏览器（含远程调试开关状态）。"""
    from . import browser_attach
    # 根目录 readdir + 逐项 stat 是文件 IO：下线程，不占事件循环
    return _json(await asyncio.to_thread(browser_attach.discover_payload))


# ─────────────────────────────────────────────────────────
# PID 管理
# ─────────────────────────────────────────────────────────
def _write_pid(port: int):
    with open(user_data.pid_file(), "w") as f:
        json.dump({"pid": os.getpid(), "port": port, "service": _SERVICE_NAME}, f)


def _remove_pid():
    try:
        os.remove(user_data.pid_file())
    except FileNotFoundError:
        pass


# ─────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    port = 7788
    base_url = "http://localhost:3000"
    headless = False
    slow_mo = 0
    viewport_w = 1440
    viewport_h = 900
    http_proxy = None
    record_video_dir = None
    record_har_path = None
    no_storage = False
    auto_save_state = False
    ignore_https_errors = False
    mode = "managed"
    attach_dir = ""
    cdp_endpoint = ""
    stealth_enabled = True

    # 配置查找顺序：BROWSER_AGENT_CONFIG 指定路径 → 仓库根 browser_agent.yaml（包目录上一级）
    config_file = os.getenv("BROWSER_AGENT_CONFIG") or os.path.join(_HERE, "..", "browser_agent.yaml")
    if os.path.exists(config_file):
        try:
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
            # 支持嵌套格式（browser.server.proxy）和扁平格式
            browser_cfg = y.get("browser", y)
            server_cfg = y.get("server", y)
            proxy_cfg = y.get("proxy", y)
            port = server_cfg.get("port", port)
            base_url = browser_cfg.get("base_url", y.get("base_url", base_url))
            headless = browser_cfg.get("headless", y.get("headless", headless))
            slow_mo = browser_cfg.get("slow_mo", y.get("slow_mo", slow_mo))
            viewport = browser_cfg.get("viewport", {})
            viewport_w = viewport.get("width", y.get("viewport_w", viewport_w))
            viewport_h = viewport.get("height", y.get("viewport_h", viewport_h))
            http_proxy = proxy_cfg.get("server", y.get("http_proxy", http_proxy))
            record_video_dir = y.get("record_video_dir", record_video_dir)
            record_har_path = y.get("record_har_path", record_har_path)
            ignore_https_errors = bool(browser_cfg.get("ignore_https_errors", y.get("ignore_https_errors", ignore_https_errors)))
            mode = str(browser_cfg.get("mode", y.get("mode", mode)) or "managed")
            attach_dir = str(browser_cfg.get("attach_dir", y.get("attach_dir", attach_dir)) or "")
            cdp_endpoint = str(browser_cfg.get("cdp_endpoint", y.get("cdp_endpoint", cdp_endpoint)) or "")
            stealth_cfg = y.get("stealth", {})
            stealth_enabled = bool(stealth_cfg.get("enabled", True))
            # auto_load_state 配置
            storage_cfg = y.get("storage", {})
            if not storage_cfg.get("auto_load_state", True):
                no_storage = True
            auto_save_state = bool(storage_cfg.get("auto_save_state", auto_save_state))
        except ImportError:
            print("[警告] 找到 yaml 配置但未安装 PyYAML，请执行 pip install pyyaml", file=sys.stderr)
        except Exception as e:
            print(f"[警告] 读取 yaml 配置失败: {e}", file=sys.stderr)

    cli_attach = False
    for a in args:
        if a == "--headless":
            headless = True
            slow_mo = 0
        elif a == "--no-storage":
            no_storage = True
        elif a == "--attach":
            mode = "attach"
            cli_attach = True
        elif a == "--no-stealth":
            stealth_enabled = False
        elif a.startswith("--port="):
            port = int(a.split("=")[1])
        elif a.startswith("--url="):
            base_url = a.split("=", 1)[1]
        elif a.startswith("--slow-mo="):
            slow_mo = int(a.split("=")[1])
        elif a.startswith("--http-proxy="):
            http_proxy = a.split("=", 1)[1]
        elif a.startswith("--attach-dir="):
            attach_dir = a.split("=", 1)[1]
            mode = "attach"
            cli_attach = True
        elif a.startswith("--cdp-endpoint="):
            cdp_endpoint = a.split("=", 1)[1]
            mode = "attach"
            cli_attach = True
        elif a.startswith("http://") or a.startswith("https://"):
            base_url = a

    # 优先级 CLI > 环境变量 > 配置文件：附着类参数在解析分支里已置 cli_attach，
    # 环境变量只在无显式附着参数时改写模式（否则会静默吞掉 --attach）
    if not cli_attach:
        mode = os.getenv("BROWSER_AGENT_MODE") or mode

    _state.config.update({
        "port": port, "base_url": base_url, "headless": headless, "slow_mo": slow_mo,
        "viewport_w": viewport_w, "viewport_h": viewport_h,
        "http_proxy": http_proxy, "record_video_dir": record_video_dir, "record_har_path": record_har_path,
        "no_storage": no_storage, "auto_save_state": auto_save_state, "ignore_https_errors": ignore_https_errors,
        "mode": mode, "attach_dir": attach_dir, "cdp_endpoint": cdp_endpoint,
        "stealth_enabled": stealth_enabled,
    })

    app = web.Application(middlewares=[_maintenance_middleware])
    app.router.add_get("/status", handle_status)
    app.router.add_get("/", handle_status)
    app.router.add_get("/stream", handle_stream)
    app.router.add_post("/session/start", handle_session_start)
    app.router.add_post("/session/reset", handle_session_reset)
    app.router.add_post("/session/refresh", handle_session_refresh)
    app.router.add_post("/session/stop", handle_session_stop)
    app.router.add_post("/run", handle_run)
    app.router.add_post("/check", handle_check)
    app.router.add_get("/auth/profiles", handle_auth_profiles)
    app.router.add_post("/auth/ensure", handle_auth_ensure)
    app.router.add_get("/sites", handle_sites_list)
    app.router.add_get("/site", handle_site_get)
    app.router.add_post("/site", handle_site_write)
    app.router.add_get("/attach/discover", handle_attach_discover)
    app.router.add_post("/attach/select", handle_attach_select)
    app.router.add_post("/macro/save", handle_macro_save)
    app.router.add_post("/macro/run", handle_macro_run)
    app.router.add_get("/macro/list", handle_macro_list)
    app.router.add_post("/shutdown", handle_shutdown)

    async def on_startup(app):
        _write_pid(port)
        if mode == "attach":
            # attach 惰性连接：服务就绪即返回，首个带会话的请求才连用户浏览器。
            # 不在启动时自动导航（attach 模式不在用户浏览器里凭空开标签页）
            _log("attach 模式：等待首个请求时连接用户浏览器（偏好由 ~/.browser-agent/attach.env / 环境变量解析）")
        else:
            # 浏览器拉起失败（资源预检等）不阻断 HTTP 服务：服务常驻，
            # 资源允许后首个带会话的请求会自动重新拉起浏览器
            try:
                await _state.ensure_proxy()
                # 启动时自动导航到 base_url，避免白屏
                if base_url:
                    try:
                        await _state.proxy.navigate(base_url)
                    except Exception as e:
                        _log(f"初始导航失败: {e}")
            except Exception as e:
                _log(f"浏览器暂不启动：{e}")
                _log("HTTP 服务正常监听，资源允许后首个请求会自动拉起浏览器")

        _log(f"服务已启动 → http://127.0.0.1:{port}")
        _log(f"目标地址: {base_url} | 模式: "
             f"{'attach（附着日常浏览器）' if mode == 'attach' else ('无头' if headless else '有头（可见）')}")

        async def _heartbeat():
            while True:
                await asyncio.sleep(60)
                # 页级空闲回收兜底（零流量场景；节流与错误处理复用唯一入口）
                await _sweep_idle_tabs(None)
                # 空闲回收：所有会话超时无活动 → 关浏览器释放内存（HTTP 服务保留）
                try:
                    await _state.idle_recycle()
                except Exception as e:
                    _log(f"空闲回收异常: {e}")
                if _state.proxy and _state.proxy.is_browser_open() and _state.proxy._page:
                    try:
                        await _state.proxy._page.evaluate("1+1")
                    except Exception:
                        _log("⚠️ 心跳检测失败，页面可能已失效")
        asyncio.create_task(_heartbeat())

    async def on_cleanup(app):
        await _state.shutdown_browser()
        _remove_pid()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(app, host="127.0.0.1", port=port, print=lambda *a: None)

if __name__ == '__main__':
    main()
