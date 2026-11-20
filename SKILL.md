---
name: browser-agent
description: Python Playwright 浏览器操作代理：AI 驱动、语义感知、持久微服务架构，支持附着用户日常浏览器（登录态免费）与站点经验积累。涉及浏览器自动化、UI E2E、Playwright 驱动页面、认证会话 profile、操作外部网站时必须读。
---

# 浏览器代理技能 (Browser Agent Skill)

> **核心设计原则**：工具负责感知，AI 负责决策。本 skill 讲清 tradeoff 与技术事实，
> 怎么选由 AI 自己判断——不替你推理。
>
> **降级层级**（高层失败时逐级降级）：
>
> ```text
> 语义层（首选）：get_a11y_tree / find_and_act / form_analyze / detect_captcha
>      ↓ 失败 → ai_hint 告知降级方式
> 结构层（备用）：get_interactive_elements / get_page_summary / extract
>      ↓ 失败 → ai_hint 告知降级方式
> 底层精准（兜底）：click / fill / eval_js / screenshot（始终可用）
> ```

---

## 前置检查

启动或确认服务后，`GET /status`（或 `browser-agent-client status`）会输出：

- `browser_mode`：`managed`（自拉隔离 Chromium）/ `attach`（附着日常浏览器）
- `site_patterns`：**已积累的站点经验域名列表**——确定目标网站后，若列表命中，
  **必须先 `read_site` 读取先验**（平台特征 / 有效模式 / 已知陷阱）再操作
- `user_data_dir`：用户数据目录（profiles / 站点经验 / 状态，升级不丢）

## 操作哲学

**像人一样思考，目标驱动而非步骤驱动。** 带着目标进入，边看边判断，遇到阻碍就解决，
全程围绕「我要达成什么」做决策：

1. **拿到请求** — 先明确成功标准：需要获取什么信息、执行什么操作、达到什么结果？
   这是后续所有判断的锚点。
2. **选择起点** — 按任务性质选最可能直达的方式验证一次；不成功再调整，而不是预排
   一长串步骤机械执行。
3. **过程校验** — 每一步的结果都是**证据**，不只是成功/失败的二元信号。用它对照
   成功标准：路径在推进吗？方向错了立即调整，**不在同一个方式上反复重试**——
   选择器超时不等于「再试一次就好」，也可能是页面已跳转、目标根本不存在。
4. **完成判断** — 对照成功标准确认后才停止；但不过度操作，不为「完整」而浪费代价。

**导航成功 ≠ 任务完成**（内容就绪契约）：`navigate` 返回、`readyState` 完成只说明
页面加载了，不说明目标内容出现。遇到异步渲染/中间页，用 `watch_for_change` /
`smart_wait` / `wait_for_stable` 等到**目标内容本身**出现或确认受阻，再决定下一步。

**弹窗/登录墙未必挡路**：判断它是否真的挡住了目标——内容可能已在 DOM 中（轮播的
非当前帧、折叠区块、懒加载占位之后的真实节点），交互只是展示手段。以数据结构
（容器、属性、节点关系）为单位思考，`eval_js` / `extract` 可直接触达已加载未展示
的内容；Shadow DOM 与 iframe 边界用 `switch_frame` 或 eval 递归穿透。

**程序化 vs GUI 式操作**（按平台灵活选择）：

- 程序化（构造 URL、eval 操作 DOM）：快、精确，但对站点不是正常用户行为，可能触发风控
- GUI 式（`find_and_act` 语义点击、`fill_form` 填表）：慢、步骤多，但确定性最高

GUI 式还是**探测手段**：一次真实交互能观察到站点的实际行为（URL 模式、必需参数、
跳转逻辑），为后续程序化操作提供依据；程序化受阻时 GUI 式是可靠兜底。

**两条硬事实**：

- **站点内交互产生的链接是可靠的**：点击卡片/条目到达的 URL 天然携带平台所需的
  完整上下文（含隐式参数）。手动拼的 URL 可能缺失这些参数，导致被拦、报错页、
  甚至触发反爬。提取 URL 时保留完整地址，不要裁剪参数。
- **平台报错不可信**：「内容不存在」「页面不见了」可能反映的是访问方式问题
  （URL 缺参数、触发风控），而非内容真的不存在。换一种到达方式再下结论。

## 站点经验（越用越好）

操作经验按域名积累在 `~/.browser-agent/site-patterns/{domain}.md`，跨会话复用：

- **读**：前置检查输出 `site_patterns` 列表；`navigate` 到已积累域名时响应自动附
  `site_experience` 提示。命中即先 `read_site`（领域/子域名/别名自动匹配）获取先验。
- **写**：任务成功后，发现了可复用的模式（URL 结构、选择器、操作策略）或陷阱
  （什么会失败及为什么），用 `write_site` 记录。**只写经验证的事实，不写猜测。**
- **态度**：经验条目带发现日期，当作「可能有效的提示」而非「保证正确的事实」。
  按经验操作失败 → 回退通用模式 → 更新经验文件。

```json
{"action": "read_site", "domain": "example.com"}
{"action": "write_site", "domain": "example.com",
 "patterns": ["列表页 /explore?page=N 直接翻页"],
 "pitfalls": ["图片懒加载，提取 URL 前先滚动到底"]}
```

## 账号风险提示（操作社交平台前必须展示）

> 温馨提示：通过浏览器自动化操作社交平台存在账号被平台限流或封禁的风险，
> 已内置防护措施但无法完全避免，继续操作即视为接受。建议必要时使用小号。

## 依赖安装

```bash
pip install git+https://github.com/Howie-Hoo/browser-agent
playwright install chromium   # 仅 managed 模式需要（attach 模式附着现有浏览器，无需下载）
```

## 资源预检（managed 模式启动前 fail-fast）

Chromium 单实例常驻数百 MB 内存；低配机器余量不足时强行启动会把整机压垮。client
拉起服务与 server 建会话两侧都会预检，不通过即拒绝启动并说明原因：

- **内存**：宿主与 cgroup v2 限额可用量的较小者 < `BROWSER_AGENT_MIN_FREE_MB`（默认 768）→ 拒绝
- **负载**：配置 `BROWSER_AGENT_MAX_LOAD`（1min loadavg 上限）时才检查，默认不查
- attach 模式不拉浏览器，跳过预检

被拒时的出路：腾出内存后再跑、调低阈值（自担风险）、或改用 attach 模式（零浏览器开销）。

## 浏览器接入模式

| | managed（默认） | attach |
|---|---|---|
| 浏览器 | 自拉隔离 Chromium（Chrome for Testing） | 用户日常浏览器（Chrome/Edge/Brave/Arc…任意 Chromium 内核） |
| 登录态 | profile 注入（见「认证会话管理」） | **免费**——用户真实 profile 的 cookies 直接可用 |
| 指纹 | playwright-stealth 防指纹 | 真实浏览器环境，天然可信（自动禁用 stealth） |
| 适用 | 测试 / E2E / 多账号隔离 | 「操作我正用的网站」、登录墙重、反爬严的平台 |
| 会话隔离 | context 隔离（默认） | `context=isolated`（incognito）或 `context=user`（真实 profile） |

**attach 模式最小侵入契约**：**未经用户明确要求，不操作用户已有标签页**，只在自建
标签页里工作；用户明确要求时（如「切到我的第一个标签页」）用 `list_user_tabs` +
`adopt_tab` 收养目标页操作（收养页在服务/会话关闭时只取消注册、绝不关闭）。会话
结束只关自建标签页；绝不关闭用户的浏览器或清除其 storage（`clear_storage` /
`session/refresh` 在用户上下文中会拒绝执行）；`switch_tab` 不抢自建页焦点。

**连接授权与空闲回收**：Chrome 新式开关（chrome://inspect 勾选）下，**每条新调试连接
都要用户在浏览器里点一次允许**——因此服务采用长驻单连接：附着时点一次，之后所有操作
复用。空闲回收（`BROWSER_AGENT_IDLE_SHUTDOWN_SEC`，默认 900s，0=禁用）在 attach 模式
**只关超时会话的自建标签页、连接保持不断**：既防孤儿标签页，又不用重新授权。

**attach 安全边界**：不扫描固定端口，只读所配置浏览器数据目录的
`DevToolsActivePort`；配置的浏览器不可用即硬错并给出处理步骤，**绝不静默连到别的
浏览器**；多台浏览器共存时必须明确选择。

```bash
browser-agent-client attach --discover        # 列出本机浏览器（含远程调试开关状态）
browser-agent-client attach "<数据目录>"      # 保存偏好并以 attach 模式启动
browser-agent-client attach --forget          # 回到 managed 模式
```

前置：用户在其浏览器打开 `chrome://inspect/#remote-debugging` 勾选
"Allow remote debugging for this browser instance"（可能需重启浏览器）。
旧版本浏览器无此开关时：完全退出后用 `--remote-debugging-port=9222` 启动**同一个**
浏览器（数据目录不变）即可附着。任意 Chromium 内核浏览器都支持（清单外的把其数据
目录填给 `attach_dir`）；Firefox / Safari 非 Chromium 内核、无 CDP，不在支持范围。

## 会话模型（单实例复用 + 多会话隔离 + 会话级并行）

多任务（并行测试 / 多个 AI 会话）共享同一个服务与浏览器实例，互不干扰：

- **复用**：服务只拉起一个浏览器实例，浏览器级配置（headless/viewport/代理）在实例
  存活期间固定；实例级配置冲突时新会话收到 `warning` 说明未生效
- **隔离**：每个 `session_id` 一个独立 browser context（cookie/localStorage 互不干扰），
  各持自己的 page 集与 active tab；`BrowserAgentClient(session_id=...)` 自动生成并透传；
  **不带 session_id 的旧客户端归入 default 会话**（完全向后兼容）
- **共享**：`session/start` 传 `context: "user"` 的会话落在共享 default context
  （attach 模式 = 用户真实 profile，登录态免费）；关闭时只关自建标签页
- **并行模型**：命令批**按会话**加锁——不同会话的批真并行（contextvar 标签上下文
  保证各批路由到各自的页，互不串扰），同一会话内串行保序（页面状态有序变更）；
  同会话排队等待超过 `BROWSER_AGENT_QUEUE_TIMEOUT`（默认 600s，0=不限）返回明确超时。
  会话忙时 stop/reset/refresh 等控制操作 10s 内拿不到锁即拒绝（不被长批卡死）
- **会话关闭**：`/session/stop` 只关自己的页面/context；最后一个会话关闭时 managed
  关浏览器、attach 断开连接（用户浏览器保持运行）。多任务场景用它代替 `/shutdown`
- **空闲回收**：所有会话超过 `BROWSER_AGENT_IDLE_SHUTDOWN_SEC`（默认 900s，0=禁用，
  改动即时生效）无活动 → managed 关浏览器释放内存；attach **只关自建标签页、连接保持
  不断**（断连需用户重新授权，孤儿标签页照样清理）。下次请求自动重启/复用连接

会话自检：`GET /status` 返回 `sessions` 列表（sid/owner/active_tab/context_mode/idle）
与阈值配置。

## 启动服务

```bash
# 方式1：直接启动服务端
browser-agent-server --port=7788 --url=http://your-app.com

# 方式2：通过客户端自动管理（推荐 AI 调用方式）
browser-agent-client start --url=http://your-app.com --port=7788

# attach 模式（附着日常浏览器）
browser-agent-client attach --discover
browser-agent-client attach "$HOME/Library/Application Support/Google/Chrome"

# 无头模式（默认有头）
browser-agent-server --headless --port=7788

# 未 pip install 时（仓库内直跑，两条等价）
python -m browser_agent.server --port=7788
python -m browser_agent.client start --port=7788
```

## 配置文件 `browser_agent.yaml`

从 `config.example.yaml` 复制到仓库根改名 `browser_agent.yaml`，或用环境变量
`BROWSER_AGENT_CONFIG` 指定任意路径；命令行参数优先级最高。

```yaml
browser:
  mode: managed          # managed | attach
  attach_dir: ""         # attach：浏览器用户数据目录（留空按偏好/环境变量/唯一浏览器解析）
  cdp_endpoint: ""       # attach：显式 CDP 端点（高级用法）
  headless: false
  base_url: "http://localhost:3000"
  slow_mo: 0
  ignore_https_errors: true
  viewport: {width: 1440, height: 900}
server: {port: 7788}
proxy: {server: ""}
stealth: {enabled: true} # attach 模式自动禁用
storage:
  auto_load_state: false
  auto_save_state: true  # 活跃页在共享用户上下文时自动跳过（不导出用户全部登录态）
```

**用户数据目录**：profiles / 站点经验 / state.json / downloads 全在
`~/.browser-agent/`（`BROWSER_AGENT_HOME` 可覆盖），pip 升级重装不丢；旧版落在包目录
里的数据首次运行自动迁移（只复制不覆盖）。

---

## AI 标准操作流程

```json
// 0. 目标站点已有经验 → 先读先验（前置检查 site_patterns 命中时）
{"action": "read_site", "domain": "example.com"}

// 1. 导航（自动附带 page_summary；已有经验的域名自动附 site_experience 提示）
{"action": "navigate", "url": "https://example.com", "wait_until": "networkidle"}

// 2. 探测验证码（登录/提交前必查）
{"action": "detect_captcha"}

// 3. 填表前分析表单结构
{"action": "form_analyze"}

// 4. 语义点击（无需 selector，首选）
{"action": "find_and_act", "description": "登录按钮", "act": "click"}

// 5. 等待结果——等到目标内容/URL 变化，而非固定延时
{"action": "watch_for_change", "timeout": 10000,
 "conditions": [{"type": "url_contains", "value": "/console"}]}

// 6. 读取 SPA 页面结构
{"action": "get_a11y_tree"}

// 7. 批量填写表单
{"action": "fill_form", "fields": {"#phone": "13800138000", "#pwd": "secret"}}

// 8. 任务成功且发现可复用模式/陷阱 → 写回经验（只写经验证的事实）
{"action": "write_site", "domain": "example.com", "patterns": ["…"], "pitfalls": ["…"]}
```

---

## 命令与 HTTP API

全部命令表（导航 / 点击 / 输入与表单 / 等待 / DOM 读取 / 高级感知 / 截图 / 多标签页 /
iframe / 状态持久化 / 认证会话 profile / 站点经验 / 宏指令 / 通用参数）与 HTTP 端点
清单见 [`docs/command-reference.md`](docs/command-reference.md)——查询型参考，按需 Ctrl+F。

---

## 透明被动能力（自动生效）

| 能力 | 说明 |
| --- | --- |
| Stealth 防指纹 | 自动注入 playwright-stealth（attach 模式自动禁用） |
| AI 自愈 Selector | 点击 Timeout 时自动尝试文字模糊匹配 |
| 失败诊断截图 | 失败步骤自动附加截图 + possible_cause |
| navigate 自动摘要 | 导航成功自动附加 page_summary；已积累域名附 site_experience |
| 强制 200 响应 | 所有 HTTP 返回 200，逻辑错误通过 ok 字段区分 |

---

## 媒体与视频

- **图片内容优先 DOM 直取**：判断信息在图里时，用 `eval_js` / `get_attr` 从 DOM 拿
  图片 URL 再定向读取，比全页截图精准得多。公开资源直接下载后读；需要登录态的才在
  浏览器内 navigate + screenshot。
- **懒加载**：提取图片 URL 前先 `scroll` 到底（或 `scroll_until_found`），未进视口
  的图可能尚未加载。
- **视频**：`eval_js` 操控 `<video>`（时长 / seek 到任意时间点 / 播放暂停），配合
  `screenshot` 采帧，可对视频内容离散采样分析。
- **省 token**：元素级截图用 `clip_screenshot`，别动辄整页。

## 并行调研：子 Agent 分治

任务包含多个**独立**目标（同时调研 N 个产品 / N 个来源）时，分发子 Agent 并行执行，
主 Agent 只汇总：

- **速度**：并行执行，总耗时≈单个子任务时长
- **上下文保护**：抓取内容不进主 Agent 上下文，主 Agent 只接收摘要

**会话分治**：每个子 Agent 独立 `session_id`（context 隔离 + 会话级并行，多子 Agent
  的浏览器操作真正同时执行），
任务结束各自 `/session/stop`。需要登录态时（attach 模式）用 `context: "user"` 共享
用户 profile。

| 适合分治 | 不适合分治 |
|----------|-----------|
| 目标相互独立，结果互不依赖 | 下一步需要上一步的结果 |
| 每个子任务量足够大（多页抓取、多轮交互） | 简单单页查询，分治开销大于收益 |
| 需要浏览器或长时间运行的任务 | 几次读取就能完成的轻量查询 |

**子 Agent prompt 写法：目标导向，不写步骤**——主 Agent 说清**要什么**（「获取」「调研」
「了解」），避免用暗示具体手段的动词（「搜索」「抓取」「点击」）把子 Agent 锚定到
错误路径；仅在必要且确信时限定怎么做。子 Agent prompt 中写明
`必须加载 browser-agent skill 并遵循指引`。

## 认证会话管理（多项目接入）

browser_agent 的认证会话层是**通用、零项目知识**的——它只懂「storage_state 快照 +
登录态判定规则」，不懂 JWT、不懂 localStorage 键名、不懂任何登录端点。接入一个新
项目只需两步：

### 1. 写项目侧适配器，生成 profile 文件

「profile」= 一组按名字落盘的浏览器登录态（cookies + localStorage），存于
`~/.browser-agent/profiles/{name}.json`，可跨会话/跨重启恢复、多账号隔离。profile
文件格式（= Playwright storageState + auth 元数据）：

```jsonc
{
  "name": "myapp-admin",
  "auth": {
    "origin": "https://app.example.com",          // 必填：check 与 localStorage 注入的目标源
    "check": {                                      // 登录态判定规则，三种类型任选其一
      "type": "url_pattern",                        // goto origin 后，url 匹配 pattern 即「未登录」
      "pattern": "/login", "negative": true         // negative=true 翻转：不在 /login 页 = 已登录
      // 或 "type":"js", "script":"()=>!!localStorage.getItem('token')"
      // 或 "type":"cookie_exists", "name":"sid", "domain":".example.com"
    }
  },
  "storage_state": {
    "cookies": [],
    "origins": [{"origin":"https://app.example.com",
                 "localStorage":[{"name":"token","value":"<凭证值>"}]}]
  }
}
```

适配器负责「怎么拿到凭证」——直签 token、调登录 API、或浏览器内走真实登录表单均可
（attach 模式下用户已登录的场景**无需 profile**，直接 `context: "user"` 会话即可）——
然后把凭证填进 `storage_state.origins[].localStorage`，把判定规则填进 `auth.check`，
写到 `~/.browser-agent/profiles/<name>.json`。**凭证只写文件，绝不进 stdout/日志**。

### 2. 在 browser_agent 里加载 profile，打开受保护页

```jsonc
POST /run {"commands":[
  {"action":"load_profile","name":"myapp-admin"},          // 加载登录态（默认建隔离 context）
  {"action":"navigate","url":"https://app.example.com/dashboard"},
  {"action":"check_auth"}                                    // 复核登录态
]}
```

或用复合端点 `POST /auth/ensure {profile, login_commands, into}`：profile 仍有效就
复用，失效就执行 `login_commands`（任意命令序列，如 `navigate` 登录页 → `fill_form`
→ `click`）重新登录并落盘。profile 相关命令（`save_profile` / `load_profile` /
`check_auth` / `ensure_auth` / `list_profiles` 等）见命令参考。

**登录判断哲学**：核心问题只有一个——**目标内容拿到了吗？** 打开页面先直接尝试获取
目标内容；只有确认拿不到且判断登录能解决时，才走 profile / 请用户在浏览器中登录。
不要一上来就做登录流程。

### 关键技术约束

- **Playwright 的 `storage_state()` 只能导出，不能导入已有 context**。因此
  `load_profile` 默认 `into="new"`（用 `new_context(storage_state=...)` 原子注入，含
  httpOnly/Secure，最可靠）；`into="current"` 为回退，需先 `goto(origin)` 同源才能写
  localStorage。
- **`auto_save_state: true`** 在服务关闭前自动落盘活跃会话（`~/.browser-agent/state.json`
  + 当前活跃 profile）；活跃页在共享用户上下文（attach/user）时**自动跳过**——不把
  用户日常浏览器的全部登录态悄悄落盘。
- **路径安全**：profile 名仅允许字母数字/下划线/连字符/点，首字符须为字母数字（防
  路径穿越）；`list_profiles` 不返回凭证内容。
- **多账号隔离**：每个 `load_profile(name, into="new")` 建独立 context（`profile::<name>`），
  互不污染；`new_isolated_session(name, profile=...)` 也可在建隔离会话时注入指定 profile。

---

## 浏览器进程清理（强制）

测试结束后**必须**关闭浏览器实例，防止 Chrome for Testing 进程堆积占用内存。本
skill 自带幂等清理脚本（无残留时空操作；精准匹配本服务进程 + playwright driver +
Chrome for Testing，不误杀；attach 模式不会杀用户浏览器——它只匹配本服务自拉进程）：

```bash
browser-agent-cleanup                 # pip 安装后（console 入口，cwd 无关）
bash browser_agent/cleanup.sh         # 仓库内直跑
```

仅靠 AI 自觉清理不可靠——pytest 被 kill / 异常中断时 fixture teardown 不执行，进程就
残留。**接入项目时应配自动兜底**，三层防御都复用上面的脚本：

**① 测试 fixture teardown**（单次测试正常结束）：browser fixture `yield` 后 `client.shutdown()`

**② pytest 兜底**（项目 `conftest.py`，覆盖异常 / 中断）——路径用 `__file__` 解析，不依赖 cwd：

```python
import atexit, contextlib, subprocess
from pathlib import Path

# browser-agent 仓库克隆位置（按实际路径调整）
BROWSER_AGENT_REPO = Path(__file__).resolve().parents[1] / "browser-agent"

def _cleanup_browser_agent() -> None:
    script = BROWSER_AGENT_REPO / "browser_agent" / "cleanup.sh"
    with contextlib.suppress(Exception):
        subprocess.run(["bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

atexit.register(_cleanup_browser_agent)

@pytest.fixture(scope="session", autouse=True)
def _cleanup_after_session():
    yield
    _cleanup_browser_agent()
```

**③ Claude Code Stop hook**（`.claude/settings.json`，会话级最强兜底，覆盖测试被强杀）——用
`$CLAUDE_PROJECT_DIR` 绝对路径（Stop hook 的 cwd 不一定是项目根）：

```json
"hooks": { "Stop": [{ "hooks": [{ "type": "command", "command": "browser-agent-cleanup", "timeout": 10 }] }] }
```

> **接入新项目（任意 AI agent 通用）**：克隆本仓库并 `pip install -e <仓库路径>`（或
> `pip install git+<仓库地址>`），按 ②③ 接入 conftest + Stop hook 即获得自动兜底。
> 若用裸 Playwright API（不经本服务），在 fixture teardown 显式
> `browser.close(); playwright.stop()`。
