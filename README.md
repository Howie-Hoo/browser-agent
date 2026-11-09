# browser-agent

> **给 AI 代理装上真实浏览器的「手和眼」——感知优先、selector 兜底的本地 Playwright 微服务。**

[English](README.en.md)

## 为什么用它

- **模型没有视觉 / 没有 computer-use 也能操作网页。** 感知层把页面翻译成结构化文本（无障碍语义树、页面摘要、表单结构分析），不需要截图视觉理解、不需要官方 computer-use API——任何能发 HTTP 请求或跑 shell 命令的模型，都获得对真实浏览器的完整操作能力。
- **终端里的编码代理原生没有浏览器。** Claude Code / Cursor 这类 agent 改完前端代码后，只能靠测试断言间接确认。本服务让它们直接打开真实页面验证：SPA 渲染、cookie、登录态、JS 报错，看的是真相而不是猜。
- **selector 太脆。** 语义点击 `find_and_act`（用自然语言描述目标，无需 selector）+ 点击超时自动文字模糊匹配自愈，页面改版不至于全线失效。
- **每次测试冷启动浏览器太慢。** 服务常驻：单 Chromium 实例全程复用，多任务各占隔离 context，批命令排队执行，空闲自动回收内存。
- **登录态反复造。** 命名 profile 把登录态（cookies + localStorage + 判定规则）落盘复用，跨重启恢复、多账号互不污染。

设计哲学：**工具负责感知，AI 负责决策。**

## 工作方式

```
AI 代理（Claude Code / Cursor / 任意程序）
   │  JSON 命令批（HTTP）
   ▼
BrowserAgentClient（CLI / Python API，自动拉起服务 + 健康检查）
   ▼
browser-agent 服务（aiohttp 常驻：资源预检 / 批级排队 / 空闲回收）
   │  单 Chromium 实例复用
   ├─ session A ─ context 隔离（cookie / localStorage 互不干扰）
   ├─ session B ─ context 隔离 + 认证 profile（登录态跨重启）
   └─ 底层永远可用：click / fill / eval_js / screenshot
```

**三层降级**——高层失败逐级回落，永远有路可走：

```
语义层（首选）：get_a11y_tree / find_and_act / form_analyze / detect_captcha
     ↓ 失败 → ai_hint 告知降级方式
结构层（备用）：get_interactive_elements / get_page_summary / extract
     ↓ 失败 → ai_hint 告知降级方式
底层精准（兜底）：click / fill / eval_js / screenshot（始终可用）
```

## 快速开始

```bash
pip install .          # 或 pip install git+https://github.com/Howie-Hoo/browser-agent
playwright install chromium

# 启动服务（客户端自动管理，推荐）
browser-agent-client start --url=http://localhost:3000 --port=7788
```

**Python API：**

```python
from browser_agent.client import BrowserAgentClient

with BrowserAgentClient(base_url="http://localhost:3000") as client:
    report = client.run([
        {"action": "navigate", "url": "http://localhost:3000"},
        {"action": "detect_captcha"},                      # 登录/提交前探测验证码
        {"action": "form_analyze"},                        # 填表前分析表单结构
        {"action": "find_and_act", "description": "登录按钮", "act": "click"},   # 语义点击
        {"action": "watch_for_change", "timeout": 10000,   # 服务端条件监听，消除轮询
         "conditions": [{"type": "url_contains", "value": "/console"}]},
        {"action": "get_a11y_tree"},                       # 无障碍语义树（SPA 首选）
    ])
    print(report["summary"])
```

**CLI：** `status | start | run <json> | check <type> | reset | shutdown`

命令全表（40+ 命令 / 15 个 HTTP 端点）见 [docs/command-reference.md](docs/command-reference.md)。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 三层感知降级 | 语义层 → 结构层 → 底层精准操作，失败自动建议降级路径 |
| 单实例多会话 | 一个 Chromium、N 个隔离 context；命令批串行排队（`BROWSER_AGENT_QUEUE_TIMEOUT` 防无限堆积）；全空闲超时自动关浏览器回收内存 |
| 认证会话 profile | 命名登录态落盘（Playwright storageState + 判定规则三型：`url_pattern` / `js` / `cookie_exists`）；`ensure_auth` 有效即复用、失效即按登录命令序列重登；多账号隔离；凭证只落文件不进日志 |
| 资源预检 fail-fast | 启动前查内存（宿主与 cgroup v2 取较小者），低于 `BROWSER_AGENT_MIN_FREE_MB` 拒绝拉起，防低配机器被 Chromium 压垮 |
| AI 友好细节 | 恒返 200 + `ok` 字段、失败自动截图 + possible_cause、导航自动附页面摘要、裁剪截图省 token、stealth 防指纹 |
| 进程清理 | 幂等清理脚本（`browser-agent-cleanup`），pytest conftest / Claude Code Stop hook 三层兜底接入 |

profile 接入两步法与格式定义见 [SKILL.md](SKILL.md)「认证会话管理」。

## AI 代理接入

本仓库根的 [SKILL.md](SKILL.md) 是给 AI 代理读的操作手册（降级层级、标准流程、会话模型、约束）。在 Claude Code 里接入：把仓库克隆到项目旁，`pip install -e <仓库路径>`，再在项目 `CLAUDE.md` 里指向本仓库 SKILL.md 即可。

## 配置

从 `config.example.yaml` 复制到仓库根改名 `browser_agent.yaml`（或用 `BROWSER_AGENT_CONFIG` 指定路径）。命令行参数优先级最高。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `BROWSER_AGENT_MIN_FREE_MB` | 768 | 启动浏览器要求的最低可用内存（MB） |
| `BROWSER_AGENT_MAX_LOAD` | 不检查 | 1min loadavg 上限，超过拒绝启动 |
| `BROWSER_AGENT_QUEUE_TIMEOUT` | 600 | 命令批排队等待上限（秒，0 = 不限） |
| `BROWSER_AGENT_IDLE_SHUTDOWN_SEC` | 900 | 全会话空闲多久自动关浏览器（秒，0 = 禁用） |
| `BROWSER_AGENT_REPORT_DIR` | 仓库 temp/ | 执行报告 / 截图输出目录 |
| `BROWSER_AGENT_CONFIG` | — | 配置文件路径覆盖 |

## License

[MIT](LICENSE)
