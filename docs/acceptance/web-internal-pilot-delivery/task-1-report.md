# Task 1：Postgres 认证基础报告

状态：DONE；实现、自审、聚焦验证与精确提交完成。

## 接口（供 Task 2 可信 CLI/API 装配）

```python
# shared.authentication
class AuthPrincipal(BaseModel):
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId

class IssuedSession(BaseModel):
    principal: AuthPrincipal
    token: SecretStr
    csrf_token: SecretStr
    expires_at: datetime

class AuthenticationService(Protocol):
    async def login(self, username: str, password: SecretStr) -> IssuedSession: ...
    async def authenticate(self, token: SecretStr, *, csrf_token: SecretStr | None = None) -> AuthPrincipal: ...
    async def get_session(self, token: SecretStr) -> IssuedSession: ...
    async def logout(self, token: SecretStr) -> None: ...

# infra.authentication.service
class PostgresAuthentication:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None: ...
    async def create_account(self, username: str, password: SecretStr, employee_id: EmployeeId, *, session: AsyncSession | None = None) -> AuthPrincipal: ...
    async def reset_password(self, username: str, password: SecretStr) -> None: ...
    async def set_enabled(self, username: str, enabled: bool) -> None: ...
    async def revoke_all(self, *, username: str | None = None) -> None: ...
```

DTO 均冻结。IssuedSession 材料字段 SecretStr、repr=False、exclude=True，默认序列化不返回会话或 CSRF。API 必须显式提取 CSRF 到安全响应、会话仅到 Set-Cookie，不可直接依赖 model_dump 导出材料。get_session 为控制器已批准增补，调用当前认证检查、重建相同 CSRF、返回原 expiry，GET 没有写入或续期。

统一错误均位于 shared.authentication：AuthenticationDenied（固定中文认证拒绝，API 401）；AuthenticationRateLimited（固定中文限流，API 429）；AuthenticationInputInvalid（固定中文可信管理输入错误）。底层 SQLAlchemy 异常通过 from None 隐藏，不输出连接或参数。账号名为1–64小写ASCII字符，正则 `[a-z0-9][a-z0-9_.-]{0,63}`，不自动改写。

create_account 仅绑定本租户既有活跃且有非空 user_id 的 Employee；不 seed Employee，不复制角色。外部 session 必须已开启事务，方法只 flush，由调用方负责 commit/rollback，失败必须回滚。已验证调用方先插入随机新ID和新user_id的Employee，再create_account(session=...)同事务提交/回滚；新员工未提交时对并发登录不可见且尚无账号，不会形成既有Employee锁冲突。调用方不得在调用前锁住既有认证账号或员工而反转锁顺序。revoke_all(username=None) 只撤销构造器绑定租户的会话，供恢复后使用；指定 username 只撤销该账号，未找到会话幂等成功。

## 实现与事务

- 固定标准库 scrypt 参数与严格编码格式；随机16字节 salt；密码15–128 Unicode字符且UTF-8≤512字节，保留首尾空格，拒绝无效Unicode。
- 进程级最多两个工作线程和两个工作槽；哈希不阻塞事件循环，取消等待不释放尚在计算的槽。未知/停用路径做同成本虚拟验证。
- 32字节随机会话；CSRF 按更新设计以解码session token为密钥、`tradeos:csrf:v1`为上下文派生HMAC-SHA256并URL-safe无padding编码。数据库仅保存编码后的token与CSRF的SHA-256摘要。校验材料要求canonical32字节格式，摘要常量时间比较。
- 账号、会话、限流三个表均带tenant复合主键；账号→员工、会话→账号复合外键；一个租户attempts桶和至多一个unknown桶，未知用户名不会扩表。
- 登录尝试单独事务先提交：30次/分钟租户桶。随后认证事务先租户attempts行锁再账号行锁；五次/15分钟账号失败与unknown共享失败桶在抛拒绝异常前提交。窗口由DB实际时钟判断。成功登录或重置清账号失败数，重置不清租户尝试数。
- 登录、账号创建、重置、启停、批量撤销共享租户锁，事务内持有锁至发行/撤销提交。登录哈希在线程内但持锁，因此同租户串行。重置哈希在锁前运行，取锁后更新摘要/版本并撤销，不存在基于旧密码验证后越过已完成重置的发行。
- 会话绝对8小时、每账号至多五个活动值，第六个撤销最旧。authenticate单条联表读取当前账号enabled/version、员工active/user映射及会话撤销/到期。user映射变化使旧会话失败关闭。
- reset/set_enabled同事务增加版本与撤销；重新启用不恢复旧会话。logout幂等撤销精确租户会话。批量revoke与发行线性化；之后发起的正确密码登录仍合法。已经接纳的在途请求不承诺回滚。

## TDD 与验证

初始 RED 命令：

```text
.venv/bin/python -m pytest tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py -q --tb=no
12 failed in 6.24s
```

失败因尚不存在的认证能力（测试中的函数内导入），不是Docker/数据库或依赖缺失。真实Postgres夹具成功运行既有0059迁移，未skip。

初次GREEN为11通过、1失败；失败是到期测试将expires_at移到created_at前，触发现有新增有效期CHECK。修正fixture同时移动created_at后20通过。随后受影响迁移回归22通过；schema对比开始触发既有sourcing循环外键的SAWarning，改用仅认证三表加employees的metadata对比后无warning。没有屏蔽告警。

最终命令与结果在下方补齐。主验证集覆盖真实多连接Postgres登录、CSRF恢复、到期、退出、五会话上限、账号及租户限流、未知桶有界、重启限流持久化、窗口恢复、错误一致、跨租户FK、原子绑定回滚、用户映射变化及并发reset/disable/revoke/cap。密码单测覆盖边界、精确参数拒绝、盐随机性、Unicode与空格、canonical token及取消时工作槽上限。

迁移：0059→0060→0059→0060；新增表列、PK、FK、CHECK、索引、唯一约束往返一致，Alembic compare_metadata 无差异。另运行既有 head→base→head 回归。

环境实测：Python3.12.14、SQLAlchemy2.0.52、alembic1.19.1、asyncpg0.31.0、pytest9.1.1、pydantic2.13.5。使用既有venv与本机pgvector/pgvector:pg16测试镜像，无重装或真实provider。

## 文件

新增 shared/authentication.py；infra/authentication/{__init__,passwords,service}.py；migrations/versions/0060_web_authentication.py；tests/unit/test_authentication_passwords.py；tests/integration/test_web_authentication.py；docs/adr/0067-web-pilot-authentication.md。修改 infra/db/tables.py。按控制器指令一并提交其已更新的spec/plan文档；未阅读或实现后续完整计划。报告保留在被忽略的 .superpowers/sdd 路径，不强制加入版本库。

## 自审与代价

ADR0028已占用，按任务要求顺延到0067；迁移0060空闲。未改九条硬边界。无app导入或员工seed，无真实外发，凭证材料没有写入报告。

同租户哈希期间持锁是明确吞吐代价，仅声明本机内测；历史会话保留，活动上限不等于总历史量有界。本轮不新增清理政策。DB连接池与API体积限额由后续装配负责。外部AsyncSession异常后须回滚，可信调用方须保持锁顺序。已有.git AppleDouble pack索引告警仍存在，不修改共享.git；git操作退出码正常。未改output或旧scratch。

## 最终验证（2026-09-07）

```text
.venv/bin/python -m pytest tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py tests/integration/test_migrations.py::test_six_tables_exist_with_tenant_id tests/integration/test_migrations.py::test_roundtrip_downgrade_base_then_upgrade_head -q --tb=no
25 passed in 21.07s

.venv/bin/python -m mypy --follow-imports=silent shared/authentication.py infra/authentication/passwords.py infra/authentication/service.py
Success: no issues found in 3 source files

.venv/bin/python -m ruff check shared/authentication.py infra/authentication infra/db/tables.py tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py migrations/versions/0060_web_authentication.py
All checks passed!

.venv/bin/python scripts/check_boundaries.py
结构自检通过（全部七项）

git diff --cached --check
退出码0，无空白差异错误（既有AppleDouble索引告警另列）
```

最终25项无skip、无warning。自审新增user_id空字符串的RED：参数测试1失败、2通过；增加非空映射守卫后上述最终验证全部通过。外部事务测试包含新Employee→账号的成功提交及完整回滚。未运行全库历史9318或任何真实Provider。

提交：`6c8a2de5f443eba8c705e8044757fdb636b50acf` — feat(auth): persist tenant-bound pilot accounts and revocable sessions。

## 修复轮次 1：I1 测试失败诊断泄漏

评审问题：pytest断言重写会展开原始密码/会话/CSRF比较和repr防护检查；成功测试或`--tb=no`不能保证常规CI失败路径安全。接受此发现，本轮仅修改两个新增测试文件，认证算法、迁移与接口均不变。

修复：敏感比较、校验函数调用、会话恢复/存储/寿命判断与repr检查在assert之外计算；断言仅接收布尔值和固定AUTH错误码。认证调用先得到不含凭证的Principal再比较员工ID；账号事务异常结果和迁移子进程状态同样先转换为布尔值，避免展开含会话或捕获输出的容器。

新增三种受控故障：CSRF派生返回错误材料、密码记录repr泄漏、IssuedSession repr泄漏。测试仅在临时目录写入不含canary值的故障插件源文件，合成canary通过子进程环境传递。真实pytest子进程运行精确既有测试节点，启用常规断言重写和`--tb=long`，45秒超时，stdout/stderr全部捕获，永不打印或附加到错误；仅检查预期失败退出码、固定目标guard码和五项canary均未出现在诊断中。异常处理也只报告固定安全码。故障只在显式子进程插件生效，没有正常收集的故意失败测试；涉及Postgres的回归放在integration层。

RED（新增回归最初位于unit文件，修复后按DB依赖归入integration文件）：

```text
.venv/bin/python -m pytest tests/unit/test_authentication_passwords.py::test_authentication_failures_do_not_render_materials -q --tb=short
3 failed in 7.64s
```

三个父测试仅输出 `AUTH_FAILURE_DIAGNOSTIC_LEAK` 和 `False`；已捕获的子进程原始诊断没有输出。此RED真实验证原测试三条失败路径均会泄漏。

局部GREEN（移动文件前）：

```text
.venv/bin/python -m pytest tests/unit/test_authentication_passwords.py::test_authentication_failures_do_not_render_materials -q --tb=short
3 passed in 5.72s
```

最终回归位于 `tests/integration/test_web_authentication.py::test_authentication_failures_do_not_render_materials`，继续执行真实既有unit/integration节点验证，不替换为只测复制断言。

修复轮次1最终验证：

```text
.venv/bin/python -m pytest tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py -q --tb=short
26 passed in 24.51s

.venv/bin/python -m ruff check tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py
All checks passed!

.venv/bin/python scripts/check_boundaries.py
结构自检通过（全部七项）

git diff --check -- tests/unit/test_authentication_passwords.py tests/integration/test_web_authentication.py
退出码0
```

无skip、无warning；三种真实失败诊断回归与原有真实Postgres/迁移往返均通过。I1已解决，没有认证算法或其他任务范围扩张。测试故障子进程输出仍仅在测试进程内读取，报告不含canary或原始诊断。

修复轮次1提交：`cef101e0b80c84fba672ccf496529745a2078283` — test(auth): keep failing credential assertions confidential。
