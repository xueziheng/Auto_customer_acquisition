### Task 2: API真实会话入口及可信本机账号维护

**Files:**
- Create: `apps/api/authentication.py`, `apps/api/routers/authentication.py`, `apps/api/pilot_accounts.py`
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
assert (await client.post('/auth/logout', headers=csrf_headers)).status_code == 204
assert (await client.get('/auth/session')).status_code == 401
```
对老板/经理/员工从当前Employee重新取权限，测试改角色/归属/停用后下一次请求不可沿旧授权；保留dev头模式与未配置nondev回归。CLI验证跨tenantmanager、错误role、重复username都原子拒绝，密码不进输出。
- [ ] **Step 2: 运行新测试记录RED。** 不输出HTTP原始Set-Cookie或登录JSON。
- [ ] **Step 3: 实现同源认证中间件与路由。** 明确session/dev/unconfigured三状态互斥，安装时验证origin为`http://127.0.0.1:<port>`且dev_mode=False。中间件在业务router之前验证session并设置可信Principal/tenant；identity复用当前Employee公共服务推导actor；禁止信浏览器role或header。原匿名unsubscribe使用原matcher，不得扩大退订的匿名匹配。会话模式额外仅允许无身份GET `/health/live`和`/health/ready`原固定安全探针，仍检查精确Host；`/health/capabilities`保留认证。不建监控默认账号，不允许任意health子路径绕过。登录4KiB流式上限、安全固定验证错误、响应no-store；sessionToken不进入JSON，CSRF只进会话私有响应。用户名校验与secret字段避免422回显。
```python
if authentication is not None and resolved_settings.dev_mode:
    raise ValueError('authentication_configuration_invalid')
```
可信管理入口只在本机操作者运行；创建Employee与账号原子，不写业务审批。停用/重置撤销会话；必要时失效Employee由显式选项明确区分账号可登录状态和员工业务活跃。
- [ ] **Step 4: 运行GREEN、受影响身份/租户API回归、生成OpenAPI类型、结构自检。** 精确记录命令、测试数。Task4再验证运行profile接线。
- [ ] **Step 5: 自审并提交。** 报告cookie名、CSRF头名、DTO名称和CLI函数签名，后续任务严格消费。


## Controller interface handoff (Task1 reviewed implementation facts; final scoped I1 review pending at preparation)

Read task-1-report.md interface section only for exact AuthenticationService/management/error/username contracts. IssuedSession fields are excluded from default model_dump: explicitly extract csrf for private response; session token only Set-Cookie. Principal has no role.
Task1 review cross-task pointers: task-1-review.md cross-task section must be covered by Task2; later profile/provider/browser concerns remain Tasks3–5. Newly inserted Employee with random new IDs then create_account(session=...) in same active transaction is supported and tested. Do not pre-lock an existing Employee/account before the auth tenant lock.
Task2 supports callable trusted account command composition and terminal parsing/getpass; actual PilotConfig loading is Task4, so avoid inventing a second credentials/config format or importing nonexistent infra.pilot. Document precise callable for Task4.
Include one API test mounted under /api, since Starlette raw scope path/root_path differs from root-only tests. Exact auth/health/anonymous matchers must use application-relative path safely; cookie Path stays /api. This is verification of specified same-origin mount, not a new endpoint.
Safe test-output rule: never put raw password/cookie/token/CSRF/DSN expressions into pytest-rewritten assertions. Compare internally and assert only boolean + fixed code; do not print captured request/response diagnostics.

Existing raw upload: client uploadWorkArtifact posts the actual Blob MIME (not always octet-stream) to POST /work-uploads; router/domain validate MIME and enforce bounded body. Real-mode security guard must preserve that explicit route after Origin/custom-header/CSRF checks, letting existing domain decide MIME. Existing routers keep their own request-body/MIME contract (including bodyless commands); every protected write still requires exact Origin/custom header/CSRF. Only login has an auth-specific application/json requirement. Do not impose login 4KiB limit on uploads. Check actual schema for bodyless existing endpoints before hard Content-Type rejection; request-shape guards must preserve authorized existing flows.

## Controller ruling: provisioning ownership (binding amendment)

Add public pure validate_employee_provisioning in domains/employees/service.py: tenant_id, name, role, manager: EmployeeView|None, returning None. It validates known Role, nonblank bounded name and provided manager same-tenant/active/boss-or-manager; no IO or authorization grant, original EmployeeService read/write actor rules stay. CLI only maps facts and invokes it. Add tests/unit/test_employee_provisioning.py and relevant existing Employee tests.
Use existing public create_account(session=...) in external transaction after inserting random new-ID Employee; it takes/retains tenant lock. Then CLI reads current manager with shared lock, domain-validates and sets relationship before outer commit. Requested missing manager fails, never degrades to optional None. All errors roll back Employee and account. No new provisioning_scope method needed, no private helpers or copied bucket SQL. ADR0067 addendum records pure domain validator/atomic sequence, leave0068 for notification. Controller corrected formal plan/spec; include documents in exact commit.

Cookie namespace amendment: use tradeos_session_<validated origin port>, e.g. tradeos_session_8123; no extra public config flag. Cookie jar is host/path-bound, not port-bound. Test source/restored origins on different127.0.0.1 ports with one cookie jar: login/logout each selects only its own named cookie. Frontend never reads the HttpOnly cookie. Binding spec updated.
