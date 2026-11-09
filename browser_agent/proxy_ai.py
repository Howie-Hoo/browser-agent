from __future__ import annotations

"""
BrowserProxyAI — AI 高级方法 Mixin
由 BrowserProxy 通过多继承组合使用，不独立实例化。
"""
import asyncio
import base64
import json
import os
import time
from typing import Optional

from .utils import log, emit


class BrowserProxyAI:
    """AI 高级感知、交互与数据提取方法。"""

    # ── AI DOM 标注抽取 ─────────────────────────────────────────
    async def get_annotated_dom(self) -> dict:
        script = """
        () => {
            let elements = document.querySelectorAll('a, button, input, textarea, select, [role="button"], [role="link"], [role="menuitem"]');
            let result = [];
            let id = 1;
            for (let el of elements) {
                let rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0 || window.getComputedStyle(el).visibility === 'hidden') continue;
                
                let box = document.createElement('div');
                box.className = '__ai_anno_box';
                box.style.position = 'absolute';
                box.style.left = (window.scrollX + rect.left) + 'px';
                box.style.top = (window.scrollY + rect.top) + 'px';
                box.style.width = rect.width + 'px';
                box.style.height = rect.height + 'px';
                box.style.backgroundColor = 'rgba(255, 0, 0, 0.1)';
                box.style.border = '2px solid red';
                box.style.zIndex = '2147483647';
                box.style.pointerEvents = 'none';
                
                let label = document.createElement('div');
                label.innerText = '[' + id + ']';
                label.style.position = 'absolute';
                label.style.top = '-14px';
                label.style.left = '-2px';
                label.style.backgroundColor = 'red';
                label.style.color = 'white';
                label.style.fontSize = '12px';
                label.style.fontWeight = 'bold';
                label.style.padding = '0 2px';
                box.appendChild(label);
                
                document.body.appendChild(box);
                
                result.push({
                    id: id,
                    tag: el.tagName.toLowerCase(),
                    text: el.innerText ? el.innerText.trim().slice(0, 50) : (el.value || ''),
                    bbox: [Math.floor(rect.left), Math.floor(rect.top), Math.floor(rect.width), Math.floor(rect.height)]
                });
                id++;
            }
            return result;
        }
        """
        annotations = await self._page.evaluate(script)
        
        save_path = f"temp/annotated_{int(time.time()*1000)}.png"
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        await self._page.screenshot(path=save_path)
        
        await self._page.evaluate("() => { document.querySelectorAll('.__ai_anno_box').forEach(e => e.remove()); }")
        
        emit({"type": "get_annotated_dom", "count": len(annotations), "path": save_path})
        return {"ok": True, "elements": annotations, "screenshot_path": save_path}

    # ── AI 友好高层方法 (V4) ─────────────────────────────────

    async def get_page_summary(self) -> dict:
        """返回面向 AI 的页面语义摘要，无需截图即可让 AI 理解当前页面。"""
        script = """
        () => {
            const title = document.title;
            const url = location.href;
            const h1 = document.querySelector('h1')?.innerText?.trim() || '';
            // 提取页面所有可见文字（前 800 字）
            const bodyText = document.body?.innerText?.replace(/\\s+/g,' ').trim().slice(0, 800) || '';
            // 检测表单
            const forms = [...document.querySelectorAll('form')].map(f => ({
                id: f.id || f.name || '(form)',
                fields: [...f.querySelectorAll('input,textarea,select')]
                    .filter(el => el.type !== 'hidden' && el.type !== 'submit')
                    .map(el => ({
                        type: el.type || el.tagName.toLowerCase(),
                        name: el.name || el.id || el.placeholder || '',
                        required: el.required
                    }))
            }));
            // 检测弹窗/提示
            const alerts = [
                ...document.querySelectorAll('[role="alert"],[class*="error"],[class*="toast"],[class*="notice"],[class*="banner"]')
            ].map(e => e.innerText?.trim()).filter(Boolean).slice(0, 5);
            // 检测 loading 状态
            const hasLoading = !!document.querySelector(
                '[class*="loading"],[class*="spinner"],[class*="skeleton"],[aria-busy="true"]'
            );
            // 检测可交互元素数量
            const interactiveCount = document.querySelectorAll(
                'a, button, input:not([type=hidden]), textarea, select, [role="button"]'
            ).length;
            return { title, url, h1, body_text: bodyText, forms, alerts, has_loading: hasLoading, interactive_count: interactiveCount };
        }
        """
        summary = await self._page.evaluate(script)
        emit({"type": "get_page_summary", "title": summary.get("title"), "ok": True})
        return {"ok": True, **summary}

    async def _snapshot_dom(self) -> dict:
        """拍摄当前 DOM 快照（内部使用，用于 dom_diff 比较）。"""
        script = """
        () => {
            const result = {};
            // 只记录可见文本内容及可交互元素状态
            document.querySelectorAll('[id],[class],[name],h1,h2,h3,p,span,div,button,a,input,textarea').forEach(el => {
                const key = (el.id || el.className || el.tagName).toString().slice(0, 60);
                const text = el.innerText?.trim().slice(0, 100) || el.value || '';
                if (text) result[key] = text;
            });
            return result;
        }
        """
        snap = await self._page.evaluate(script)
        self._last_snapshot = snap
        emit({"type": "snapshot_dom", "ok": True})
        return snap

    async def get_dom_diff(self, snapshot: dict = None) -> dict:
        """与上一次快照对比，返回页面的变化摘要（按 ID/class 比对文本）。"""
        snapshot = snapshot or getattr(self, "_last_snapshot", {})
        if not snapshot:
            return {"success": False, "error": "缺少参考快照，请先调用 snapshot_dom。"}
        current = await self._snapshot_dom()
        appeared, disappeared, changed = [], [], []
        old_keys = set(snapshot.keys())
        new_keys = set(current.keys())
        for k in new_keys - old_keys:
            appeared.append({"element": k, "text": current[k]})
        for k in old_keys - new_keys:
            disappeared.append({"element": k, "text": snapshot[k]})
        for k in old_keys & new_keys:
            if snapshot[k] != current[k]:
                changed.append({"element": k, "from": snapshot[k], "to": current[k]})
        has_change = bool(appeared or disappeared or changed)
        emit({"type": "get_dom_diff", "has_change": has_change})
        return {
            "ok": True, "has_change": has_change,
            "appeared": appeared, "disappeared": disappeared, "changed": changed
        }

    async def fill_form(self, fields: dict) -> dict:
        """批量填写表单。fields 为 {selector: value} 字典，自动选择 fill/check/ select 方式。"""
        results = []
        for selector, value in fields.items():
            try:
                el = self._page.locator(selector).first
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                input_type = await el.evaluate("el => el.type || ''")
                if tag == "select":
                    await el.select_option(str(value))
                elif input_type in ("checkbox", "radio"):
                    if value:
                        await el.check()
                    else:
                        await el.uncheck()
                else:
                    await el.fill(str(value))
                results.append({"selector": selector, "ok": True})
            except Exception as e:
                results.append({"selector": selector, "ok": False, "error": str(e).split("\n")[0]})
        all_ok = all(r["ok"] for r in results)
        emit({"type": "fill_form", "fields": len(fields), "all_ok": all_ok})
        return {"ok": all_ok, "results": results}

    async def wait_for_stable(self, timeout: int = 10000, strategy: str = "auto") -> dict:
        """
        智能等待页面稳定。
        strategy:
          "auto"     — 原有三阶段（networkidle → loading gone → DOM stable 300ms）
          "network"  — 仅等 networkidle
          "dom"      — 仅等 DOM stable（300ms 无变化）
          "instant"  — 仅等 100ms（已知页面已就绪时使用）
        """
        try:
            if strategy == "instant":
                await asyncio.sleep(0.1)
                emit({"type": "wait_for_stable", "ok": True, "strategy": "instant"})
                return {"ok": True, "stable": True, "strategy": "instant"}
            elif strategy == "network":
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=timeout)
                except Exception:
                    pass
                emit({"type": "wait_for_stable", "ok": True, "strategy": "network"})
                return {"ok": True, "stable": True, "strategy": "network"}
            elif strategy == "dom":
                await self._page.evaluate("""
                () => new Promise(resolve => {
                    let stable;
                    const obs = new MutationObserver(() => {
                        clearTimeout(stable);
                        stable = setTimeout(() => { obs.disconnect(); resolve(); }, 300);
                    });
                    stable = setTimeout(() => { obs.disconnect(); resolve(); }, 600);
                    obs.observe(document.body, {childList: true, subtree: true, attributes: true, characterData: true});
                })
                """)
                emit({"type": "wait_for_stable", "ok": True, "strategy": "dom"})
                return {"ok": True, "stable": True, "strategy": "dom"}
            else:  # "auto" — 原有三阶段
                # 1. 等待网络静默（最多 timeout/2）
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=timeout // 2)
                except Exception:
                    pass
                # 2. 等待 loading/spinner 消失
                try:
                    await self._page.wait_for_function(
                        """() => !document.querySelector(
                            '[class*="loading"],[class*="spinner"],[class*="skeleton"],[aria-busy="true"]'
                        )""",
                        timeout=timeout // 2
                    )
                except Exception:
                    pass
                # 3. 等待 DOM 停止变动（连续 300ms 无变化）
                await self._page.evaluate("""
                () => new Promise(resolve => {
                    let stable;
                    const obs = new MutationObserver(() => {
                        clearTimeout(stable);
                        stable = setTimeout(() => { obs.disconnect(); resolve(); }, 300);
                    });
                    stable = setTimeout(() => { obs.disconnect(); resolve(); }, 600);
                    obs.observe(document.body, {childList: true, subtree: true, attributes: true, characterData: true});
                })
                """)
                emit({"type": "wait_for_stable", "ok": True, "strategy": "auto"})
                return {"ok": True, "stable": True, "strategy": "auto"}
        except Exception as e:
            return {"ok": True, "stable": False, "strategy": strategy, "note": str(e).split("\n")[0]}


    # ── V5 AI 视角升级方法 ─────────────────────────────────

    async def click_by_id(self, element_id: int) -> dict:
        """按 get_annotated_dom 返回的序号 ID 直接点击元素，无需写 selector。"""
        script = f"""
        () => {{
            const els = document.querySelectorAll(
                'a, button, input, textarea, select, [role="button"], [role="link"], [role="menuitem"]'
            );
            const visible = [...els].filter(el => {{
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 &&
                       window.getComputedStyle(el).visibility !== 'hidden' &&
                       window.getComputedStyle(el).display !== 'none';
            }});
            const target = visible[{element_id - 1}];
            if (!target) return null;
            target.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
            target.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
            target.click();
            return {{
                tag: target.tagName.toLowerCase(),
                text: (target.innerText || target.value || target.getAttribute('aria-label') || '').trim().slice(0, 80),
                id_attr: target.id || null,
                classes: target.className || null
            }};
        }}
        """
        result = await self._page.evaluate(script)
        if not result:
            return {
                "ok": False,
                "error": f"ID [{element_id}] 不存在，请重新调用 get_annotated_dom 获取最新编号",
                "suggestion": "序号在每次 get_annotated_dom 后会重建，页面变化后请重新获取"
            }
        emit({"type": "click_by_id", "id": element_id, "element": result, "ok": True})
        return {"ok": True, "clicked": result}

    async def _try_ssr_extraction(self) -> Optional[dict]:
        """尝试从主流 SSR 框架的内联数据块中提取初始化数据。"""
        try:
            return await self._page.evaluate("""
            () => {
                if (window.__NEXT_DATA__?.props?.pageProps)
                    return {framework: 'Next.js', data: window.__NEXT_DATA__.props.pageProps};
                if (window.__NUXT__?.data)
                    return {framework: 'Nuxt.js', data: window.__NUXT__.data};
                if (window.__nuxt_payload)
                    return {framework: 'Nuxt3', data: window.__nuxt_payload};
                for (const key of ['__INITIAL_STATE__', '__STATE__', '__APP_STATE__', 'initialData']) {
                    if (window[key]) return {framework: 'SPA', key, data: window[key]};
                }
                const scriptEl = document.querySelector('script[type="application/json"]');
                if (scriptEl) {
                    try { return {framework: 'json-script', data: JSON.parse(scriptEl.textContent)}; } catch {}
                }
                return null;
            }
            """)
        except Exception:
            return None

    async def _extract_single_text(self, selector: str, timeout: int) -> Optional[str]:
        """提取单个元素文本，内置等待 + Shadow DOM 穿透兜底。"""
        try:
            try:
                await self._page.wait_for_selector(selector, timeout=min(timeout, 2000))
            except Exception:
                pass
            loc = self._page.locator(selector).first
            if not await loc.count():
                return None
            # inner_text 可能在 data:/file: URL 中返回空，回退 textContent
            text = (await loc.inner_text()).strip()
            if not text:
                text = (await loc.evaluate("el => el.textContent || el.value || ''")).strip()
            return text or None
        except Exception:
            try:
                safe_sel = selector.replace("'", '"')
                return await self._page.evaluate(f"""
                () => {{
                    function deepQuery(root, sel) {{
                        const el = root.querySelector(sel);
                        if (el) return el.innerText?.trim() || null;
                        for (const child of root.querySelectorAll('*')) {{
                            if (child.shadowRoot) {{
                                const found = deepQuery(child.shadowRoot, sel);
                                if (found) return found;
                            }}
                        }}
                        return null;
                    }}
                    return deepQuery(document, '{safe_sel}');
                }}
                """)
            except Exception:
                return None

    async def extract(self, schema: dict, wait_timeout: int = 3000) -> dict:
        """
        多框架兼容的结构化数据提取。
        兼容 Vue / React / Next.js / Nuxt / 原生 HTML。
        内置 4 层兼容策略：
          1. 标准 DOM querySelector（含等待异步渲染）
          2. Shadow DOM 穿透提取
          3. SSR/框架数据块嗅探 (__NEXT_DATA__, __NUXT__ 等)
          4. 数据质量检测 + quality_issues 告知 AI 备用方案
        schema 格式：
          "key": "css-selector"                             → 提取该选择器文本
          "key": {"selector": ".row", "fields": {          → 列表/表格提取
              "name": "td:nth-child(1)",
              "price": "td:nth-child(2)",
              "link": "@href"                               → @前缀取属性
          }}
        """
        result_data = {}
        quality_issues = []

        for key, definition in schema.items():
            try:
                if isinstance(definition, str):
                    val = await self._extract_single_text(definition, wait_timeout)
                    if val is None:
                        quality_issues.append({
                            "key": key, "selector": definition,
                            "reason": "选择器未匹配到元素，可能是异步渲染未完成或选择器失效",
                            "alternatives": [
                                "先调用 wait_for_stable 等待渲染",
                                "用 get_page_summary.body_text 提取页面文本",
                                "用 eval_js 手动查询"
                            ]
                        })
                    result_data[key] = val

                elif isinstance(definition, dict) and "selector" in definition:
                    sel = definition["selector"]
                    fields = definition.get("fields", {})
                    attr_only = definition.get("attribute")
                    limit = definition.get("limit", 100)

                    # wait_for_selector 仅作预热；失败后仍然直接查 count()，
                    # 兼容 data: URL / file:// 等非标协议下 networkidle 超时的场景
                    try:
                        await self._page.wait_for_selector(sel, timeout=min(wait_timeout, 2000))
                    except Exception:
                        pass
                    count = await self._page.locator(sel).count()

                    if count == 0:
                        ssr_data = await self._try_ssr_extraction()
                        result_data[key] = {
                            "items": [], "count": 0,
                            "ssr_data_available": bool(ssr_data),
                            "ssr_data": ssr_data
                        }
                        quality_issues.append({
                            "key": key, "selector": sel,
                            "reason": "选择器匹配 0 个元素，可能是客户端渲染尚未完成",
                            "alternatives": [
                                "调用 wait_for_stable 后重试",
                                "若 ssr_data_available=true，数据已在 ssr_data 字段中",
                                f"用 eval_js: document.querySelectorAll('{sel}').length 验证"
                            ]
                        })
                        continue

                    items = []
                    for i in range(min(count, limit)):
                        el = self._page.locator(sel).nth(i)
                        if attr_only:
                            items.append(await el.get_attribute(attr_only))
                        elif fields:
                            row = {}
                            for fname, fdef in fields.items():
                                try:
                                    if fdef is None:
                                        row[fname] = (await el.inner_text()).strip()
                                    elif isinstance(fdef, str) and fdef.startswith("@"):
                                        row[fname] = await el.get_attribute(fdef[1:])
                                    elif isinstance(fdef, str):
                                        child = el.locator(fdef).first
                                        row[fname] = (await child.inner_text()).strip() if await child.count() > 0 else None
                                except Exception:
                                    row[fname] = None
                            items.append(row)
                        else:
                            items.append((await el.inner_text()).strip())

                    result_data[key] = {"items": items, "count": len(items)}

                    # 数据质量检测：80% 行为空则告警
                    if items and fields:
                        empty_rows = sum(1 for r in items if isinstance(r, dict) and all(v is None or v == "" for v in r.values()))
                        if len(items) > 0 and empty_rows / len(items) > 0.8:
                            quality_issues.append({
                                "key": key, "selector": sel,
                                "reason": f"提取到 {len(items)} 个元素但 {empty_rows} 个内容为空，可能是虚拟 DOM 未挂载素材",
                                "alternatives": [
                                    "调用 wait_for_stable 后重试",
                                    "调用 scroll 触发懒加载后重试",
                                    "用 eval_js 验证元素内容是否真实存在"
                                ]
                            })
            except Exception as e:
                result_data[key] = {"error": str(e).split("\n")[0]}
                quality_issues.append({
                    "key": key,
                    "reason": str(e).split("\n")[0],
                    "alternatives": ["尝试 eval_js 或 get_page_summary"]
                })

        data_quality = "good" if not quality_issues else ("partial" if result_data else "failed")
        emit({"type": "extract", "keys": list(result_data.keys()), "quality": data_quality})
        return {
            "ok": True,
            "data": result_data,
            "data_quality": data_quality,
            "quality_issues": quality_issues,
            "ai_hint": (None if not quality_issues else
                        f"提取存在 {len(quality_issues)} 个问题。请查看 quality_issues 中的 alternatives 字段采取备用方案。")
        }

    async def get_session_context(self) -> dict:
        """返回当前会话的完整状态快照，帮助 AI 在任务中途或恢复时重建上下文。"""
        page = self._page
        cookies = await page.context.cookies() if page else []
        return {
            "ok": True,
            "current_url": page.url if page else "",
            "page_title": await page.title() if page else "",
            "active_tab": self._active_tab,
            "open_tabs": list(self._pages.keys()),
            "contexts": list(self._contexts.keys()),
            "cookies_count": len(cookies),
            "viewport": self.viewport,
            "base_url": self.base_url,
            "downloads_pending": list(self._downloads.keys()),
            "proxy_configured": bool(self.proxy_server),
        }

    async def get_interactive_elements(self, limit: int = 50, _use_cache: bool = True) -> dict:
        """
        以纯文本清单返回当前页面所有可交互元素（无截图）。
        附带最可靠的 selector 推荐（优先 id > data-testid > name > aria-label）。
        配合 click_by_id 使用，id 字段与 get_annotated_dom 的编号一致。
        """
        # O3: 元素缓存 — 同页面 2s 内复用
        import time as _time
        current_url = self._page.url if self._page else ""
        now = _time.monotonic()
        if _use_cache and self._elements_cache and self._elements_cache_url == current_url and (now - self._elements_cache_time) < 2.0:
            emit({"type": "get_interactive_elements", "count": len(self._elements_cache), "cached": True})
            return {
                "ok": True, "elements": self._elements_cache, "total": len(self._elements_cache),
                "cached": True,
                "ai_hint": "优先使用有 selector 字段的元素直接 click/fill；无 selector 的元素用 click_by_id 通过 id 编号点击"
            }

        script = f"""
        () => {{
            const SEL = 'a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled]), [role="button"]:not([disabled]), [role="link"], [role="tab"], [role="menuitem"]';
            const els = [...document.querySelectorAll(SEL)].filter(el => {{
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            }}).slice(0, {limit});
            return els.map((el, i) => {{
                let selector = null;
                if (el.id) selector = '#' + CSS.escape(el.id);
                else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
                else if (el.getAttribute('name')) selector = '[name="' + el.getAttribute('name') + '"]';
                else if (el.getAttribute('aria-label')) selector = '[aria-label="' + el.getAttribute('aria-label') + '"]';
                const type = el.tagName.toLowerCase() + (el.type ? '[' + el.type + ']' : '');
                // 主文本：优先 innerText，其次 value（不再合并 placeholder/aria-label/title）
                const text = (el.innerText || el.value || '').trim().slice(0, 80);
                // 独立匹配源字段
                const placeholder = el.placeholder || null;
                const ariaLabel = el.getAttribute('aria-label') || null;
                const title = el.getAttribute('title') || null;
                // 关联 label：通过 for=id 或 closest('label')
                let label = null;
                try {{
                    if (el.id) {{
                        const labelEl = document.querySelector('label[for="' + el.id + '"]');
                        if (labelEl) label = (labelEl.innerText || '').trim().slice(0, 60);
                    }}
                    if (!label) {{
                        const closestLabel = el.closest('label');
                        if (closestLabel && closestLabel !== el) label = (closestLabel.innerText || '').trim().slice(0, 60);
                    }}
                }} catch(e) {{}}
                return {{ id: i+1, type, text, placeholder, ariaLabel, title, label, selector, href: el.href || null }};
            }});
        }}
        """
        elements = await self._page.evaluate(script)
        # O3: 更新元素缓存
        self._elements_cache = elements
        self._elements_cache_url = current_url
        self._elements_cache_time = now
        emit({"type": "get_interactive_elements", "count": len(elements)})
        return {
            "ok": True, "elements": elements, "total": len(elements),
            "ai_hint": "优先使用有 selector 字段的元素直接 click/fill；无 selector 的元素用 click_by_id 通过 id 编号点击"
        }

    # ── V6 高级感知与控制 ──────────────────────────────────────

    async def get_a11y_tree(self, root_selector: str = None, interesting_only: bool = True) -> dict:
        """
        获取页面无障碍语义树 (Accessibility Tree)。
        比 DOM/innerHTML 更语义化，对 React/Vue/Angular SPA 尤其有效。
        内置兜底：若语义树为空则自动降级到 get_interactive_elements。
        返回 nodes 数组 + ai_readable 文本 + 兜底建议。
        """
        try:
            kwargs = {"interesting_only": interesting_only}
            if root_selector:
                root_el = await self._main_page.query_selector(root_selector)
                if root_el:
                    kwargs["root"] = root_el
            snapshot = await self._main_page.accessibility.snapshot(**kwargs)

            def flatten_tree(node, depth=0, result=None):
                if result is None:
                    result = []
                if not node:
                    return result
                role = node.get("role", "")
                name = node.get("name", "")
                if role and name:
                    result.append({
                        "depth": depth,
                        "role": role,
                        "name": name[:120],
                        "value": (node.get("value") or "")[:120] or None,
                        "checked": node.get("checked"),
                        "disabled": node.get("disabled"),
                        "expanded": node.get("expanded"),
                    })
                for child in node.get("children", []):
                    flatten_tree(child, depth + 1, result)
                return result

            flat_nodes = flatten_tree(snapshot) if snapshot else []
            ai_readable = "".join(
                "  " * n["depth"] + f"[{n['role']}] {n['name']}" +
                (f" = {n['value']}" if n.get("value") else "") +
                (" (disabled)" if n.get("disabled") else "")
                for n in flat_nodes
            )

            # 兜底验证：树为空则自动降级
            if not flat_nodes:
                fallback = await self.get_interactive_elements(50)
                emit({"type": "get_a11y_tree", "nodes": 0, "fallback": True})
                return {
                    "ok": True,
                    "nodes": [],
                    "ai_readable": "",
                    "total": 0,
                    "data_quality": "empty",
                    "fallback_used": True,
                    "fallback_result": fallback,
                    "ai_hint": "无障碍树为空（页面可能屏蔽了 ARIA 或使用了非标准框架）。已自动降级执行 get_interactive_elements 作为兜底，结果在 fallback_result 字段中。"
                }

            emit({"type": "get_a11y_tree", "nodes": len(flat_nodes), "ok": True})
            return {
                "ok": True,
                "nodes": flat_nodes,
                "ai_readable": ai_readable,
                "total": len(flat_nodes),
                "data_quality": "good"
            }
        except Exception as e:
            # 异常时降级
            try:
                fallback = await self.get_interactive_elements(50)
            except Exception:
                fallback = None
            return {
                "ok": True,
                "nodes": [],
                "ai_readable": "",
                "total": 0,
                "data_quality": "error",
                "error": str(e).split(chr(10))[0],
                "fallback_result": fallback,
                "ai_hint": f"get_a11y_tree 执行失败: {str(e).split(chr(10))[0]}。已降级执行 get_interactive_elements，结果在 fallback_result 中。"
            }

    async def get_page_health(self) -> dict:
        """
        返回页面运行时健康状态：JS 控制台错误、资源加载失败、关键性能指标。
        数据来自持续监听的事件流（自服务启动后持续累积）。
        告知 AI 潜在的页面异常情况。
        """
        try:
            perf = await self._main_page.evaluate("""
            () => {
                try {
                    const nav = performance.getEntriesByType('navigation')[0] || {};
                    const paints = Object.fromEntries(
                        performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)])
                    );
                    return {
                        dom_ready_ms: Math.round(nav.domContentLoadedEventEnd || 0),
                        load_complete_ms: Math.round(nav.loadEventEnd || 0),
                        first_paint_ms: paints['first-paint'] || null,
                        first_contentful_paint_ms: paints['first-contentful-paint'] || null,
                        resources_count: performance.getEntriesByType('resource').length
                    };
                } catch(e) { return {error: e.message}; }
            }
            """)
        except Exception as e:
            perf = {"error": str(e).split(chr(10))[0]}

        recent_errors = self._console_errors[-20:]
        recent_failures = self._failed_requests[-20:]
        is_healthy = len(recent_errors) == 0 and len(recent_failures) == 0

        emit({"type": "get_page_health", "healthy": is_healthy, "errors": len(recent_errors), "failures": len(recent_failures)})
        return {
            "ok": True,
            "healthy": is_healthy,
            "console_errors": recent_errors,
            "failed_requests": recent_failures,
            "performance": perf,
            "ai_hint": (
                None if is_healthy else
                f"页面存在 {len(recent_errors)} 个控制台错误、{len(recent_failures)} 个资源加载失败。"
                f"这可能导致页面功能不完整或交互异常。建议：检查 console_errors 中的 JS 报错后决定是否继续操作，"
                f"或先尝试 navigate 刷新页面后重试。"
            )
        }

    async def scroll_until_found(
        self, selector: str, max_scrolls: int = 10,
        scroll_step: int = 800, timeout_per_scroll: int = 1000
    ) -> dict:
        """
        持续向下滚动直到目标选择器出现，适用于懒加载/无限滚动页面。
        兜底机制：
          - 到达页面底部 → 告知 AI 并建议换筛选条件
          - 达到 max_scrolls → 告知 AI 并建议增加 max_scrolls
          - 始终返回当前已加载的内容数量
        """
        for i in range(max_scrolls):
            count = await self._page.locator(selector).count()
            if count > 0:
                try:
                    await self._page.locator(selector).first.scroll_into_view_if_needed()
                except Exception:
                    pass
                emit({"type": "scroll_until_found", "found": True, "scrolls": i})
                return {"ok": True, "found": True, "scrolls_needed": i, "elements_found": count}

            prev_h = await self._page.evaluate("document.body.scrollHeight")
            await self._page.evaluate(f"window.scrollBy(0, {scroll_step})")
            await self._page.wait_for_timeout(timeout_per_scroll)
            new_h = await self._page.evaluate("document.body.scrollHeight")

            if new_h == prev_h and i > 0:
                emit({"type": "scroll_until_found", "found": False, "reason": "page_bottom"})
                return {
                    "ok": True, "found": False, "scrolls_needed": i + 1,
                    "reason": "已滚动至页面底部仍未找到目标元素",
                    "ai_hint": (
                        f"选择器 '{selector}' 在本页不存在（已到底部）。"
                        f"建议：1) 检查选择器是否正确；2) 用 get_page_summary 确认当前页面；"
                        f"3) 页面可能需要分页操作（下一页按钮）而非无限滚动"
                    )
                }

        # 达到最大滚动次数
        final_count = await self._page.locator(selector).count()
        emit({"type": "scroll_until_found", "found": final_count > 0, "reason": "max_scrolls"})
        return {
            "ok": True, "found": final_count > 0,
            "scrolls_needed": max_scrolls,
            "elements_found": final_count,
            "reason": f"已达到最大滚动次数 ({max_scrolls})" + (" 且已找到元素" if final_count > 0 else "，未找到元素"),
            "ai_hint": (
                None if final_count > 0 else
                f"在 {max_scrolls} 次滚动后仍未找到 '{selector}'。"
                f"建议：增加 max_scrolls 参数（如 20）；或先调用 get_page_summary 确认页面结构"
            )
        }

    # 匹配源优先级权重（O1: 扩展匹配源）
    _MATCH_SOURCES = [
        ("text", 1.0),
        ("label", 0.9),
        ("ariaLabel", 0.85),
        ("placeholder", 0.8),
        ("title", 0.75),
    ]

    @staticmethod
    def _score_match(desc_lower: str, source_text: str, weight: float) -> float:
        """对单个匹配源计算分数，乘以权重"""
        text = (source_text or "").lower()
        if not text:
            return 0
        if text == desc_lower:
            return 100 * weight
        elif desc_lower in text:
            return (50 + int((len(desc_lower) / max(len(text), 1)) * 50)) * weight
        elif any(w in text for w in desc_lower.split() if len(w) > 1):
            return 20 * weight
        return 0

    async def find_and_act(self, description: str, action: str = "click", value: str = None) -> dict:
        """
        语义搜索元素并立即执行动作（一步到位，减少 AI 往返次数）。
        description: 自然语言描述，如 "登录按钮"、"手机号输入框"
        action: click | fill | check | get_text
        匹配源：text / label / ariaLabel / placeholder / title（多源评分取最高）
        兜底机制：
          - 未匹配到 → 返回全部元素清单 + 建议改用 get_interactive_elements 手动定位
          - 低置信度匹配（相似度 < 30%）→ 标注警告，AI 自行决定是否继续
        """
        elements_result = await self.get_interactive_elements(limit=60)
        items = elements_result.get("elements", [])

        desc_lower = description.lower().strip()
        candidates = []
        for el in items:
            best_score_for_el = 0
            best_source = None
            for source_name, weight in self._MATCH_SOURCES:
                score = self._score_match(desc_lower, el.get(source_name), weight)
                if score > best_score_for_el:
                    best_score_for_el = score
                    best_source = source_name
            if best_score_for_el > 0:
                candidates.append((best_score_for_el, el, best_source))

        if not candidates:
            emit({"type": "find_and_act", "found": False})
            return {
                "ok": False,
                "error": f"未找到匹配 '{description}' 的元素",
                "all_elements": items,
                "ai_hint": (
                    f"描述 '{description}' 未匹配到任何可交互元素。"
                    f"可用元素清单已在 all_elements 中返回。"
                    f"建议：查看 all_elements 后用 click_by_id 或 click/fill 直接操作"
                )
            }

        best_score, best_el, best_source = sorted(candidates, key=lambda x: -x[0])[0]
        low_confidence = best_score < 30

        # 执行动作
        action_result = None
        try:
            if action == "click":
                action_result = await self.click_by_id(best_el["id"])
            elif action == "fill" and value is not None:
                sel = best_el.get("selector")
                if sel:
                    action_result = await self.fill(sel, value)
                else:
                    action_result = await self.click_by_id(best_el["id"])
            elif action == "check":
                sel = best_el.get("selector")
                if sel:
                    action_result = await self.check(sel)
                else:
                    action_result = {"ok": False, "error": "无法获取 selector 执行 check"}
            elif action == "get_text":
                action_result = {"ok": True, "text": best_el.get("text")}
            else:
                action_result = {"ok": False, "error": f"不支持的 action: {action}，可用: click/fill/check/get_text"}
        except Exception as e:
            action_result = {"ok": False, "error": str(e).split(chr(10))[0]}

        emit({"type": "find_and_act", "description": description, "matched": best_el.get("text"), "score": best_score, "source": best_source, "ok": action_result.get("ok", True)})
        return {
            "ok": action_result.get("ok", True),
            "matched_element": best_el,
            "match_score": best_score,
            "match_source": best_source,
            "low_confidence": low_confidence,
            "action_result": action_result,
            "ai_hint": (
                f"低置信度匹配（得分 {best_score}/100，来源 {best_source}），匹配到 '{best_el.get(best_source) or best_el.get('text')}'，请确认是否为目标元素。"
                f"如有误，请查看 all_elements（调用 get_interactive_elements）后手动指定"
                if low_confidence else None
            )
        }


    # ── V7 高级交互 ──────────────────────────────────────────────

    async def detect_captcha(self) -> dict:
        """
        探测当前页面是否存在验证码，并识别类型（滑块/图片/reCAPTCHA/hCaptcha/短信验证码）。
        兜底机制（始终保留）：
          1. 一旦检测到验证码，ai_hint 第一条建议始终是"提示用户手动操作"
          2. 若 JS 探测失败，回退到关键字扫描 + screenshot，让 AI 视觉判断
        数据质量：探测到的信号越多越可靠，partial 时需 AI 结合截图确认。
        """
        try:
            js_result = await self._main_page.evaluate("""
            () => {
                const checks = {
                    recaptcha: !!document.querySelector('.g-recaptcha, iframe[src*="recaptcha"]'),
                    hcaptcha: !!document.querySelector('.h-captcha, iframe[src*="hcaptcha"]'),
                    slider: !!(document.querySelector('[class*="slider-captcha"],[class*="drag-captcha"],[class*="verify-slider"],[id*="slider"],[class*="nc-lang-cnt"]')),
                    image_click: !!(document.querySelector('[class*="geetest"],[class*="captcha-image"],canvas[class*="captcha"]')),
                    sms_dialog: !!(document.querySelector('[placeholder*="验证码"],[placeholder*="短信"]') &&
                                   document.querySelector('[class*="dialog"],[class*="modal"],[role="dialog"]')),
                    text_captcha: !!document.querySelector('input[class*="verify"],img[class*="captcha"]'),
                    cloudflare: !!document.querySelector('#challenge-form, #challenge-running, .cf-challenge-running'),
                };
                const found = Object.entries(checks).filter(([k,v]) => v).map(([k]) => k);
                const kw_found = ['blocked','robot','captcha','verify','human','验证','机器人','人机校验'].filter(
                    kw => document.body.innerText.toLowerCase().includes(kw.toLowerCase())
                );
                return { types: found, keywords: kw_found, page_title: document.title };
            }
            """)
            types = js_result.get("types", [])
            keywords = js_result.get("keywords", [])
            has_captcha = bool(types) or len(keywords) >= 2
            data_quality = "good" if types else ("partial" if keywords else "good")
        except Exception as js_err:
            types, keywords, has_captcha = [], [], False
            data_quality = "error"

        # 兜底截图（无论如何都截一张）
        try:
            shot_bytes = await self._main_page.screenshot(type="jpeg", quality=60)
            shot_b64 = base64.b64encode(shot_bytes).decode()
        except Exception:
            shot_b64 = None

        hint_lines = []
        if has_captcha:
            hint_lines.append("❗ 检测到验证码，建议处理方式（按优先级）：")
            hint_lines.append("1. [推荐] 暂停任务，通过 notify_user 提示用户手动完成验证后继续（永远保留此选项）；")
            hint_lines.append("2. 若是滑块，可尝试 eval_js 注入 mouse.move 模拟拖拽；")
            hint_lines.append("3. 若是 reCAPTCHA/hCaptcha，可接入第三方打码服务（2captcha/Anti-Captcha）；")
            hint_lines.append("4. 刷新页面重试（navigate 当前 URL）看是否概率出现；")
            hint_lines.append(f"已截图供视觉确认（screenshot_base64）。类型信号：{types}，关键字：{keywords}")
        elif data_quality == "partial":
            hint_lines.append("低信号（仅关键字匹配）：建议查看 screenshot_base64 截图人工确认是否有验证码。")

        emit({"type": "detect_captcha", "has_captcha": has_captcha, "types": types, "quality": data_quality})
        return {
            "ok": True,
            "has_captcha": has_captcha,
            "captcha_types": types,
            "blocked_keywords": keywords,
            "data_quality": data_quality,
            "screenshot_base64": shot_b64,
            "ai_hint": "\n".join(hint_lines) if hint_lines else None,
        }

    async def form_analyze(self, form_selector: str = "form") -> dict:
        """
        分析表单结构，返回所有字段类型/必填/校验规则/下拉选项。
        AI 填表前先调用，获取完整表单蓝图，避免盲填失败。
        兜底机制：
          - 若 form_selector 找不到表单，自动放宽到 body 范围扫描
          - 返回 fill_guide（直接可用的 fill_form 参考格式）
        数据质量：若字段数为 0，告知 AI 原因和备选方案。
        """
        def make_js(sel):
            return f"""
            () => {{
                const container = document.querySelector('{sel}') || document.body;
                const inputs = container.querySelectorAll('input:not([type=hidden]), select, textarea, [contenteditable="true"]');
                const seen = new Set(), result = [];
                inputs.forEach(el => {{
                    if (seen.has(el) || (!el.offsetParent && el.type !== 'submit')) return;
                    seen.add(el);
                    const labelEl = el.id ? document.querySelector('label[for="'+el.id+'"]') : el.closest('label');
                    const label = (labelEl && labelEl !== el) ? labelEl.innerText.trim().replace(/\\s+/g,' ').substring(0,60) : '';
                    const f = {{
                        tag: el.tagName.toLowerCase(), type: el.type || 'text',
                        name: el.name || null, id: el.id || null, label: label || null,
                        placeholder: el.placeholder || null, required: el.required,
                        disabled: el.disabled, current_value: el.value || null,
                        max_length: el.maxLength > 0 ? el.maxLength : null,
                        pattern: el.pattern || null,
                        selector: el.id ? '#'+el.id : (el.name ? '[name="'+el.name+'"]' : null),
                    }};
                    if (el.tagName === 'SELECT') {{
                        f.options = Array.from(el.options).map(o => ({{value:o.value, label:o.text.trim(), selected:o.selected}}));
                    }}
                    if (el.type === 'radio' || el.type === 'checkbox') {{
                        const grp = el.name ? container.querySelectorAll('[name="'+el.name+'"]') : [el];
                        f.options = Array.from(grp).map(r => ({{value:r.value, label:(r.labels&&r.labels[0]&&r.labels[0].innerText)||r.value, checked:r.checked}}));
                    }}
                    result.push(f);
                }});
                return result;
            }}
            """

        # 先尝试指定选择器，若无结果降级到 body
        fields = await self._page.evaluate(make_js(form_selector))
        fallback_used = False
        if not fields and form_selector != "body":
            fields = await self._page.evaluate(make_js("body"))
            fallback_used = True

        required = [f for f in fields if f.get("required")]
        fill_guide = {
            f.get("selector") or f.get("name"): f"[{f.get('type')}]{(' (必填)' if f.get('required') else '')} {f.get('label') or f.get('placeholder') or ''}"
            for f in fields if (f.get("selector") or f.get("name")) and not f.get("disabled")
        }

        quality = "good" if fields else "empty"
        hint = None
        if not fields:
            hint = (f"选择器 '{form_selector}' 未找到表单字段（已自动扩展到 body 范围仍为空）。"
                    "建议：1) 检查页面是否已完全加载（wait_for_stable）；"
                    "2) 用 get_page_summary 查看页面是否有动态渲染的表单；"
                    "3) 直接用 eval_js 查询具体字段选择器后 fill 填写")
        elif fallback_used:
            hint = f"在 '{form_selector}' 未找到表单，已自动扩展至 body 范围找到 {len(fields)} 个字段。"

        emit({"type": "form_analyze", "fields": len(fields), "ok": True})
        return {
            "ok": True, "total_fields": len(fields),
            "required_count": len(required), "fields": fields,
            "required_fields": required, "fill_guide": fill_guide,
            "data_quality": quality, "fallback_to_body": fallback_used,
            "ai_hint": hint,
        }

    async def watch_for_change(self, conditions: list, timeout: int = 10000, poll_interval: int = 500) -> dict:
        """
        服务端等待指定条件满足（消除 AI 反复轮询的往返开销）。
        条件类型（conditions 数组，满足任意一个即返回）：
          - {type: "text_appears",    value: "成功"}
          - {type: "text_disappears", value: "加载中"}
          - {type: "url_contains",    value: "/console"}
          - {type: "url_changes"}       （无需 value）
          - {type: "element_visible",  selector: ".toast"}
          - {type: "element_hidden",   selector: ".spinner"}
          - {type: "title_changes"}     （无需 value）
        兜底：超时时返回当前页面摘要 + ai_hint，AI 自行判断下一步。
        """
        import time as _time, asyncio as _asyncio
        t0 = _time.monotonic()
        deadline = t0 + timeout / 1000.0
        init_url = self._page.url
        init_title = await self._page.title()

        while _time.monotonic() < deadline:
            for cond in conditions:
                ctype = cond.get("type", "")
                val = cond.get("value", "")
                sel = cond.get("selector", val)
                try:
                    met = False
                    if ctype == "text_appears":
                        body = await self._page.evaluate("document.body.innerText")
                        met = val.lower() in body.lower()
                    elif ctype == "text_disappears":
                        body = await self._page.evaluate("document.body.innerText")
                        met = val.lower() not in body.lower()
                    elif ctype == "url_contains":
                        met = val in self._page.url
                    elif ctype == "url_changes":
                        met = self._page.url != init_url
                    elif ctype == "element_visible":
                        met = bool(await self._page.locator(sel).count()) and await self._page.locator(sel).is_visible()
                    elif ctype == "element_hidden":
                        c = await self._page.locator(sel).count()
                        met = c == 0 or not await self._page.locator(sel).is_visible()
                    elif ctype == "title_changes":
                        met = (await self._page.title()) != init_title
                    if met:
                        waited = int((_time.monotonic() - t0) * 1000)
                        summary = await self.get_page_summary()
                        emit({"type": "watch_for_change", "matched": ctype, "ok": True})
                        return {
                            "ok": True, "matched": True, "matched_condition": cond,
                            "waited_ms": waited, "current_url": self._page.url,
                            "page_summary": summary,
                        }
                except Exception:
                    pass
            await _asyncio.sleep(poll_interval / 1000.0)

        # 超时兜底
        summary = await self.get_page_summary()
        cond_names = [f"{c.get('type')}({c.get('value','')[:20]})" for c in conditions]
        emit({"type": "watch_for_change", "matched": False})
        return {
            "ok": True, "matched": False, "waited_ms": timeout,
            "conditions_checked": cond_names, "current_url": self._page.url,
            "page_summary": summary,
            "ai_hint": (
                f"等待 {timeout}ms 后，{len(conditions)} 个条件均未满足：{cond_names}。"
                f"当前页面摘要在 page_summary 字段。建议：1) 检查条件值是否正确（大小写/空格）；"
                f"2) 增加 timeout 参数；3) 查看 page_summary 手动判断页面状态"
            )
        }

    async def hover_submenu(self, hover_selector: str, click_selector: str = None, wait_after_hover: int = 600) -> dict:
        """
        悬停元素触发 CSS :hover 下拉菜单，然后可选点击子菜单项。
        适用于导航菜单、下拉选择器等 hover 触发的 UI 组件。
        兜底机制：
          - hover 后若 click_selector 不可见，返回已出现的菜单文本清单供 AI 重新选择
          - 若 hover 本身失败，建议改用 eval_js 触发 mouseover 事件
        """
        try:
            await self._page.hover(hover_selector)
            await self._page.wait_for_timeout(wait_after_hover)
        except Exception as e:
            return {
                "ok": False, "error": str(e).split(chr(10))[0],
                "ai_hint": (
                    f"hover '{hover_selector}' 失败。备选方案："
                    f"1) eval_js 触发：document.querySelector('{hover_selector}').dispatchEvent(new MouseEvent('mouseover', {{bubbles:true}}))；"
                    "2) 检查选择器是否正确（get_interactive_elements）"
                )
            }

        # 扫描悬停后出现的菜单内容
        appeared = []
        try:
            appeared = await self._page.evaluate("""
            () => {
                const sels = ['[class*="dropdown"]','[class*="submenu"]','[class*="menu-list"]',
                              '[class*="nav-dropdown"]','[role="menu"]'];
                const texts = [];
                sels.forEach(s => {
                    document.querySelectorAll(s).forEach(el => {
                        if (el.offsetParent) texts.push(el.innerText.trim().substring(0,120));
                    });
                });
                return [...new Set(texts)].filter(t => t.length > 0).slice(0, 20);
            }
            """)
        except Exception:
            pass

        if click_selector:
            try:
                await self._page.wait_for_selector(click_selector, state="visible", timeout=2000)
                await self._page.click(click_selector)
                emit({"type": "hover_submenu", "hovered": hover_selector, "clicked": click_selector, "ok": True})
                return {"ok": True, "hovered": hover_selector, "clicked": click_selector, "appeared_menus": appeared}
            except Exception as e:
                return {
                    "ok": False, "hovered": True, "error": f"click_selector '{click_selector}' 不可见",
                    "appeared_menus": appeared,
                    "ai_hint": (
                        f"悬停成功但目标 '{click_selector}' 不可见。"
                        f"已出现的菜单文本在 appeared_menus 字段，请重新确认目标选择器后用 click 命令直接点击。"
                    )
                }

        emit({"type": "hover_submenu", "hovered": hover_selector, "ok": True})
        return {
            "ok": True, "hovered": hover_selector, "clicked": None,
            "appeared_menus": appeared,
            "ai_hint": f"悬停完成，出现了 {len(appeared)} 个菜单区块。请从 appeared_menus 中确认目标后，指定 click_selector 参数重新调用，或直接用 click 点击目标元素。" if appeared else None
        }

    async def smart_wait(self, condition: str, value: str = None, timeout: int = 10000) -> dict:
        """
        精细单条件等待。比 wait_for_stable 更精准，支持：
          url_is / url_contains / url_changes
          text_appears / text_disappears
          selector_visible / selector_hidden / selector_count_gte（格式: "5|.item"）
          network_idle / dom_stable
        兜底：超时时返回当前 URL + 页面摘要，AI 自行判断下一步。
        """
        import asyncio as _asyncio, time as _time
        t0 = _time.monotonic()
        init_url = self._page.url
        try:
            if condition == "url_is":
                await self._page.wait_for_url(value, timeout=timeout)
            elif condition == "url_contains":
                await self._page.wait_for_function(
                    f"() => window.location.href.includes('{value}')", timeout=timeout)
            elif condition == "url_changes":
                await self._page.wait_for_function(
                    f"() => window.location.href !== '{init_url}'", timeout=timeout)
            elif condition == "text_appears":
                await self._page.wait_for_function(
                    f"() => document.body.innerText.toLowerCase().includes('{value.lower()}')", timeout=timeout)
            elif condition == "text_disappears":
                await self._page.wait_for_function(
                    f"() => !document.body.innerText.toLowerCase().includes('{value.lower()}')", timeout=timeout)
            elif condition == "selector_visible":
                await self._page.wait_for_selector(value, state="visible", timeout=timeout)
            elif condition == "selector_hidden":
                await self._page.wait_for_selector(value, state="hidden", timeout=timeout)
            elif condition == "selector_count_gte":
                count_str, sel = value.split("|", 1)
                await self._page.wait_for_function(
                    f"() => document.querySelectorAll('{sel}').length >= {int(count_str)}", timeout=timeout)
            elif condition == "network_idle":
                await self._page.wait_for_load_state("networkidle", timeout=timeout)
            elif condition == "dom_stable":
                await self.wait_for_stable(timeout)
            else:
                return {
                    "ok": False, "error": f"未知条件: {condition}",
                    "ai_hint": ("支持条件：url_is / url_contains / url_changes / "
                                "text_appears / text_disappears / selector_visible / "
                                "selector_hidden / selector_count_gte / network_idle / dom_stable")
                }
            waited = int((_time.monotonic() - t0) * 1000)
            emit({"type": "smart_wait", "condition": condition, "ok": True})
            return {"ok": True, "condition": condition, "value": value, "waited_ms": waited, "current_url": self._page.url}
        except Exception as e:
            summary = None
            try:
                summary = await self.get_page_summary()
            except Exception:
                pass
            return {
                "ok": False, "error": str(e).split(chr(10))[0],
                "condition": condition, "value": value, "current_url": self._page.url,
                "page_summary": summary,
                "ai_hint": (
                    f"等待条件 '{condition}={value}' 在 {timeout}ms 内未满足。"
                    "当前页面摘要在 page_summary 字段。建议："
                    "1) 增大 timeout；2) 用 get_page_summary 确认页面状态；"
                    "3) 改用 watch_for_change 同时监听多个条件"
                )
            }

    async def drag_drop(self, source_selector: str, target_selector: str = None,
                         target_x: float = None, target_y: float = None, steps: int = 15) -> dict:
        """
        模拟人工拖拽操作（mouse.down + move steps + mouse.up）。
        支持：source元素 → target元素，或 source元素 → 坐标(x,y)。
        适用：看板拖拽、排序列表、文件拖拽上传区域（注意：file input 用 set_input_files 更直接）。
        兜底：元素不可见时返回 bbox 建议 + 截图 + 备选方案说明。
        """
        try:
            src_loc = self._page.locator(source_selector)
            if not await src_loc.count():
                return {"ok": False, "error": f"源元素 '{source_selector}' 不存在",
                        "ai_hint": "源选择器无效，请先用 get_interactive_elements 确认元素存在"}
            src_box = await src_loc.bounding_box()
            if not src_box:
                return {"ok": False, "error": f"源元素 '{source_selector}' 不可见（无 BBox）",
                        "ai_hint": "元素存在但不可见，请先 scroll_until_found 或 wait_for_stable 等待渲染"}
            sx = src_box["x"] + src_box["width"] / 2
            sy = src_box["y"] + src_box["height"] / 2
        except Exception as e:
            return {"ok": False, "error": str(e).split(chr(10))[0],
                    "ai_hint": "获取源元素失败，请检查选择器是否正确"}

        if target_selector:
            try:
                tgt_loc = self._page.locator(target_selector)
                tgt_box = await tgt_loc.bounding_box()
                if not tgt_box:
                    return {"ok": False, "error": f"目标元素 '{target_selector}' 不可见",
                            "ai_hint": "目标元素不可见，可能需要先滚动到目标位置（scroll_until_found）"}
                tx = tgt_box["x"] + tgt_box["width"] / 2
                ty = tgt_box["y"] + tgt_box["height"] / 2
            except Exception as e:
                return {"ok": False, "error": str(e).split(chr(10))[0]}
        elif target_x is not None and target_y is not None:
            tx, ty = float(target_x), float(target_y)
        else:
            return {"ok": False, "error": "必须提供 target_selector 或 target_x+target_y",
                    "ai_hint": "drag_drop 需要目标位置：target_selector（CSS选择器）或 target_x/target_y（页面坐标）"}

        try:
            mouse = self._page.mouse
            await mouse.move(sx, sy)
            await mouse.down()
            import asyncio as _asyncio
            for step in range(1, steps + 1):
                nx = sx + (tx - sx) * step / steps
                ny = sy + (ty - sy) * step / steps
                await mouse.move(nx, ny)
                await _asyncio.sleep(0.008)
            await mouse.up()
            emit({"type": "drag_drop", "from": source_selector, "ok": True})
            return {
                "ok": True,
                "from": {"selector": source_selector, "x": round(sx), "y": round(sy)},
                "to": {"selector": target_selector, "x": round(tx), "y": round(ty)},
            }
        except Exception as e:
            return {
                "ok": False, "error": str(e).split(chr(10))[0],
                "ai_hint": (
                    "拖拽执行失败。备选方案：1) 若为文件上传，改用 set_input_files 命令；"
                    "2) 尝试 eval_js 触发 dragstart/drop 事件；3) 检查页面是否有拖拽权限限制"
                )
            }

    async def clip_screenshot(self, selector: str, path: str = None, padding: int = 8) -> dict:
        """
        只截取指定元素区域的截图（非全页），大幅减少 Token 消耗（通常 5~15% 全页大小）。
        适用：只需看某个错误消息框、图表、Modal 的具体内容时。
        兜底：
          - 元素不存在 → 返回错误 + 建议用 get_page_summary 定位
          - 截图失败 → 降级到全页截图并告知 AI
        """
        try:
            loc = self._page.locator(selector)
            cnt = await loc.count()
            if cnt == 0:
                return {
                    "ok": False, "error": f"元素 '{selector}' 不存在",
                    "ai_hint": "选择器无效或元素未加载。建议：wait_for_stable 后重试，或用 get_page_summary 确认元素是否存在"
                }
            if not path:
                path = os.path.join("temp", f"clip_{int(time.time()*1000)}.png")
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            await loc.first.screenshot(path=path)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            file_size_kb = os.path.getsize(path) // 1024
            emit({"type": "clip_screenshot", "selector": selector, "size_kb": file_size_kb, "ok": True})
            return {
                "ok": True, "selector": selector, "path": path,
                "screenshot_base64": b64, "size_kb": file_size_kb,
                "ai_hint": f"元素 '{selector}' 截图完成（{file_size_kb}KB）。可直接将 screenshot_base64 发送给视觉模型分析。"
            }
        except Exception as e:
            # 兜底：全页截图
            fallback_b64 = None
            try:
                shot = await self._page.screenshot(type="jpeg", quality=70)
                fallback_b64 = base64.b64encode(shot).decode()
            except Exception:
                pass
            return {
                "ok": False, "error": str(e).split(chr(10))[0],
                "fallback_screenshot_base64": fallback_b64,
                "ai_hint": f"元素截图失败，已提供全页截图作为兜底（fallback_screenshot_base64）。原因：{str(e).split(chr(10))[0]}"
            }
