#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地浏览器资源检索 — 从本机 Chromium 系浏览器的书签与历史记录定位 URL。

典型场景（公网搜索覆盖不到的目标）：
- 组织内部系统："我们的 XX 平台"、"公司那个 YY 后台"
- 用户访问过但不记得地址的页面："我之前看的那个讲 X 的文章"
- 最近高频访问的网站（sort=visits）

纪律（与 attach 浏览器发现一致的边界）：
- 只读文件；浏览器范围限定在已知目录清单内可辨认的数据目录
- 历史库是 SQLite，浏览器运行时被锁 —— 复制到临时文件后以只读模式查询
- 不触碰浏览器进程、不发任何网络请求

模块函数为纯文件操作（CLI / 命令批 / 测试可直接用）；
BrowserProxyLocalSearch mixin 把检索挂进命令分发（search_local）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import browser_attach
from .utils import read_json, strictly_inside

# Chrome 历史库时间戳：自 1601-01-01 起的微秒数（与 Unix epoch 的固定差）
_WEBKIT_EPOCH_DIFF_US = 11644473600000000

# 单 profile 历史查询硬上限：防误扫超大历史库拖垮事件循环
_MAX_HISTORY_ROWS = 10000

_SINCE_RE = re.compile(r"^(\d+)([dhm])$")
_VALID_ONLY = ("bookmarks", "history")
_VALID_SORT = ("recent", "visits")


def parse_since(value: str) -> Optional[float]:
    """时间窗 → Unix 秒。支持 1d / 7h / 30m 与 YYYY-MM-DD；非法抛 ValueError。"""
    v = str(value or "").strip()
    if not v:
        return None
    m = _SINCE_RE.match(v)
    if m:
        unit = {"d": 86400.0, "h": 3600.0, "m": 60.0}[m.group(2)]
        return time.time() - int(m.group(1)) * unit
    try:
        return datetime.strptime(v, "%Y-%m-%d").timestamp()
    except ValueError:
        raise ValueError(f"无效 since 值: {value}（用 1d / 7h / 30m / YYYY-MM-DD）")


@dataclass(frozen=True)
class LocalSearchQuery:
    """一次本地检索的不可变参数（构造即校验，经 from_params 创建）。"""

    keywords: Tuple[str, ...] = ()
    only: Optional[str] = None
    browser: Optional[str] = None
    limit: int = 20
    since: Optional[float] = None
    sort: str = "recent"

    @classmethod
    def from_params(cls, keywords=None, only=None, browser=None, limit: int = 20,
                    since=None, sort: str = "recent") -> "LocalSearchQuery":
        """从命令/CLI 参数构建；非法输入抛 ValueError（信息可直接展示）。"""
        if keywords is None:
            ks: Tuple[str, ...] = ()
        elif isinstance(keywords, str):
            ks = tuple(kw.strip() for kw in keywords.split() if kw.strip())
        elif isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
            ks = tuple(k.strip() for k in keywords if k.strip())
        else:
            raise ValueError("keywords 需为字符串（空格分词，多词 AND）或字符串数组")
        if only is not None and only not in _VALID_ONLY:
            raise ValueError(f"only 仅支持 {' / '.join(_VALID_ONLY)}")
        if sort not in _VALID_SORT:
            raise ValueError(f"sort 仅支持 {' / '.join(_VALID_SORT)}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit 需为非负整数（0 = 不限）")
        if since is None:
            since_sec = None
        elif isinstance(since, (int, float)):
            since_sec = float(since)
        else:
            since_sec = parse_since(str(since))
        return cls(keywords=ks, only=only, browser=browser,
                   limit=limit, since=since_sec, sort=sort)


# ── 浏览器 / profile 枚举 ──────────────────────────────────

def _list_profiles(data_dir: str) -> list:
    """从 Local State 的 info_cache 枚举 profile（目录名, 显示名）；读不到回落 Default。"""
    state = read_json(os.path.join(data_dir, "Local State"))
    info = (state or {}).get("profile", {}).get("info_cache", {}) or {}
    out = [(str(k), (v or {}).get("name") or str(k)) for k, v in info.items()]
    return out or [("Default", "Default")]


def _iter_profiles(roots: Optional[list] = None):
    """(浏览器显示名, 数据目录) × profile → (浏览器名, profile 名, profile 目录)。

    info_cache 的键直接来自磁盘上的 Local State（不可信输入）：绝对路径键会
    替换 join 目标、`..` 键会逃出浏览器目录——一律按越界拒绝（全仓统一谓词
    utils.strictly_inside）。
    """
    for name, raw_dir in (roots if roots is not None else browser_attach.default_roots()):
        d = browser_attach.expand_path(raw_dir)
        if not browser_attach.is_browser_data_dir(d):
            continue
        for p_dir, p_name in _list_profiles(d):
            full = os.path.join(d, p_dir)
            if strictly_inside(full, d) and os.path.isdir(full):
                yield name, p_name, full


# ── 书签检索（JSON 递归） ──────────────────────────────────

def _search_bookmarks(profile_dir: str, browser: str, profile: str,
                      keywords: Tuple[str, ...]) -> list:
    path = os.path.join(profile_dir, "Bookmarks")
    # 书签无时间维度，无关键词的列举无意义（历史才支持浏览式列举）
    if not keywords or not os.path.isfile(path):
        return []
    data = read_json(path)
    if data is None:  # 读不到 / 损坏 / 非对象——统一当无书签
        return []
    needles = [k.lower() for k in keywords]
    out: list = []

    def walk(node, trail: Tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "url":
            name = str(node.get("name") or "")
            url = str(node.get("url") or "")
            hay = f"{name} {url}".lower()
            if all(n in hay for n in needles):
                out.append({"browser": browser, "profile": profile,
                            "name": name, "url": url, "folder": " / ".join(trail)})
        children = node.get("children")
        if isinstance(children, list):
            sub = trail + (str(node["name"]),) if node.get("name") else trail
            for child in children:
                walk(child, sub)

    for root in (data or {}).get("roots", {}).values():
        walk(root, ())
    return out


# ── 历史检索（SQLite 复制后只读查询） ──────────────────────

def _search_history(profile_dir: str, browser: str, profile: str,
                    keywords: Tuple[str, ...], since: Optional[float],
                    sort: str, q_limit: int = 0) -> list:
    src = os.path.join(profile_dir, "History")
    if not os.path.isfile(src):
        return []
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        # 连同 -wal/-journal 一起复制：浏览器运行中主文件可能处于事务中间态，
        # 孤本常过不了 SQLite 头校验（撕裂快照）
        try:
            shutil.copy2(src, tmp)
            for suffix in ("-wal", "-journal"):
                side = f"{src}{suffix}"
                if os.path.isfile(side):
                    shutil.copy2(side, f"{tmp}{suffix}")
        except OSError:
            # 浏览器持有独占锁（Windows 常见）：该 profile 本次无历史可查
            return []
        conds = ["last_visit_time > 0"]
        params: list = []
        for kw in keywords:
            esc = (kw.lower().replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
            conds.append(r"LOWER(title || ' ' || url) LIKE ? ESCAPE '\'")
            params.append(f"%{esc}%")
        if since is not None:
            conds.append("last_visit_time >= ?")
            params.append(int(since * 1_000_000) + _WEBKIT_EPOCH_DIFF_US)
        order = ("visit_count DESC, last_visit_time DESC" if sort == "visits"
                 else "last_visit_time DESC")
        # 全局 top-limit 由各 profile 的同序 top-limit 归并而来即可，
        # 不必每 profile 都拉满硬上限
        row_cap = min(q_limit, _MAX_HISTORY_ROWS) if q_limit else _MAX_HISTORY_ROWS
        sql = (f"SELECT title, url, visit_count, last_visit_time FROM urls "
               f"WHERE {' AND '.join(conds)} ORDER BY {order} LIMIT ?")
        params.append(row_cap)
        conn = sqlite3.connect(Path(tmp).as_uri() + "?mode=ro", uri=True)
        try:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                # 快照撕裂 / 库损坏：该 profile 本次无历史可查，不炸整体检索
                return []
        finally:
            conn.close()
        out = []
        for title, url, visits, ts_us in rows:
            ts = int((ts_us - _WEBKIT_EPOCH_DIFF_US) / 1_000_000)
            # 廉价范围校验替代逐行 fromtimestamp：腐坏时间戳（pre-1970/远未来，
            # 后者在 Windows 会让 fromtimestamp 抛错）在切顶前就丢弃
            if not 0 < ts < 4102444800:  # 1970 → 2100
                continue
            out.append({"browser": browser, "profile": profile,
                        "title": str(title or ""), "url": str(url or ""),
                        "visit_count": int(visits or 0),
                        "visited_at": ts})
        return out
    finally:
        for leftover in (tmp, f"{tmp}-wal", f"{tmp}-journal"):
            try:
                os.remove(leftover)
            except OSError:
                pass


# ── 检索入口 ───────────────────────────────────────────────

def run_local_search(q: LocalSearchQuery) -> dict:
    """执行检索。返回 {"ok", "bookmarks", "history", "counts", "searched"}。"""
    roots = browser_attach.default_roots()
    if q.browser:
        roots = [r for r in roots if r[0].lower() == q.browser.lower()]
        if not roots:
            names = ", ".join(n for n, _ in browser_attach.default_roots()) or "无"
            return {"ok": False,
                    "error": f"已知浏览器清单里没有 {q.browser}（本机可查：{names}）"}
    do_bookmarks = q.only != "history"
    do_history = q.only != "bookmarks"

    bookmarks: list = []
    history: list = []
    searched: dict = {}
    work = list(_iter_profiles(roots))
    for bname, pname, _pdir in work:
        searched.setdefault(bname, [])
        if pname not in searched[bname]:
            searched[bname].append(pname)

    def _one(item):
        bname, pname, pdir = item
        bms = _search_bookmarks(pdir, bname, pname, q.keywords) if do_bookmarks else []
        his = _search_history(pdir, bname, pname, q.keywords, q.since,
                              q.sort, q.limit) if do_history else []
        return bms, his

    # 各 profile 只读且相互独立：并发跑（书签解析 + 历史库复制是主要时延）
    if work:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(work))) as pool:
            for bms, his in pool.map(_one, work):
                bookmarks.extend(bms)
                history.extend(his)

    if not searched:
        return {"ok": False, "bookmarks": [], "history": [],
                "error": "未找到任何可辨认的 Chromium 系浏览器数据目录",
                "remedy": "确认本机已安装 Chrome / Edge / Brave 等；"
                          "browser-agent-client attach --discover 可查看已知目录清单"}

    # 跨 profile / 浏览器合并后全局重排 + 切顶
    if q.sort == "visits":
        history.sort(key=lambda h: (-h["visit_count"], -h["visited_at"]))
    else:
        history.sort(key=lambda h: -h["visited_at"])
    if q.limit:
        bookmarks, history = bookmarks[:q.limit], history[:q.limit]
    # ISO 只对切顶后存活的行为格式化（fromtimestamp 不再为丢弃行付费）
    for h in history:
        h["visited_at_iso"] = datetime.fromtimestamp(
            h["visited_at"]).isoformat(timespec="seconds")

    return {"ok": True,
            "bookmarks": bookmarks, "history": history,
            "counts": {"bookmarks": len(bookmarks), "history": len(history)},
            "searched": [{"browser": b, "profiles": ps} for b, ps in searched.items()]}


def search(keywords=None, only: Optional[str] = None, browser: Optional[str] = None,
           limit=20, since=None, sort: str = "recent") -> dict:
    """单一检索入口：参数归一（含字符串 limit 数字化）→ 校验错误报文 → 执行。
    CLI（client.find_url）与命令批（search_local）共用，错误形状只有一份。"""
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            return {"ok": False, "error": f"--limit 需为非负整数，收到: {limit}"}
    try:
        query = LocalSearchQuery.from_params(keywords, only, browser,
                                             limit, since, sort)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return run_local_search(query)


class BrowserProxyLocalSearch:
    """本地浏览器资源检索命令（search_local；纯文件操作，无需浏览器）。"""

    async def search_local(self, keywords=None, only: Optional[str] = None,
                           browser: Optional[str] = None, limit=20,
                           since=None, sort: str = "recent") -> dict:
        """从本机浏览器书签/历史检索 URL。keywords 多词 AND 匹配 title+url；
        only 限定 bookmarks|history；since 时间窗（1d/7h/30m/YYYY-MM-DD，仅历史）；
        sort=recent|visits；limit 0=不限。书签无关键词时不检索（无时间维度）。
        全部文件 IO（含复制历史库、profile 并发）在线程池执行，不阻塞事件循环。"""
        return await asyncio.to_thread(search, keywords=keywords, only=only,
                                       browser=browser, limit=limit,
                                       since=since, sort=sort)
