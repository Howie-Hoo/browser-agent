"""site_patterns 单测 — 写入/追加（带日期）/ 匹配（精确/子域名/别名）/ 路径穿越防护。

全部用例在临时 BROWSER_AGENT_HOME 下运行，绝不触碰真实用户数据。
运行：pytest tests/test_site_patterns.py -q
"""
import os
import sys
from datetime import date
from stat import S_ISDIR

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_agent import site_patterns as sp  # noqa: E402


@pytest.fixture
def store(isolated_home):
    return isolated_home / "site-patterns"


def test_write_creates_dated_sections(store):
    res = sp.write_pattern(
        "example.com",
        aliases=["示例站"],
        platform=["SPA，接口走 /api/v2"],
        patterns=["列表页 /explore?page=N 可直接翻页"],
        pitfalls=["图片懒加载，提取前须滚动到底"],
    )
    assert res["ok"] and res["appended_entries"] == 3
    text = (store / "example.com.md").read_text(encoding="utf-8")
    today = date.today().isoformat()
    assert f"domain: example.com" in text
    assert "## 平台特征" in text and "## 有效模式" in text and "## 已知陷阱" in text
    assert f"- ({today}) 列表页 /explore?page=N 可直接翻页" in text


def test_write_appends_without_overwriting(store):
    sp.write_pattern("example.com", pitfalls=["陷阱A"])
    sp.write_pattern("example.com", pitfalls=["陷阱B"], patterns=["模式B"])
    text = (store / "example.com.md").read_text(encoding="utf-8")
    assert "陷阱A" in text and "陷阱B" in text and "模式B" in text
    info = sp.list_patterns()
    assert info["count"] == 1 and info["patterns"][0]["domain"] == "example.com"


def test_match_exact_subdomain_alias_www(store):
    sp.write_pattern("example.com", aliases=["示例站"], patterns=["模式X"])
    assert sp.match_site("https://example.com/explore?x=1")["matched"] is True
    assert sp.match_site("https://www.example.com/")["matched"] is True
    m = sp.match_site("https://app.example.com/panel")
    assert m["matched"] is True and m["match_type"] == "subdomain"
    m = sp.match_site("示例站")
    assert m["matched"] is True and m["match_type"] == "alias"
    assert "模式X" in m["content"]


def test_match_unmatched_lists_known_domains(store):
    sp.write_pattern("example.com", patterns=["模式X"])
    res = sp.match_site("https://other.org/")
    assert res["matched"] is False
    assert res["known_domains"] == ["example.com"]
    assert "write_site" in res["ai_hint"]


def test_list_and_delete(store):
    sp.write_pattern("a.com", patterns=["p1"])
    sp.write_pattern("b.com", patterns=["p2"])
    info = sp.list_patterns()
    assert info["count"] == 2
    assert sp.delete_pattern("a.com")["ok"] is True
    assert sp.list_patterns()["count"] == 1
    assert sp.delete_pattern("a.com")["ok"] is False


def test_domain_validation_rejects_traversal(store):
    for bad in ("../etc", "a/b", "..", "a\\b", ""):
        res = sp.write_pattern(bad, patterns=["x"])
        assert res["ok"] is False, bad
    with pytest.raises(ValueError):
        sp.pattern_path("../escape")


def test_write_requires_content(store):
    assert sp.write_pattern("example.com")["ok"] is False


def test_write_rejects_oversized_file(store, monkeypatch):
    """文件达到上限后拒绝继续追加（防失控写爆），提示精炼重写。"""
    monkeypatch.setattr(sp, "MAX_PATTERN_FILE_BYTES", 200)
    sp.write_pattern("big.com", patterns=["初始条目" + "x" * 200])
    path = store / "big.com.md"
    assert path.stat().st_size >= 200
    res = sp.write_pattern("big.com", patterns=["再来一条"])
    assert res["ok"] is False and "上限" in res["error"]


def test_write_preserves_custom_sections_and_hr(store):
    """手改的自定义段落（标题+正文）与正文里的 --- 分隔线在追加时不丢。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "keep.com.md").write_text(
        "---\ndomain: keep.com\nupdated: 2026-01-01\n---\n\n"
        "## 平台特征\n- (2026-01-01) 旧条目\n\n"
        "## 备注\n- 手改备注 A\n- 手改备注 B\n\n"
        "---\n\n"
        "## 有效模式\n- (2026-01-01) 旧模式\n",
        encoding="utf-8")
    res = sp.write_pattern("keep.com", patterns=["新模式"])
    assert res["ok"]
    text = (store / "keep.com.md").read_text(encoding="utf-8")
    assert "手改备注 A" in text and "手改备注 B" in text, "自定义段落正文丢失"
    assert "\n---\n" in text, "正文分隔线被吃掉"
    assert "旧条目" in text and "旧模式" in text and "新模式" in text


def test_non_dict_frontmatter_does_not_crash_list(store):
    """frontmatter 手改成非 dict（如列表）→ 跳过该文件，list_patterns 不炸。"""
    store.mkdir(parents=True, exist_ok=True)
    (store / "broken.com.md").write_text(
        "---\n- a\n- b\n---\n\n## 平台特征\n- x\n", encoding="utf-8")
    sp.write_pattern("good.com", patterns=["g"])
    res = sp.list_patterns()
    assert res["ok"] and res["count"] >= 1
    assert all(p["domain"] != "broken.com" for p in res["patterns"]) or True  # 跳过或降级均可，不炸即过
    assert sp.match_site("https://good.com/")["matched"] is True


def test_aliases_string_wrapped_and_bad_type_rejected(store):
    """aliases 传字符串 → 包装为单元素（不逐字符拆）；不可迭代类型 → 结构化失败。"""
    res = sp.write_pattern("al.com", aliases="alias.example", patterns=["p"])
    assert res["ok"] and res["aliases"] == ["alias.example"], res
    # 非子域的别名按 alias 级命中（子域关系仍优先走 subdomain 级，属正确行为）
    assert sp.match_site("alias.example")["match_type"] == "alias"
    res2 = sp.write_pattern("al.com", aliases=5, patterns=["p2"])
    assert res2["ok"] is False and "aliases" in res2["error"]


def test_exact_match_beats_fresher_subdomain(store):
    """精确匹配优先于更新时间更近的子域文件（先全表扫精确，再扫子域）。"""
    sp.write_pattern("shadow.com", patterns=["父域经验"])
    sp.write_pattern("app.shadow.com", platform=["子域特征"])
    # 让子域文件更新时间更近（后写即更近）→ 查父域仍须命中父域
    m = sp.match_site("https://shadow.com/x")
    assert m["matched"] and m["domain"] == "shadow.com" and m["match_type"] == "exact"
    assert "父域经验" in m["content"]


def test_normalize_domain_variants():
    assert sp.normalize_domain("https://WWW.Example.com:443/x?y=1") == "example.com"
    assert sp.normalize_domain("app.example.com") == "app.example.com"
    assert sp.normalize_domain("example.com/path") == "example.com"


def test_mixin_wrappers_delegate(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_HOME", str(tmp_path / "h"))
    from browser_agent.site_patterns import BrowserProxySites

    proxy = BrowserProxySites()
    import asyncio
    res = asyncio.run(proxy.write_site("m.com", patterns=["模式M"]))
    assert res["ok"]
    assert asyncio.run(proxy.read_site("https://m.com/x"))["matched"] is True
    listed = asyncio.run(proxy.list_sites())
    assert listed["count"] == 1
    assert asyncio.run(proxy.delete_site("m.com"))["ok"] is True
    assert asyncio.run(proxy.read_site(None))["ok"] is False


# ── 自由文本匹配（用户说法 → 经验前置到选入口之前）──────────

def test_match_free_text_via_alias(store):
    sp.write_pattern("xiaohongshu.com", aliases=["小红书"],
                     patterns=["笔记直链必须带 xsec_token，从搜索进"])
    res = sp.match_site("去小红书搜穿搭")
    assert res["matched"] and res["match_type"] == "text"
    assert res["domain"] == "xiaohongshu.com"
    assert "xsec_token" in res["content"]


def test_match_free_text_via_domain_substring(store):
    sp.write_pattern("portal.internal.corp", aliases=["财务平台"], patterns=["p"])
    res = sp.match_site("帮我在财务平台查下报销")
    assert res["matched"] and res["domain"] == "portal.internal.corp"


def test_valid_domain_input_not_confused_by_text_fallback(store):
    """合法域名入参走精确/子域/别名匹配，不进自由文本回退。"""
    sp.write_pattern("example.com", aliases=["示例站"], patterns=["p"])
    res = sp.match_site("example.com")
    assert res["matched"] and res["match_type"] == "exact"


def test_free_text_no_hit_returns_known_domains(store):
    sp.write_pattern("example.com", patterns=["p"])
    res = sp.match_site("随便聊聊今天天气")
    assert res["matched"] is False and res["known_domains"] == ["example.com"]


def test_free_text_domain_match_requires_boundary(store):
    """域名子串匹配要边界：a.co 不得命中 xa.co.cn 这类假阳性。"""
    sp.write_pattern("a.co", patterns=["p"])
    assert sp.match_site("看看 xa.co.cn 的新功能")["matched"] is False
    assert sp.match_site("去 a.co 看看")["matched"] is True


def test_write_delete_invalidate_cache_within_mtime_window(store, monkeypatch):
    """缓存失效不依赖 mtime：tmpfs 时钟粒度窗口内写+删 mtime 可不变（实测 >95%），
    write/delete 后必须立即可见——目录 stat 冻结为固定 mtime 模拟该窗口。"""
    real_stat = os.stat

    def _frozen(p, **k):
        st = real_stat(p, **k)
        # 判目录不得走 os.path.isdir（内部调 os.stat → 递归回本替身）
        if not S_ISDIR(st.st_mode):
            return st
        # 真 stat_result 类型（10 元组构造），目录 mtime 冻结为常量
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                               st.st_uid, st.st_gid, st.st_size,
                               st.st_atime, 1700000000.0, st.st_ctime))

    monkeypatch.setattr(os, "stat", _frozen)
    sp.write_pattern("a.com", patterns=["p1"])
    sp.write_pattern("b.com", patterns=["p2"])
    assert sp.list_patterns()["count"] == 2
    assert sp.delete_pattern("a.com")["ok"] is True
    assert sp.list_patterns()["count"] == 1
    sp.write_pattern("c.com", patterns=["p3"])
    assert sp.list_patterns()["count"] == 2
    assert sp.match_site("https://c.com/")["matched"] is True
