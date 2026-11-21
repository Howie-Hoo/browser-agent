#!/usr/bin/env bash
# 发版脚本：预检 → 版本一致性 → 单测 → tag → 推送 → GitHub Release 一体化。
#
# 用法：
#   scripts/release.sh                # 以 browser_agent/__version__ 为准
#   scripts/release.sh 2026.11.22     # 显式指定版本（仍须与 __version__ 一致）
#   scripts/release.sh --skip-tests   # 跳过单测（不建议）
#
# 前提：版本号 bump 已随版本提交进入 main（惯例 `feat: vX — 主题`，正文即变更清单，
#      会被原样用作 release notes）。
# 产物：annotated tag vX（指向发布时 HEAD）+ GitHub Release（自动标 latest）。

set -euo pipefail

cd "$(dirname "$0")/.."

die()  { echo "✗ $*" >&2; exit 1; }
info() { echo "→ $*"; }

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

SKIP_TESTS=0
VERSION_ARG=""
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    -h|--help)    usage ;;
    *)            VERSION_ARG="$arg" ;;
  esac
done

# ---------- 1. 预检 ----------
command -v gh >/dev/null 2>&1 || die "缺少 gh CLI（brew install gh 后 gh auth login）"
gh auth status >/dev/null 2>&1 || die "gh 未登录：gh auth login"

[ "$(git branch --show-current)" = "main" ] || die "发版必须在 main 分支"
[ -z "$(git status --porcelain)" ] || die "工作区不干净：先提交或 stash"

git fetch --quiet origin main --tags
[ "$(git rev-parse main)" = "$(git rev-parse origin/main)" ] \
  || die "main 与 origin/main 不一致：先 push / pull 同步"

# ---------- 2. 版本一致性（三处必须同版本） ----------
pkg_version=$(python3 -c 'import re;print(re.search(r"__version__ = \"([^\"]+)\"",open("browser_agent/__init__.py").read()).group(1))')
plugin_version=$(python3 -c 'import json;print(json.load(open(".claude-plugin/plugin.json"))["version"])')
version="${VERSION_ARG:-$pkg_version}"
tag="v$version"

[ "$pkg_version" = "$version" ] \
  || die "browser_agent/__init__.py = $pkg_version ≠ $version"
[ "$plugin_version" = "$version" ] \
  || die ".claude-plugin/plugin.json = $plugin_version ≠ $version（插件市场靠它识别更新，必须随版本同步）"

version_commit=$(git log -n1 --format=%H --grep="v$version" main)
[ -n "$version_commit" ] || die "main 历史中找不到标题含 v$version 的版本提交"
subject=$(git log -1 --format=%s "$version_commit")
desc=$(python3 -c 'import re,sys;m=re.search(r"^\w+: v\S+ — (.+)$",sys.argv[1]);print(m.group(1) if m else sys.argv[1])' "$subject")
info "发版 ${tag}：$desc"

# ---------- 3. 回归（E2E 默认跳过，见 AGENTS.md） ----------
if [ "$SKIP_TESTS" -eq 0 ]; then
  info "单测回归"
  if command -v pytest >/dev/null 2>&1; then pytest tests/ -q
  else uv run pytest tests/ -q
  fi
fi

# ---------- 4. tag（annotated，指向发布时 HEAD） ----------
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  tag_commit=$(git rev-parse "$tag^{commit}")
  [ "$tag_commit" = "$(git rev-parse HEAD)" ] \
    || die "tag $tag 已指向 ${tag_commit:0:8} ≠ HEAD；如需重打：git tag -d $tag && git push origin :refs/tags/$tag 后重跑"
  info "tag $tag 已存在且指向 HEAD，复用"
else
  git tag -a "$tag" -m "v${version}：$desc"
  info "已创建 tag $tag"
fi

# ---------- 5. 推送 ----------
git push origin main
git push origin "refs/tags/$tag"

# ---------- 6. GitHub Release（notes 取自版本提交正文） ----------
if gh release view "$tag" >/dev/null 2>&1; then
  info "release $tag 已存在，跳过创建"
else
  prev=$(git tag --sort=-v:refname | grep -vx "$tag" | head -1)
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  {
    echo "## 本次变更"
    echo
    git log -1 --format=%b "$version_commit"
    echo "### 版本"
    echo
    if [ -n "$prev" ]; then echo "- ${prev#v} → **$version**"
    else echo "- 首个正式版本"; fi
  } > "$tmp"
  gh release create "$tag" --verify-tag --latest \
    --title "$tag — $desc" --notes-file "$tmp"
fi

gh release view "$tag" --json url -q '"✓ 发版完成：" + .url'
