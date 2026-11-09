---
name: browser_agent
description: Python Playwright 浏览器操作代理：AI 驱动、语义感知、持久微服务架构。涉及浏览器自动化、UI E2E、Playwright 驱动页面、认证会话 profile 时必须读。
---

# 浏览器代理技能 (Browser Agent Skill)

> **核心设计原则**：工具负责感知，AI 负责决策。
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

## 依赖安装

```bash
pip install .            # 或 pip install git+https://github.com/Howie-Hoo/browser-agent
playwright install chromium
```

## 资源预检（启动前 fail-fast）

Chromium 单实例常驻数百 MB 内存；低配机器（或同机已跑其他服务）余量不足时强行启动会把整机压垮。client 拉起服务（`start_service`）与 server 建会话（`ensure_proxy`）两侧都会预检，不通过即拒绝启动并说明原因：

- **内存**：宿主与 cgroup v2 限额可用量的较小者 < `BROWSER_AGENT_MIN_FREE_MB`（默认 768）→ 拒绝
- **负载**：配置 `BROWSER_AGENT_MAX_LOAD`（1min loadavg 上限）时才检查，默认不查
- 平台读不到内存信息且未装 psutil 时跳过检查（可用性优先）

被拒时的出路：腾出内存（停掉同机重型服务）后再跑、调低阈值（`BROWSER_AGENT_MIN_FREE_MB=512`，自担风险）、或改对已部署环境（build 产物、内存占用远低于 dev 热编译）执行浏览器验证。

## 会话模型（单 Chromium 复用 + 多会话隔离 + 批级排队）

多任务（并行测试 / 多个 AI 会话）共享同一个服务与 Chromium 实例，互不干扰：

- **复用**：服务只拉起一个 Chromium 实例，浏览器级配置（headless/viewport/代理）在实例存活期间固定；实例级配置冲突时新会话收到 `warning` 说明未生效
- **隔离**：每个 `session_id` 一个独立 browser context（cookie/localStorage 互不干扰），各持自己的 page 集与 active tab；`BrowserAgentClient(session_id=...)` 自动生成并透传；**不带 session_id 的旧客户端归入 default 会话**（完全向后兼容）
- **排队**：命令批（`/run`、`/check` 等）在执行锁上串行——后来者排队，等当前批完成；等待超过 `BROWSER_AGENT_QUEUE_TIMEOUT`（默认 600s，0=不限）返回明确超时错误，防无限堆积
- **会话关闭**：`/session/stop` 只关自己的 context；最后一个会话关闭时浏览器随之关闭（HTTP 服务保留）。多任务场景用它代替 `/shutdown`
- **空闲回收**：所有会话超过 `BROWSER_AGENT_IDLE_SHUTDOWN_SEC`（默认 900s，0=禁用）无活动 → 自动关闭浏览器释放内存；下次请求自动重新拉起（资源预检再次把关）

会话自检：`GET /status` 返回 `sessions` 列表（sid/owner/idle/tab）与阈值配置。

## 启动服务

```bash
# 方式1：直接启动服务端
browser-agent-server --port=7788 --url=http://your-app.com

# 方式2：通过客户端自动管理（推荐 AI 调用方式）
browser-agent-client start --url=http://your-app.com --port=7788

# 无头模式（默认有头）
browser-agent-server --headless --port=7788

# 未 pip install 时（仓库内直跑，两条等价）
python -m browser_agent.server --port=7788
python -m browser_agent.client start --port=7788
```

## 配置文件 `browser_agent.yaml`

从 `config.example.yaml` 复制到仓库根改名 `browser_agent.yaml`，或用环境变量 `BROWSER_AGENT_CONFIG` 指定任意路径；命令行参数优先级最高。

```yaml
browser:
  headless: false          # true 为无头模式
  base_url: "http://localhost:3000"
  slow_mo: 0               # 每步操作间延迟 ms
  ignore_https_errors: true # 自托管 https 源加载 profile 时需要
  viewport:
    width: 1440
    height: 900
server:
  port: 7788
proxy:
  server: ""               # 可选代理
stealth:
  enabled: true
storage:
  auto_load_state: false   # 是否自动加载上次状态
  auto_save_state: true    # 关闭服务前自动落盘活跃会话（state.json + 活跃 profile）
```

---

## AI 标准操作流程

```json
// 1. 导航（自动附带 page_summary）
{"action": "navigate", "url": "https://example.com", "wait_until": "networkidle"}

// 2. 探测验证码（登录/提交前必查）
{"action": "detect_captcha"}

// 3. 填表前分析表单结构
{"action": "form_analyze"}

// 4. 语义点击（无需 selector，首选）
{"action": "find_and_act", "description": "登录按钮", "act": "click"}

// 5. 等待结果（服务端条件监听，消除轮询）
{"action": "watch_for_change", "timeout": 10000,
 "conditions": [{"type": "url_contains", "value": "/console"}]}

// 6. 读取 SPA 页面结构
{"action": "get_a11y_tree"}

// 7. 批量填写表单
{"action": "fill_form", "fields": {"#phone": "13800138000", "#pwd": "secret"}}
```

---

## 命令与 HTTP API

全部命令表（导航 / 点击 / 输入与表单 / 等待 / DOM 读取 / 高级感知 / 截图 / 多标签页 / iframe / 状态持久化 / 认证会话 profile / 宏指令 / 通用参数）与 HTTP 端点清单见 [`docs/command-reference.md`](docs/command-reference.md)——查询型参考，按需 Ctrl+F。

---

## 透明被动能力（自动生效）

| 能力 | 说明 |
| --- | --- |
| Stealth 防指纹 | 自动注入 playwright-stealth |
| AI 自愈 Selector | 点击 Timeout 时自动尝试文字模糊匹配 |
| 失败诊断截图 | 失败步骤自动附加截图 + possible_cause |
| navigate 自动摘要 | 导航成功自动附加 page_summary |
| 强制 200 响应 | 所有 HTTP 返回 200，逻辑错误通过 ok 字段区分 |

---

## 认证会话管理（多项目接入）

browser_agent 的认证会话层是**通用、零项目知识**的——它只懂「storage_state 快照 + 登录态判定规则」，不懂 JWT、不懂 localStorage 键名、不懂任何登录端点。接入一个新项目只需两步：

### 1. 写项目侧适配器，生成 profile 文件

「profile」= 一组按名字落盘的浏览器登录态（cookies + localStorage），存于 `storage/profiles/{name}.json`，可跨会话/跨重启恢复、多账号隔离。profile 文件格式（= Playwright storageState + auth 元数据）：

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

适配器负责「怎么拿到凭证」——直签 token、调登录 API、或浏览器内走真实登录表单均可——然后把凭证填进 `storage_state.origins[].localStorage`，把判定规则填进 `auth.check`，写到 `storage/profiles/<name>.json`。**凭证只写文件，绝不进 stdout/日志**。

### 2. 在 browser_agent 里加载 profile，打开受保护页

```jsonc
POST /run {"commands":[
  {"action":"load_profile","name":"myapp-admin"},          // 加载登录态（默认建隔离 context）
  {"action":"navigate","url":"https://app.example.com/dashboard"},
  {"action":"check_auth"}                                    // 复核登录态
]}
```

或用复合端点 `POST /auth/ensure {profile, login_commands, into}`：profile 仍有效就复用，失效就执行 `login_commands`（任意命令序列，如 `navigate` 登录页 → `fill_form` → `click`）重新登录并落盘。profile 相关命令（`save_profile` / `load_profile` / `check_auth` / `ensure_auth` / `list_profiles` 等）见命令参考。

### 关键技术约束

- **Playwright 的 `storage_state()` 只能导出，不能导入已有 context**。因此 `load_profile` 默认 `into="new"`（用 `new_context(storage_state=...)` 原子注入，含 httpOnly/Secure，最可靠）；`into="current"` 为回退，需先 `goto(origin)` 同源才能写 localStorage。
- **`auto_save_state: true`**（见配置文件）在服务关闭前自动落盘活跃会话（`storage/state.json` + 当前活跃 profile），下次启动 `auto_load_state` 自动恢复。
- **路径安全**：profile 名仅允许字母数字/下划线/连字符/点，首字符须为字母数字（防路径穿越）；`list_profiles` 不返回凭证内容。
- **多账号隔离**：每个 `load_profile(name, into="new")` 建独立 context（`profile::<name>`），互不污染；`new_isolated_session(name, profile=...)` 也可在建隔离会话时注入指定 profile。

---

## 浏览器进程清理（强制）

测试结束后**必须**关闭浏览器实例，防止 Chrome for Testing 进程堆积占用内存。本 skill 自带幂等清理脚本（无残留时空操作；精准匹配本服务进程 + playwright driver + Chrome for Testing，不误杀）：

```bash
browser-agent-cleanup                 # pip 安装后（console 入口，cwd 无关）
bash browser_agent/cleanup.sh         # 仓库内直跑
```

仅靠 AI 自觉清理不可靠——pytest 被 kill / 异常中断时 fixture teardown 不执行，进程就残留。**接入项目时应配自动兜底**，三层防御都复用上面的脚本：

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

**③ Claude Code Stop hook**（`.claude/settings.json`，会话级最强兜底，覆盖测试被强杀）——用 `$CLAUDE_PROJECT_DIR` 绝对路径（Stop hook 的 cwd 不一定是项目根）：

```json
"hooks": { "Stop": [{ "hooks": [{ "type": "command", "command": "browser-agent-cleanup", "timeout": 10 }] }] }
```

> **接入新项目**：克隆本仓库并 `pip install -e <仓库路径>`，按 ②③ 接入 conftest + Stop hook 即获得自动兜底（Stop hook 用 `browser-agent-cleanup` 入口则与克隆位置无关）。若用裸 Playwright API（不经本服务），在 fixture teardown 显式 `browser.close(); playwright.stop()`。
