#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器代理服务端 (Browser Agent Server)
基于 aiohttp 的纯异步 HTTP 微服务，持久运行 Playwright 浏览器实例。

端点：
  GET  /status          健康检查
  POST /session/start   启动/重配会话
  POST /session/reset   重置页面（保留浏览器）
  POST /run             执行命令批次
  POST /check           预设检验套件
  POST /shutdown        优雅关闭

启动：
  python -m browser_agent.server [--port=7788] [--url=http://...] [--headless] [--slow-mo=N]
  # pip 安装后等价：browser-agent-server [--port=7788] ...
"""
import asyncio
import sys
import json
import os
import time
import uuid
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
from . import __version__ as _VERSION
_SERVICE_NAME = "browser_agent"
script_dir = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(script_dir, "browser_agent.pid")

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

DEFAULT_SESSION = "default"


class Session:
    """一个隔离会话 = 浏览器内独立 context + page 集 + 自己的 active tab。

    多任务共享同一 Chromium 实例：context 隔离 cookie/storage，命令批在
    _run_lock 上排队执行，执行前切换到该会话的 active tab——排到时页面状态完好。
    """

    def __init__(self, sid: str, owner: str = "", tab: str = "default"):
        self.sid = sid
        self.owner = owner
        self.active_tab = tab      # proxy._pages 中的键（含 sid 前缀）
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
        }
        self.macros = {}
        self.stream_clients = set()
        self._broker_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()  # O2: 并发请求安全锁（命令批排队）

    def _tab_key(self, sid: str, tab: str) -> str:
        return tab if sid == DEFAULT_SESSION else f"{sid}::{tab}"

    def _session_context(self, sid: str):
        """会话对应的 browser context（default 会话 = 浏览器自带 default context）。"""
        if not self.proxy:
            return None
        return self.proxy._contexts.get("default" if sid == DEFAULT_SESSION else sid)

    async def ensure_proxy(self, cfg: Optional[dict] = None):
        """启动浏览器（幂等）。锁保护整个创建流程，防止并发启动。"""
        async with self._broker_lock:
            if self.proxy is not None and self.proxy.is_browser_open():
                return
            # 资源预检兜底（client 侧已查，此处防直接 HTTP 调 server 绕过）：
            # 低配机器余量不足时拒绝拉起 Chromium，防整机被压垮
            from .utils import check_system_resources

            ok, detail = check_system_resources()
            if not ok:
                raise RuntimeError(f"资源预检未通过：{detail}")
            c = {**self.config, **(cfg or {})}
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
            )
            await self.proxy.start()
            self.sessions.clear()
            # 首个（通常是 default）会话直接落在浏览器自带的 default context 上
            self._register_session(DEFAULT_SESSION)
            _log(f"浏览器已启动，初始会话: {self.session_id}")

    def _register_session(self, sid: str, owner: str = "") -> Session:
        """登记会话（active tab 指向其 context 的首个 page）。"""
        ctx = self._session_context(sid)
        tab = next((k for k, p in self.proxy._pages.items() if p.context is ctx), "default")
        sess = Session(sid, owner, tab=tab)
        self.sessions[sid] = sess
        if not self.session_id:
            self.session_id = sid
        return sess

    async def ensure_session(self, sid: str, owner: str = "") -> tuple:
        """确保会话存在（不存在则在现有浏览器上开独立 context）。

        返回 (session, created, warning)。浏览器级配置（headless/viewport 等）只在
        浏览器冷启动时生效——浏览器已在运行时新会话沿用现有实例，返回 warning 说明。
        """
        warnings = None
        if self.proxy is not None and self.proxy.is_browser_open() and sid not in self.sessions:
            warnings = "浏览器已在运行（被其他会话共享）：本次传入的实例级配置（headless/viewport/代理等）未生效，会话沿用现有实例"
        await self.ensure_proxy()
        if sid in self.sessions:
            sess = self.sessions[sid]
            sess.touch()
            return sess, False, None

        # 新会话：在共享浏览器上开隔离 context（独立 cookie/storage）
        if sid == DEFAULT_SESSION:
            # default 的 context 由浏览器启动时创建，这里只补登记（浏览器刚重启等场景）
            sess = self._register_session(sid, owner)
            return sess, True, warnings

        res = await self.proxy.new_isolated_session(sid)
        if not res.get("ok", True):
            return None, False, res.get("error", "创建会话上下文失败")
        sess = self._register_session(sid, owner)
        return sess, True, warnings

    def switch_to_session(self, sid: str) -> bool:
        """把 proxy 的 active tab 切到该会话的 tab（命令执行前调用）。"""
        sess = self.sessions.get(sid)
        if not sess or not self.proxy:
            return False
        pages = self.proxy._pages
        if sess.active_tab in pages:
            self.proxy._active_tab = sess.active_tab
        else:
            # 会话的 page 已被关闭：按 context 归属回落到该会话任一存留 tab
            ctx = self._session_context(sid)
            fallback = next((k for k, p in pages.items() if p.context is ctx), None)
            if fallback is None:
                return False
            sess.active_tab = fallback
            self.proxy._active_tab = fallback
        sess.touch()
        self.session_id = sid
        return True

    async def close_session(self, sid: str) -> dict:
        """关闭单个会话（只关自己的 context，不动其他会话）。

        最后一个会话关闭时顺带关闭整个浏览器（单任务场景行为与旧版一致）。
        """
        sess = self.sessions.pop(sid, None)
        if not self.proxy or not sess:
            return {"ok": True, "closed": sid, "note": "会话不存在或已关闭"}
        ctx = self._session_context(sid)
        if ctx:
            # context.close() 自动关闭其下全部 page；注册表按 context 归属清理
            for key in [k for k, p in list(self.proxy._pages.items()) if p.context is ctx]:
                self.proxy._pages.pop(key, None)
            try:
                await ctx.close()
            except Exception:
                pass
            self.proxy._contexts.pop("default" if sid == DEFAULT_SESSION else sid, None)
        if not self.sessions and self.proxy:
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
        ctx = self._session_context(sid)
        if not ctx:
            return "会话上下文不存在"
        old = self.proxy._pages.get(sess.active_tab)
        from playwright_stealth import Stealth
        page = await ctx.new_page()
        await Stealth().apply_stealth_async(page)
        page.set_default_timeout(self.proxy.default_timeout)
        page.on("crash", lambda p: _log(f"⚠️ 会话 {sid} 页面崩溃！状态已失效"))
        page.on("download", self.proxy._on_download)
        self.proxy._pages[sess.active_tab] = page
        self.proxy._active_tab = sess.active_tab
        if old:
            try:
                await old.close()
            except Exception:
                pass
        sess.touch()
        _log(f"会话 {sid} 页面已重置")
        return ""

    def sessions_info(self) -> list:
        now = time.time()
        return [
            {
                "session_id": s.sid,
                "owner": s.owner,
                "active_tab": s.active_tab,
                "idle_sec": int(now - s.last_active),
                "age_sec": int(now - s.created_at),
            }
            for s in self.sessions.values()
        ]

    async def idle_recycle(self):
        """所有会话空闲超阈值 → 关闭浏览器释放内存（HTTP 服务保留，下次自动重启浏览器）。"""
        if IDLE_SHUTDOWN_SEC <= 0 or not self.sessions:
            return
        now = time.time()
        # 任一会话仍在活跃期即不回收（须所有会话全部超时才关浏览器）
        if any(now - s.last_active < IDLE_SHUTDOWN_SEC for s in self.sessions.values()):
            return
        idle = {s.sid: int(now - s.last_active) for s in self.sessions.values()}
        _log(f"空闲回收：所有会话超时无活动 {idle}，关闭浏览器（{IDLE_SHUTDOWN_SEC}s 阈值）")
        await self.shutdown_browser()

    async def shutdown_browser(self):
        if self.proxy:
            # auto_save：在 stop() 之前保存活跃会话（storage_state 需活跃 context）
            if self.config.get("auto_save_state") and self.proxy._page:
                try:
                    storage_dir = os.path.join(os.path.dirname(__file__), "storage")
                    state_path = os.path.join(storage_dir, "state.json")
                    await self.proxy.save_storage(state_path)
                    ap = getattr(self.proxy, "_active_profile", None)
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
        _log("浏览器已关闭")


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


async def execute_commands(commands: list, out_dir: str, compact: bool = False, generate_report: bool = True) -> dict:
    proxy = _state.proxy
    session_id = _state.session_id or "unknown"
    steps, screenshots, errors = [], [], []

    for i, cmd in enumerate(commands):
        action = cmd.get("action", cmd.get("type", ""))
        comment = cmd.get("comment", "")
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
            
            # 支持业务层面的 False（如断言失败，或者常规 action 的 success: False）
            is_ok = True
            if isinstance(result, dict):
                is_ok = result.get("success", result.get("ok", True))
                if action == "screenshot" and result.get("path"):
                    screenshots.append(result["path"])
            
            if is_ok:
                step_record = {"step": i+1, "action": action, "comment": comment, "ok": True, "data": result, "duration_ms": dur}
                # navigate 后自动附加页面摘要，让 AI 就烟知道当前在哪里
                if action == "navigate" and proxy and proxy._page:
                    try:
                        summary_data = await proxy.get_page_summary()
                        step_record["data"]["page_summary"] = summary_data
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


async def execute_check(check_type: str, out_dir: str) -> dict:
    from .run_check import (
        check_full_screenshot, check_panels, check_dom,
        check_perf, check_a11y, check_interact,
    )
    proxy = _state.proxy
    session_id = _state.session_id or "unknown"
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
        await _state.ensure_session(sid, str(body.get("owner") or ""))
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    _state.switch_to_session(sid)
    report = await execute_commands(_state.macros[name], report_dir())
    return _json(report)

async def handle_status(request: web.Request) -> web.Response:
    browser_active = _state.proxy is not None and _state.proxy.is_browser_open()
    page_url = _state.proxy.current_url if browser_active else None
    return _json({
        "service": _SERVICE_NAME,
        "version": _VERSION,
        "status": "running",
        "browser_active": browser_active,
        "session_id": _state.session_id,
        "current_url": page_url,
        "base_url": _state.config["base_url"],
        "headless": _state.config["headless"],
        "port": _state.config["port"],
        "sessions": _state.sessions_info(),
        "queue_timeout_sec": QUEUE_TIMEOUT_SEC,
        "idle_shutdown_sec": IDLE_SHUTDOWN_SEC,
    })


async def handle_session_start(request: web.Request) -> web.Response:
    """启动/获取会话 — 单 Chromium 多会话隔离模型。

    浏览器未运行则拉起（实例级配置生效）；已在运行则在其上为本会话开独立
    context（cookie/storage 互不干扰）。多任务命令批在 _run_lock 上排队执行。
    """
    body = await _read_body(request)
    sid = str(body.get("session_id") or DEFAULT_SESSION)
    for k in ("base_url", "headless", "slow_mo", "viewport_w", "viewport_h"):
        if k in body:
            _state.config[k] = body[k]
    try:
        sess, created, warning = await _state.ensure_session(sid, str(body.get("owner") or ""))
        if sess is None:
            return _json({"ok": False, "error": warning or "会话创建失败"})
        _state.switch_to_session(sid)
        resp = {
            "ok": True,
            "session_id": sid,
            "created": created,
            "base_url": _state.config["base_url"],
            "headless": _state.config["headless"],
            "active_sessions": len(_state.sessions),
        }
        if warning:
            resp["warning"] = warning
        return _json(resp)
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


async def handle_session_reset(request: web.Request) -> web.Response:
    """重置会话页面（当前会话 context 内新开 page，cookie/storage 一并清空）。"""
    body = await _read_body(request)
    sid = str(body.get("session_id") or DEFAULT_SESSION)
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
        context = proxy._page.context
        await context.clear_cookies()
        await proxy._page.evaluate("""() => {
            localStorage.clear();
            sessionStorage.clear();
        }""")
        proxy._elements_cache = []
        _state.sessions[sid].touch()
        _log(f"会话 {sid} 已刷新（cookies + storage 已清除）")
        return _json({"ok": True, "session_id": sid})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


async def handle_run(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if not body:
        return _json({"success": False, "ok": False, "error": "无效的 JSON 请求体"}, 400)

    commands = body.get("commands", body.get("steps", []))
    if not commands:
        return _json({"success": False, "ok": False, "error": "缺少 commands 字段"}, 400)
    if isinstance(commands, dict):
        commands = [commands]

    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""))
    except Exception as e:
        return _json({"success": False, "ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    if not _state.switch_to_session(sid):
        return _json({"success": False, "ok": False, "error": f"会话 '{sid}' 无可用页面"}, 400)

    acquired = await _acquire_run_lock()
    if not acquired:
        return _json({
            "success": False, "ok": False,
            "error": f"排队超时：等待其他会话的命令批完成超过 {QUEUE_TIMEOUT_SEC}s（当前活跃会话 '{_state.session_id}'；可用 BROWSER_AGENT_QUEUE_TIMEOUT 调整）",
        })
    try:
        # 若页面空白且无 navigate 指令则自动导航
        has_nav = any(c.get("action", c.get("type", "")) == "navigate" for c in commands)
        if not has_nav and _state.proxy.current_url in ("about:blank", ""):
            commands = [{"action": "navigate", "url": _state.config["base_url"]}] + commands

        report = await execute_commands(
            commands, report_dir(),
            compact=body.get("compact", False),
            generate_report=body.get("report", True),
        )
        return _json(report)
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc().split('\n')[-2]
        return _json({"success": False, "ok": False, "error": f"执行崩溃: {err_msg}"}, 500)
    finally:
        _state._run_lock.release()


async def handle_check(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if not body:
        body = {}
    check_type = body.get("type", "full")

    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""))
    except Exception as e:
        return _json({"success": False, "ok": False, "error": f"浏览器启动失败: {e}"}, 500)
    if not _state.switch_to_session(sid):
        return _json({"success": False, "ok": False, "error": f"会话 '{sid}' 无可用页面"}, 400)

    acquired = await _acquire_run_lock()
    if not acquired:
        return _json({"success": False, "ok": False, "error": f"排队超时（{QUEUE_TIMEOUT_SEC}s）"})
    try:
        if _state.proxy.current_url in ("about:blank", ""):
            await _state.proxy.navigate(_state.config["base_url"])
        report = await execute_check(check_type, report_dir())
        return _json(report)
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc().split('\n')[-2]
        return _json({"success": False, "ok": False, "error": f"检查崩溃: {err_msg}"}, 500)
    finally:
        _state._run_lock.release()


async def handle_shutdown(request: web.Request) -> web.Response:
    resp = _json({"ok": True, "message": "正在关闭..."})
    asyncio.get_event_loop().call_later(0.3, lambda: asyncio.ensure_future(_do_shutdown(request.app)))
    return resp


async def _do_shutdown(app):
    await _state.shutdown_browser()
    _remove_pid()
    await app.shutdown()
    await app.cleanup()
    # run_app 的事件循环不因 cleanup 而停，须显式 stop 才真正退出进程
    asyncio.get_event_loop().stop()


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


async def _acquire_run_lock() -> bool:
    """带超时获取命令批执行锁（多任务排队；超时返回 False 防无限堆积）。"""
    if QUEUE_TIMEOUT_SEC <= 0:
        await _state._run_lock.acquire()
        return True
    try:
        await asyncio.wait_for(_state._run_lock.acquire(), timeout=QUEUE_TIMEOUT_SEC)
        return True
    except asyncio.TimeoutError:
        return False


# ─────────────────────────────────────────────────────────
# 认证会话端点（profile 管理）
# ─────────────────────────────────────────────────────────
async def handle_auth_profiles(request: web.Request) -> web.Response:
    """GET /auth/profiles — 列出所有 profile 元数据（不含 storage_state 内容）。"""
    try:
        await _state.ensure_proxy()
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"})
    acquired = await _acquire_run_lock()
    if not acquired:
        return _json({"ok": False, "error": f"排队超时（{QUEUE_TIMEOUT_SEC}s）"})
    try:
        res = await _state.proxy.list_profiles()
    finally:
        _state._run_lock.release()
    return _json(res)


async def handle_auth_ensure(request: web.Request) -> web.Response:
    """POST /auth/ensure {profile, login_commands, into} — 确保已登录，必要时执行登录命令并落盘。"""
    body = await _read_body(request)
    sid = _sid_from_body(body)
    try:
        await _state.ensure_session(sid, str(body.get("owner") or ""))
    except Exception as e:
        return _json({"ok": False, "error": f"浏览器启动失败: {e}"})
    if not _state.switch_to_session(sid):
        return _json({"ok": False, "error": f"会话 '{sid}' 无可用页面"})
    acquired = await _acquire_run_lock()
    if not acquired:
        return _json({"ok": False, "error": f"排队超时（{QUEUE_TIMEOUT_SEC}s）"})
    try:
        res = await _state.proxy.ensure_auth(
            body.get("profile"), body.get("login_commands"), body.get("into", "new")
        )
    finally:
        _state._run_lock.release()
    return _json(res)


# ─────────────────────────────────────────────────────────
# PID 管理
# ─────────────────────────────────────────────────────────
def _write_pid(port: int):
    with open(_PID_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "port": port, "service": _SERVICE_NAME}, f)


def _remove_pid():
    try:
        os.remove(_PID_FILE)
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
            # auto_load_state 配置
            storage_cfg = y.get("storage", {})
            if not storage_cfg.get("auto_load_state", True):
                no_storage = True
            auto_save_state = bool(storage_cfg.get("auto_save_state", auto_save_state))
        except ImportError:
            print("[警告] 找到 yaml 配置但未安装 PyYAML，请执行 pip install pyyaml", file=sys.stderr)
        except Exception as e:
            print(f"[警告] 读取 yaml 配置失败: {e}", file=sys.stderr)

    for a in args:
        if a == "--headless":
            headless = True
            slow_mo = 0
        elif a == "--no-storage":
            no_storage = True
        elif a.startswith("--port="):
            port = int(a.split("=")[1])
        elif a.startswith("--url="):
            base_url = a.split("=", 1)[1]
        elif a.startswith("--slow-mo="):
            slow_mo = int(a.split("=")[1])
        elif a.startswith("--http-proxy="):
            http_proxy = a.split("=", 1)[1]
        elif a.startswith("http://") or a.startswith("https://"):
            base_url = a

    _state.config.update({
        "port": port, "base_url": base_url, "headless": headless, "slow_mo": slow_mo, 
        "viewport_w": viewport_w, "viewport_h": viewport_h,
        "http_proxy": http_proxy, "record_video_dir": record_video_dir, "record_har_path": record_har_path,
        "no_storage": no_storage, "auto_save_state": auto_save_state, "ignore_https_errors": ignore_https_errors,
    })

    app = web.Application()
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
    app.router.add_post("/macro/save", handle_macro_save)
    app.router.add_post("/macro/run", handle_macro_run)
    app.router.add_get("/macro/list", handle_macro_list)
    app.router.add_post("/shutdown", handle_shutdown)

    async def on_startup(app):
        _write_pid(port)
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
        _log(f"目标地址: {base_url} | 模式: {'无头' if headless else '有头（可见）'}")

        async def _heartbeat():
            while True:
                await asyncio.sleep(60)
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
