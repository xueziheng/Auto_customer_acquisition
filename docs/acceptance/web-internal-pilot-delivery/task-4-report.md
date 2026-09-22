# Task 4 完成报告：真实登录 Web 与持久本机运行

状态：实现完成，待 controller 独立审查。基线 `d0be8f9fdb6ed0199ca5e1a375db3962e19f6f30`。
分支：`codex/web-internal-pilot`。工作区：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`。
提交：`0d370b1b8b545ad66207977ed0410d5eec3fcd93`。未 push、merge、运行真实用户 profile 或真实外部 Provider。

## 最终接口与 Task 5 使用方法

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

## Web 认证协议

消费现有生成 `SessionResponse`/`LoginRequest`，`gen:api` 运行后文件内容未变化。
`authentication.ts` 导出 `restoreSession/login/logout/currentIdentity/listenForSessionInvalidation`。
生产 API base 固定相对 `/api`，所有请求 cookie `credentials=same-origin`；认证请求使用 `X-TradeOS-Request: 1`、写请求使用内存 CSRF `X-CSRF-Token`。
真实身份绝不发送 `X-Tenant-Id/X-Employee-Id`，两者只保留在隔离 fixed-dev 身份。
原始上传同样使用 CSRF，保留 Blob 实际 MIME。退出必须收到204才宣称完成，失败显示无法确认服务器撤销。
Cookie 名/Path/有效期保持 Task2，Web 不读取 cookie；没有 token、密码或身份进入 localStorage/sessionStorage/URL。

首次恢复前不挂业务；生产根组件监听每个 generation，业务路由与通知在身份变化时重建/失效时卸载。
旧请求响应按发起时 generation 隔离；旧身份迟到401不会清除新身份；403保留会话。
跨标签 BroadcastChannel 仅发送字符串 `invalidate`，不发送身份或 token；成功登录轮换也广播，避免其他标签继续渲染旧员工资料。
原隔离 DEV 入口继续由原页面请求通道处理身份变化，PROD 永不开放受控选择器。
390px 顶栏改为随内容增高，避免退出按钮新增一行后遮住导航。

Task5 仍应覆盖完整角色/停用/重置/备份恢复浏览器链。特别：登录轮换后其他已登录标签清空；恢复 profile 旧会话验证应按目标端口 cookie 名实际带入旧值，避免“无 cookie401”假证据。

## 运行边界与扩展批准

- Task4 supervisor 是 CLI 外的单独文件，三入口不互相 import；保留 canonical API/scheduler/notification 工厂、流程定义、scheduler singleton 与关闭机制。
- start 在操作锁内依次核验 build、固定端口、所有存储归属、当前 schema，启动三个 OwnedProcess；真实三 ready 后才发布 running/握手成功，寿命期间释放锁。
- TERM 只设置意图；逐个实际 OwnedProcess.stop 后才清进程记录、再停止存储。失败不会先清掉活进程记录，也不进行广域 kill。
- 启动失败 allowlist 只传固定诊断，包括 profile_busy、port_unavailable、schema_not_current；原始异常与子进程日志不穿透边界。
- API 使用 UnconfiguredModelClient，调用固定失败 model_not_configured，永不伪造模型结果。
- scheduler pilot 配置将邮箱引用与 DKIM selector 明确为 None；禁止配置研究、联系人、Campaign 外部端口；DNS step 明确 fail/dns_not_configured，拒绝 resolver 不联网；默认 parser 仍要求完整旧字段。
- S3 增加显式 pilot parser，返回 dev_mode=False，只允许带端口精确127.0.0.1 HTTP；默认 parser 不变，没有通过 dev=true 绕过真实认证。
- 通知 LOCAL_IN_APP 复用真实持久 job、站内通道、优先级/模板/受众/dedup；默认 production 缺邮件依旧拒绝。ADR0068 与就近 AGENTS 已更新。
- 三进程均安装既有精确 loopback socket/DNS 审计边界；这是进程级纵深防线，不是 OS 沙箱。
- controller 批准 `infra/pilot/resources.py` 仅 reserve_port 增加 SO_REUSEADDR（无 SO_REUSEPORT），解决停止后立即稳定端口重启；活跃端口占用依旧拒绝。
- controller 批准修改旧 Web header mock 断言及 Task3 未接线占位断言；catalog mock 在接收请求时捕获 provider 当前 tenant，不在响应返回后读取身份，不降低旧 A/新 B 隔离证明。
- controller 的正式计划文件范围修订与源代码一起提交；未修改根 AGENTS、全局硬边界或其他领域文件。

## RED / GREEN 与实际验证

1. 新 authentication.spec 初次 RED：模块缺失；随后5项 GREEN，补生产 App 根节点行为后合计6项。LoginPanel.spec RED：组件缺失；随后1项 GREEN。覆盖登录、恢复、退出失败、CSRF上传、403、迟到401、跨标签登录轮换、真实根节点挂载/卸载。
2. runtime 初次 RED：API/scheduler pilot 模块及 LOCAL_IN_APP 缺失，共5失败；随后 GREEN。S3 parser 独立 RED 缺失方法 → GREEN。
3. 真三进程首次启动暴露 S3 nondev HTTP parser 拒绝；加入批准 seam 后启动成功。快速停启测试 RED 端口 TIME_WAIT → SO_REUSEADDR 后 GREEN，活占用与重复启动拒绝保持。
4. 原始浏览器 screenshot 发现390px导航被固定高度遮住；增加 nav.bottom<=topbar.bottom 检查 RED → 响应高度调整后同一流程 GREEN。
5. 自审新增 profile_busy 固定诊断和符号链接 profile 拒绝：各自先 RED，后 GREEN。start 使用 absolute 保留链接供 profile 校验，不先 resolve 绕过。

实际执行结果（不把不同组相加当独立测试数）：

- `npm --prefix apps/web test`：34文件、418 passed。
- 后续 `npm --prefix apps/web test -- src/api/authentication.spec.ts src/components/LoginPanel.spec.ts tests/api-client-identity.test.ts tests/catalog-product-proposal.test.ts`：4文件46 passed。
- Web `typecheck`、`build`、`gen:api` 均通过；生产构建不含 dev选择器入口。
- Web `lint`：0 errors、87 warnings。全部来自未改文件：OutreachWorkbench.vue34、billing/BillingUnavailable.vue10、manual-phase1/ManualOperations.vue12、products/ProductSupplyCenter.vue31；未扩散清理。
- 最终 `.venv/bin/python3.12 -m pytest tests/integration/test_pilot_runtime.py tests/integration/test_pilot_notifications.py -q --tb=short`：14 passed，20.08秒；含真实三进程、重复启动、精确停止/快速原端口再启、schema落后拒绝、真实站内记录、默认邮件拒绝、网络边界、S3拒绝、CLI安全/忙锁/链接拒绝。
- `.venv/bin/python3.12 -m pytest tests/unit/test_pilot_profile.py tests/unit/test_artifact_store_config.py tests/integration/test_pilot_runtime.py tests/integration/test_pilot_notifications.py -q --tb=short`：137 passed（随后新增busy/link测试分别通过，最终runtime组已覆盖）。
- API/auth/account/旧runtime影响组：`tests/unit/test_api_runtime_config.py tests/unit/test_scheduler_worker_config.py tests/integration/test_api_runtime.py tests/integration/test_api_session_authentication.py tests/integration/test_web_authentication.py tests/integration/test_pilot_accounts.py tests/unit/test_pilot_profile.py tests/integration/test_controlled_notification_delivery.py`：207 passed，1个旧Task3占位断言失败；批准替换该断言后，profile及pilot37项组合通过，最终新增runtime14项亦通过。未重跑历史9318全库基线。
- `.venv/bin/python3.12 -m pytest tests/unit/test_notification_worker.py tests/integration/test_notification_worker.py -q --tb=short`：27 passed。
- `.venv/bin/python3.12 -m pytest tests/integration/test_web_core_runtime.py -q --tb=short`：34 passed，含 canonical/singleton/生命周期回归。
- 所有修改Python文件 `ruff check` 通过；结构自检 `scripts/check_boundaries.py` 通过。
- `mypy --explicit-package-bases --follow-imports=silent` 对三个新入口、supervisor/CLI、scheduler config/runtime、S3 config、notification runtime 通过。初次检查修复 argparse Never 和 optional邮箱引用的类型错误；未安装额外依赖。

## 实际浏览器 QA

Browser plugin/skill 不在当前可用技能目录，按 frontend-testing-debugging 使用项目已安装 Python Playwright；没有安装依赖。
执行 `PYTHONPATH=. .venv/bin/python3.12 /tmp/task4-browser.py`，脚本在进程中生成随机合成密码，经既有账号事务创建合成员工，启动新独立 owned profile。
Chromium context 只允许当前 loopback origin，关闭 trace/HAR；异常只输出固定 stage/type，不输出请求、输入值、cookie、CSRF或底层异常。
最终结果：`passed / built_login_restore_multitab_logout / viewports1440,390 / owned_cleanup complete`。

| 检查 | 证据 |
|---|---|
| 页面身份/非空 | URL为该profile的`/crm/opportunities`，title含TradeOS，匿名为中文登录面板 |
| overlay/console | 无vite错误遮罩、无pageerror；认证前预期401是会话恢复，不当作应用故障 |
| 登录 | 真实账号表单登录后出现机会看板与退出按钮 |
| 恢复 | reload后真实cookie恢复业务页面 |
| 多标签 | 第二标签恢复同一会话，第一标签退出后两者都回登录 |
| 浏览器持久存储 | localStorage.length + sessionStorage.length 为0 |
| 390px | 无水平溢出，顶栏导航在其容器范围内 |
| 截图 | 已实际查看1440登录与390业务截图；无密码/认证材料 |

截图保存在仓外：`/private/tmp/tradeos-pilot-task4-login.png`、`/private/tmp/tradeos-pilot-task4-login-mobile.png`、`/private/tmp/tradeos-pilot-task4-business-mobile.png`。
Task5 的完整E2E和运营文档仍按原分工开展；本次真实浏览器不是用mock替代后端。

## 资源与文件审计

所有实际测试使用新合成 profile，没有真实用户 profile、真实密码、邮箱、模型或搜索 Provider。
Docker固定本机 `/var/run/docker.sock`，只用已有镜像，没有pull。fixture先精确 stop核验进程与两个容器，再按本fixture owner核验后移除测试容器/卷；任何清理失败会使测试失败。浏览器脚本 finally 同样精确停止本次profile并移除本次容器/卷，关闭context/browser/DB/client。
最终 runtime 测试证明 supervisor 已死、recorded进程静止，三个应用端口可重新保留，数据卷在生产stop路径保留；只有测试fixture删除本次拥有的测试资源。
没有 cleanup历史输出、共享git、其他owner资源；历史未跟踪 output 文件计数保持400。git stderr只捕获计数（status审计4行），未展开AppleDouble内容。

修改与新增文件共26个：三个pilot入口；notification runtime/health/AGENTS；scheduler config/runtime；Web App/client/authentication/LoginPanel及4个相关测试文件；S3 config/AGENTS；ADR0068；正式计划；profile reserve_port；CLI/supervisor；两份integration测试与旧profile单测。具体精确文件列表以提交为准。

剩余限制：仅本机loopback HTTP，未验收共享TLS部署/真实Provider；进程审计不是OS沙箱；通知只承诺站内；浏览器完整不同角色/重置停用/备份恢复链属于Task5。没有已知阻塞未完成实现项。

提交后审计：tracked dirty=0，历史output未跟踪=400，其他未跟踪=0。精确git add26文件；diff --cached --check通过；commit stderr捕获567行只计数、不展开。

## Fix round 1 — I1 / I2 / I3

修复基线 `0d370b1b8b545ad66207977ed0410d5eec3fcd93`。按 task-4-review.md 三项 Important 定向修改；M1 的未改页面 lint 告警延期，不扩大到全库验收。沿用 TDD 与 frontend-testing-debugging；没有子代理。本轮 gate 仍交 controller 复审。

### 修复及接口

- I1：logout 在同一认证变更内先隐藏身份，再 GET `/api/auth/session` 取得当前 cookie 绑定 CSRF，仅保留在函数内供 POST `/api/auth/logout` 使用，绝不恢复业务身份。网络失败后“重试退出”直接重复此流程；204确认撤销，GET401确认原会话已经无效。其他失败仍明确报告未确认撤销。
- I2：`currentAuthenticationMutation(): "login" | "logout" | null`、`subscribeAuthenticationMutation(listener)` 与 `supportsAuthenticationMutations()` 为 App 提供忙状态与能力判定。登录/退出共同持有 `navigator.locks.request("tradeos-authentication", {mode:"exclusive"}, callback)`，覆盖响应头接收和状态收尾；本页重叠 login/restore 被明确拒绝，跨页 login 等待锁。没有递归取同名锁，重试GET位于已持锁 callback 内。退出收尾只修改相同 generation 的身份和广播。
- App 在退出挂起时卸载业务、通知及登录面板，禁用恢复/退出重试；登录期间显示处理中。无 Web Locks 明确拒绝认证变更与生产业务显示，不提供弱后备。锁名及广播仅固定字面量，不含账号、租户、密码、token 或 CSRF。controller 的设计文档修订随本轮提交；当前仅承诺已验证的本机 Chromium。
- 自审发现：为排队登录忽略匿名失效广播时，仍必须推进“没有认证 mutation 的匿名恢复”generation，否则旧GET可重新发布会话。新增失败测试后修正为仅在认证 mutation 期间忽略匿名广播；正常匿名恢复仍收到失效并隔离迟到GET。
- I3：`PilotSupervisor.close(*, failed: bool = False)` 接收失败终态。精确停止原 OwnedProcess，清除已死记录并停止本profile存储后，故障路径保留 `failed/operation_failed`；资源清理失败也保留失败。正常TERM沿用 `stopped/requested_stop`。CLI、账号命令、profile配置和Task5既有fixture接口没有改变。

本轮精确改6文件：`apps/web/src/api/authentication.ts`、`apps/web/src/api/authentication.spec.ts`、`apps/web/src/App.vue`、`scripts/pilot_web_supervisor.py`、`tests/integration/test_pilot_runtime.py`、controller修改的 `docs/superpowers/specs/2026-09-07-web-internal-pilot-design.md`。

### RED / GREEN 与命令

Node命令均前置 `/Users/xueziheng/.nvm/versions/node/v24.15.0/bin`；Python均为 `.venv/bin/python3.12`。

- 首轮 Web RED：`npm --prefix apps/web test -- src/api/authentication.spec.ts`，4 failed / 6 passed，分别覆盖I1直接重试、I2迟到204/503、本机缺WebLocks拒绝。证据 `/tmp/task4-fix1-web-red.txt`。
- I2匿名恢复补充 RED：`npm --prefix apps/web test -- src/api/authentication.spec.ts -t '匿名恢复挂起'`，1 failed / 12 skipped，失败为旧身份被重新发布；修正后纳入下项。证据 `/tmp/task4-fix1-restore-red.txt`。
- 最终 Web GREEN：`npm --prefix apps/web test -- src/api/authentication.spec.ts src/components/LoginPanel.spec.ts`，2文件14 passed（572ms）；包括真实App挂载/卸载、I1服务器状态模型、I2忙状态/两个迟到状态/generation/跨模块锁顺序/无WebLocks及匿名恢复。证据 `/tmp/task4-fix1-web-green-final.txt`。这些是前端定向测试，不能代替下述真实服务器证据。
- I3真实进程 RED：`.venv/bin/python3.12 -m pytest tests/integration/test_pilot_runtime.py -q --tb=short -k unexpected_owned`，1 failed，running后精确SIGKILL拥有的scheduler，最终观察到旧实现 `stopped` 而非 `failed`。证据 `/tmp/task4-fix1-runtime-red.txt`。
- 最终真实进程 GREEN：`.venv/bin/python3.12 -m pytest tests/integration/test_pilot_runtime.py -q --tb=short -k 'unexpected_owned or actual_three'`，2 passed / 11 deselected，18.98秒。新断言核对原supervisor与所有原应用进程均不live、存储静止、无活实例记录、status仍failed；原正常三进程启动/停止/同端口重启/schema拒绝同时通过。证据 `/tmp/task4-fix1-runtime-green-final.txt`。
- `npm --prefix apps/web run typecheck` 通过；最终 `npm --prefix apps/web run build`（自带vue-tsc）通过，Vite394ms。API契约未改，本轮无需重新生成类型。
- `apps/web/node_modules/.bin/eslint apps/web/src/api/authentication.ts apps/web/src/api/authentication.spec.ts apps/web/src/App.vue` 通过，0输出；仅当前3个前端文件，不代表全库零告警。上轮4个未改页面87 warnings仍在台账，没有重跑或清理它们。
- `.venv/bin/python3.12 -m ruff check scripts/pilot_web_supervisor.py tests/integration/test_pilot_runtime.py` 与对应 `ruff format --check` 通过。初次format check指出两处格式，格式化后通过。
- `.venv/bin/python3.12 -m mypy --explicit-package-bases --follow-imports=silent scripts/pilot_web_supervisor.py` 通过；`.venv/bin/python3.12 scripts/check_boundaries.py` 通过。
- 未重跑历史9318或无关全Web418组；所有本轮运行均围绕三项修复。

### 实际浏览器 QA 与清理

Browser plugin/skill 不在当前可用目录，使用现有 Python Playwright Chromium；未安装依赖。目标流程为真实登录→首次退出网络失败→直接重试→服务器撤销，以及A标签退出挂起→B标签登录排队→A响应头完成→B登录与刷新存活。

命令：`PATH=/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH PYTHONPATH=. .venv/bin/python3.12 /tmp/task4-fix1-browser.py`。该仓外脚本创建独立合成profile、真实持久账号、生产build及三个真实进程；随机密码/cookie/CSRF仅在进程内，关闭trace/HAR，没有输出底层异常或请求材料。上下文只允许该profile loopback origin。

I1通过Playwright route仅中止首次logout POST，先用实际cookie值调用真实 `PostgresAuthentication.authenticate` 确认仍有效；直接点击“重试退出”后得到204，再对同一个旧值认证得到AuthenticationDenied。取cookie使用 `origin + '/api'` 与该端口的 `session_cookie_name(origin)`，不是无cookie请求401。重试期间没有业务界面恢复。

I2把A的真实logout POST留在route待续，传播固定失效事件让B显示登录面板；B提交真实账号后500ms内无login网络请求。释放A路由后观察顺序严格为 `logout_headers` → `login_request`。验证A旧值已经撤销、B实际cookie解析为不同员工，B页面刷新后会话值仍有效。登录与退出均消费真实后端，未伪造响应或Set-Cookie。

页面title含TradeOS、URL保持profile同源、登录/处理中界面非空、无vite错误遮罩、无pageerror；首次匿名恢复401与主动中止网络请求为探针预期，未声称所有console资源错误为零。检查1440×1000与390×844，390无水平溢出，localStorage+sessionStorage为0。
截图已查看：`/private/tmp/tradeos-pilot-task4-fix1-retry-complete.png`（390px空登录表单）、`/private/tmp/tradeos-pilot-task4-fix1-pending-logout.png`（390px仅退出处理中，业务/登录都隐藏）。没有截图已填写的密码表单。

探针早期失败属于脚本：过严URL判定、根路径cookie读取遗漏/api、Playwright同步循环内直接asyncio.run；修正探针后运行，两条实际流程通过。每次失败/通过均finally精确stop/require_stopped并按owner核验移除本次容器和卷，关闭browser/context/DB/client；没有触碰真实profile、共享git或历史output。固定Docker socket，现有镜像，无pull、真实Provider、桌面或外部客户端。完整角色/重置/停用/备份恢复浏览器链仍属Task5。

最终源代码重新build后再次运行同一浏览器命令，两条流程再次passed，owned_cleanup complete。提交前审计：tracked dirty仅上述6文件；untracked总数400且全部historical output；git status stderr捕获4行只计数。

Fix提交：`ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`。精确6文件；cached diff check通过；commit stderr捕获268行仅计数。提交后审计：tracked dirty=0，untracked=400且全部historical output，其他未跟踪0。I1/I2/I3本轮修复完成，Task4最终gate等待controller复审；M1及Task5既有范围保持延期。
