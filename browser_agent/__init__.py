"""browser-agent：给 AI 代理用的本地浏览器操作微服务（Playwright）。"""
# CalVer：v{YYYY}.{M}.{D}（月日不补零），同日多次发版追加 .1/.2 序号
__version__ = "2026.11.9"

# 惰性导出：client/server 顶层缺依赖时会 sys.exit(1)，急切导入会让
# `import browser_agent` 在未装依赖的环境里直接崩溃
_EXPORTS = {"BrowserAgentClient": ("browser_agent.client", "BrowserAgentClient")}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module_name, attr = _EXPORTS[name]
    return getattr(importlib.import_module(module_name), attr)


def __dir__():
    return [*_EXPORTS, "__version__"]
