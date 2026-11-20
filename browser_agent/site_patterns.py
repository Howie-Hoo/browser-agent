#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点经验（site-patterns）— 按域名积累的操作先验，跨会话复用。

存储：~/.browser-agent/site-patterns/{domain}.md（用户数据目录，升级不丢）
格式：YAML frontmatter（domain / aliases / updated）+ 三段正文：
  ## 平台特征  架构、反爬行为、登录需求、内容加载方式等事实
  ## 有效模式  已验证的 URL 模式、操作策略、选择器
  ## 已知陷阱  什么会失败以及为什么

条目带发现日期，是「可能有效的提示」而非「保证正确的事实」——
按经验操作失败时回退通用模式，并更新经验文件（SKILL.md 契约）。

模块函数为纯文件操作（server / client / 测试可直接用）；
BrowserProxySites mixin 把它们挂进命令分发，让 AI 在命令批内读写。
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from typing import Optional
from urllib.parse import urlparse

import yaml

from .user_data import site_patterns_dir

# 域名校验：常规 hostname 字符，禁路径分隔符与穿越（纵深防御另见路径归一校验）
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")

# 单域名经验文件上限：防失控的 write_site 循环把文件写爆
MAX_PATTERN_FILE_BYTES = 256 * 1024

# 注意：write_pattern/delete_pattern 的临界区**不含 await**——单事件循环下
# 天然原子；若未来在写路径加入 await，必须先引入写锁。

# 写入段落：命令字段 → 正文标题
SECTION_TITLES = {
    "platform": "平台特征",
    "patterns": "有效模式",
    "pitfalls": "已知陷阱",
}
SECTION_KEYS = {v: k for k, v in SECTION_TITLES.items()}


def validate_domain(domain: str) -> Optional[str]:
    """返回错误信息；None 表示合法。"""
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
        return "domain 只能是合法主机名（字母数字、点、连字符；不允许路径分隔符）"
    return None


def normalize_domain(target: str) -> str:
    """从 URL 或裸域名提取规范 hostname（去端口、小写、去 www. 前缀）。"""
    raw = (target or "").strip()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    raw = raw.split("/")[0].split(":")[0].strip().lower()
    return raw[4:] if raw.startswith("www.") else raw


def pattern_path(domain: str) -> str:
    err = validate_domain(domain)
    if err:
        raise ValueError(err)
    from .utils import safe_join
    return safe_join(site_patterns_dir(), domain, ".md", "domain")


def _parse(file_path: str) -> dict:
    """解析单个经验文件 → {domain, aliases, updated, content, sections}。

    - frontmatter 只认**首个**成对 `---` 围栏（正文里的 `---` 分隔线不影响）
    - frontmatter 解析结果不是 dict（手改坏了）→ 当空元数据，不抛错
    - 未知段落标题按原样保留键（含其正文行），写回时不丢内容
    """
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    meta: dict = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                try:
                    loaded = yaml.safe_load("\n".join(lines[1:i]))
                except yaml.YAMLError:
                    loaded = None
                if isinstance(loaded, dict):
                    meta = loaded
                body_start = i + 1
                break
    body = "\n".join(lines[body_start:]).strip()
    sections: dict = {}
    current = None
    for line in body.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            current = SECTION_KEYS.get(title, title)
            sections.setdefault(current, [])
        elif current is not None and line.strip():
            sections[current].append(line.rstrip())
    return {
        "domain": meta.get("domain") or os.path.basename(file_path)[:-3],
        "aliases": meta.get("aliases") or [],
        "updated": str(meta.get("updated") or ""),
        "content": body,
        "sections": sections,
    }


_PARSE_CACHE: dict = {}


def _load_all() -> list:
    """全量解析（带缓存）。/status 每次操作前被轮询、navigate 每步带经验提示、
    read_site 频繁调用——无缓存时是 O(文件数) 的读盘+YAML 解析，且为事件循环上的
    同步 IO。以目录 mtime 为键：写入/删除/手改都会改变目录 mtime → 自动失效，
    每次调用只付一次 stat 的代价。"""
    root = site_patterns_dir()
    try:
        mtime = os.stat(root).st_mtime_ns
    except OSError:
        return []
    key = (root, mtime)
    if _PARSE_CACHE.get("key") != key:
        items = []
        for fn in os.listdir(root):
            if not fn.endswith(".md"):
                continue
            try:
                items.append(_parse(os.path.join(root, fn)))
            except (OSError, ValueError):
                continue
        _PARSE_CACHE.clear()
        _PARSE_CACHE["key"] = key
        _PARSE_CACHE["items"] = items
    return _PARSE_CACHE.get("items", [])


def list_patterns() -> dict:
    """列出全部站点经验元数据（按 updated 倒序）。单个文件损坏跳过，不炸整体。"""
    items = [{
        "domain": i["domain"],
        "aliases": i["aliases"] if isinstance(i["aliases"], list) else [],
        "updated": i["updated"],
        "sections": {k: len(v) for k, v in i["sections"].items() if v},
    } for i in _load_all()]
    items.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return {"ok": True, "patterns": items, "count": len(items)}


def match_site(target: str) -> dict:
    """按 URL/域名找最佳匹配的经验文件。

    匹配优先级：**精确域名 > 子域名（a.example.com ↔ example.com）> 别名**
    （同级中取 updated 最新）——先全表扫精确，再扫子域名，绝不因更新顺序
    让子域文件遮蔽精确匹配。未命中时返回已知域名清单，提示 AI 顺手续写。
    """
    host = normalize_domain(target)
    if not host:
        return {"ok": False, "error": "无法从入参解析域名", "target": target}
    all_items = _load_all()  # 完整解析结果（含正文），免二次读盘

    def _find(level):
        best = None
        for i in all_items:
            d = i["domain"].lower()
            hit = False
            if level == "exact":
                hit = host == d
            elif level == "subdomain":
                hit = host.endswith("." + d) or d.endswith("." + host)
            elif level == "alias":
                aliases = i["aliases"] if isinstance(i["aliases"], list) else []
                hit = host in [str(a).lower() for a in aliases]
            if hit and (best is None or (i["updated"] or "") > (best["updated"] or "")):
                best = i
        return best

    for level in ("exact", "subdomain", "alias"):
        best = _find(level)
        if best is not None:
            return {
                "ok": True, "matched": True, "domain": best["domain"], "match_type": level,
                "aliases": best["aliases"] if isinstance(best["aliases"], list) else [],
                "updated": best["updated"],
                "content": best["content"],
                "ai_hint": "经验条目带日期，当作可能有效的提示而非保证；失败则回退通用模式并更新本文件",
            }
    return {
        "ok": True, "matched": False, "domain": host,
        "known_domains": [i["domain"] for i in all_items],
        "ai_hint": "该域名暂无站点经验。任务成功后如发现了可复用的模式/陷阱，用 write_site 记录（只写经验证的事实）",
    }


def _as_str_list(value, field: str) -> tuple:
    """归一为 (非空字符串数组, 错误)。字符串 → [value]；数组校验元素；其他类型报错。"""
    if value is None:
        return [], None
    if isinstance(value, str):
        return ([value] if value.strip() else []), None
    if isinstance(value, list) and all(isinstance(i, str) and i.strip() for i in value):
        return value, None
    return [], f"{field} 需为非空字符串或字符串数组"


def write_pattern(domain: str, aliases: Optional[list] = None,
                  platform: Optional[list] = None,
                  patterns: Optional[list] = None,
                  pitfalls: Optional[list] = None) -> dict:
    """追加（或创建）站点经验。只追加带日期条目，不覆盖既有内容。"""
    err = validate_domain(domain)
    if err:
        return {"ok": False, "error": err}
    domain = normalize_domain(domain) or domain
    entries = {"platform": platform, "patterns": patterns, "pitfalls": pitfalls}
    if not any(entries.values()):
        return {"ok": False, "error": "至少提供 platform / patterns / pitfalls 之一（字符串或字符串数组）"}
    norm_entries = {}
    for key, items in entries.items():
        norm, err = _as_str_list(items, key)
        if err:
            return {"ok": False, "error": err}
        norm_entries[key] = norm
    new_aliases, err = _as_str_list(aliases, "aliases")
    if err:
        return {"ok": False, "error": err}
    today = date.today().isoformat()
    path = pattern_path(domain)
    if os.path.isfile(path) and os.path.getsize(path) >= MAX_PATTERN_FILE_BYTES:
        return {
            "ok": False,
            "error": f"经验文件已达 {MAX_PATTERN_FILE_BYTES // 1024}KB 上限，拒绝继续追加",
            "ai_hint": "经验应精炼而非堆积：delete_site 后只重写仍然有效的事实",
        }
    existing = _parse(path) if os.path.isfile(path) else {"aliases": [], "content": "", "sections": {}}

    old_aliases = existing["aliases"] if isinstance(existing["aliases"], list) else []
    merged_aliases = list(dict.fromkeys(
        [str(a) for a in old_aliases] + [str(a) for a in new_aliases]
    ))
    meta = {
        "domain": domain,
        "aliases": merged_aliases,
        "updated": today,
    }

    # 重组正文：固定段落顺序（既有条目在前、新条目追加段尾），
    # 之后**原样保留**未知段落（标题 + 正文行，防手改内容丢失）
    body_parts = []
    appended = 0
    for key, title in SECTION_TITLES.items():
        section_lines = list(existing["sections"].get(key, []))
        for item in norm_entries[key]:
            section_lines.append(f"- ({today}) {item.strip()}")
            appended += 1
        if section_lines:
            body_parts.append(f"## {title}\n" + "\n".join(section_lines))
    # 未知段落按原样保留（键即原始标题）。已知段落已按固定顺序发射过，
    # 这里必须按**规范键**（platform/patterns/pitfalls）排除，防止重复发射
    for sec_key, sec_lines in existing["sections"].items():
        if sec_key in SECTION_TITLES or not sec_lines:
            continue
        body_parts.append(f"## {sec_key}\n" + "\n".join(sec_lines))

    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    content = f"---\n{front}\n---\n\n" + "\n\n".join(body_parts) + "\n"

    root = site_patterns_dir()
    os.makedirs(root, exist_ok=True)
    # 原子写：先写临时文件再替换，避免半截文件
    fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "domain": domain, "path": path,
            "appended_entries": appended, "aliases": merged_aliases}


def delete_pattern(domain: str) -> dict:
    err = validate_domain(domain)
    if err:
        return {"ok": False, "error": err}
    path = pattern_path(normalize_domain(domain) or domain)
    if not os.path.isfile(path):
        return {"ok": False, "error": f"域名 '{domain}' 无经验文件"}
    os.remove(path)
    return {"ok": True, "domain": domain}


class BrowserProxySites:
    """站点经验命令（list_sites / read_site / write_site / delete_site）。

    纯文件操作的 mixin，由 BrowserProxy 组合后经 _dispatch 暴露为命令，
    使 AI 能在命令批内完成「读先验 → 操作 → 写经验」闭环。
    """

    async def list_sites(self) -> dict:
        return list_patterns()

    async def read_site(self, domain: Optional[str] = None) -> dict:
        if not domain:
            return {"ok": False, "error": "需要 domain 或 url 字段"}
        return match_site(domain)

    async def write_site(self, domain: str, aliases: Optional[list] = None,
                         platform: Optional[list] = None,
                         patterns: Optional[list] = None,
                         pitfalls: Optional[list] = None) -> dict:
        return write_pattern(domain, aliases=aliases, platform=platform,
                             patterns=patterns, pitfalls=pitfalls)

    async def delete_site(self, domain: str) -> dict:
        return delete_pattern(domain)
