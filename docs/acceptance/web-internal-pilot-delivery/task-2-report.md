# Task 2：API 真实会话入口与可信本机账号维护

状态：DONE；实现、自审、聚焦验证与精确提交完成。profile 读取按任务分工由 Task 4 接线，本任务没有新增配置格式。

## 精确接口（Task 4 必须消费）

```python
# apps.api.main
create_app(*, settings=None, dependencies=None, lifespan=None,
           cors_allowed_origins=(), readiness_probe=None, unsubscribe_service=None,
           authentication: AuthenticationService | None = None,
           authentication_origin: str | None = None) -> FastAPI

# apps.api.runtime：其余原参数保持
create_runtime_app_from_settings(settings, *, secret_resolver, object_store_settings,
                                model_client=None, manual_send=None,
                                gmail_transport=None, inbound_mailbox=None,
                                authentication: AuthenticationService | None = None,
                                authentication_origin: str | None = None) -> FastAPI

# apps.api.authentication（OpenAPI components 中均有名称）
class LoginRequest(BaseModel):
    username: str  # 精确 [a-z0-9][a-z0-9_.-]{0,63}，不改写
    password: SecretStr  # repr=False，不允许额外字段

class SessionResponse(BaseModel):
    employee: EmployeeView
    csrf_token: str  # repr=False；仅私有 no-store HTTP 响应可序列化，禁止日志
    expires_at: datetime  # 原绝对到期时间

# apps.api.pilot_accounts
@dataclass(frozen=True)
class AccountCommand:
    action: Literal["create", "reset-password", "enable", "disable"]
    username: str
    name: str | None = None
    role: str | None = None
    manager_id: str | None = None

async def run_account_command(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: TenantId,
    command: AccountCommand, *, password: SecretStr | None = None,
) -> None: ...
def parse_command(argv: list[str]) -> AccountCommand: ...
def read_password() -> SecretStr: ...
def run_interactive(session_factory: async_sessionmaker[AsyncSession],
                    tenant_id: TenantId, argv: list[str]) -> int: ...
def main(argv: list[str] | None = None) -> int: ...

# domains.employees.service：控制器批准新增的公开纯校验，不产生 IO/权限
validate_employee_provisioning(tenant_id: TenantId, *, name: str, role: str,
                              manager: EmployeeView | None) -> None
```

- API 相对路径：`POST /auth/login` 返回 200 SessionResponse；`GET /auth/session` 返回 200 SessionResponse；`POST /auth/logout` 成功返回 204。内测 mount 后浏览器使用 `/api/auth/*`。
- Cookie 最终名称为 `tradeos_session_<port>`，port 来自已验证的 `authentication_origin`，内部函数 `session_cookie_name(origin: str) -> str` 派生，无新增公开配置。控制器确认 Cookie 不按端口隔离，故替换最初暂定固定名称；源和恢复环境不会覆盖彼此选用的 Cookie。前端不能读取 HttpOnly Cookie，也不需要知道名称。
- 属性：HttpOnly、SameSite=Strict、Path=/api、无 Domain、仅已批准 loopback HTTP 模式不设 Secure；expires 为认证服务原绝对到期时间。会话 token 只写 Set-Cookie，不进入 JSON。
- 每个浏览器不安全请求必须发 `Origin: <精确 origin>` 和 `X-TradeOS-Request: 1`。已登录写操作还必须发 `X-CSRF-Token: <GET session 或 login 私有响应的 csrf_token>`。无来源推测或转发头信任。新模式不使用 CORS。
- 三种入口状态互斥：auth+dev 无效；auth 必须提供精确 `http://127.0.0.1:<1..65535>` 且不得尾斜线/路径/其他主机；无 auth 的 dev 保留原头模式；无 auth 的 nondev 保留原失败关闭。单给 origin 也配置拒绝，固定 ValueError `authentication_configuration_invalid`。
- 认证拒绝 401 `authentication_required`；登录限流 429 `authentication_rate_limited`；输入400 `authentication_input_invalid`、体积413 `authentication_body_too_large`、MIME415 `authentication_json_required`。来源/重复头/开发身份头固定403；错CSRF为401（与会话失效同类），缺CSRF为403。所有错误固定中文，无原输入/底层参数；公共安全错误 helper 现在统一 no-store，覆盖最外层500回退。
- 登录只接受精确 application/json，流式累加上限4096字节。现有业务写入维持原 MIME/空命令协议，不复制上传上限；真实 `/work-uploads` 路由验证5000字节 image/png能经认证层到原应用服务。每次 login 成功后撤销当前 origin 所选旧 Cookie；若后续身份解析/撤销失败，新会话也撤销且不发 Cookie。退出服务失败返回500、保留 Cookie，不能假称撤销。
- 从当前 Employee 公共服务读取 tenant/employee/user 映射、active 与 role，经理下属也每次重读；Principal 不携带角色。角色、经理归属、员工停用与账户重置/停用在后续请求生效。
- application-relative path 使用 Starlette 现有 get_route_path，已实际 mount `/api` 验证。匿名退订复用未修改的原 matcher；真实会话额外只放行 GET `/health/live` 与 `/health/ready` 的原固定探针，仍校验 Host。capabilities、其他health路径、子路径/尾斜线及不安全方法不放行。

## 可信账号命令与原子性

调用形状：`create --username <name> --name <显示名> --role <role> [--manager-id <id>]`；其他三个命令只带 `--username`。没有 password argv/env 参数。parse_command 对未知或无效 argv 只抛固定 AuthenticationInputInvalid，argparse 不回显参数；help 只输出固定用法。read_password 要求 stdin 与 stderr 都为 TTY，经 getpass 读两次；无回显能力时将 GetPassWarning 转成固定异常，拒绝管道/静默回显降级。

`python -m apps.api.pilot_accounts` 当前可解析上述命令及 help；实际 profile 配置尚未接线时返回2和固定 `account_profile_not_configured`，不自行读 DATABASE_URL 或另造凭证配置。Task 4 需把受限 PilotConfig 的 factory/tenant 接到上述可信调用函数。推荐在其自有 async 生命周期中调用 run_account_command 并负责 engine dispose；run_interactive 是同步组合方便入口，内部 asyncio.run，外部仍须明确资源所有权。

create 先按当前租户调用领域纯校验，再在 factory.begin 中插入随机新 Employee/User ID。调用公开 `auth.create_account(..., session=session)`取得并持有租户→账号锁后，才读取锁定经理；机械映射 EmployeeView 交领域校验，写 manager_id 并提交。指定经理不存在/跨租户/停用/非boss或manager均回滚；重复username也回滚新员工。无私有认证方法访问、无重复限流bucket SQL、无新增 provisioning_scope。领域角色直接来自原 Role 枚举，不在 CLI 复制规则；ADR0067补充说明。

enable/disable 只改可登录状态；reset-password/disable撤销旧会话，enable不会恢复旧会话。不会修改 Employee.is_active、历史归属或写审批；没有追加 HR 生命周期功能。

## TDD 与验证证据

按任务依赖顺序记录（输出仅计数/安全错误，未输出运行凭证、Cookie、CSRF或数据库地址）：

- API RED：`.venv/bin/python -m pytest tests/integration/test_api_session_authentication.py -q --tb=no` → 31 failed in 11.39s，缺少 create_app 的认证参数/入口；首次 GREEN → 31 passed in 14.62s。
- CLI RED：`.venv/bin/python -m pytest tests/integration/test_pilot_accounts.py -q --tb=no` → 6 failed in 2.98s，模块尚未实现；首次 GREEN → 6 passed in 5.32s。
- 领域校验 RED：`.venv/bin/python -m pytest tests/unit/test_employee_provisioning.py -q --tb=no` → 6 failed in 0.10s，公开校验函数尚未存在；后续三组联合 GREEN → 49 passed in 20.45s。
- Schema专项扩充：`...test_api_session_authentication.py -k 'rotation or membership or schema or errors_are_uniform or malformed_injected or stream_limit' -q --tb=no` → 1 failed,5 passed，缺 LoginRequest named component；加入命名 schema 后进入上述联合 GREEN。
- 退出失败与限流：`...test_api_session_authentication.py -k 'service_failure or rate_limit_mapping' -q --tb=no` → 1 failed,1 passed，最外层500缺少no-store；公共错误helper修复后相关用例GREEN。
- 端口隔离 RED：`...test_api_session_authentication.py -k two_loopback_ports -q --tb=no` → 1 failed，第二个环境替换了固定名 Cookie；按端口派生后 `-k 'two_loopback_ports or rotation or cookie_session'` → 3 passed in 6.02s。期间一次改名编辑遗漏 set_cookie 的 name 参数被同组测试检测，已修复后通过。
- 既有精确OpenAPI契约初次回归出现2失败，仅为新增三个已规定auth路径未进旧精确集合；明确增加三个路径，无改成子集断言。其余140通过；最终受影响集合如下全部通过。

最终主要验证命令：

```text
.venv/bin/python -m pytest tests/integration/test_api_session_authentication.py tests/integration/test_pilot_accounts.py tests/unit/test_employee_provisioning.py tests/unit/test_employees_service.py tests/unit/test_api_app.py tests/unit/test_web_identity_management.py tests/unit/test_work_uploads_router.py tests/integration/test_crm_api.py tests/integration/test_crm_handoff_api.py tests/integration/test_unsubscribe_api.py -q --tb=no
147 passed in 31.18s

# 仅后续测试断言防诊断泄漏收敛及 dev互斥测试改用有效origin 后，重跑受影响 API 专项：
.venv/bin/python -m pytest tests/integration/test_api_session_authentication.py -q --tb=no
42 passed in 21.27s

.venv/bin/python -m mypy --follow-imports=silent apps/api/authentication.py apps/api/routers/authentication.py apps/api/pilot_accounts.py apps/api/identity.py apps/api/main.py apps/api/middleware.py apps/api/runtime.py domains/employees/service.py
Success: no issues found in 8 source files

.venv/bin/python -m ruff check apps/api/authentication.py apps/api/routers/authentication.py apps/api/pilot_accounts.py apps/api/identity.py apps/api/main.py apps/api/middleware.py apps/api/runtime.py domains/employees/service.py tests/integration/test_api_session_authentication.py tests/integration/test_pilot_accounts.py tests/unit/test_employee_provisioning.py tests/unit/test_api_app.py
All checks passed!

.venv/bin/python scripts/check_boundaries.py
结构自检全部七项通过
```

Web 在 apps/web 下执行：`PATH="<worktree>/.venv/bin:/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm run gen:api` 成功，openapi-typescript7.13.0生成 api.d.ts；`PATH="/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm run typecheck` 退出0。生成后无DTO或schema变化，端口Cookie内部修订不影响API JSON类型。

运行环境复用既有 Python3.12 .venv、Node24.15.0 和本机PG测试容器；无依赖重装、skip或warning。全库历史9318未重跑。实际Provider、消息发送、部署、push、merge、备份/恢复和真实浏览器留后续任务，未作为本任务已验收项。

## 自审、变更与边界

新增 apps/api/authentication.py、routers/authentication.py、pilot_accounts.py；修改identity/main/middleware/runtime；生成web api.d.ts；新增API/CLI集成测试与领域纯校验单测；更新既有API精确schema测试；修改员工service公开校验及ADR0067。按控制器明确要求一并提交其plan/spec修订（含后续冷备份技术方案），不把后续任务文档修订当作本任务实现。

自审已检查当前身份映射、匿名路径、锁顺序、登录轮换/失败清理、Cookie端口隔离、读取不续期、固定错误和测试失败输出边界。认证材料比较均在断言外化为布尔，不向pytest展开原密码/Cookie/CSRF。已有共享.git AppleDouble pack索引警告继续出现，未修理.git；不触碰output/旧scratch。报告位于被忽略的.superpowers/sdd目录，不强制加版本库。

## 最后补充验证与交付说明

`.venv/bin/python -m pytest tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py -q --tb=no` → 26 passed in 6.40s，覆盖原runtime构造/默认失败关闭/生命周期与数据库真实装配。

`git diff --check -- <本任务精确路径>` 退出0。Cookie端口命名只保证profile会话不互相覆盖；HTTP Cookie仍会发送到同host的其他端口，因此它不是针对不可信本机服务的安全隔离，可信OS/loopback边界保持原约束。

提交：`af4d9b64ba9df078adcfab81bd12bbd64341b28a` — `feat(api): add same-origin pilot sessions and trusted account commands`。16个精确文件，未加入被忽略报告、output、旧scratch或共享.git。`git diff --cached --check`退出0；既有Git警告只计数，未改共享索引。
