# 命令与 HTTP API 参考

> 查询型速查：browser_agent 微服务的全部命令、参数与 HTTP 端点，按需 Ctrl+F。操作约束（降级层级 / AI 标准流程 / 资源预检 / 会话模型 / 认证会话机制 / 进程清理）见 [`SKILL.md`](../SKILL.md)。

## 命令参考

### 导航与页面

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `navigate` | `url`, `wait_until` | 导航到 URL，自动附加 page_summary |
| `reload` | `wait_until` | 刷新页面 |
| `get_title` | — | 页面 title |
| `get_url` | — | 当前 URL |
| `get_page_summary` | — | AI 友好页面摘要（title/url/h1/body/forms/alerts） |

### 点击（从精确到语义）

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `click` | `selector`, `button`, `click_count` | CSS 选择器精确点击（底层，永远可用） |
| `click_text` | `text`, `exact` | 按文字内容点击 |
| `click_by_id` | `id`(数字) | 按 get_annotated_dom 序号点击 |
| `find_and_act` | `description`, `act`, `value` | 语义匹配（无需 selector） |

### 输入与表单

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `fill` | `selector`, `value` | 直接填入（底层） |
| `type` | `selector`, `text`, `delay` | 逐字输入 |
| `fill_form` | `fields`({selector: value}) | 批量填表 |
| `form_analyze` | `form_selector` | 分析表单结构，返回 fill_guide |
| `select` | `selector`, `value/label/index` | 下拉选择 |
| `check` / `uncheck` | `selector` | 复选框操作 |

### 等待

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `wait_for` | `selector`, `state`, `timeout` | 等待元素出现/消失 |
| `wait_for_stable` | `timeout` | 智能等待（网络静默+DOM稳定） |
| `smart_wait` | `condition`, `value`, `timeout` | 精细单条件等待 |
| `watch_for_change` | `conditions`, `timeout` | 多条件监听（消除轮询） |
| `wait_ms` | `ms` | 固定延迟 |

### DOM 读取

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `get_text` / `get_all_text` | `selector` | 获取元素文本 |
| `get_attr` | `selector`, `attr` | 获取属性值 |
| `get_count` | `selector` | 元素数量 |
| `get_html` | `selector`, `outer` | innerHTML/outerHTML |
| `get_value` | `selector` | 输入框当前值 |
| `is_visible` / `is_enabled` / `is_checked` | `selector` | 状态检查 |

### 高级感知（AI 友好）

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `get_a11y_tree` | `root_selector`, `interesting_only` | 无障碍语义树（SPA 首选） |
| `get_interactive_elements` | `limit` | 可交互元素清单 |
| `get_annotated_dom` | — | 可交互元素标注截图 |
| `extract` | `schema`, `wait_timeout` | 多框架结构化提取 |
| `snapshot_dom` / `get_dom_diff` | — | DOM 快照与变化对比 |

### 截图

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `screenshot` | `path`, `full_page`, `selector`, `clip` | 全页/区域/元素截图 |
| `clip_screenshot` | `selector`, `padding` | 元素截图（节省 Token） |

### 多标签页

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `new_tab` | `name` | 同 context 新标签（共享 cookie） |
| `new_isolated_session` | `name` | 隔离 context（独立 cookie） |
| `switch_tab` / `close_tab` | `name` | 切换/关闭标签 |

### iframe

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `switch_frame` | `selector/name/url_contains` | 切换 iframe 上下文 |
| `switch_to_main_frame` | — | 退出 iframe |

### 状态持久化

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `save_storage` | `path` | 保存完整会话状态（单一 state.json） |
| `clear_storage` | — | 清除磁盘+内存状态（全新测试起点；不影响 profiles/） |
| `save_cookies` / `load_cookies` | `path` | Cookie 持久化 |

### 认证会话（命名 profile）

profile 机制与项目接入写法见 [`SKILL.md`](../SKILL.md)「认证会话管理」。

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `save_profile` | `name`, `include_auth?` | 把当前活跃 context 的 storage_state 落盘为命名 profile（保留旧 auth 规则） |
| `load_profile` | `name`, `into?="new"`, `navigate?=true` | 加载登录态；`into="new"`（推荐，原子注入）建隔离 context，`into="current"` 注入当前页 |
| `has_profile` | `name` | 查询 profile 是否存在 |
| `list_profiles` | — | 列出所有 profile 元数据（**绝不返回 token 内容**） |
| `delete_profile` | `name` | 删除 profile |
| `check_auth` | `name?` | 按 `auth.check` 判定登录态是否有效（不给 name 则查当前活跃 profile） |
| `ensure_auth` | `name?`, `login_commands?`, `into?` | profile 有效则复用；否则执行 `login_commands`（任意命令序列）→ 再 check → 落盘 |
| `new_isolated_session` | `name`, `profile?` | 建隔离 context；传 `profile` 则同时注入其登录态 |

### 其他

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `eval_js` | `script`, `selector` | 执行 JS（底层万能工具） |
| `hover` / `hover_submenu` | `selector` | 鼠标悬停 |
| `scroll` / `scroll_to_element` / `scroll_until_found` | — | 滚动操作 |
| `drag_and_drop` / `drag_drop` | `source`, `target` | 拖拽操作 |
| `detect_captcha` | — | 探测验证码 |
| `get_page_health` | — | JS 错误/资源失败/性能指标 |
| `assert_text` / `assert_url` / `assert_visible` / `assert_value` | — | 断言 |

### 宏指令（批级）

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `loop` | `selector`, `commands`, `limit` | 对 selector 匹配的每个元素循环执行命令（支持 `{{index}}`/`{{count}}` 占位） |
| `multi_page_batch` | `urls`, `schema`, `limit`, `delay_ms` | 跨页批量提取 |

### 通用参数（任意命令可附加）

| 参数 | 说明 |
| --- | --- |
| `stop_on_error` | 步骤失败时中止后续命令 |
| `comment` | 步骤备注 |
| `max_response_chars` | 限制响应大小（防上下文爆炸） |
| `if_error_then` | 失败时执行的备用命令链 |
| `timeout` | 命令超时（ms） |

## HTTP API

```text
GET  /status          健康检查
POST /session/start   启动/重配会话
POST /session/reset   重置页面
POST /session/refresh 清除当前会话 cookies + storage（保留浏览器）
POST /session/stop    关闭单个会话（最后一个会话关闭时浏览器随之关闭）
POST /run             执行命令批次
POST /check           预设检验套件
POST /macro/save      保存宏
POST /macro/run       运行宏
GET  /macro/list      列出宏
GET  /auth/profiles   列出所有 profile 元数据（不含 token 内容）
POST /auth/ensure     确保已登录：{profile, login_commands, into} — 失效则执行登录命令并落盘
GET  /stream          SSE 实时推送
POST /shutdown        优雅关闭（浏览器 + HTTP 服务）
```

所有端点恒返 200，逻辑成败看响应体 `ok` 字段。
