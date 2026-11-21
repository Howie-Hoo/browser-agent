#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BrowserProxyInput — 点击 / 键盘输入 / 表单 / 鼠标滚动命令 Mixin
由 BrowserProxy 通过多继承组合使用，不独立实例化。
（从 proxy.py 拆出以维持文件规模约束；self._page 等核心状态见 proxy.py）
"""
from __future__ import annotations

import math
from typing import Optional

from .utils import emit


class BrowserProxyInput:
    """交互命令族：精确/语义点击、坐标点击、输入、表单、滚动、拖拽。"""

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

    async def click_xy(self, x: float, y: float, button: str = "left",
                       click_count: int = 1, delay: int = 0) -> dict:
        """按视口坐标发送真实鼠标事件（move → down → up）。

        适用 canvas / 地图 / 视频播放器 / 滑块等无 selector 的可视化目标；
        坐标取法：get_bounding_box 返回的中心。clicked 只代表事件已发出，
        是否命中目标需用 get_page_summary / eval_js 事后核对。
        """
        if (isinstance(x, bool) or isinstance(y, bool)
                or not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                or not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0):
            return {"ok": False, "error": "x / y 需为有限的非负视口坐标"}
        if (isinstance(delay, bool) or not isinstance(delay, int) or delay < 0
                or isinstance(click_count, bool) or not isinstance(click_count, int)):
            return {"ok": False, "error": "delay / click_count 需为非负整数"}
        mouse = self._page.mouse
        await mouse.move(x, y)
        for _ in range(max(1, click_count)):
            await mouse.down(button=button)
            await mouse.up(button=button)
            if delay:
                await self._page.wait_for_timeout(delay)
        count = max(1, click_count)
        emit({"type": "click_xy", "x": x, "y": y, "button": button, "ok": True})
        return {"ok": True, "clicked": True, "x": x, "y": y,
                "button": button, "click_count": count}

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

    async def upload_file(self, selector: str, files) -> dict:
        """给 file input 设置本地文件（绕过文件对话框）。

        files：单个路径字符串或路径数组（多文件上传按数组传）。"""
        if isinstance(files, str):
            files = [files]
        if isinstance(files, bool) or not isinstance(files, list) or not files \
                or not all(isinstance(f, str) and f.strip() for f in files):
            return {"ok": False, "error": "files 需为非空文件路径或路径数组"}
        await self._page.set_input_files(selector, files)
        emit({"type": "upload_file", "selector": selector, "files": len(files), "ok": True})
        return {"ok": True, "files": len(files)}

    # ── 鼠标/滚动 ─────────────────────────────────────────

    async def hover(self, selector: str) -> dict:
        await self._page.hover(selector)
        emit({"type": "hover", "selector": selector, "ok": True})
        return {"ok": True}

    async def scroll(self, selector: Optional[str] = None, x: int = 0, y: int = 500,
                     direction: Optional[str] = None) -> dict:
        """滚动页面/元素。direction=top|bottom 直接滚到页面顶/底（触发懒加载
        的常用写法）；与 selector/x/y 互斥且优先。底部取 body 与
        documentElement 的较大 scrollHeight——standards 模式页面 body 常不是
        滚动容器，只看 body 会静默滚不动。"""
        if direction == "top":
            await self._page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            await self._page.evaluate(
                "() => window.scrollTo(0, Math.max("
                "document.body ? document.body.scrollHeight : 0,"
                "document.documentElement ? document.documentElement.scrollHeight : 0))")
        elif direction is not None:
            return {"ok": False, "error": "direction 仅支持 top | bottom"}
        elif selector:
            await self._page.locator(selector).evaluate(f"el => el.scrollBy({x}, {y})")
        else:
            await self._page.evaluate(f"window.scrollBy({x}, {y})")
        emit({"type": "scroll", "ok": True, "direction": direction})
        return {"ok": True, "direction": direction}

    async def scroll_to_element(self, selector: str) -> dict:
        await self._page.locator(selector).scroll_into_view_if_needed()
        emit({"type": "scroll_to_element", "selector": selector, "ok": True})
        return {"ok": True}

    async def drag_and_drop(self, source: str, target: str) -> dict:
        await self._page.drag_and_drop(source, target)
        emit({"type": "drag_and_drop", "ok": True})
        return {"ok": True}
