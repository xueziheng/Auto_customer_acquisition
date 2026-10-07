# Task 5 审查：Spec Compliance ❌，Task quality Needs fixes

范围：仅审查 `ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242..541bf2d289b1b79e01bc46766a96628d790dcf2b` 的 Task 5 测试与操作文档。生产代码没有变化。完整 diff 按连续片段阅读一次，之后只提取定位行号；没有运行 git、广泛检索、重跑已报告门禁或派生审查者。

## Spec Compliance

- ❌ 本任务部分验收要求尚未由新增测试证明：跨标签直接账号切换和共同失效、实际延迟退出响应、退出后的旧 token 服务端拒绝。具体见 I1–I3。
- ✅ brief 中五个交付文件均有对应改动：`tests/e2e/test_web_pilot.py:1`、`docs/operations/web-internal-pilot.md:1`、`docs/acceptance/2026-09-07-web-internal-pilot.md:1`、`docs/operations/web-core-capability-matrix.md:1`、`HANDBOOK.md:775`。
- ✅ 未把前序生产实现混入本任务；报告准确记录生产源 ecfb4d2、测试修订 c1cdad8 以及其后的文档提交，未把历史 9318/411 当成本轮结果（`docs/acceptance/2026-09-07-web-internal-pilot.md:5`）。
- ⚠️ 跨任务待控制者核对：全部 Python/Web 门禁的当前覆盖与既有 lint 87 warnings 的已接受延期、账号 CLI 的真实 TTY/getpass 与禁止参数输入、缺少 Web Locks 的安全拒绝、当前员工权限变更后的重读、生产资源所有权/备份归档安全和 0060 迁移均主要位于未变前序代码；本次没有重新扩大审查这些已通过的 gates。
- ⚠️ 文档中的首次账号操作声称团队页能读取经理 Employee ID（`docs/operations/web-internal-pilot.md:99`）；本次 E2E 用直接数据库查询取得 ID（`tests/e2e/test_web_pilot.py:145`），不能据此证明 UI 操作步骤。控制者可对照前序 UI 验收，毋须以此重复整套门禁。

## Strengths

- 真实 built Web、实际 owned PG/对象存储与当前服务端角色共同参与验收：启动真实 profile，boss/sales 分别检查当前身份与团队 API 200/403（`tests/e2e/test_web_pilot.py:373`、`:409`、`:532`）。没有用合成 HTTP 响应替代业务/认证主链。
- 完整 stop/start 后校验业务数据库 canonical marker 和对象字节 SHA；恢复后再次检查源环境独立、目标业务关联及对象内容（`tests/e2e/test_web_pilot.py:226`、`:270`、`:434`、`:596`）。
- 恢复撤销测试专门把源 token 放到目标端口对应 Cookie 名，避免“匿名 401”伪证明；源 cookie 在源 profile 重启后仍成功，目标随后重新登录（`tests/e2e/test_web_pilot.py:594`、`:636`、`:651`）。
- 测试清理记录并核验 exact owner、容器/卷标识与创建时间，不把测试删除逻辑写入生产 stop（`tests/e2e/test_web_pilot.py:59`）。政策文档使用不能运行的人工决策占位符；备份权限、同故障域风险、仅 Chromium/loopback、真实 Provider/TLS 未运行均明确（`docs/operations/web-internal-pilot.md:33`、`:135`、`:153`）。

## Issues

### Critical

无。

### Important

**I1 — 直接账号切换和多标签失效断言没有覆盖仍挂载的旧业务页。**

位置：`tests/e2e/test_web_pilot.py:494`、`:512`、`:528`、`:547`。

当前流程先由 A 点击退出，再由 B 登录 sales；检查 A 没有团队内容时，A 已因自身退出隐藏了业务页。因此即使“登录轮换发失效广播/卸载旧视图”的实现完全损坏，此断言仍会通过。之后 disable/reset-password 仅令 B reload，而 A 一直处于登录页，也未验证两个已登录标签页共同失效。验收文档第 4 项把该场景描述成“登录轮换通过广播先卸载 A 的旧业务视图”，超过实际证据（`docs/acceptance/2026-09-07-web-internal-pilot.md:28`）。

修复：增加独立场景，让 A 保持已登录 boss 业务视图、B 保持登录表单，B 直接登录 sales，禁止先退出 A；证明 A 的旧视图清除及 B 的当前服务端身份/权限。再让两个标签均挂载同一有效会话的业务壳，通过其中一个标签触发失效请求，检查另一标签也清除旧状态。现有退出竞争场景可以保留。

**I2 — `logout_headers` 实际记录的是未发出的请求，不能证明延迟响应/Set-Cookie 顺序。**

位置：`tests/e2e/test_web_pilot.py:494`–`:525`；相关宣称：`docs/acceptance/2026-09-07-web-internal-pilot.md:28`。

`page.route` 在请求送到服务器前拦截；handler 立即记录 `logout_headers`，等 release 后才 `route.continue_()`。所以被延迟的是请求，标记发生时根本没有退出响应头。之后仅检查 `login_request` 晚于该标记，不能排除 login 先于真实 logout 响应完成或迟到 Set-Cookie 的情况。0.3 秒等待也只证明该有限观察窗口里没有请求。

修复：真实执行退出请求后暂缓把服务器响应交给页面，明确记录实际响应交付/完成事件；在此期间启动 B 登录，证明请求仍被阻塞。释放退出响应后再证明登录顺序及刷新后的 sales 会话仍有效。正确命名事件并同步修正文档，只报告实际测得的顺序。

**I3 — 普通退出后的 401 只证明 Cookie 被清除，没有证明旧服务器会话被撤销。**

位置：`tests/e2e/test_web_pilot.py:474`–`:480`、`:578`–`:583`。

退出后用同一个 Cookie jar 请求 session。成功 logout 会清 Cookie，因此即使服务器完全没有撤销旧会话，这两处依然得到 401，仍会通过 `LOGOUT_RETRY_DID_NOT_REVOKE_SESSION` / `FINAL_LOGOUT_DID_NOT_REVOKE_SESSION`。disable/reset-password 的检查保留原 Cookie，恢复场景也主动携带旧 token，后者的强证据做法应延伸到 logout。当前文档把这些 401 写成服务器会话撤销证据（`docs/acceptance/2026-09-07-web-internal-pilot.md:26`）。

修复：退出前仅在测试进程中保存 token；成功退出后在隔离请求 context 内，以原名/路径实际携带该旧 token，断言服务器 401，同时核验正常浏览器 Cookie 已清除。不得输出 token 或原始失败对象。

### Minor

**M1 — Console 过滤范围比“仅已断言负路径”更宽。**

位置：`tests/e2e/test_web_pilot.py:396`–`:400`、`:458`–`:471`；相关宣称：`.superpowers/sdd/2026-09-07-web-internal-pilot/task-5-report.md:47`、`docs/acceptance/2026-09-07-web-internal-pilot.md:39`。

任意页面/资源的 401 或 403 记录都会被忽略，没有关联到被明确断言的请求；`expected_network_fault` 为真期间，甚至所有 console warning/error 都被忽略，而不只模拟的网络错误。这会隐藏同时发生的应用警告，报告“其余 warning/error 仍失败”不准确。建议按已观测 URL/方法/预期阶段和固定错误类别限定过滤，安全记录未知事件计数；若暂不收紧，应如实缩小无错误结论。

## Checks 与证据边界

- 已阅读根 AGENTS、tests/AGENTS、Task 5 brief/report、绑定设计及 reviewer prompt。`docs/AGENTS.md` 不存在，所检查的 tests/e2e、docs/operations、docs/acceptance 与报告目录没有额外 AGENTS；这是现有规则文件情况，没有据此扩大任务。
- 已报告完整 E2E：c1cdad875bea547e20fdaefbf006ac31f8ec9c3b，1 passed/28.52s。仅把它作为实现者提供的实际运行记录，本审查未重跑。
- 唯一额外检查针对具体“Playwright fill 超时是否会回显密码”的疑点：检查已安装 Playwright 的 `Locator.fill` 和 `Connection.wrap_api_call`，随后对空白本机页用随机合成值执行 100ms 缺失 locator probe，输出仅 `SYNTHETIC_FILL_VALUE_IN_RAW_ERROR=false`，进程 exit 0。没有输出随机值、原始错误、请求或配置。该疑点未成立，因此不列为漏洞；这也不是对所有异常路径的凭证安全证明。
- 没有读取私有 profile/config、容器环境、Cookie 或历史 output；没有重跑报告门禁，没有修改生产/测试/文档或共享 git。

## Assessment

**Task quality: Needs fixes。** 三项 Important 都是验收的可判别性缺口，而非本次证实的生产认证缺陷；修复应限定新增测试与准确证据文档。完成这些测试后，才能可信地关闭 Task 5 的会话和跨标签验收要求。

计数：Critical 0 / Important 3 / Minor 1。
