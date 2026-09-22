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

## Controller acceptance preflight

Existing tests/e2e/conftest.py has a session fixture but no autouse fixture; create independent owned pilot fixtures in this new test, not the controlled E2EStack. Existing test_web_core_controlled.py is reference only and drives a Vite/dev/fake-provider four-process launcher; pilot must drive its own three-process built-Web CLI. Do not reuse its fixed output/acceptance/task12 directory or read historical output data.
Keep browser passwords/cookies/CSRF entirely inside test process; avoid trace/HAR/request logging and assertion-rewritten raw-secret expressions. On expected failures capture privately and expose fixed diagnostics. Screenshots, if useful, should show signed-in synthetic UI or blank login, never typed passwords or dev identity selection.
The current capability matrix is historical controlled delivery. Add an explicit separately evidenced pilot section and qualify global current claims: baseline 0059 migration, Vite, four processes, deletion-on-stop and real-auth-unverified refer to controlled baseline only. Preserve historical commit/test evidence; do not silently present old 9318/411 counts as this branch's results. Shared TLS deployment remains unverified even after local login passes.
Task2 API contract: logout success is 204 with no JSON body; port-suffixed cookie only avoids overlapping-profile cookie replacement and does not isolate a hostile local service. Task4 consumes exact schema. Domain permissions remain checked server-side on every request; authenticated UI labels are not proof.
Business fixture may seed clearly synthetic data to demonstrate persistence/roles, but must not claim it proves automated demand discovery or customer validation. Synthetic scoring/handoff values stay isolated in test profile; operations document policy shape uses nonrunnable placeholders requiring operator decisions.

Browser scenario also covers same-cookie-jar account switch in another tab: login rotation invalidates prior-session tabs through the no-payload logout event; old employee view must unmount before it can display data authenticated as the new employee. Use API current authorization evidence, not UI labels alone.

Committed Task3 lifecycle: PilotConfig.read(profile / "config.json"); PilotProfile(profile) uses fixed local Docker; start_storage()/migrate()/stop()/status()/check_schema() are synchronous, internal asyncio.run means call via asyncio.to_thread from async browser tests. After storage start use refreshed profile.config with actual dynamic database/object ports; web_port==api_port remains stable. Close profile.client explicitly. backup_profile(profile,destination) requires all processes/storage stopped; restore_profile(backup,new_profile) returns new stopped PilotProfile after session revocation. Restored tenant/bucket/key preserved, owner/ports/resources new. Both profile and backup private; test cleanup only exact test-created owners after verification (production deliberately has no delete/destroy).
Task3 last source c669eec passed scoped review: interrupted restoration and all diagnostic writes failing still stop actual owned storage; no need repeat those failure-specific tests in browser acceptance unless new integration exposes a gap. Actual machine reboot was not performed and cannot be claimed; process-birth prior-boot semantics have targeted unit evidence, full app stop/start supplies browser persistence evidence.

Named restore-browser verification risk: cookie names include API port, and restored profile has a new port. A 401 with no matching cookie only proves anonymous refusal, not revocation. To verify restored old sessions, within the isolated test process carry the pre-backup source cookie value into the target port's correct cookie name/path, then assert session request refused (safe status only), and re-login using retained synthetic account. Never print/transcribe cookie values, CSRF, or credentials. Verify source still unchanged/independent. Task4 already reported actual login/refresh/two-tab logout smoke at1440/390; Task5 adds full integrated lifecycle, role/current-authorization, account disable and backup/restore context rather than merely repeating that smoke.

## Task4 committed interface handoff (0d370b1, pending review)


所有命令在本工作区运行。Python 为 `.venv/bin/python3.12`；Node 为 `/Users/xueziheng/.nvm/versions/node/v24.15.0/bin/node`。
政策必须由操作者显式给出，格式沿 Task3，未新增默认账号、市场、评分或 SLA。

```bash
PATH="$PWD/.venv/bin:/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm --prefix apps/web run gen:api
PATH="/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm --prefix apps/web run build
.venv/bin/python3.12 scripts/run_web_pilot.py init --profile PROFILE --policy-file POLICY_JSON
.venv/bin/python3.12 scripts/run_web_pilot.py start --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py status --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role ROLE
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role sales --manager-id EMPLOYEE_ID
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE reset-password --username USERNAME
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE disable --username USERNAME
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE enable --username USERNAME
.venv/bin/python3.12 scripts/run_web_pilot.py stop --profile PROFILE
```

`init` 留下已迁移、已停止的存储；`start` 只检查 schema、不迁移，需要已有 `apps/web/dist/index.html`。
先 start 再 accounts。create/reset-password 仅真实 TTY `getpass` 两次输入，不提供 argv/env/管道密码入口。
账号 CLI 仅绑定该 profile 当前数据库和 tenant；锁内 schema 核验后委托 Task2 `run_account_command`，事务由既有账号/员工组合处理。
未知参数不会回显，不在无效启动时输出 completed。status 的 `web_url` 为同源 `http://127.0.0.1:<api_port>`。

窄 Python seams：

```python
scripts.run_web_pilot.start_profile(path: Path) -> None
scripts.run_web_pilot.account_profile(path: Path, argv: list[str]) -> None
scripts.pilot_web_supervisor.launch(path: Path) -> None
scripts.pilot_web_supervisor.PilotSupervisor(path: Path, *, root: Path = ROOT)
# supervisor.start()/ready()/close()；测试只对自己创建的实例负责，不接管未知 PID。
apps.api.pilot.runtime_settings(config: PilotConfig) -> Phase1RuntimeSettings
apps.api.pilot.create_pilot_app(config: PilotConfig, build: Path) -> FastAPI
apps.api.pilot.mount_web(business: FastAPI, build: Path) -> FastAPI
apps.scheduler_worker.pilot.create_pilot_factory(config: PilotConfig) -> SchedulerRuntimeFactory
apps.notification_worker.pilot.runtime_settings(config: PilotConfig) -> NotificationWorkerConfig
apps.notification_worker.pilot.run(config: PilotConfig) -> int  # async
SchedulerWorkerConfig.from_pilot_environ(environ: Mapping[str, str]) -> SchedulerWorkerConfig
S3ObjectStoreSettings.from_pilot_environ(environ: Mapping[str, str]) -> S3ObjectStoreSettings
```

Task5 合成账户 fixture 可继续调用已有 `apps.api.pilot_accounts.run_account_command(session_factory, tenant_id, AccountCommand(...), password=SecretStr(...))`；随机密码只能留进程内。账户管理没有第二套配置、默认数据或 HTTP 管理端点。
加载 config 使用 `PilotConfig.read(profile_path / "config.json")`；启动后必须 reload 当前存储端口。

三个真实入口（supervisor 执行，账号材料不进 argv/env）：

```text
python -m apps.api.pilot PROFILE/config.json INHERITED_API_SOCKET_FD
python -m apps.scheduler_worker.pilot PROFILE/config.json
python -m apps.notification_worker.pilot PROFILE/config.json
```

API 健康：`/api/health/live`、`/api/health/ready`；两个 worker 各自端口 `/health/ready`。
API capability 需要真实会话；通知 capability 返回 `mode=local_in_app,in_app=enabled,email=disabled`。
API 认证仓储的 sessionmaker 绑定 canonical API 自有 engine，未再开第二套 engine 生命周期；外层静态 app 显式进入/退出被挂载业务 lifespan。
未知 `/api/...` 不会返回 SPA，路径穿越和缺失 assets 返回404（业务未知 API 仍先经过认证门禁）。


Evidence versioning: prefer commit the new E2E test once self-reviewed, run acceptance at that concrete commit, then commit docs referencing tested HEAD/results. If test fixes required, commit corrected test before final acceptance run. Never imply documentation-only later commit was itself a fresh full-suite run; exact tested production source and test revision must be recoverable. Controller whole-branch archive will record final docs/source commit chain. No need to invent a RED run for an acceptance test that passes first time.

Task4 fix1 compatibility ruling: same-origin login/logout serialized using navigator.locks to prevent late Set-Cookie clearing new login; localbusy/generation fence remain. Browser lacking Web Locks must receive clear safe failure, no weak fallback. Operations doc lists feature requirement and actual tested Chromium version, does not claim untested browsers. Full browser acceptance should include direct logout retry after simulated network failure and delayed logout/new-login across tabs, using actual server session/cookie state and no credentials in logs. Await final Task4 fix report for exact interfaces.

Task4 fix1 source ecfb4d2 (review pending): CLI/profile callable interfaces unchanged. PilotSupervisor.close(*,failed=False) now preserves unexpected child failure as failed/operation_failed after exact cleanup; manual TERM remains stopped/requested_stop. Frontend currentAuthenticationMutation/subscribeAuthenticationMutation/supportsAuthenticationMutations provide busy/compatibility, no raw secrets. Authentication lock fixed name tradeos-authentication, no payload. Logout hides business, privately GET session for CSRF then POST; GET401 means alreadyinvalid, POST204 success. Login/logout WebLock covers response headers and state; pending local mutation blocks login/restore; generation fences broadcasts/updates.
Fix actual rebuilt Chromium proof already passed direct retry and sameoldcookie serverrevocation, plus heldAlogout route/Bloginqueued with logout_headers before login_request and Bcookie authenticating distinct employee afterrefresh. Avoid reproducing probe-only failure modes: browser context cookie lookup must use origin + /api and exact port cookie name; synchronous Playwright has an event loop, run asyncio-only services in separate thread or use asyncPlaywright/async fixture consistently. Final Task5 still captures durable complete lifecycle E2E in repository and operator docs.

Task4 final gate: scoped I1/I2/I3 review Approved at ecfb4d2, no new blocking findings. Accepted interfaces above current. Existing lint87 warnings remain deferred, no user-profile initialization has occurred.
