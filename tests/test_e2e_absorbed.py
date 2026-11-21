"""E2E 实战（managed 模式）— 本次能力升级的全链路真浏览器验证。

覆盖：click_xy 坐标命中/未命中、scroll direction、/status 实例身份、
maintenance 排水拒新批（确定性：用长批占住排水窗口）、页级空闲回收、
search_local 命令批（本机无浏览器目录时跳过）。

运行：RUN_E2E=1 pytest tests/test_e2e_absorbed.py -v
不打 RUN_E2E 自动跳过。绝不触碰真实用户数据：HOME 一律临时目录。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skipif(not os.getenv("RUN_E2E"), reason="需要 RUN_E2E=1（真实浏览器）")

from e2e_helpers import (Service, fresh_home, cleanup_home, start_page_server,  # noqa: E402
                         ok_step, wait_active_batches, launched_service)

PAGE_PORT = 18721
SVC_PORT = 18722
DRAIN_SVC_PORT = 18723
TABIDLE_SVC_PORT = 18724


@pytest.fixture(scope="module")
def pages():
    srv = start_page_server(PAGE_PORT)
    yield f"http://127.0.0.1:{PAGE_PORT}"
    srv.shutdown()


@pytest.fixture(scope="module")
def svc(pages):
    home = fresh_home("ba-absorbed-home-")
    s = Service(SVC_PORT, home, extra_args=[f"--url={pages}"])
    s.start()
    yield s
    s.shutdown()
    cleanup_home(home)


# ── click_xy：真实鼠标事件命中坐标 ────────────────────────

def test_click_xy_hits_target_center(svc, pages):
    svc.run([{"action": "navigate", "url": f"{pages}/xy"}], sid="xy")
    box = ok_step(svc.run([{"action": "get_bounding_box", "selector": "#xy-target"}],
                          sid="xy"), 0)["box"]
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    r = svc.run([
        {"action": "click_xy", "x": cx, "y": cy},
        {"action": "eval_js", "script":
            "({hits: parseInt(document.getElementById('hits').textContent),"
            " last: window.lastClick})"},
    ], sid="xy")
    d = ok_step(r, 1)
    assert d["value"]["hits"] == 1, "坐标点击应命中目标"
    assert abs(d["value"]["last"]["x"] - cx) <= 2
    assert abs(d["value"]["last"]["y"] - cy) <= 2


def test_click_xy_miss_outside_target(svc, pages):
    svc.run([{"action": "navigate", "url": f"{pages}/xy"}], sid="xy2")
    # 点目标区外（页面左上角空白）：不计数
    r = svc.run([
        {"action": "click_xy", "x": 5, "y": 5},
        {"action": "eval_js",
         "script": "parseInt(document.getElementById('hits').textContent)"},
    ], sid="xy2")
    assert ok_step(r, 1)["value"] == 0


def test_click_xy_double_click(svc, pages):
    svc.run([{"action": "navigate", "url": f"{pages}/xy"}], sid="xy3")
    box = ok_step(svc.run([{"action": "get_bounding_box", "selector": "#xy-target"}],
                          sid="xy3"), 0)["box"]
    r = svc.run([
        {"action": "click_xy", "x": box["x"] + box["width"] / 2,
         "y": box["y"] + box["height"] / 2, "click_count": 2},
        {"action": "eval_js",
         "script": "parseInt(document.getElementById('hits').textContent)"},
    ], sid="xy3")
    assert ok_step(r, 1)["value"] == 2


# ── scroll direction ──────────────────────────────────────

def test_scroll_direction_bottom_then_top(svc, pages):
    svc.run([{"action": "navigate", "url": f"{pages}/tall"}], sid="sc")
    r = svc.run([
        {"action": "scroll", "direction": "bottom"},
        {"action": "eval_js", "script": "window.scrollY"},
        {"action": "scroll", "direction": "top"},
        {"action": "eval_js", "script": "window.scrollY"},
    ], sid="sc")
    assert ok_step(r, 1)["value"] > 3000, "direction=bottom 应滚到页面底部"
    assert ok_step(r, 3)["value"] == 0, "direction=top 应回到顶部"


def test_scroll_direction_invalid_reported(svc, pages):
    r = svc.run([{"action": "navigate", "url": f"{pages}/tall"},
                 {"action": "scroll", "direction": "sideways"}], sid="sc2")
    step = r["steps"][1]
    assert not step["ok"] and "direction" in step.get("error", "")


# ── /status 实例身份 ──────────────────────────────────────

def test_status_instance_identity_fields(svc):
    s1 = svc.get("/status")
    s2 = svc.get("/status")
    for key in ("instance_id", "pid", "started_at", "revision",
                "maintenance", "tab_idle_sec"):
        assert key in s1, f"/status 缺 {key}"
    assert s1["instance_id"] == s2["instance_id"], "同一实例 instance_id 稳定"
    assert s1["maintenance"] is False
    assert s1["pid"] == svc.proc.pid, "pid 应为服务进程真实 pid"
    # revision 与测试进程按同一算法计算的一致（同一份源码）
    import browser_agent.server as bs
    assert s1["revision"] == bs._RUNNING_REVISION


# ── maintenance：排水期拒新批（确定性构造）────────────────

def test_maintenance_rejects_new_batches_during_drain(pages):
    with launched_service(DRAIN_SVC_PORT, pages, prefix="ba-drain-") as s:
        # 长批占住排水窗口：watch_for_change 等 5s 必然超时返回
        bg = {}

        def _long_batch():
            bg["r"] = s.run([{"action": "watch_for_change", "timeout": 5000,
                              "conditions": [
                                  {"type": "url_contains", "value": "__never__"}]}],
                            sid="drain")

        t = threading.Thread(target=_long_batch)
        t.start()
        assert wait_active_batches(lambda: s.get("/status"), 1,
                                   timeout=8) >= 1, "长批应进入执行"

        sh = s.post("/shutdown")
        assert sh["ok"] and sh.get("instance_id")

        # 排水必然进行中（长批未结束）→ 新批被拒
        r2 = s.run([{"action": "get_title"}], sid="drain2")
        assert r2["ok"] is False, "排水期不应接收新命令批"
        assert r2.get("maintenance") is True and "关停" in r2["error"]

        t.join(timeout=20)
        assert bg["r"] is not None, "在途长批应正常收尾（排水语义）"
        # 进程在长批结束后优雅退出（无需 kill）
        s.proc.wait(timeout=20)


# ── 页级空闲回收（事件驱动：批完成即扫描）─────────────────

def test_tab_idle_recycling_closes_stale_keeps_active(pages):
    with launched_service(TABIDLE_SVC_PORT, pages, prefix="ba-tabidle-",
                          env={"BROWSER_AGENT_TAB_IDLE_SEC": "3"}) as s:
        # 会话内三页：初始页 + a + b；切到 a 后，b（非活跃自建页）应被回收
        r0 = s.run([
            {"action": "navigate", "url": f"{pages}/xy"},
            {"action": "new_tab", "name": "a"},
            {"action": "navigate", "url": f"{pages}/tall"},
            {"action": "new_tab", "name": "b"},
            {"action": "navigate", "url": f"{pages}/second"},
            {"action": "switch_tab", "name": "a"},
        ], sid="tidle")
        assert r0["ok"], [st.get("error") for st in r0["steps"] if not st["ok"]]

        # 静默等超过 idle 阈值（轮询探测会把 b 摸活——批入口/切页都刷新活动时间）
        time.sleep(4.5)

        # 任意新批完成即触发事件驱动回收（本批所属会话不受自持锁误跳过）
        ping = s.run([{"action": "get_title"}], sid="tidle")
        assert ping["ok"] and "长页" in ok_step(ping, 0)["title"]

        probe = s.run([{"action": "switch_tab", "name": "b"}], sid="tidle")
        step = probe["steps"][0]
        assert not step["ok"] and "不存在" in step.get("error", ""), \
            f"空闲自建页 b 应在批完成后被事件驱动回收：{step}"

        alive = s.run([{"action": "get_title"}], sid="tidle")
        assert alive["ok"] and "长页" in ok_step(alive, 0)["title"], \
            "活跃页 a 不应被误伤"


# ── search_local 命令批（本机无浏览器目录则跳过）──────────

def test_search_local_real_batch(svc, pages):
    r = svc.run([{"action": "navigate", "url": f"{pages}/second"},
                 {"action": "search_local", "keywords": ["the"], "limit": 3}], sid="sl")
    d = ok_step(r, 1)
    if not d.get("ok") and "未找到任何可辨认" in d.get("error", ""):
        pytest.skip("本机无可辨认的浏览器数据目录")
    assert d["ok"] and "counts" in d and "searched" in d
    assert d["counts"]["history"] <= 3
