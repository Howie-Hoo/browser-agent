# 命令与 HTTP API 参考

> 查询型速查：browser_agent 微服务的全部命令、参数与 HTTP 端点，按需 Ctrl+F。操作约束（降级层级 / AI 标准流程 / 资源预检 / 会话模型 / 认证会话机制 / 进程清理）见 [`SKILL.md`](../SKILL.md)。

## 命令参考

### 导航与页面

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `navigate` | `url`, `wait_until` | 导航到 URL，自动附加 page_summary |
| `reload` | `wait_until` | 刷新页面 |
| `go_back` | `wait_until?="load"` | 后退一页；无历史时 `went_back: false` 如实回报（不报错） |
| `get_title` | — | 页面 title |
| `get_url` | — | 当前 URL |
| `get_page_summary` | — | AI 友好页面摘要（title/url/h1/body/forms/alerts） |

### 点击（从精确到语义）

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `click` | `selector`, `button`, `click_count` | CSS 选择器精确点击（底层，永远可用） |
| `click_xy` | `x`, `y`, `button`, `click_count`, `delay` | 按视口坐标真实鼠标点击（move → down → up）；canvas/地图/视频/滑块等无 selector 目标，坐标取 `get_bounding_box` 中心，命中与否需事后核对 |
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
| `screenshot` | `path`, `full_page`, `selector`, `clip`, `image_type?="png"`, `quality?=80`, `scale?="css"\|"device"` | 全页/区域/元素截图；`image_type: "jpeg"` + `quality` 省 token（内容型页面比 PNG 小 3~5 倍）；`scale: "css"` 高分屏上出图更小 |
| `clip_screenshot` | `selector`, `padding` | 元素截图（节省 Token） |

### 多标签页与会话

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `new_tab` | `name` | 同 context 新标签（共享 cookie）；多会话下名称自动按会话隔离 |
| `new_isolated_session` | `name`, `profile?` | 隔离 context（独立 cookie；传 `profile` 同时注入登录态） |
| `new_shared_session` | `name` | 共享 default context 建会话页（attach 模式=用户真实 profile；关闭只关自建标签页） |
| `list_user_tabs` | — | 列出 default context 全部标签页（含用户已打开的；attach 模式定位用户 tab） |
| `adopt_tab` | `index`, `name?`, `activate?=true` | 收养用户已有标签页纳入操作——**仅限用户明确要求时**；收养页只注销绝不关闭 |
| `switch_tab` / `close_tab` | `name` | 切换/关闭标签（attach 模式自建页切换不抢焦点；收养页激活、close 仅注销） |

### iframe

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `list_frames` | — | 只读列出主文档**直系** iframe（文档顺序 `index` + `name/url/id/src`，便于指认）；嵌套 iframe 先进入外层再列；标记当前是否已在 frame 上下文 |
| `switch_frame` | `selector/name/url_contains` | 切换 iframe 上下文（失败时回显全部 frame 供选择） |
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

### 站点经验（site-patterns）

机制与读写契约见 [`SKILL.md`](../SKILL.md)「站点经验」。存于 `~/.browser-agent/site-patterns/`，纯文件操作，无需浏览器。

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `list_sites` | — | 列出全部站点经验元数据（域名/别名/更新日期/条目数） |
| `read_site` | `domain` / `url` / **自由文本** | 匹配（精确 > 子域名 > 别名 > 自由文本子串）并返回经验全文；未命中返回已知域名清单。自由文本让用户原话（「去小红书搜xx」）直接对上经验域名——先验在选入口之前就该可用 |
| `write_site` | `domain`, `aliases?`, `platform?`, `patterns?`, `pitfalls?` | 追加带日期条目（只追加不覆盖；域名做路径穿越校验） |
| `delete_site` | `domain` | 删除该域名的经验文件 |

`navigate` 到已积累的域名时，响应自动附 `site_experience` 提示（domain/match_type/updated）。

### 本地浏览器资源检索

从本机 Chromium 系浏览器的**书签 / 历史**检索 URL（公网搜不到的内部系统、之前看过的页面）。
纯本地只读操作（历史库复制到临时文件后只读查询），不触碰浏览器进程、无需服务在运行。

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `search_local` | `keywords`, `only?`, `browser?`, `limit?`, `since?`, `sort?` | 多词 AND 匹配标题+URL；`only=bookmarks\|history`；`browser` 限定浏览器名；`since=1d\|7h\|30m\|YYYY-MM-DD`（仅历史）；`sort=recent\|visits`；`limit` 0=不限。书签无关键词时不检索 |

CLI 等价：`browser-agent-client find-url [关键词…] [--only …] [--browser …] [--limit N] [--since …] [--sort …]`

### 其他

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `eval_js` | `script`, `selector`, `isolated?` | 执行 JS（底层万能工具）；`isolated: true` 在隔离环境执行——页面改写 DOM API（反自动化站点）不影响结果，代价是看不到页面 JS 全局变量、只作用于主文档 |
| `hover` / `hover_submenu` | `selector` | 鼠标悬停 |
| `scroll` / `scroll_to_element` / `scroll_until_found` | `scroll` 另有 `direction=top\|bottom` | 滚动操作；direction 直接滚到页面顶/底（scrollHeight，触发懒加载） |
| `upload_file` | `selector`, `files`（路径或路径数组） | 给 file input 设置本地文件（绕过文件对话框）；多文件上传传数组 |
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

**调用约束（来源校验）**：带 body 的请求必须 `Content-Type: application/json`（否则
415）；带浏览器 `Origin` 头一律 403（浏览器页面不是本服务的客户端——防任意网页
跨站驱动本机服务）；`Host` 必须为回环主机名（防 DNS rebinding）。业务端点恒返 200
（成败看 `ok` 字段），协议级拒绝（403/415/409）用真实状态码。curl 示例：

```bash
curl -s http://127.0.0.1:7788/status
curl -s -X POST http://127.0.0.1:7788/run -H "Content-Type: application/json" \
     -d '{"commands":[{"action":"get_title"}]}'
```

```text
GET  /status          健康检查（含 browser_mode / site_patterns（域名+别名）/
                      browser_starting / log_file / user_data_dir / instance_id / pid /
                      started_at / revision / maintenance / tab_idle_sec）
POST /session/start   启动/重配会话（context: "isolated" | "user"）
POST /session/reset   重置页面
POST /session/refresh 清除当前会话 cookies + storage（共享用户上下文中拒绝执行）
POST /session/stop    关闭单个会话（managed 最后一个会话关闭时关浏览器；attach 只断开连接）
POST /run             执行命令批次
POST /check           预设检验套件
GET  /sites           站点经验列表
GET  /site            读取站点经验（?domain= / ?url= / 用户原话，支持自由文本匹配）
POST /site            追加站点经验 {domain, aliases?, platform?, patterns?, pitfalls?}
GET  /attach/discover 列出可附着的本机浏览器（含远程调试 TCP 探测状态、应用数据根目录
                      文件夹名、Agent 下一步清单 next_steps）
POST /attach/select   保存附着偏好 {attach_dir | cdp_endpoint | forget}（重启服务后生效）
POST /macro/save      保存宏
POST /macro/run       运行宏
GET  /macro/list      列出宏
GET  /auth/profiles   列出所有 profile 元数据（不含 token 内容）
POST /auth/ensure     确保已登录：{profile, login_commands, into} — 失效则执行登录命令并落盘
GET  /stream          SSE 实时推送
POST /shutdown        维护端点，实例门控：body 须带当前 instance_id（先 GET /status 取得）；
                      不匹配 409 + 回显当前 instance_id；受理即进入 maintenance——排水期间
                      /run /session/start|reset|refresh|stop /check /auth/ensure
                      /auth/profiles /macro/run 拒绝新请求（ok=false + maintenance=true）；
                      /status /sites /macro/list /attach/* /shutdown 仍可用（排水中
                      同 instance_id 的关停重试得到 ok:true「正在关停」，过期 id 恒 409）
```

`/status` 的服务实例字段：`instance_id`（进程内 UUID）+ `pid` + `started_at` 用于确认
连的是同一个实例；`revision` 是核心源码指纹——升级代码后旧服务仍在跑可被识别
（升级需重启服务生效）；`browser_starting: true` 表示浏览器连接中（attach 时≈等待
用户在浏览器点「允许」）；`log_file` 是全生命周期日志路径（排障先看）。

**CLI 退出码**：`0` 成功；`1` 硬失败；`2` 需要用户动作（attach 等待授权弹窗、目标
浏览器未开/未开调试开关）——处理完重试即可，agent 脚本可据此分支。

CLI 补充：

```text
browser-agent-client find-url [关键词…] [--only bookmarks|history] [--browser <名>]
                               [--limit N] [--since 1d|7h|YYYY-MM-DD] [--sort recent|visits]
browser-agent-client attach --probe <应用数据根目录下的文件夹>   # 清单外浏览器受控探测
```
