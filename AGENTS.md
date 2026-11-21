# browser-agent

面向 AI 编程代理的本地浏览器微服务（Playwright）：感知优先、selector 兜底、多会话
隔离、认证 profile 复用；v2026.11.20 起支持附着用户日常浏览器（attach 模式）与
站点经验积累。Python ≥ 3.10。

## 模块地图

- 使用契约（操作哲学 / 降级层级 / 站点经验 / 并行分治 / attach 用法）→ `SKILL.md`
- 命令与 HTTP API 速查 → `docs/command-reference.md`
- 浏览器操作核心（两种模式的 start/stop、命令分发 `_dispatch`）→ `browser_agent/proxy.py`
- 点击/输入/表单/滚动命令族（mixin）→ `browser_agent/proxy_input.py`
- AI 高级感知（a11y 树 / find_and_act / form_analyze / watch_for_change…）→ `browser_agent/proxy_ai.py`（mixin）
- 多标签页 / iframe / 持久化 / 断言 → `browser_agent/proxy_tabs.py`（mixin）
- 认证 profile（storage_state + 判定规则三型）→ `browser_agent/proxy_auth.py`（mixin）
- 站点经验（读写/匹配，纯文件操作 + 命令 mixin）→ `browser_agent/site_patterns.py`
- 本地浏览器资源检索（书签/历史，纯文件操作 + 命令 mixin）→ `browser_agent/local_search.py`
- 用户数据目录（~/.browser-agent/，一次性迁移）→ `browser_agent/user_data.py`
- attach 模式（浏览器发现 / DevToolsActivePort 四分类 / 偏好 / 端点解析 /
  进程级 pin / 清单外浏览器受控探测 / 分类 remedy 与代开命令提示）→
  `browser_agent/browser_attach.py`
- HTTP 服务（会话模型 / 会话级并行调度 / 忙时拒绝 / 关停排水 / 全部端点）→
  `browser_agent/server.py`（`build_app()` 独立构建，供测试挂载）
- HTTP 来源校验（回环 Host 白名单 / 拒绝浏览器 Origin / body 必须 JSON——
  判定为纯函数）→ `browser_agent/http_guard.py`
- 全生命周期日志（滚动文件，单写者=服务进程）→ `browser_agent/logs.py`
- 客户端 CLI 与 Python API → `browser_agent/client.py`
- 进程清理（幂等精确进程树：三重身份核对 + 优雅关停优先，跨平台，
  绝不按进程名全局查杀）→ `browser_agent/cleanup.py`

## 回归验证

```bash
pytest tests/ -q                            # 195 单测：会话模型/锁语义/dispatch 命名空间/守卫/维护态/页级空闲回收
                                           #       （含在途会话锁守卫/撕裂快照容错/越界 profile 拒绝）/用户数据迁移/站点经验（含自由文本匹配）/
                                           #       本地资源检索/attach 解析+pin+四分类+受控探测/click_xy/scroll direction/端口防护/
                                           #       HTTP 来源校验+关停实例门（真实中间件链+全路由攻防矩阵）/精确清理决策/全生命周期日志/
                                           #       go_back/upload 多文件/list_frames/eval 隔离/截图参数/CLI 解析+退出码+attach 就绪等待
                                           # （另有 64 个 E2E 用例默认跳过）
RUN_E2E=1 pytest tests/test_e2e_managed.py -v        # E2E 标的：managed 全命令面 + 并行证明（真并行/同会话保序/跨会话隔离）
RUN_E2E=1 pytest tests/test_e2e_absorbed.py -v       # E2E：click_xy 真命中/scroll 方向/实例身份/maintenance 排水/页级回收
RUN_E2E=1 pytest tests/test_e2e_attach.py -v         # E2E：attach 真机（CDP 附着 + 守卫 + 断开不杀浏览器 + 错误路径四分类 +
                                                   #        CLI attach 成功全链路 + 挂死端点连接超时）
RUN_E2E=1 pytest tests/test_e2e_absorbed_attach.py -v  # E2E：调试端口防护真拦截 + 运行中 pin 拒切 + probe 真机
RUN_E2E=1 pytest tests/test_e2e_lifecycle.py -v      # E2E 生命周期闭环：CLI 全链路/attach 就绪与退出码 2/精确清理真机（优雅+挂死强杀）/
                                                   #   经验前置闭环/go_back/JPEG/多文件上传/list_frames/隔离求值/日志链完整性
```

手工验证清单（弹框/真人交互无法自动化，改动 attach 连接链路后按此复验）：

- **新式授权链路**（chrome://inspect 开关，弹框只能人点）：`browser-agent-client
  attach <日常浏览器目录>` → 浏览器弹授权框 → 点「允许」→ CLI 退出码 0 +
  “浏览器已附着 ✓”；**不点** → 2s 授权提示 + 20s 窗口超时 → 退出码 2 且服务
  存活（`browser_active: false`）；重跑 attach → 再弹框 → 点允许 → 0。全程只操作
  自建标签页（空白自建页会被自动导航到 base_url，注意传 `--url` 或知悉此行为）。
  2026-11-21 已在真实日常 Chrome（新式开关 + ws 端点 + HTTP 404）全链路验证。

测试纪律：**绝不触碰真实用户数据与真实浏览器**——用 `BROWSER_AGENT_HOME` 指向临时
目录、FakeProxy 替身、伪造浏览器数据目录（Local State + DevToolsActivePort 假文件）；
attach E2E 用临时 profile 的独立 Chrome 实例（`--remote-debugging-port=0` 自管端口），
不碰用户日常 profile。

## 发版流程

```bash
scripts/release.sh          # 预检 → 版本一致性 → 单测 → tag → 推送 → GitHub Release
```

- 版本号三处必须一致才能发：`browser_agent/__init__.py`、`.claude-plugin/plugin.json`
  （插件市场靠它识别更新）、版本提交（惯例 `feat: vX — 主题`，**正文会被原样用作
  release notes**）；脚本会逐一校验，不一致即中止
- tag 为 annotated `vX`，指向发布时 HEAD；release 自动标 latest
- 补历史版本的 release 用 `gh release create vX --verify-tag --notes-file …`，
  且先补旧版再发新版，避免抢走 latest

## 不变式（改动时必须保持）

- **并发模型**：操作目标页由 `proxy._current_tab`（contextvars 存**可变 router 字典**
  `{"tab","sid"}`——wait_for 子任务写全版本回传）决定。批入口唯一合法写入点是
  `_begin_batch(sid, tab)`；任何切换页的代码必须走 `_set_current_tab`；标签页命令的
  sid 命名空间化只在 `_dispatch` 咽喉点做（`_TAB_KEY_ACTIONS`，注意 load_profile 的
  name 是 profile 名**不在**其中）；按页隔离的运行态（frames/routes/健康缓冲/元素
  缓存/active profile）必须以 `_tab()` 为键存取。新增共享可变状态时先问：它按页隔离了吗？
- 命令批**按会话**加锁（跨会话并行、同会话串行）；控制操作短超时拒绝忙会话；
  关停先排水（等在途批 ≤10s；inflight 计数在 `AppState.ensure_session` 与
  `execute_commands` 两层——CDP 授权等待对排水可见，批后扫页在减计数**之前**完成）；
  `/shutdown` 触发即置 **maintenance**——由 aiohttp 中间件按白名单统一拒绝
  （白名单：`/` `/status` `/stream` `/sites` `/macro/list` `/attach/discover`
  `/attach/select`；新端点默认受保护，不靠逐处粘贴守卫）
- 用户数据（profiles / site-patterns / state.json / downloads / attach.env /
  browser_agent.pid 等运行态）只落在 `~/.browser-agent/`，**永不写包目录**——
  pip 升级不能丢数据、不留 stale pid。
- attach 模式最小侵入：未经用户明确要求不操作用户已有标签页（明确要求时经
  `list_user_tabs` / `adopt_tab` 收养，收养页只注销绝不关闭）；只关自建标签页；
  绝不 close 用户 default context、不清用户 storage、不抢自建页焦点、不把用户
  登录态悄悄落盘；空闲回收只清标签页、**连接永不断**（Chrome 每次重连都要用户授权）。
- attach 安全边界：不扫描固定端口；只读所配置数据目录的 DevToolsActivePort；
  目标不可用即硬错（带 candidates + 分类 remedy），**绝不静默连到别的浏览器**；
  失败四分类：文件层三类（not-enabled / unreadable / invalid）在
  `read_devtools_info` 单点判定（含非 ASCII 损坏字节），offline 由
  `_describe_browser_dir` / `_resolve_dir` 的 TCP 探测补充并带 reason——
  auto 解析据此分诊，崩溃残留的 stale 端口文件不得误报为可附着；
  remedy 按类给（agent 先代开浏览器再升级找用户；unreadable 指向显式端点配置）；
  `error_response` 的 klass 是必填关键字（漏传=TypeError，不许静默误分类）；
  `resolve_endpoint` 含同步文件 IO + TCP 探测，事件循环上调用必须 `to_thread`；
  进程级身份 pin——身份令牌含**端点直连**（目录 ↔ 端点、端点 ↔ 端点' 都算换目标），
  目录身份比较经 realpath + 大小写归一（macOS/Windows 大小写不敏感卷），
  本进程已附着后偏好改成另一目标，重连硬错不静默换；pin 只在 attach**完整启动成功**
  后落（半启动失败不留幽灵 pin）。
- **HTTP 来源校验**（http_guard，最外层中间件）：回环 Host 白名单、拒绝一切带
  Origin 的请求、带 body 必须 application/json——绑 127.0.0.1 的「仅本机」语义
  由它真正执行（防任意网页 CSRF 驱动本机服务 / DNS rebinding 读响应）；判定是
  纯函数 `guard_rejection`，拒绝必须落日志。协议级拒绝用真实状态码（403/415/409），
  **业务端点恒返 200** 的契约不变（`_json` 的 status 参数只供协议级拒绝）。
- **关停实例门**：`/shutdown` 必须携带当前 `instance_id`（GET /status 取得），
  失配 409 回显当前 id——防维护竞态误杀已被替换的新实例；排水语义不变。
- **进程清理只精确不全局**：cleanup.py 三重身份核对（pid 文件 → HTTP 身份 →
  命令行）后只杀本服务进程树（独立进程组 / taskkill /T），优雅关停优先；
  pid 文件缺 port 或损坏 → 拒杀（宁可放过）；服务本体已死时按
  「ppid==1 + playwright 特征」回收孤儿子进程（活项目实例有活父链，绝不误伤）；
  本机 HTTP 探测绕过代理环境变量（ProxyHandler({})）；
  进程存活探测统一走 `utils.is_pid_alive`（Windows 的 os.kill(pid,0)
  实为 CTRL_C_EVENT，禁止直接用）；
  **严禁 reintroduce `pkill -f` 类按名查杀**（会命中其他项目的 Playwright 与
  共享本服务的其他会话）。client 拉起服务必须 `start_new_session`（POSIX）/
  `DETACHED_PROCESS`（Windows），这是进程树清理与终端解耦的前提。
- attach 端口防护：自建页/收养页**页级** + 服务自建 context（isolated 会话）
  **context 级**（覆盖弹窗与后续页）拦截页面脚本对本机 CDP 调试端口
  （127.0.0.1/localhost:{port}）的 http 探测；WebSocket 握手不经 route，不在
  拦截范围；用户 default context 只做页级（不改用户环境网络行为）；收养结束
  `unroute` 撤防护不留残余；只拦本连接端口、挂载失败不阻断建页；
  managed 模式（pipe 传输）不涉及。
- 页级空闲回收（`BROWSER_AGENT_TAB_IDLE_SEC`）只关**自建**页；收养页、各会话
  active_tab、**在途批会话（锁被持有）名下的页**永不回收（批中途 switch_tab 的
  目标页等批结束才回写 active_tab，期间靠 `_set_current_tab` 触碰保活 + 锁守卫
  双保险）；扫描为批完成后事件驱动（`_sweep_idle_tabs` 节流）+ 心跳兜底；
  `--probe` 探测范围严格限定在应用数据根目录内
  （Local State 标志、深度/数量上限、软链接不越界）。
- 共享用户上下文中 `clear_storage` / `session/refresh` / `auto_save` 必须拒绝或跳过。
- 业务端点恒返 200，逻辑成败看 `ok` 字段；协议级拒绝（来源校验 403/415、关停
  实例门 409）用真实状态码。凭证只落文件，不进日志/响应/站点经验。
- 全生命周期日志（logs.py）**单写者**：只有服务进程 activate；log_path 无副作用
  （不建目录不触发迁移）；新增事件打点走 `_log`/`utils.log`，不要绕过直写文件。
- 所有文件操作入口做路径穿越校验（profile 名 / 域名）。
