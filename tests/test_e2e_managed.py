"""E2E 回归标的（managed 模式）— 覆盖文档化命令面的真实浏览器全链路。

运行（需要浏览器环境）：
    RUN_E2E=1 pytest tests/test_e2e_managed.py -v
不打 RUN_E2E 时自动跳过（CI / 无浏览器环境不炸）。
覆盖面对照 docs/command-reference.md：导航/点击/表单/等待/DOM/感知/截图/
标签页/iframe/持久化/profile/断言/宏/check 套件/会话与守卫/站点经验/排队。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(not os.getenv("RUN_E2E"), reason="需要 RUN_E2E=1（真实浏览器）")

from e2e_helpers import (Service, fresh_home, cleanup_home, start_page_server,  # noqa: E402
                         ok_step, wait_active_batches)

PAGE_PORT = 18701
SVC_PORT = 18702


@pytest.fixture(scope="module")
def pages():
    srv = start_page_server(PAGE_PORT)
    yield f"http://127.0.0.1:{PAGE_PORT}"
    srv.shutdown()


@pytest.fixture(scope="module")
def svc():
    home = fresh_home()
    s = Service(SVC_PORT, home, extra_args=[f"--url=http://127.0.0.1:{PAGE_PORT}"])
    s.start()
    yield s
    s.shutdown()
    cleanup_home(home)


# ── 导航与页面 ────────────────────────────────────────────

def test_navigate_attaches_summary(svc, pages):
    r = svc.run([{"action": "navigate", "url": pages}])
    d = ok_step(r, 0)
    assert "E2E 标的页" in d.get("title", "") or d.get("title") == "E2E 标的页"
    assert "page_summary" in d
    r2 = svc.run([{"action": "get_title"}, {"action": "get_url"}, {"action": "reload"}])
    assert r2["ok"]


# ── 点击（精确 → 语义）与自愈 ─────────────────────────────

def test_click_flavors_and_counter(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "click", "selector": "#counter"},
        {"action": "click", "selector": "#counter"},
        {"action": "get_text", "selector": "#count"},
        {"action": "click_text", "text": "语义目标按钮"},
        {"action": "find_and_act", "description": "语义目标按钮", "act": "click"},
        # 选择器失效触发文字自愈（css 用 has-text 包装）
        {"action": "click", "selector": "button:has-text('计数')"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 2)["text"] == "2"


# ── 输入与表单 ────────────────────────────────────────────

def test_form_analyze_fill_select_check(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "form_analyze", "form_selector": "#signup"},
        {"action": "fill_form", "fields": {"#phone": "13800138000", "#pwd": "s3cret"}},
        {"action": "type", "selector": "#phone", "text": "13900139000", "clear_first": True},
        {"action": "select", "selector": "#city", "value": "bj"},
        {"action": "check", "selector": "#agree"},
        {"action": "get_value", "selector": "#phone"},
        {"action": "is_checked", "selector": "#agree"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    fa = ok_step(r, 0)
    assert fa.get("fill_guide") or fa.get("fields") or fa.get("form")
    assert ok_step(r, 5)["value"] == "13900139000"
    assert ok_step(r, 6)["checked"] is True


# ── 等待家族 ──────────────────────────────────────────────

def test_wait_family(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "wait_for", "selector": "#lazy", "state": "attached"},
        {"action": "smart_wait", "condition": "text_appears", "value": "异步内容出现", "timeout": 5000},
        {"action": "wait_for_text", "text": "异步内容出现"},
        {"action": "get_text", "selector": "#lazy"},
        {"action": "wait_ms", "ms": 50},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 3)["text"] == "异步内容出现"


def test_watch_for_change_on_navigation(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "click", "selector": "#next"},
        {"action": "watch_for_change", "timeout": 8000,
         "conditions": [{"type": "url_contains", "value": "/second"}]},
        {"action": "assert_url", "expected": "/second"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]


# ── DOM 读取 ──────────────────────────────────────────────

def test_dom_reads(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "get_count", "selector": "#items li"},
        {"action": "get_attr", "selector": "#next", "attr": "href"},
        {"action": "get_html", "selector": "#count"},
        {"action": "is_visible", "selector": "#counter"},
        {"action": "is_enabled", "selector": "#counter"},
    ])
    assert r["ok"]
    assert ok_step(r, 0)["count"] == 5
    assert ok_step(r, 1)["value"].endswith("/second")


# ── 高级感知 ──────────────────────────────────────────────

def test_perception_layer(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "get_a11y_tree"},
        {"action": "get_interactive_elements", "limit": 20},
        {"action": "get_page_summary"},
        {"action": "snapshot_dom"},
        {"action": "get_dom_diff"},
        {"action": "extract", "schema": {"title": "#main-title", "count": "#count"}},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    a11y = ok_step(r, 0)
    assert a11y.get("tree") or a11y.get("nodes") or a11y
    ex = ok_step(r, 5)
    data = ex.get("data") or ex
    assert "标的" in str(data.get("title", ""))


# ── 截图 ──────────────────────────────────────────────────

def test_screenshots(svc, pages, tmp_path):
    svc.run([{"action": "navigate", "url": pages}])
    p1 = str(tmp_path / "full.png")
    p2 = str(tmp_path / "clip.png")
    r = svc.run([
        {"action": "screenshot", "path": p1, "full_page": True},
        {"action": "clip_screenshot", "selector": "#signup", "path": p2},
        {"action": "screenshot", "selector": "#counter"},
    ])
    assert r["ok"]
    assert os.path.isfile(p1) and os.path.getsize(p1) > 1000
    assert os.path.isfile(p2)


# ── 多标签页（多会话同名不撞键）──────────────────────────

def test_tabs_across_sessions(svc, pages):
    for sid in ("e2e-a", "e2e-b"):
        r = svc.run([
            {"action": "new_tab", "name": "work"},
            {"action": "navigate", "url": pages},
            {"action": "switch_tab", "name": "work"},
            {"action": "close_tab", "name": "work"},
        ], sid=sid)
        assert r["ok"], (sid, [s.get("error") for s in r["steps"] if not s["ok"]])
    svc.post("/session/stop", {"session_id": "e2e-a"})
    svc.post("/session/stop", {"session_id": "e2e-b"})


# ── iframe ────────────────────────────────────────────────

def test_iframe_switch(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "switch_frame", "selector": "iframe#embed"},
        {"action": "get_text", "selector": "#iframe-title"},
        {"action": "click", "selector": "#if-btn"},
        {"action": "get_text", "selector": "#if-btn"},
        {"action": "switch_to_main_frame"},
        {"action": "get_text", "selector": "#main-title"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert "iframe 内部标题" in ok_step(r, 1)["text"]
    assert "已点" in ok_step(r, 3)["text"]
    assert "标的" in ok_step(r, 5)["text"]


# ── 状态持久化（落用户数据目录）──────────────────────────

def test_storage_persistence_to_user_home(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "save_cookies"},
        {"action": "load_cookies"},
        {"action": "save_storage"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    home = svc.get("/status")["user_data_dir"]
    assert os.path.isfile(os.path.join(home, "cookies.json"))
    assert os.path.isfile(os.path.join(home, "state.json"))


def test_clear_storage_allowed_in_isolated_session(svc, pages):
    r = svc.post("/session/start", {"session_id": "iso-1"})
    assert r["ok"]
    svc.run([{"action": "navigate", "url": pages}], sid="iso-1")
    r2 = svc.run([{"action": "clear_storage"}], sid="iso-1")
    assert r2["ok"], r2["steps"][0]
    svc.post("/session/stop", {"session_id": "iso-1"})


# ── 认证 profile 全链路 ──────────────────────────────────

def test_profile_roundtrip(svc, pages):
    r = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "save_profile", "name": "e2e-prof"},
        {"action": "has_profile", "name": "e2e-prof"},
        {"action": "list_profiles"},
        {"action": "load_profile", "name": "e2e-prof", "into": "new", "navigate": False},
        {"action": "delete_profile", "name": "e2e-prof"},
        {"action": "list_profiles"},
    ], sid="prof-e2e")
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 2)["exists"] is True
    assert ok_step(r, 3)["count"] >= 1
    assert ok_step(r, 6)["count"] == 0
    svc.post("/session/stop", {"session_id": "prof-e2e"})


# ── 站点经验闭环 ──────────────────────────────────────────

def test_site_patterns_closed_loop(svc, pages):
    host = "127.0.0.1"
    r = svc.run([
        {"action": "write_site", "domain": host, "patterns": ["测试页 /second 可直达"]},
        {"action": "read_site", "domain": pages},
        {"action": "navigate", "url": pages},
        {"action": "list_sites"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 1)["matched"] is True
    nav = r["steps"][2]["data"]
    assert nav.get("site_experience", {}).get("domain") == host
    assert any(p["domain"] == host for p in ok_step(r, 3)["patterns"])
    # 清理：该域经验删除
    r2 = svc.run([{"action": "delete_site", "domain": host}])
    assert r2["ok"]


# ── 断言 ──────────────────────────────────────────────────

def test_assertions(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "assert_text", "selector": "#main-title", "expected": "标的"},
        {"action": "assert_url", "expected": "127.0.0.1"},
        {"action": "assert_visible", "selector": "#counter"},
        {"action": "fill", "selector": "#phone", "value": "13800000000"},
        {"action": "assert_value", "selector": "#phone", "expected": "13800000000"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]


# ── 宏 ────────────────────────────────────────────────────

def test_macros(svc, pages):
    assert svc.post("/macro/save", {"name": "e2e-macro", "commands": [
        {"action": "navigate", "url": pages},
        {"action": "get_title"},
    ]})["ok"]
    r = svc.post("/macro/run", {"name": "e2e-macro"})
    assert r["ok"], r.get("summary")


# ── check 套件 ────────────────────────────────────────────

def test_check_suites(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    for t in ("dom", "a11y"):
        r = svc.post("/check", {"type": t})
        assert r["ok"], (t, r.get("summary"))


# ── 会话模型与守卫 ────────────────────────────────────────

def test_user_session_and_guards(svc, pages):
    r = svc.post("/session/start", {"session_id": "shared-e2e", "context": "user"})
    assert r["ok"] and r["context_mode"] == "user"
    svc.run([{"action": "navigate", "url": pages}], sid="shared-e2e")
    # default 会话 + shared-e2e 共享 default context → 清除类操作被拒
    refusal = svc.post("/session/refresh", {"session_id": "shared-e2e"})
    assert refusal["ok"] is False and "共享" in refusal.get("error", "")
    r2 = svc.run([{"action": "clear_storage"}], sid="shared-e2e")
    assert r2["ok"] is False
    # isolated 会话不受影响
    svc.post("/session/start", {"session_id": "iso-guard", "context": "isolated"})
    r3 = svc.run([{"action": "get_title"}], sid="iso-guard")
    assert r3["ok"]
    # 关闭只影响自己
    stop = svc.post("/session/stop", {"session_id": "shared-e2e"})
    assert stop["ok"] and "browser_shutdown" not in stop
    svc.post("/session/stop", {"session_id": "iso-guard"})


def test_status_fields(svc):
    st = svc.get("/status")
    for key in ("browser_mode", "site_patterns", "user_data_dir", "sessions",
                "queue_timeout_sec", "idle_shutdown_sec", "version"):
        assert key in st, key
    assert st["browser_mode"] == "managed"


# ── 并行模型：跨会话真并行 / 同会话串行 ─────────────────

def test_parallel_batches_across_sessions(svc, pages):
    """两会话的长批并发执行：飞行中 active_batches ≥ 2（结构性并行证明），
    各自命令落在各自的页；墙钟只作宽松下限（不受系统瞬时负载影响的确定性判据）。"""
    import threading, time as _time
    results = {}

    def _batch(sid, page_url):
        results[sid] = svc.run([
            {"action": "navigate", "url": page_url},
            {"action": "wait_ms", "ms": 1500},
            # 关键：等待期间另一会话在跑——之后读取的仍是本会话的页
            {"action": "get_text", "selector": "h1"},
        ], sid=sid)

    t0 = _time.monotonic()
    observed = {}
    obs = threading.Thread(target=lambda: observed.update(n=wait_active_batches(lambda: svc.get("/status"), n=2)),
                           daemon=True)
    obs.start()
    t1 = threading.Thread(target=_batch, args=("par-a", pages))
    t2 = threading.Thread(target=_batch, args=("par-b", pages + "/second"))
    t1.start(); t2.start(); t1.join(); t2.join()
    elapsed = _time.monotonic() - t0
    assert results["par-a"]["ok"] and results["par-b"]["ok"], \
        [ (k, [s.get("error") for s in v.get("steps", []) if not s.get("ok")]) for k, v in results.items() ]
    # 命中各自页面（隔离：等待后没被另一会话切走）
    text_a = next(s for s in results["par-a"]["steps"] if s["action"] == "get_text")["data"]["text"]
    text_b = next(s for s in results["par-b"]["steps"] if s["action"] == "get_text")["data"]["text"]
    assert "标的" in text_a and "第二页" in text_b
    # 确定性判据：飞行中同时存在 ≥2 个批（真并行）；墙钟宽松下限防倒退回串行
    obs.join(timeout=8)
    assert observed.get("n", 0) >= 2, "未观测到两批同时执行（退化为串行）"
    assert elapsed < 3.2, f"两批并发耗时 {elapsed:.2f}s——明显串行化"
    svc.post("/session/stop", {"session_id": "par-a"})
    svc.post("/session/stop", {"session_id": "par-b"})


def test_same_session_batches_serialize(svc, pages):
    """同一会话的两个批串行：后批等待前批（页面状态有序变更）。"""
    import threading, time as _time
    results = {}
    order = []

    def _batch(tag):
        results[tag] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "wait_ms", "ms": 600},
            {"action": "eval_js", "script": f"({tag!r})"},
        ], sid="serial-1")
        order.append(tag)

    t1 = threading.Thread(target=_batch, args=("first",))
    t2 = threading.Thread(target=_batch, args=("second",))
    t1.start()
    import time as _sleep; _sleep.sleep(0.4)  # 保证 first 先到达拿锁（同会话串行）
    t2.start(); t1.join(); t2.join()
    assert results["first"]["ok"] and results["second"]["ok"]
    assert order == ["first", "second"], "同会话批必须按到达顺序串行"
    svc.post("/session/stop", {"session_id": "serial-1"})


def test_tab_isolation_between_concurrent_sessions(svc, pages):
    """会话 A 切换/新建标签页，不影响会话 B 正在操作的页。"""
    import threading
    results = {}

    def _a():
        results["a"] = svc.run([
            {"action": "new_tab", "name": "extra"},
            {"action": "navigate", "url": pages + "/second"},
            {"action": "switch_tab", "name": "extra"},
            {"action": "get_text", "selector": "h1"},
        ], sid="iso-a")

    def _b():
        results["b"] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "wait_ms", "ms": 300},
            {"action": "get_text", "selector": "h1"},
        ], sid="iso-b")

    t1 = threading.Thread(target=_a); t2 = threading.Thread(target=_b)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results["a"]["ok"] and results["b"]["ok"], \
        [(k, [s.get("error") for s in v.get("steps", []) if not s.get("ok")]) for k, v in results.items()]
    text_b = next(s for s in results["b"]["steps"] if s["action"] == "get_text")["data"]["text"]
    assert "标的" in text_b, "会话 B 的页面被会话 A 的切页操作污染"
    svc.post("/session/stop", {"session_id": "iso-a"})
    svc.post("/session/stop", {"session_id": "iso-b"})


# ── eval_js / 媒体模式 ────────────────────────────────────

def test_eval_js_and_dom_hidden_content(svc, pages):
    svc.run([{"action": "navigate", "url": pages}])
    r = svc.run([
        {"action": "eval_js", "script": "document.querySelectorAll('#items li').length"},
        {"action": "eval_js", "script": "() => document.title", "selector": None},
        {"action": "eval_js", "script": "el => el.tagName", "selector": "#count"},
    ])
    assert r["ok"], [s.get("error") for s in r["steps"] if not s["ok"]]
    assert ok_step(r, 0)["value"] == 5
    assert ok_step(r, 2)["value"].lower() == "span"


# ── 并发专项：旧全局锁/全局 active_tab 模型的致命场景 ──────

def test_concurrent_watch_survives_churn(svc, pages):
    """杀手场景：会话 A 点击后 watch 页面变化，期间会话 B 高频导航/切页——
    A 的 watch 与后续读取必须仍落在 A 自己的页上。"""
    import threading
    results = {}

    def _a():
        results["a"] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "click", "selector": "#next"},
            {"action": "watch_for_change", "timeout": 6000,
             "conditions": [{"type": "url_contains", "value": "/second"}]},
            {"action": "get_url"},
            {"action": "get_text", "selector": "#second-title"},
        ], sid="watch-a")

    def _b():
        for _ in range(4):
            svc.run([
                {"action": "navigate", "url": pages + "/second"},
                {"action": "new_tab", "name": "churn"},
                {"action": "switch_tab", "name": "churn"},
                {"action": "close_tab", "name": "churn"},
            ], sid="churn-b")
        results["b"] = {"done": True}

    t1 = threading.Thread(target=_a); t2 = threading.Thread(target=_b)
    t1.start(); t2.start(); t1.join(); t2.join()
    a = results["a"]
    assert a["ok"], [s.get("error") for s in a["steps"] if not s["ok"]]
    url = next(s for s in a["steps"] if s["action"] == "get_url")["data"]["url"]
    text = next(s for s in a["steps"] if s["action"] == "get_text")["data"]["text"]
    assert "/second" in url, f"A 的 watch/读取被 B 的并发操作污染: {url}"
    assert "第二页" in text
    svc.post("/session/stop", {"session_id": "watch-a"})
    svc.post("/session/stop", {"session_id": "churn-b"})


def test_concurrent_perception_cache_isolation(svc, pages):
    """两会话并发调用 get_interactive_elements：各自命中各自的缓存与数量。"""
    import threading
    results = {}

    def _hit(sid, url):
        r = svc.run([
            {"action": "navigate", "url": url},
            {"action": "get_interactive_elements"},
            {"action": "get_interactive_elements"},  # 第二次应命中本页缓存
        ], sid=sid)
        results[sid] = r

    t1 = threading.Thread(target=_hit, args=("pc-a", pages))
    t2 = threading.Thread(target=_hit, args=("pc-b", pages + "/second"))
    t1.start(); t2.start(); t1.join(); t2.join()
    for sid, expect_gt in (("pc-a", 4), ("pc-b", -1)):
        r = results[sid]
        assert r["ok"], (sid, [s.get("error") for s in r["steps"] if not s["ok"]])
        steps = [s for s in r["steps"] if s["action"] == "get_interactive_elements"]
        first, second = steps[0]["data"], steps[1]["data"]
        assert second.get("cached") is True, f"{sid} 第二次读取未命中缓存"
        assert first["total"] == second["total"]
        if expect_gt > 0:
            assert first["total"] >= expect_gt
    # 主页元素多于第二页——数量确实来自各自的页
    a_total = [s for s in results["pc-a"]["steps"] if s["action"] == "get_interactive_elements"][0]["data"]["total"]
    b_total = [s for s in results["pc-b"]["steps"] if s["action"] == "get_interactive_elements"][0]["data"]["total"]
    assert a_total > b_total
    svc.post("/session/stop", {"session_id": "pc-a"})
    svc.post("/session/stop", {"session_id": "pc-b"})


def test_concurrent_iframe_isolation(svc, pages):
    """A 进 iframe 操作，B 并发读主页——各自的帧上下文互不串。"""
    import threading
    results = {}

    def _a():
        results["a"] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "switch_frame", "selector": "iframe#embed"},
            {"action": "wait_ms", "ms": 400},
            {"action": "get_text", "selector": "#iframe-title"},
            {"action": "switch_to_main_frame"},
        ], sid="fr-a")

    def _b():
        results["b"] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "wait_ms", "ms": 200},
            {"action": "get_text", "selector": "#main-title"},
        ], sid="fr-b")

    t1 = threading.Thread(target=_a); t2 = threading.Thread(target=_b)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results["a"]["ok"] and results["b"]["ok"], \
        [(k, [s.get("error") for s in v.get("steps", []) if not s.get("ok")]) for k, v in results.items()]
    text_a = next(s for s in results["a"]["steps"] if s["action"] == "get_text")["data"]["text"]
    text_b = next(s for s in results["b"]["steps"] if s["action"] == "get_text")["data"]["text"]
    assert "iframe 内部标题" in text_a, "A 的帧上下文被 B 污染"
    assert "标的" in text_b
    svc.post("/session/stop", {"session_id": "fr-a"})
    svc.post("/session/stop", {"session_id": "fr-b"})


def test_control_op_serializes_with_batch(svc, pages):
    """控制操作与会话批的正确互斥：不插队（等批完成或忙时拒绝）、绝不破坏在途批。

    4s 批 < 10s 控制时限 → stop 等待后成功（本测试验证此路径 + 批完好）；
    忙时拒绝路径由单元测试覆盖（锁超时语义）。
    """
    import threading, time as _t
    done = {}

    def _long():
        done["batch"] = svc.run([
            {"action": "navigate", "url": pages},
            {"action": "wait_ms", "ms": 4000},
        ], sid="busy-1")

    t = threading.Thread(target=_long); t.start()
    _t.sleep(1.0)  # 确保批已在执行
    st = svc.get("/status")
    assert st["active_batches"] >= 1, "in-flight 计数未反映正在执行的批"
    t0 = _t.monotonic()
    stop = svc.post("/session/stop", {"session_id": "busy-1"})
    stop_elapsed = _t.monotonic() - t0
    t.join()
    # 批完好——控制操作绝不腰斩在途批
    assert done["batch"]["ok"], "在途批被控制操作破坏"
    # stop 要么等批结束后成功（不插队），要么忙时拒绝；二者必居其一且都不许是"立即插队成功"
    if stop["ok"]:
        assert stop_elapsed >= 2.0, f"stop 在 {stop_elapsed:.2f}s 内完成——插队了（应在批结束后才拿到锁）"
    else:
        assert "忙" in stop.get("error", ""), stop
        svc.post("/session/stop", {"session_id": "busy-1"})
    assert svc.get("/status")["active_batches"] == 0


def test_active_tab_sync_across_batches(svc, pages):
    """批 1 里 new_tab 后不切回——该会话下个批从新页继续；空白页触发 auto-navigate。"""
    svc.post("/session/start", {"session_id": "sync-1"})
    r1 = svc.run([
        {"action": "navigate", "url": pages},
        {"action": "new_tab", "name": "leftover"},   # 新页成为会话落点（about:blank）
    ], sid="sync-1")
    assert r1["ok"]
    # 批 2 不带 navigate：auto-navigate 应作用于**新页**（锁内重钉的验证）
    r2 = svc.run([{"action": "get_url"}], sid="sync-1")
    assert r2["ok"], r2["steps"][0]
    assert r2["steps"][0]["data"]["url"].startswith("http"), \
        f"新页未被 auto-navigate（读到 {r2['steps'][0]['data'].get('url')}）"
    svc.post("/session/stop", {"session_id": "sync-1"})


def test_concurrent_site_writes(svc, pages):
    """并发 20 个 write_site 同域名：全部成功、无损坏、条目数正确（写锁验证）。"""
    import threading
    outs = []

    def _w(i):
        outs.append(svc.post("/site", {"domain": "parallel.test",
                                      "patterns": [f"并发条目 {i}"]}))

    threads = [threading.Thread(target=_w, args=(i,)) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(o["ok"] for o in outs), [o for o in outs if not o["ok"]][:2]
    r = svc.run([{"action": "read_site", "domain": "parallel.test"}])
    content = next(s for s in r["steps"] if s["action"] == "read_site")["data"]["content"]
    assert content.count("并发条目") == 20, f"条目数 {content.count('并发条目')} ≠ 20（写入丢失/损坏）"
    assert svc.run([{"action": "delete_site", "domain": "parallel.test"}])["ok"]


def test_shutdown_drains_inflight_batch(pages):
    """关停排水：批执行中收到 shutdown → 批先完成（响应正常返回），服务随后退出。"""
    from e2e_helpers import Service, fresh_home, cleanup_home
    home = fresh_home("ba-drain-")
    s = Service(18771, home, extra_args=[f"--url={pages}"])
    s.start()
    import threading
    result = {}
    def _batch():
        result["r"] = s.run([
            {"action": "navigate", "url": pages},
            {"action": "wait_ms", "ms": 2000},
            {"action": "get_title"},
        ], sid="drain-1")
    t = threading.Thread(target=_batch); t.start()
    import time as _t; _t.sleep(0.8)
    s.post("/shutdown", {"instance_id": s.get("/status")["instance_id"]})
    t.join(timeout=30)
    assert result.get("r", {}).get("ok") is True, f"在途批被关停腰斩: {result}"
    try:
        s.proc.wait(timeout=10)
        exited = True
    except Exception:
        exited = False
        s.proc.kill()
    assert exited, "关停后进程未退出"
    cleanup_home(home)
