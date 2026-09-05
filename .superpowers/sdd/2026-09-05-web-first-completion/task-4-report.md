# Task4 实现报告：受控启动入口与停止说明

日期：2026-09-05。状态：实现与本批验证完成，等待控制器独立审查；没有继续 Task5。

- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- base：`68fe023fbe8c838ca4cd264c25f3baac0eec2672`
- 源码提交：`d9a6b03a6f1e97b9bf7ee111f757f599f15ab4d0`（含截图证据）；本报告另作文档提交。
- 正式子规格：`docs/superpowers/specs/2026-09-05-web-core-controlled-launcher.md`
- 用户操作说明：`docs/operations/web-core-local.md`

## 实现和消费接口

`scripts/run_web_core_controlled.py` 是正式前台入口，提供 `--api-port`、`--web-port`、
`--scheduler-port`（默认0选择空闲端口）及 `--directory`（运行目录父目录）。
`make web-controlled PYTHON=.venv/bin/python` 调用同一入口。
`scripts/controlled_web_supervisor.py:Supervisor` 只监督资源、迁移子进程、应用子进程和健康；
业务装配分别在 `apps/api/controlled.py` 和 `apps/scheduler_worker/controlled.py`。
没有 apps/infra 导入 tests，没有复制旧 e2e conftest，没有在 infra 放域编排。

| 接口 | 契约与实际边界 |
|---|---|
| `ControlledConfig` / `ControlledIdentity` | extra forbidden、frozen typed模型；配置0600/父目录0700，当前UID和owner目录一致；只接受本机自有库配置，未知引用固定拒绝 |
| `ControlledConfig.runtime_environment()` / `resolve(reference)` | 完整显式合成设置及受信凭证解析；模型没有config/resolver对象，不继承业务环境 |
| `OwnedContainers.create/verify/close` | 新PG/MinIO、owner label+完整ID；发布端口核验127.0.0.1；迁移前再核验；不复用、不pull、不prune |
| `OwnedProcess.start/verified/stop/public` | 独立session、PID出生时间和PGID；记录子进程出生身份；TERM后有界等待，核验后才KILL；身份未知不发信号 |
| `Supervisor.restart/close/write_status` | HUP只重启应用，保留同owner PG/MinIO/外部场景；停止逐层清理；主失败原因和清理错误分开保留 |
| `ControlledGmailTransport(path, tenant_id=...)` | 实现原 `send`/`search` transport seam，SQLite只模拟Provider邮箱，不读取业务PG；应用重启不丢消息头和外部引用 |
| `ControlledProviderCall` / `list_calls()` | typed tuple记录每次真实send/search端口调用的ID、操作、时间；tenant过滤；重复send即使同ref也记录两次，不用唯一邮件数代替调用次数 |
| `ControlledDnsResolver.resolve` | 仅受控域和两个具名TXT子域；原DNS Connector、Gateway和认证工作流继续工作；未知域/类型固定拒绝 |
| `ControlledModelClient` / `CONTROLLED_RESEARCH_MESSAGE` | 仅精确中文合成演练输入返回既有typed研究提案响应；原TradeManager Guardrails/schema继续校验；未知输入拒绝 |
| `install_network_boundary` | 实际Python socket审计拒绝未知地址、DNS和端口；仅本次PG/MinIO目标与当前loopback监听；不是OS沙箱保证 |
| `controlledWebConfig()` / `configureControlledIdentity()` | 仅DEV且非PROD、显式owner配置；只选择本次员工，保持fixed-dev，generation变化使旧页失效；原服务端持久RBAC不变 |
| `/__controlled/status` | Vite只返回监督器安全健康投影；超过4秒无更新为unknown；不是固定ready |

API使用原 `create_runtime_app_from_settings` 的 Gmail external seam；scheduler使用3b最终
`CanonicalSchedulerBootstrap`、唯一runtime core和原 `SchedulerRuntimeFactory`。
API、scheduler、Vite是真正三个独立进程；PG/MinIO是真正新建容器。API父进程预占FD，
scheduler与Vite严格loopback绑定。所有启动/失败/健康探测均不输出raw异常或子进程日志。

只seed本次tenant ID、六个持久Employee/UserId和经理/销售映射：两名独立老板、经理、销售、
寻源、产品。现有模型没有Tenant实体表，因此没有自造Tenant表。没有预插Playbook、政策、
审批、认证通过、预热、SENT、Message、Need或Opportunity。生成的基础设施秘密仅在受信进程使用，
没有读取已有 `.env`、DSN、Token、Cookie或生产配置。

最小可审批路径：老板甲从正常API/Web设置提交Playbook/国家政策，老板乙独立审批，
原scheduler处理Outbox后激活。真实HTTP测试证明自批被既有域以400拒绝、独立审批后激活。
国家键明确用 `KE`，合成政策不伪称法律结论。指挥中心可粘贴完整中文研究演练语句生成真实提案；
研究外部组disabled时确认执行准确拒绝。用户不必在自然语言框手工填写DTO JSON。

## RED / GREEN 与发现后的修复

所有pytest均使用下列前缀，未使用外部TEST_DATABASE_URL：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest
```

以下是实施中实际观察到的RED及随后GREEN；不把后来追加的每一条断言都声称单独RED：

| 范围/命令尾部 | RED | GREEN |
|---|---|---|
| `tests/integration/test_web_core_launcher.py -k 'occupied_port or missing_node' -q --tb=short` | exit1，两项缺启动入口/JSON失败状态 | exit0，2 passed |
| 同文件 `-k 'provider_mail or dns_unknown'` | exit1，controlled providers模块缺失 | 实现后相关4项累计通过，最终总跑再次通过 |
| 同文件 `-k explicit_chinese` | exit1，模型返回controlled_model_input_required | exit0，1 passed，真实Guardrails后的提案 |
| `npm --prefix apps/web test -- tests/controlled-identity.test.ts` | 修正测试环境后2项因缺configure函数失败，exit1 | exit0，2 passed |
| 真实HUP/TERM | 已绑定端口TIME_WAIT导致重启失败 | reserve使用SO_REUSEADDR且仍listen占位；真实HUP新PID且资源保留 |
| migration failure + Docker close failure | 清理错误打断私有文件删除 | 分层finally/固定错误；主migration_failed保留且私有配置删除 |
| dead process leader | 已退出leader留下已记录的子进程 | 逐个核对出生身份和PGID，关闭残留孩子 |
| `-k provider_calls` | 缺list_calls/独立调用记录，exit1 | exit0，重复发送两个调用、跨重建持久、跨tenant隔离 |
| Web全套首次回归 | 5 failed / 332 passed，普遍identity remount破坏原报价测试 | 只在显式controlled模式remount；最终337 passed |

实现后的测试还覆盖：PG创建后失败、MinIO创建后失败、migration/identity/API/scheduler/Web各阶段
真实子进程失败、占用端口的外来监听者存活、PID出生不匹配绝不发信号、其他owner容器不被删除、
scheduler SIGKILL引发整体失败及资源清理、未知网络地址在实际connect前拒绝。
真实Provider构造探针把OpenAI/Gmail/DNS/Tavily/page/Hunter原构造器改为抛错后仍能启动受控栈；
另在原scheduler cycle外包2秒测试延迟，TERM后观察cycle完成标记再退出0。
测试没有替换域、审批、Gateway、Outbox、预算或锁；测试延迟只证明生命周期，不模拟业务完成。

## 最终命令与结果

| 命令 | 退出码与实际结果 |
|---|---|
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_launcher.py tests/evals/test_research_evals.py -q --tb=short` | 0；23 passed in 65.23s：17 launcher + 6研究对抗eval；不是全历史后端测试 |
| `npm --prefix apps/web test -- --reporter=dot` | 0；337 passed，27 files；有既有VueRouter R0004提示 |
| `npm --prefix apps/web run typecheck` | 0 |
| `npm --prefix apps/web run lint` | 0；0 errors、172 warnings，基线说明见下 |
| `npm --prefix apps/web run build` | 0 |
| `.venv/bin/python -m ruff check infra/controlled apps/api/controlled.py apps/scheduler_worker/controlled.py scripts/controlled_web_supervisor.py scripts/run_web_core_controlled.py tests/integration/test_web_core_launcher.py` | 0；All checks passed |
| `.venv/bin/python -m mypy infra/controlled/config.py infra/controlled/network.py infra/controlled/providers.py infra/controlled/resources.py apps/api/controlled.py apps/scheduler_worker/controlled.py scripts/controlled_web_supervisor.py scripts/run_web_core_controlled.py infra/controlled/__init__.py` | 0；9 source files无问题 |
| `.venv/bin/python scripts/check_boundaries.py` | 0；依赖、金额、置信度、事件、租户、AGENTS、域结构7项通过 |
| `.venv/bin/python scripts/scan_sensitive.py --staged`（源码提交前） | 0；包含全部本批源码/文档新文件，无命中 |
| `.venv/bin/python scripts/scan_sensitive.py` | 1；仅下述4处旧fixture命中，未称全仓通过 |
| `git diff --cached --check`（源码提交前，经Python捕获stderr） | 0 |

前端安全日志位于 `/tmp/tradeos-task4-web-tests.log`、`/tmp/tradeos-task4-web-typecheck.log`、
`/tmp/tradeos-task4-web-lint.log`、`/tmp/tradeos-task4-web-build.log`。
真实Chromium验证桌面和390px、受控老板乙切换、当前ready、冷启动未配置Playbook；
截图在 `task-4-evidence/controlled-desktop.png`、`task-4-evidence/controlled-390.png`。

### 可重装依赖证据

在全新临时Python3.12 venv执行 `python -m pip install -e '.[dev]'`，退出0；不是复制
现有私有venv或Catalog的.pth。安装使用显式公开pip配置，不读取用户凭证配置。
新增声明docker、psutil并要求python-dotenv>=1.2.3，确保dotenv禁用开关受支持。
新环境路径为
`/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-controlled-install-k9iqdno2/venv`。
用该环境的 `bin/python scripts/run_web_core_controlled.py --directory <本次新父目录>`
实际启动到ready，再TERM退出0，stderr为空，sys.path没有Catalog来源。
最终安全status位于
`/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-controlled-fresh-final-hwwtz3d7/tradeos-controlled-2fbd4108402f48c59b26e5f8389f96c0/status.json`，
复核 `status=stopped`、`reason=requested_stop`、`cleanup_errors=[]`。
Node使用24；本批没有新增Node包，实际运行复用本工作树已有node_modules，文档提供干净`npm ci`步骤。

### 基线告警：未在本批修改

全量敏感扫描只报告位置/类别，从未输出匹配值：

| 文件 | 行 | 类别 |
|---|---:|---|
| `tests/unit/test_agent_worker.py` | 220 | password-assignment |
| `tests/unit/test_context_builder.py` | 231 | password-assignment |
| `tests/unit/test_context_builder.py` | 648 | password-assignment |
| `tests/unit/test_context_builder.py` | 1148 | password-assignment |

`git diff --quiet 68fe023fbe8c838ca4cd264c25f3baac0eec2672 --` 上述两个文件，退出0。
Task12全量门禁处理/裁定；没有修改旧fixture或扫描规则。

ESLint JSON逐文件统计：App.vue有25条，使用 `git show <base>:apps/web/src/App.vue`
作为 `eslint --stdin --stdin-filename src/App.vue -f json` 输入，base同为25条、退出0。
其余147条位于完全未改的下列文件；对这些文件执行同一base的`git diff --quiet`退出0：

| 文件（apps/web/下） | warnings |
|---|---:|
| src/components/NotificationBadge.vue | 3 |
| src/views/NotificationCenter.vue | 23 |
| src/views/OutreachWorkbench.vue | 34 |
| src/views/SendingIdentityCenter.vue | 26 |
| src/views/billing/BillingUnavailable.vue | 10 |
| src/views/manual-phase1/ManualOperations.vue | 12 |
| src/views/products/ProductSupplyCenter.vue | 31 |
| src/views/runs/RunCenter.vue | 8 |

本批新增文件与修改的client.ts均0 warning；未全站reformat。

## 清理、自审与后续限制

最终核验本机Docker中带 `tradeos.controlled.owner` label的容器残留0；本次临时运行目录
中的私有config残留0；遍历安全status内记录的PID/子PID并核对出生时间后，存活owner进程残留0。
最终停止status保留安全诊断，受控邮箱和私有配置删除。没有pkill/prune/gitclean，未操作外来容器。
在SIGKILL监督器或断电时不能承诺自动清理，文档要求以label+ID/PID出生身份人工核验，禁止盲删。

自审核对了分层、唯一core、身份初始化范围、模型无凭证、正常审批、失败主因保留、
逐层有界清理、受控DEV身份generation及PROD拒绝。源码改动已单独提交，工作未越入桌面、真实
Provider、部署、merge/push；没有修改控制器总体plan/ledger。

| 后续任务 | 本入口仍缺能力及消费约定 |
|---|---|
| Task5 | 正文自动入站尚未实现；扩展typed读取口，沿用owner受控邮箱/消息ID/游标，不直插业务结果 |
| Task6 | 完整回复、发送未知结果/重试合成场景、通知投递worker装配仍需实现；可直接消费当前send/search/list_calls持久场景，不等Task12才开始 |
| Task8 | 发件身份register/start_warmup/管理读取Web入口；当前合法冷启动等待配置，不能伪造认证或预热来演示Campaign |
| Task12 | 研究、联系人、寻源、报价等具体外部合成组扩展与全量验收；处理基线扫描/lint门禁，不能把本报告当A1–A10全链完成 |

Agent和Browser保持disabled。Campaign真实发送core已装配，但新环境尚无合法sender；
正常Campaign提案→独立审批→激活仍必须等待真实前置配置。本批未声称真实发信、研究执行、
Validated Need、Opportunity或商业成功，也没有用预制业务数据绕过这些前置条件。

## Fix1：首次快照前退出的leader清理（review I1）

审查基点：`b818263e283c496385d0d55629b3cd05359937ee`。
修复源码：`f1632658bfe8f2a4377c171aaba171ddc512179a`。本节另作文档提交。
只修I1及直接关联的短进程/握手/FD生命周期；没有修改controller ledger、其他brief、业务域或Web。

原问题实际复现：leader生成孩子后立即退出，从未调用public；原stop成功返回，孩子仍在原组存活。
仅加快children快照无法消除这个窗口。新增 `infra/controlled/process_anchor.py` 作为小型exec包装：

1. 原Popen创建新session后，包装先创建同组anchor；anchor只继承私有控制端点和ready写端，
   环境仅PATH及dotenv禁用值，不继承业务监听FD、其他管道写端或业务配置。
2. anchor发送PID，父监督器核对PID出生时间、PGID/SID及原leader身份后通过私有通道确认。
   anchor再放行ready管道，包装才exec原命令。业务PID、原退出码、API显式传入FD保持原语义。
3. `OwnedProcess`保留anchor PID/出生时间/控制端点；原leader退出并被reap后，仍依据存活anchor
   发现和核验整组成员，不依赖父子树或首次public快照。安全status增加`anchor:{pid,born}`。
4. TERM期间anchor忽略TERM，持续保留组归属；原scheduler仍完成当前cycle。升级时只KILL核验过的
   业务leader/孩子，保留anchor到真实组成员清空；随后请求anchor退出并有界核验。
   不能核对归属、仍有存活成员或升级强停均返回非零。已登记但逃离原组的孩子仍按原保护拒绝发信号。
5. 握手失败关闭私有控制通道，尚未exec的业务命令不运行。anchor在通道失联后对自身原组TERM，
   最多两秒后KILL；start有界等待包装退出并抛固定`process_handshake_failed`，不输出raw异常。
   实际测试在anchor已发送PID但父方尚未确认时注入接收失败，核验包装退出且整组无存活残留。
6. 一次性迁移/身份初始化原来成功后直接从列表移除；新锚点使这条路径必须先`process.stop()`。
   这个直接伴生问题另做RED→GREEN，只在监督器消费处增加这一行，不改变迁移或身份行为。

### Fix1测试证据

所有下列pytest使用完整命令前缀：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_launcher.py
```

| 命令尾部 | 真实结果 |
|---|---|
| `-k leader_exit_before_first_snapshot -q --tb=short`，修复前 | exit1；1 failed / 17 deselected in 2.20s，断言`unrecorded owner child survived stop`；测试finally清理孩子 |
| `-k 'leader_exit_before_first_snapshot or dead_process_leader or process_birth_mismatch' -q --tb=short`，修复后 | exit0；3 passed / 15 deselected in 0.37s |
| `-k short_lived_bootstrap -q --tb=short`，补run_once清理前 | exit1；1 failed / 20 deselected in 0.44s，短进程结束后anchor仍running；finally清理 |
| `-k 'short_lived_bootstrap or anchor or leader or process_birth_mismatch' -q --tb=short`，补清理后 | exit0；6 passed / 15 deselected in 0.78s |
| `-q --tb=short`，完整launcher回归 | exit0；22 passed in 67.26s，包括HUP、TERM当前cycle、worker崩溃、各阶段失败、正常审批和原外来资源保护 |
| `-k anchor_survives -q --tb=short`，随后追加的升级清理用例 | exit0；1 passed / 22 deselected in 1.26s，孩子忽略TERM，stop报process_forced_stop，同时核验孩子及anchor均不存活 |

最终覆盖23个launcher用例：完整22项回归通过后仅追加最后1项并单跑；没有把两次结果伪称一次23项总跑。
接入两个附加测试时发生过一次测试代码插入位置错误导致collection SyntaxError（exit2），修正后执行；
此错误不是产品RED证据。生产改动的RED仅为上表两个明确生命周期断言。

新增具名用例：无public立即退出、活anchor握手失败、anchor不保留监听FD/管道写端、短bootstrap释放、
TERM升级清理；原出生不匹配保护增加anchor_born参数。监听FD用例在业务退出且anchor尚活时重新bind
同一端口，并读取原业务管道EOF，最后核验anchor退出。

### Fix1静态验证、资源核验与提交

- `.venv/bin/python -m ruff check infra/controlled/resources.py infra/controlled/process_anchor.py scripts/controlled_web_supervisor.py tests/integration/test_web_core_launcher.py`：exit0，All checks passed。
- `.venv/bin/python -m mypy infra/controlled/resources.py infra/controlled/process_anchor.py scripts/controlled_web_supervisor.py`：exit0，3 source files无问题。
- `.venv/bin/python scripts/check_boundaries.py`：exit0，7项结构自检通过。
- `.venv/bin/python scripts/scan_sensitive.py infra/controlled/resources.py infra/controlled/process_anchor.py scripts/controlled_web_supervisor.py tests/integration/test_web_core_launcher.py docs/operations/web-core-local.md docs/superpowers/specs/2026-09-05-web-core-controlled-launcher.md`：exit0。
- 源码提交前`git diff --cached --check`与`scan_sensitive.py --staged`：均exit0。
- git仍通过Python subprocess捕获stderr，仅输出安全stdout/退出码，没有改git身份或处理共享.git噪声。

最终真实资源核验：owner label容器残留0；本轮及此前受控目录私有config残留0；遍历安全status记录
的supervisor/业务/children/**anchor**并核对出生身份，存活owner进程残留0。无status的无快照/握手
用例在测试内直接核验原组和孩子/anchor无存活残留。没有pkill、prune或删除外来资源。

自审：握手先于exec、锚点在TERM期间存活、业务FD不泄漏给锚点、短进程锚点释放、异常主因保留、
身份未知非零、没有盲按可复用PID发信号。操作说明与子规格同步该机制。没有新增依赖（锚点只用标准库），
没有Web修改，因此未重跑337项Web套件、旧扫描或全站lint；此前Task12边界和旧告警结论不变。
