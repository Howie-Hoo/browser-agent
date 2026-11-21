#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机 HTTP API 来源校验 — 让「绑 127.0.0.1」真正等于「仅本机可用」。

绑定回环只挡住了外网直连，挡不住两类本机攻击面（沙箱实测命中）：
- **跨站简单请求（CSRF）**：任意网页可用 `fetch(..., {mode:'no-cors'})` 发
  Content-Type 为 text/plain 的 POST——浏览器不预检、服务端 `request.json()`
  照样解析。attach + context=user 时 default 会话跑在用户真实登录态里，
  等于把一个已登录标签页的控制权交给了任意网页。
- **DNS rebinding**：不校验 Host 时，攻击者域名重绑定到 127.0.0.1 后即同源，
  响应可读，API 全量暴露。

三条规则（判定为纯函数，便于单测；中间件只做套用与打点）：
1. **Host 白名单**：必须是本机回环主机名（带不带 :port 均可）。
2. **拒绝一切带 Origin 的请求**：本服务的合法客户端（httpx/curl/Python）
   从不发 Origin；带 Origin = 来自浏览器页面。
3. **带 body 的请求必须 application/json**：非简单 content-type 会强制浏览器
   发 CORS 预检，服务无 CORS 头即被拦——这是 CSRF 的机制性防御，不止靠规则 2。

协议级拒绝不走「恒返 200」业务契约（用 403/415），便于运维侧一眼区分
「请求根本不该进来」与「业务执行失败」。
"""
from __future__ import annotations

from typing import Optional, Tuple

import aiohttp.web as web

from . import logs

# 回环主机名（Host 头可带 :port；端口写法由客户端决定，不参与判定）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _strip_port(host: str) -> str:
    """去掉 Host 头里的 :port（IPv6 的 [::1]:port 形态单独处理）。"""
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def guard_rejection(host: str, origin: str, content_type: str,
                    has_body: bool) -> Optional[Tuple[int, str, str]]:
    """纯判定：请求是否应被拒。返回 (status, error, reason) 或 None（放行）。

    - host：Host 头原值（aiohttp request.host；HTTP/1.0 可能缺省 → 空串放行）
    - origin：Origin 头值，无则空串
    - content_type：Content-Type 头值，无则空串
    - has_body：是否有请求体
    """
    if host:
        bare = _strip_port(host.strip().lower())
        if bare not in _LOOPBACK_HOSTS:
            return (403, f"拒绝非本机 Host: {host}", "bad-host")
    if origin:
        return (403, f"拒绝携带 Origin 的请求（浏览器页面不是本服务的客户端）: {origin}",
                "browser-origin")
    if has_body and not content_type.lower().startswith("application/json"):
        return (415, f"带请求体的调用必须 application/json（当前: {content_type or '未声明'}）",
                "content-type")
    return None


@web.middleware
async def request_guard(request: web.Request, handler):
    """aiohttp 中间件：来源校验（在维护态中间件之前，最先执行）。"""
    rejection = guard_rejection(
        host=request.host,
        origin=request.headers.get("Origin", ""),
        content_type=request.headers.get("Content-Type", ""),
        has_body=request.can_read_body,
    )
    if rejection is not None:
        status, error, reason = rejection
        # 与其他生命周期事件一致：文件 + 服务控制台各一份（拒绝该被看见）
        print(f"[防护] 来源校验拒绝 {reason}: {request.method} {request.path}", flush=True)
        logs.log(f"HTTP 来源校验拒绝: {reason} | {request.method} {request.path} "
                 f"host={request.host} origin={request.headers.get('Origin', '')}", "warning")
        return web.json_response(
            {"ok": False, "error": error, "rejected": reason}, status=status)
    return await handler(request)
