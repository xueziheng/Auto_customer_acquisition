# Web Internal Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可停止重启、备份恢复、真实账号登录的本机Web内测入口。

**Architecture:** 单租户Postgres会话，HttpOnly同源cookie；独立持久化profile和具名数据卷，沿用canonical业务工厂。受控演示和桌面接口保留。

**Tech Stack:** Python3.12、FastAPI、Postgres、SQLAlchemy、scrypt、Vue3、TypeScript、Vite、Ant Design Vue、Docker。

**Spec:** `docs/superpowers/specs/2026-09-07-web-internal-pilot-design.md`

## Global Constraints

- 实现上方Spec全部约束；修改前读取根HANDBOOK及路径适用AGENTS。
- 凭证不输出、不进模型、不提交；合成测试只输出安全计数/错误码。
- 所有新表和查询带tenant；真实模式不接受开发身份头；当前权限来自Employee。
- 只使用loopback、本地已有镜像、owned资源；不接真实Provider、不发送客户消息、不部署共享服务器、不改桌面契约。
- 不种业务政策；首用显式配置。无认证依赖的非dev入口仍失败关闭；原controlled入口语义保持。
- 不改旧验收output或旧scratch；不修复共享.git；不push/merge；只提交本任务精确文件。
- `.venv/bin/python`为Python3.12；Node24位于`/Users/xueziheng/.nvm/versions/node/v24.15.0/bin`，仅对子进程PATH补充。不更改HOME或shell配置。
- 先运行有意义的失败测试，再实现；运行覆盖改动的测试和`python scripts/check_boundaries.py`。不重复全库历史9318基线；审查不重跑同版本已报告测试。

### Task 1: Postgres账号、密码、限流和可撤销会话

**Files:**
- Create: `shared/authentication.py`
- Create: `infra/authentication/__init__.py`, `infra/authentication/passwords.py`, `infra/authentication/service.py`
- Modify: `infra/db/tables.py`
- Create: `migrations/versions/0060_web_authentication.py`（先核对当前head；若编号已占用，顺延并记录）
- Create: `tests/unit/test_authentication_passwords.py`, `tests/integration/test_web_authentication.py`
- Create: `docs/adr/0028-web-pilot-authentication.md`（若编号占用则顺延）

**Interfaces:**
- Produces `shared.authentication.AuthPrincipal(tenant_id, employee_id, user_id)`、`IssuedSession(principal, token, csrf_token, expires_at)`及AuthenticationService Protocol，签名与Spec一致。
- Produces `infra.authentication.service.PostgresAuthentication(session_factory, tenant_id)`，实现login/authenticate/logout/get_session；管理方法create_account/reset_password/set_enabled/revoke_all精确类型在报告列出，供Task2可信CLI使用。
- Consumes `EmployeeRow`当前tenant/employee/user映射；不负责创造Employee、不新增业务角色逻辑。

- [ ] **Step 1: 写行为失败测试。** 用现有真实PG fixture种两个租户Employee，密码为测试进程随机生成且不打印；验证正确密码可登录、错误/未知/停用同类失败、摘要不等于原token、到期与撤销失败关闭。核心断言形状：
```python
issued = await auth.login(username, password)
assert (await auth.authenticate(issued.token)).employee_id == employee_id
await auth.logout(issued.token)
with pytest.raises(AuthenticationDenied):
    await auth.authenticate(issued.token)
```
补充scrypt严格参数和长度边界、重复会话上限、账号与租户限流、未知用户名桶有界、重启持久化、并发login/reset/disable互斥，跨租户employee绑定拒绝。
- [ ] **Step 2: 运行上述两个新测试文件记录RED。** 失败必须因能力缺失，不得把依赖或Docker未启动视作TDD证据。
- [ ] **Step 3: 实现冻结DTO、安全错误类型、密码与Postgres服务。** 以完整Spec为算法边界；所有密码/会话材料repr=False且错误固定。账户变更与会话发行使用账户行锁/版本重查，哈希工作线程有界；认证每次查当前账号和Employee活跃、user映射。通过单条迁移建tenant复合约束，ORM一致。登录失败计数提交不能随异常rollback；未知用户名用固定桶，不按任意字符串扩表。
```python
# 密码计算在受限线程内；随机session只有摘要持久化。
digest = hashlib.scrypt(password.get_secret_value().encode('utf-8'),
    salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=268435456)
```
- [ ] **Step 4: 运行GREEN与迁移往返。** 新测试文件、现有migration/schema相关测试、结构自检；只保存安全日志。记录实际测试数和版本。
- [ ] **Step 5: 自审并精确提交。** 报告接口、事务/撤销语义、RED/GREEN、变更文件、已知限制。密码或token不得出现在报告。

### Task 2: API真实会话入口及可信本机账号维护

**Files:**
- Create: `apps/api/authentication.py`, `apps/api/routers/authentication.py`, `apps/api/pilot_accounts.py`
- Modify: `domains/employees/service.py`, `docs/adr/0067-web-pilot-authentication.md`
- Create: `tests/unit/test_employee_provisioning.py`
- Modify: `apps/api/identity.py`, `apps/api/middleware.py`, `apps/api/main.py`, `apps/api/runtime.py`
- Create: `tests/integration/test_api_session_authentication.py`, `tests/integration/test_pilot_accounts.py`
- Modify: `apps/web/src/api/api.d.ts`（生成）

**Interfaces:**
- Consumes Task1 AuthenticationService与管理服务精确签名，以Task1报告为实现事实。
- Produces `create_app(..., authentication: AuthenticationService | None = None, authentication_origin: str | None = None)`与runtime同名显式参数；提供`/auth/login`、`/auth/session`、`/auth/logout`，均进入OpenAPI。
- Produces `python -m apps.api.pilot_accounts`管理CLI：从受限配置读DB/tenant，交互getpass；命令create/reset-password/enable/disable，create显式username/name/role与可选manager，无密码argv/env参数。
- 当前配置文件适配等Task3 profile实现后由Task4接线；本任务管理函数必须可独立通过显式session factory/tenant调用测试，不能导入尚不存在的profile代码。

- [ ] **Step 1: 写失败集成测试。** HTTPX实际cookie流与真实PG账号，验证下面调用次序，并检查body、重复头、跨站、错Host、CSRF缺失/错值、过期、停用和伪造开发头都拒绝。
```python
response = await client.post('/auth/login', json=credentials, headers=trusted_headers)
assert response.status_code == 200
assert 'httponly' in response.headers['set-cookie'].lower()
assert (await client.get('/auth/session')).status_code == 200
assert (await client.post('/auth/logout', headers=csrf_headers)).status_code == 200
assert (await client.get('/auth/session')).status_code == 401
```
对老板/经理/员工从当前Employee重新取权限，测试改角色/归属/停用后下一次请求不可沿旧授权；保留dev头模式与未配置nondev回归。CLI验证跨tenantmanager、错误role、重复username都原子拒绝，密码不进输出。
- [ ] **Step 2: 运行新测试记录RED。** 不输出HTTP原始Set-Cookie或登录JSON。
- [ ] **Step 3: 实现同源认证中间件与路由。** 明确session/dev/unconfigured三状态互斥，安装时验证origin为`http://127.0.0.1:<port>`且dev_mode=False。中间件在业务router之前验证session并设置可信Principal/tenant；identity复用当前Employee公共服务推导actor；禁止信浏览器role或header。原匿名unsubscribe使用原matcher，不得扩大退订的匿名匹配。会话模式额外仅允许无身份GET `/health/live`和`/health/ready`原固定安全探针，仍检查精确Host；`/health/capabilities`保留认证。不建监控默认账号，不允许任意health子路径绕过。登录4KiB流式上限、安全固定验证错误、响应no-store；sessionToken不进入JSON，CSRF只进会话私有响应。用户名校验与secret字段避免422回显。
```python
if authentication is not None and resolved_settings.dev_mode:
    raise ValueError('authentication_configuration_invalid')
```
可信管理入口只在本机操作者运行；创建Employee与账号原子，不写业务审批。员工域公开纯`validate_employee_provisioning(tenant_id, *, name, role, manager: EmployeeView | None) -> None`负责角色/姓名及同租户活跃boss或manager关系检查；纯校验不授予权限或写数据。CLI在外部事务插入随机新ID员工，再调用公开`create_account(session=...)`取得并保留tenant锁，然后锁读当前经理事实、调用域校验并设置关系，最后一起提交；任何拒绝都回滚员工与账号。请求了经理但查不到必须拒绝。CLI不得访问认证私有方法或复制attempts桶SQL。停用/重置撤销会话；必要时失效Employee由显式选项明确区分账号可登录状态和员工业务活跃。
- [ ] **Step 4: 运行GREEN、受影响身份/租户API回归、生成OpenAPI类型、结构自检。** 精确记录命令、测试数。Task4再验证运行profile接线。
- [ ] **Step 5: 自审并提交。** 报告cookie名、CSRF头名、DTO名称和CLI函数签名，后续任务严格消费。

### Task 3: 独立持久化profile与冷备份恢复

**Files:**
- Create: `infra/pilot/AGENTS.md`, `infra/pilot/__init__.py`, `infra/pilot/config.py`, `infra/pilot/resources.py`, `infra/pilot/backup.py`
- Create: `scripts/run_web_pilot.py`
- Create: `tests/unit/test_pilot_profile.py`, `tests/integration/test_pilot_persistence.py`

**Interfaces:**
- Produces `PilotConfig.read(path: Path)`验证0700/0600/owner并隐藏凭证；`database_url: SecretStr`、`tenant_id: str`、当前端口、精确资源ID、`runtime_environment() -> dict[str,str]`、SecretResolver.resolve接口。
- Produces CLI `init --profile PATH --policy-file PATH`、`migrate --profile PATH`、`start --profile PATH`、`stop --profile PATH`、`status --profile PATH`、`backup --profile PATH --destination NEW_PATH`、`restore --backup PATH --profile NEW_PATH`。Task4完成start应用接线；本任务infra生命周期可独立由测试调用。
- Consumes现有OwnedProcess精确PID/birth停止协议；不复用OwnedContainers.close，不改变controlled资源语义。不导入apps、不种Employee或业务数据。

- [ ] **Step 1: 写失败测试。** profile权限/软链接/错owner/并发锁、端口更新、旧ID和错volume拒绝；实际owned PG+对象存储具名卷写合成数据并停止重启。
```python
await profile.stop()
await profile.start_storage()
assert await read_synthetic_marker(profile) == expected_marker
assert await read_object_hash(profile) == expected_hash
```
backup仅stopped，restore仅fresh；坏SHA/缺文件/穿越/软链接/错版本拒绝；验证原目标不变，恢复数据和对象相同，恢复会话撤销。
- [ ] **Step 2: 运行RED。** 容器只使用现有本地镜像，测试清理仅本测试新owner、删除动作不得触碰用户profile。
- [ ] **Step 3: 实现profile、锁、持久卷和冷备份。** 长期profile不使用tempdir、不在stop删卷；资源身份以实际label+ID+mount核对。独立政策输入必须完整显式，不复制controlled业务默认值；技术密钥随机且只入私有配置。迁移仅显式命令，start检查head。镜像ID写入manifest；备份流式写入新私有暂存目录再原子完成，所有文件有SHA；restore验证整包后创建独立owner卷，禁止解包出界，失败以固定错误码标记。恢复DB后按tenant撤销会话。runtime应用接线留明确私有launch协议，不能以空start假成功。
```python
with exclusive_profile_lock(profile_path):
    verify_owned_resources(config)
    stop_exact_owned_processes(config)
    stop_exact_owned_containers(config)  # 不remove，不删除数据卷
```
- [ ] **Step 4: 运行GREEN、实际持久化/恢复测试、结构自检。** 保存安全计数和hash比对结论，不输出实际私有config或业务原件。
- [ ] **Step 5: 自审并提交。** 报告精确config/lifecycle/launch接口、剩余Task4接线点，不能宣称CLI start整体验收已完成。

### Task 4: 真实登录Web与完整内测运行入口

**Files:**
- Create: `apps/api/pilot.py`, `apps/scheduler_worker/pilot.py`, `apps/notification_worker/pilot.py`
- Modify: `apps/notification_worker/runtime.py`, `apps/notification_worker/AGENTS.md`
- Create: `docs/adr/0068-local-in-app-notifications.md`, `tests/integration/test_pilot_notifications.py`
- Modify: `scripts/run_web_pilot.py`, `apps/api/runtime_config.py`（仅明确pilot配置接线需要时）
- Create: `apps/web/src/api/authentication.ts`, `apps/web/src/components/LoginPanel.vue`
- Modify: `apps/web/src/api/client.ts`, `apps/web/src/App.vue`
- Create: `apps/web/src/api/authentication.spec.ts`, `apps/web/src/components/LoginPanel.spec.ts`, `tests/integration/test_pilot_runtime.py`
- Modify: `apps/web/src/api/api.d.ts`（仅重新生成）

**Interfaces:**
- ConsumesTask2明确认证参数/DTO/headers，Task3 PilotConfig与owned资源接口；以各任务报告精确签名接线。
- Produces同源生产构建Web与`/api`业务API；统一CLI能启动/停止完整API/scheduler/notification，账号CLI只接受本profile，真实模式没有开发身份选择器。
- 原有API客户端保留isolated dev模式，新增cookie请求credentials same-origin与CSRF内存读取，登录后identity只用于状态隔离不作后端授权断言。

- [ ] **Step 1: 写失败测试。** 初始无会话仅登录面板、会话恢复后才挂业务路由、401撤销与旧请求隔离、403正常展示、退出同步、不保存secret、raw upload同样CSRF。runtime测试核对唯一canonical工厂、非dev、拒绝外部model/邮箱/搜索、同源资源路径/未知API404、worker单实例及schema落后拒绝。
```typescript
await restoreSession()
expect(currentIdentity()).toBeNull()
await login(username, password)
expect(currentIdentity()?.mode).toBe('authenticated')
await logout()
expect(currentIdentity()).toBeNull()
```
- [ ] **Step 2: 运行RED。** 按Task2实际导出的命名实现测试；不手写重复API响应类型。
- [ ] **Step 3: 实现运行组合与Web。** API同一进程挂静态SPA，`/api`挂原业务app并正确运行lifespan；API/worker不互相import。各入口消费profile显式政策与独立技术配置，认证API最终dev_mode=False。没有真实外部客户端，也没有ControlledModelClient伪成功；拒绝适配器与网络限制同时生效，公开能力矩阵准确标未配置。worker保持原canonical消费与关闭。通知新增显式`NotificationRuntimeMode.LOCAL_IN_APP`，仅pilot入口使用；默认PRODUCTION无邮件仍拒绝，旧CONTROLLED_IN_APP保留。复用同一站内持久投递策略，健康披露email disabled，完成仅代表站内；不创建邮件client、不把fake adapter称作真实投递。以ADR0068记录并更新就近AGENTS，新增该mode回归。CLI start检查build/schema并更新当前端口/安全status，stop保留数据，账号CLIgetpass接profile。Web中文登录、加载/失败/退出状态，应用根监听所有identity generation，卸载通知、隔离迟到结果；多标签只广播注销事件。生产构建不能开启受控选择器。
- [ ] **Step 4: 运行GREEN。** Web测试/typecheck/lint/build、受影响API/runtime/client测试、结构自检。实际三进程启动并验证健康和安全能力；不要接外网或实际用户密码。
- [ ] **Step 5: 自审并提交。** 报告运行命令与后续Task5合成fixture的窄接口。

### Task 5: 浏览器、关停恢复验收及可恢复交付文档

**Files:**
- Create: `tests/e2e/test_web_pilot.py`
- Create: `docs/operations/web-internal-pilot.md`, `docs/acceptance/2026-09-07-web-internal-pilot.md`
- Modify: `docs/operations/web-core-capability-matrix.md`, `HANDBOOK.md`

**Interfaces:**
- Consumes完整CLI/profile/authentication/browser契约；实际生产构建，不使用dev员工头。
- Produces独立owned合成profile验收与安全证据，明确普通用户首次配置和设置密码所需交互。不创建真实默认员工，不把测试业务政策写入正式profile。

- [ ] **Step 1: 写真实浏览器验收。** 随机合成密码只在testprocess传浏览器；老板与员工分别登录，读取tenant业务fixture；刷新、完整stop/start、备份新目标restore、旧会话失效、重新登录、logout、跨标签注销/账号停用、390px无溢出。数据库/对象hash验证调用Task3公共协议，不能导入test代码进入生产。
```python
assert await page.get_by_role('button', name='登录', exact=True).is_visible()
# 输入在测试进程内完成；报告不打印密码/请求/cookie。
await sign_in(page, generated_credentials)
await restart_pilot(profile)
await page.reload()
assert await safe_business_marker(page) == expected_marker
```
- [ ] **Step 2: 运行验收记录准确失败或通过。** 新测试若一次通过，标为新增验收证据而非虚构RED。失败修复仅属于本任务测试/文档；发现前序生产缺陷提交控制者裁定和派回原实现者，不能悄悄改变边界。
- [ ] **Step 3: 写完整操作说明。** 环境依赖、生产Web build、显式业务政策文件形状（不可运行占位符）、init/migrate/start/stop/status/账号命令、getpass密码交互、备份恢复到新目标、敏感备份权限、禁用能力、TLS/真实Provider未验收。关机前stop、磁盘空间、备份与原盘同故障域的风险如实说明。
- [ ] **Step 4: 跑覆盖本任务的新E2E与所需结构检查，记录精确HEAD和结果。** 先前同版本检查不重复；更新能力矩阵区分controlled/pilot/独立Linux能力，禁用项不写已完成。
- [ ] **Step 5: 自审并提交。** 完成后控制者作一次全分支独立审查、最多一波最终修复和一次限定复审，归档账本与审查，再结束交付。
