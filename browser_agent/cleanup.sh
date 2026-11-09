#!/usr/bin/env bash
# browser-agent 残留进程清理（HTTP 服务 + playwright driver + Chrome for Testing）。
# 幂等：无残留时 pkill 返回非零，|| true 兜底，脚本始终正常退出。
# 接入方式（pytest conftest / Claude Code Stop hook）见仓库 README「进程清理」。
set -u
pkill -f "browser_agent[.]server" 2>/dev/null || true
pkill -f "browser-agent-server" 2>/dev/null || true
pkill -f "playwright/driver" 2>/dev/null || true
pkill -f "Google Chrome for Testing" 2>/dev/null || true
