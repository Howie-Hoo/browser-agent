# v2026.11.20 迁移说明（用户数据目录 + 站点经验 + attach 模式）

本版把持久数据从包目录迁到用户目录、新增站点经验与 attach 模式。**自动迁移，无需
手动操作**——升级（`pip install --force-reinstall` / `pip install git+...` 均可）后首次
运行服务时，旧数据自动搬到新位置。

## 变更内容

| 项 | 旧位置（只读保留，不再读取） | 新位置 |
| --- | --- | --- |
| 认证 profile | `<包目录>/browser_agent/storage/profiles/` | `~/.browser-agent/profiles/` |
| 会话状态 state.json | `<包目录>/browser_agent/storage/state.json` | `~/.browser-agent/state.json` |
| cookies.json | `<包目录>/browser_agent/storage/cookies.json` | `~/.browser-agent/cookies.json` |
| 下载文件 | `<仓库>/storage/downloads/` | `~/.browser-agent/downloads/` |
| 站点经验 | （本版新增） | `~/.browser-agent/site-patterns/` |
| 附着偏好 | （本版新增） | `~/.browser-agent/attach.env` |

迁移语义：**只复制、不覆盖、不删除**（已有同名文件保留新位置版本）；完成后写
`~/.browser-agent/.migrated` 标记，此后不再扫描旧位置。`BROWSER_AGENT_HOME` 环境
变量可覆盖根目录（测试隔离用）。

## 交给 Agent 执行（如需验证迁移）

把下面这段话原样发给你的 agent：

```text
我的 browser-agent 升级到了 2026.11.20。请帮我确认数据迁移：
1. 运行 browser-agent-client status（或直接 GET /status），记下 user_data_dir；
2. 确认旧位置 <包目录>/browser_agent/storage/profiles/ 里的 profile 都出现在
   <user_data_dir>/profiles/ 下（list_profiles 对照）；
3. 确认 <user_data_dir>/.migrated 存在；
4. 如有缺失，把旧位置文件复制到新位置同名路径（不覆盖已有文件），再复核一遍。
只复制、不要删除旧位置任何文件。
```

## 手动迁移（macOS / Linux）

```bash
mkdir -p ~/.browser-agent/profiles ~/.browser-agent/site-patterns
PKG_DIR=$(python3 -c "import browser_agent, os; print(os.path.dirname(browser_agent.__file__))")
[ -d "$PKG_DIR/storage/profiles" ] && cp -Rn "$PKG_DIR/storage/profiles/." ~/.browser-agent/profiles/
[ -f "$PKG_DIR/storage/state.json" ] && cp -n "$PKG_DIR/storage/state.json" ~/.browser-agent/
[ -f "$PKG_DIR/storage/cookies.json" ] && cp -n "$PKG_DIR/storage/cookies.json" ~/.browser-agent/
touch ~/.browser-agent/.migrated
```

## 语义变化（行为级）

- 多会话下 `new_tab` / `switch_tab` / `close_tab` 的标签名按会话隔离（此前多会话
  同名标签会撞键）——单会话使用无感知。
- attach 模式（新增）：`browser-agent-client attach <浏览器数据目录>` 附着日常浏览器；
  managed 模式行为与旧版一致。
- `clear_storage` / `session/refresh` 在共享用户上下文（attach / `context=user`）中
  拒绝执行（保护用户真实登录态）。
- `auto_save_state` 在共享用户上下文中自动跳过。
