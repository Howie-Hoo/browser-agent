#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器快捷检验 (Browser Quick Check)
通过浏览器代理服务执行预设检验套件。

用法：
  python -m browser_agent.run_check [类型] [--url=http://...] [--port=7788] [--headless]

类型：
  full      全页截图（默认）
  panels    左右半屏截图
  dom       DOM 结构报告
  perf      性能报告
  a11y      可访问性检查
  interact  基础交互检验
  all       全量检验
"""
import sys
import os
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .proxy import BrowserProxy


def _ts() -> str:
    return datetime.now().strftime("%H%M%S")


# ─────────────────────────────────────────────────────────
# 检验函数（由 server.py 调用）
# ─────────────────────────────────────────────────────────

async def check_full_screenshot(proxy: BrowserProxy, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"full_{_ts()}.png")
    await proxy.screenshot(path=path, full_page=True)
    return path


async def check_panels(proxy: BrowserProxy, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    vp = proxy.viewport
    w, h, mid = vp["width"], vp["height"], vp["width"] // 2
    paths = {}

    p = os.path.join(out_dir, f"left_{_ts()}.png")
    await proxy.screenshot(path=p, clip={"x": 0, "y": 0, "width": mid, "height": h})
    paths["left"] = p

    p = os.path.join(out_dir, f"right_{_ts()}.png")
    await proxy.screenshot(path=p, clip={"x": mid, "y": 0, "width": w - mid, "height": h})
    paths["right"] = p
    return paths


async def check_dom(proxy: BrowserProxy) -> dict:
    checks = [
        ("title",     "h1, h2",           "标题元素"),
        ("buttons",   "button",           "按钮"),
        ("inputs",    "input",            "输入框"),
        ("textareas", "textarea",         "文本域"),
        ("selects",   "select",           "下拉框"),
        ("links",     "a",                "链接"),
        ("images",    "img",              "图片"),
        ("forms",     "form",             "表单"),
        ("grids",     "[class*='grid']",  "网格容器"),
        ("modals",    "[role='dialog']",  "弹窗/对话框"),
    ]
    report = []
    for key, selector, label in checks:
        try:
            res = await proxy.get_count(selector)
            report.append({"key": key, "label": label, "count": res["count"], "ok": True})
        except Exception as e:
            report.append({"key": key, "label": label, "count": 0, "error": str(e), "ok": False})
    return {"dom_report": report}


async def check_perf(proxy: BrowserProxy) -> dict:
    res = await proxy.eval_js("""(() => {
        const t = performance.getEntriesByType('navigation')[0] || {};
        const r = performance.getEntriesByType('resource');
        return {
            domContentLoaded: Math.round(t.domContentLoadedEventEnd || 0),
            loadComplete: Math.round(t.loadEventEnd || 0),
            totalResources: r.length,
            jsResources: r.filter(x => x.initiatorType === 'script').length,
            cssResources: r.filter(x => x.initiatorType === 'link').length,
            imageResources: r.filter(x => x.initiatorType === 'img').length,
        };
    })()""")
    return {"perf_report": res.get("value", {})}


async def check_a11y(proxy: BrowserProxy) -> dict:
    res = await proxy.eval_js("""(() => {
        const issues = [];
        document.querySelectorAll('img').forEach(img => {
            if (!img.alt && !img.getAttribute('aria-label'))
                issues.push({type: 'missing_alt', element: img.outerHTML.slice(0, 80)});
        });
        document.querySelectorAll('button').forEach(btn => {
            if (!btn.textContent.trim() && !btn.getAttribute('aria-label'))
                issues.push({type: 'empty_button', element: btn.outerHTML.slice(0, 80)});
        });
        document.querySelectorAll('input[type!=hidden]').forEach(inp => {
            const id = inp.id;
            if (id && !document.querySelector('label[for="' + id + '"]')
                && !inp.getAttribute('aria-label') && !inp.placeholder)
                issues.push({type: 'unlabeled_input', element: inp.outerHTML.slice(0, 80)});
        });
        return {issueCount: issues.length, issues: issues.slice(0, 10)};
    })()""")
    return {"a11y_report": res.get("value", {})}


async def check_interact(proxy: BrowserProxy) -> dict:
    items = []

    async def ok(step, data=None):
        items.append({"step": step, "ok": True, "data": data})

    async def fail(step, reason):
        items.append({"step": step, "ok": False, "reason": reason})

    try:
        t = await proxy.get_title()
        await ok("页面标题存在", {"title": t["title"]})
    except Exception as e:
        await fail("页面可访问性", str(e))

    try:
        c = await proxy.get_count("button")
        await ok("按钮渲染", {"count": c["count"]})
    except Exception as e:
        await fail("按钮渲染", str(e))

    try:
        ec = await proxy.eval_js("window.__errors ? window.__errors.length : 0")
        if ec["value"] == 0:
            await ok("无 JS 运行时错误")
        else:
            await fail("JS 错误", f"共 {ec['value']} 个错误")
    except Exception:
        await ok("JS 错误检测（跳过）")

    try:
        vis = await proxy.is_visible("input:not([type=hidden])")
        if vis["visible"]:
            await proxy.click("input:not([type=hidden])")
            await ok("输入框可点击")
        else:
            await ok("输入框不存在（跳过）")
    except Exception as e:
        await fail("输入框交互", str(e))

    try:
        await proxy.scroll(y=300)
        await proxy.wait_ms(300)
        await proxy.scroll(y=-300)
        await ok("页面滚动")
    except Exception as e:
        await fail("页面滚动", str(e))

    passed = sum(1 for i in items if i["ok"])
    return {"interact_report": {"items": items, "passed": passed, "total": len(items)},
            "passed": passed, "total": len(items)}


# ─────────────────────────────────────────────────────────
# CLI 入口（通过服务执行）
# ─────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    check_type, port, url, headless = "full", 7788, "http://localhost:3000", False

    for a in args:
        if a == "--headless":
            headless = True
        elif a.startswith("--port="):
            port = int(a.split("=")[1])
        elif a.startswith("--url="):
            url = a.split("=", 1)[1]
        elif a.startswith("http://") or a.startswith("https://"):
            url = a
        elif not a.startswith("-"):
            check_type = a

    from .client import BrowserAgentClient
    with BrowserAgentClient(port=port, base_url=url, headless=headless) as client:
        result = client.check(check_type)
        if not result.get("ok"):
            print(f"[错误] {result.get('error', '检验失败')}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
