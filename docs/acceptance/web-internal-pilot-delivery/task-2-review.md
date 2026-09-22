# Task 2 独立审查

## Spec Compliance

- ✅ 正式设计与 Task 2 范围符合；0 Critical、0 Important。审查派发消息中的经理“共享锁”要求有一项非阻断差异，见 Minor M1。
- ✅ brief 列出的新建、修改及 controller amendment 文件均有对应 diff；三个认证路径及 DTO 进入生成的 OpenAPI 类型，注销 204 与最后文档提交一致。证据：`apps/api/main.py:173`、`apps/api/routers/authentication.py:64`、`:115`、`:126`、`domains/employees/service.py:125`。
- ⚠️ `apps/api/pilot_accounts.py:202` 仍是明确失败关闭的 profile 未接线入口。这符合分工，不代表账号管理的实际 profile CLI 已交付；Task 4 必须消费 `run_account_command`，验证受限配置加载、engine 生命周期、交互设置密码与固定安全错误。
- ⚠️ `apps/api/runtime.py:101` 仅增加显式认证参数透传。真实 loopback 监听、生产 Web 同源挂载、静态路径回退、拒绝外部适配器、恢复撤销会话，以及真实浏览器/多标签页状态清理属于 Tasks 4/5；不能由 HTTPX ASGI 测试或文档更新推定已经完成。

## Strengths

- 会话模式与 dev 模式互斥，精确验证 loopback Origin；当前 Cookie 名从已验证端口推导，按原始头拒绝歧义 Cookie，并在真实模式拒绝开发身份头。证据：`apps/api/authentication.py:45`、`:74`；攻击形状、实际 `/api` mount、两个端口共用 Cookie jar 的行为测试分别位于 `tests/integration/test_api_session_authentication.py:145`、`:91`、`:577`。端口后缀仅防覆盖，不宣称隔离不可信本机服务。
- 身份复用当前员工公共服务；角色变化、经理下属变化、停用和错误 user 映射有真实数据库驱动的 API 测试。证据：`apps/api/identity.py:134`；`tests/integration/test_api_session_authentication.py:224`、`:352`、`:437`。
- 登录执行 4 KiB 流式限额及固定错误，登录后撤销旧会话，退出只在撤销成功后清 Cookie；无 token JSON 返回。证据：`apps/api/routers/authentication.py:64`、`:126`；`tests/integration/test_api_session_authentication.py:338`、`:458`、`:536`。
- 业务上传保留原 MIME/body 协议，匿名退订沿用原 matcher，匿名 health 仅放行固定 GET 探针。证据：`apps/api/authentication.py:97`；`tests/integration/test_api_session_authentication.py:285`、`:484`、`:515`。
- 创建员工及账号使用同一外部事务，先调用公开 `create_account(session=...)`，随后读取经理并调用员工域纯校验；所有新增查询均有显式 tenant 条件，没有认证私有 API 或重复限流桶 SQL。证据：`apps/api/pilot_accounts.py:49`、`domains/employees/service.py:125`；原子拒绝及合法关联测试位于 `tests/integration/test_pilot_accounts.py:15`、`:57`、`:137`。
- CLI 不提供密码参数，拒绝无 TTY 与 getpass 回显降级，账号停用不隐式停用业务员工。证据：`apps/api/pilot_accounts.py:145`、`:163`；`tests/integration/test_pilot_accounts.py:112`、`:137`、`:182`。

## Issues

### Critical

- 无。

### Important

- 无。

### Minor

- **M1 — 经理行采用排他锁，与派发消息的共享锁要求不同。** `apps/api/pilot_accounts.py:103` 使用无参数 `.with_for_update()`，产生 `FOR UPDATE`，不是共享锁。当前代码只读取经理事实，排他锁不会破坏原子性或租户隔离，但比共享锁额外阻塞兼容的读取者。正式 design/ADR 仅要求“锁读”而未限定模式，因此不阻断本次质量通过；控制器可明确接受此更强锁，或改为 `.with_for_update(read=True)` 对齐派发要求。

## Evidence and Focused Checks

- 审查范围：BASE `cef101e0b80c84fba672ccf496529745a2078283` → HEAD `b31cffdb02be82df9895bab6929dbc7f7445b38b`。完整 diff 按四段阅读一次；之后仅脚本生成函数行号索引，没有再次内容审查。未运行 Git 命令、测试、类型检查或结构检查；仅新增本报告。
- 命名风险 R1：CLI 的外部事务是否确实保留认证租户锁。单次聚焦检查 `infra/authentication/service.py:324` 的公开 `create_account`：外部事务只 flush，调用租户桶锁后读取员工，异常由外部事务回滚；与 CLI 顺序相容。未扩展审查 Task 1 的密码/限流实现。
- 命名风险 R2：认证中间件未知异常是否泄露请求资料，且最外层 500 是否安全。单次聚焦检查 `apps/api/middleware.py:141` 的既有安全中间件及 `apps/api/main.py:199` 的安装位置：只记录错误类型，不记录异常原文；本 diff 的公共错误 helper 增加 no-store。API 注入失败测试见 `tests/integration/test_api_session_authentication.py:536`。
- 命名风险 R3：业务依赖入口是否继续调用修改后的当前身份解析。单次聚焦检查 `apps/api/dependencies.py:404`：`get_request_identity` 仍直接调用 `resolve_request_identity`；没有并行缓存角色入口。
- 命名风险 R4：登录路由调用既有服务后是否继续统一未知/停用认证拒绝。单次聚焦检查 `infra/authentication/service.py:139` 的登录裁决段，并与 `tests/integration/test_api_session_authentication.py:409` 对照；无新增账号存在性响应分支。
- 已读取实现者完整测试证据：最终受影响集合 147 passed，最后 API 专项 42 passed，runtime 26 passed，mypy/ruff/结构检查、生成类型及 Web typecheck 成功。这些是报告记录的既有运行结果，本审查没有重跑，也不把此前 RED 计为现存缺陷。报告注明测试无 warning/skip；另述共享 Git AppleDouble 警告为既有非测试噪声，未修改共享 Git 状态。

## Assessment

**Task quality: Approved。**

认证传输边界、当前权限读取与可信原子初始化的责任分离清楚，测试覆盖主要安全拒绝与失败行为。M1 仅涉及比派发要求更强的锁及相应争用；不构成正确性或安全阻断。跨任务运行与浏览器验收仍须由控制器在对应任务完成后核实。
