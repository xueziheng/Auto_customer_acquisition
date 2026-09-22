# Task5b 实施报告

日期：2026-09-05。状态：实现与本地验证完成，交控制器独立审查；没有开始Task6。

- 实施base：`a55e44a5db7bc2393ff948f827df0df26d0d772e`；分支 `codex/web-core-completion`。
- 源码提交：`34be43465992844b9e61080bffab82eaea989308`。本报告另作本地文档提交，不将文档HEAD冒充源码HEAD。
- 唯一worktree：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`。
- 输入沿5a最终 `46df91c37034c7a236c8a7351c5d57c232bdc76c`（含Fix1）、Task4既有owned launcher/anchor与5b brief；未修改controller ledger、整体plan checkbox或其他brief。

## 结果和限制

交付0059三表、整页耐久事务、真实SENT精确关联、人工绑定/带版本原位恢复、待核对只读API和原singleton入站driver。API与scheduler各自构造独立实例，读取同一tenant耐久账本；没有第二业务系统、正文worker、任意cursor编辑或身份自动选择。

**受控默认仍缺完整reply消费者，自动抓取明确disabled。** 控制器裁定：只有本进程完整注册原reply_qualification与原InboundMessageStored consumer时才注入driver。当前受控 `reply_factory=None`，可人工绑定、查询和下载已有待核对，cursor不抓取/不推进，不制造无人消费的入站死信。`/health/capabilities` 的 `inbound_body=disabled/required_ports_missing` 与绑定状态分开；status的active只表示已有合法绑定。Task6补齐原消费者后沿同一条件启用。没有半截生产classifier、暂存队列或空ack。真实Provider/客户操作、部署、push、merge均`not_run`。

## 实现文件职责

| 文件 | 职责 |
|---|---|
| `workflows/reply_qualification/inbound.py` | 显式完整canonical指纹、全页预检、域服务编排、commit未知耐久核实 |
| `workflows/reply_qualification/inbound_contracts.py` | 隐藏opaque cursor的技术事实、有限DTO、页事务/核实Protocol |
| `workflows/reply_qualification/inbound_management.py` | 当前员工授权、真实人工绑定、状态与expected_version原位重试 |
| `infra/db/email_inbound_uow.py` | tenant锁→cursor锁→bound Outreach/Conversations单session→单commit；清理保留primary |
| `infra/db/repositories/email_inbound.py` | tenant技术查询、首次绑定、CAS错误状态/恢复、新事务核实与只读review |
| `infra/db/tables.py` / `migrations/versions/0059_email_inbound.py` | cursor/receipt/review及tenant复合FK、Raw一致性检查、只增trigger、非空downgrade保护 |
| `domains/sending_identity/{permissions,service,service_impl}.py` | 独立boss人工绑定action，核实际登记产生的SendingIdentityId |
| `domains/conversations/{service,source_access}.py` | 公开纯当前active boss权限端口，固定read/retry动作，不造Message借权限 |
| `tool_gateway/handlers/email_inbound_raw.py` | 当前授权、精确Raw、4MiB有界完整性、task-owned一次性slot、成功ledger后再次授权 |
| `apps/composition_support/email_inbound.py` | ADR0026唯一具名机械组合例外；两个指定manifest/Gateway/slot/Store/wrapper，各调用独立，无engine/全局cache/扫描/循环 |
| `apps/api/routers/email_inbound.py`及API原工厂 | 真实HTTP路由、受限下载、原依赖生命周期释放 |
| `apps/scheduler_worker/inbound_driver.py`及原main/runtime/controlled | 原singleton阶段前后同backend确认、固定阻断/有界等待、消费者前置与原进程释放 |

唯一机械组合消费者是 `apps.api.composition.runtime.build_phase1_dependencies` 与 `apps.scheduler_worker.runtime.SchedulerRuntimeFactory`。tenant、profile、实际外部transport、secret resolver/ref、session factory、当前权限端口和lease owner均显式传入。对象构造无网络IO；Deferred对象资源由原API/worker工厂对称aclose，异常/取消沿原生命周期处理。四个ADR0025 reader规则未放宽。共享代码不共享对象的证据见真实driver restart测试对store/raw/objects逐项`is not`及持久cursor接续。

自审去掉了ruff全文件格式化造成的既有tables/SendingIdentity无关格式变动，仅保留本批表与窄方法。

## 真实接口与交接

- `GET /email-inbound/status`：当前active boss，安全绑定/阻断/等待状态、version、identity_id与时间，无cursor/header/secret。
- `POST /email-inbound/binding`：只收`identity_id`，必须真实登记并明确人工选择；alias=`primary`、route=`controlled`、config_version=`v1`来自受控composition，拒绝extra字段。同身份幂等，任何身份/route替换拒绝；保存真实confirmed_by/confirmed_at、固定bootstrap_started_at/after_epoch，再允许profile空锚定。
- `POST /email-inbound/retry`：只收`expected_version`；陈旧版本409，未到Retry-After仍waiting且版本/期限不变，期限后原位恢复；永久/history过期可人工原位核对但仍失败时继续blocked。无reset/跳最新。
- `GET /email-inbound/reviews?limit=1..100&after=irv_…`：只返回review_id、固定reason、created_at、archived，拒绝任意额外query。
- `GET /email-inbound/reviews/{review_id}/raw`：未知404、未归档409、真实对象缺失/损坏固定503；成功为`application/octet-stream`附件、固定文件名、nosniff、CSP sandbox和no-store。无Raw如实不可读，已归档但对象缺失也不伪造成功。
- `GET /health/capabilities`：原scheduler健康服务的固定RuntimeCapability DTO列表，无业务ID/正文；不改`/health/ready`原响应语义。

5a消费：真实 `ToolGatewayEmailInboundReader.fetch(tenant_id,mailbox_alias,opaque_cursor,page_limit)` 返回 `ArchivedInboundPage`。fingerprint明确覆盖完整route/原Message-ID/In-Reply-To/UTC时间/Raw tenant+ID+hash+size/parser/guard/disposition，不hash默认脱敏model_dump。workflow不导入Gmail codec；首次持久化之后才调profile。

关联只调用原`Outreach.resolve_delivery_feedback(DeliveryCorrelationLookup)`，SYSTEM scope严格当前configured identity，真实Attempt必须SENT；无关联与别tenant已发ID只review，错页tenant/route整页拒绝。不猜From/References/Subject；引用自有出站ID不是发件人认证或Validated Need。AUTO与普通退订候选只入Message，DSN/ARF/SENT/DRAFT跳过不做反馈业务。

Task6：原InboundMessageStored→原ReplyQualificationEventHandlers→原reply Run已由真实链路核验；本批测试只起Run，不执行分类，不预插结果。完整原classifier/content reader/action ports组合及生产启用由6完成。
Task7：技术review不是Message或ReplyWork；正常关联Message沿原Conversations/ReplyWork接口后续处理，不能从review推导Validated Need。
Task8：实现Sender页明确人工绑定、待核对列表/安全下载和版本化恢复控件，并结合scheduler能力解释“已绑定/自动处理未启用”；本批没有声称Web页面完成。Task9沿固定reason/next_retry_at/expected_version恢复，不开放cursor编辑。

## 原子性与审计口径

tenant专用事务锁→cursor锁→排序整页receipt预检→原Conversations/Outreach bound UoW→一次commit。Message、InboundMessageStored/outbox、receipt、review、cursor共同提交。Provider同ID异内容、RFC同ID异原件/时间/另一条真实SENT关联整页拒绝，winner不变；两session及两mailbox并发依原域锁幂等。新事务核实真正cursor与全页receipt决定commit/close未知，不能以内存返回假成功。

原`StandardAuditLogger`是独立运行日志，没有业务审计表；保持既有`_TransactionAwareAudit` allow缓冲、提交后flush范式。提交前buffer/deny sink及域/数据库写失败整页回滚；提交后sink失败固定记录“入站提交后审计刷新失败”，不宣称业务回滚或重做。取消在提交前回滚并向上传播，已归档真实Raw保持不可变，不跟PG回滚删除。Gateway原件读取失败也不能交付页推进。

## RED证据与迭代

所有pytest统一前缀：`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest`。

- 初始fingerprint模块缺失：1 failed；新增显式指纹后1 passed（0.16s）。cursor技术仓储缺失：1 failed（7.46s）；真实独占PG保存后1 passed（6.55s）。这些是实施前功能缺失，不是Provider替身伪造结果。
- 当前权限/独立绑定action的RED为4 failed，随后单元组5 passed（0.18s）。管理组合缺失RED 1 failed（7.12s），随后真实权限/原件组2 passed（7.48s）。
- `... tests/unit/test_email_inbound_page.py -k scheduler -q --tb=short`：RED 1 failed,5 deselected（0.20s），缺原SchedulerRuntime.inbound_driver；接原入口后1 passed,5 deselected（0.20s）。最终该测试已增强为缺锁在任何phase前失败，不只检查字段存在。
- `... tests/integration/test_email_inbound_access.py -k http_status -q --tb=short`：旧路由404触发RED；新增路由后无组合真实503通过（1 passed,2 deselected,8.00s）。
- `... tests/integration/test_email_inbound_driver.py -k owned -q --tb=short`：无消费者时真实外部页产生review，触发零推进断言RED；落实消费者前置后独立进程组通过（1 passed,1 deselected,15.40s）。
- 测试搭建过程中也出现旧公开发送路径拼写、不存在的get_message_attempt、固定员工全局PK/重复业务幂等键、只读核验表名、Supervisor脚本import路径与AppleDouble迁移sidecar问题；均按真实原接口修正，不计作功能RED。真实SENT取证经tenant只读SQL，不新造业务读取API。

中间分组不是最终一次总跑：事务故障7 passed（10.54s）；poison/真实PG等待取消/双mailbox5 passed（10.10s）；provider429/503/401与真实backend失锁5 passed（7.92s）；HTTP下载/缺原件1 passed（8.94s）；真实原Outbox起Run1 passed（7.79s）；第一轮聚焦合跑45 passed（47.40s）。之后仅类型签名/格式噪声收口并补AUTO/普通退订/技术skip职责测试，最终数字以下为准，不累加这些组。

## 最终验证

最终同版本聚焦合跑：**exit0，46 passed in 45.47s**。这是一次完整命令输出；不与前述中间组相加。

最终合跑命令：

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_email_inbound_page.py tests/integration/test_email_inbound_page.py tests/integration/test_email_inbound_access.py tests/integration/test_email_inbound_driver.py tests/integration/test_email_feedback_uow.py::test_real_domain_services_share_feedback_page_transaction tests/integration/test_email_feedback_uow.py::test_post_commit_audit_failure_is_fixed_logged_not_retry_signal tests/integration/test_conversations_messages.py::test_reingest_identical_external_id_returns_same_id_without_second_event tests/integration/test_conversations_messages.py::test_reingest_semantic_mismatch_fails_closed tests/integration/test_reply_qualification_workflow.py::test_auto_reply_short_circuits_without_actions tests/integration/test_reply_qualification_workflow.py::test_reply_received_does_not_retrigger_classification -q --tb=short
```

覆盖：页第二条PG写和域audit buffer失败全回滚/原Raw仍可读；同页重放、双session、stale CAS、双mailbox；真实commit后响应异常/commit前故障/真实close后异常/提交前取消/真实PG锁等待取消；三类同ID冲突及另一个真实SENT关联；真实跨tenant出站review和错tenant整页拒绝；当前boss/非boss/离职/跨tenant/未知/无Raw/缺对象；429期限不可越过、陈旧retry/失败CAS不覆盖胜者、history过期只原位重试；实际原scheduler backend扫描前后终止、独立API/scheduler停启保留绑定；原Outbox→原Run幂等；AUTO/退订candidate与技术skip职责；直接旧Conversations/reply/feedback兼容。

静态精确作用集为本提交全部28个Python修改文件（ruff）及其中23个生产文件（mypy），通过Python subprocess参数列表执行`.venv/bin/python -m ruff check <作用集>`与`.venv/bin/python -m mypy <生产作用集>`，exit0；最终ruff输出`All checks passed!`，mypy输出`Success: no issues found in 23 source files`。没有全仓mypy/97或216套/全Web重复测试。

`.venv/bin/python scripts/check_boundaries.py`：exit0，分层/金额/置信度/事件/tenant/AGENTS/域结构全部通过。
`.venv/bin/python scripts/scan_sensitive.py <本提交增量文件>`：exit0；只扫增量，未改scanner或掩盖Task12已分配的四个旧fixture命中。
`git diff --check` 经Python subprocess捕获stderr执行：exit0。

API schema按原exporter独立生成：`.venv/bin/python apps/web/scripts/export_openapi.py`，stdout写本次临时schema文件，**exporter自身exit0后**才执行`node apps/web/node_modules/openapi-typescript/bin/cli.js /tmp/task5b-openapi.json -o apps/web/src/api/api.d.ts`，生成器自身exit0（7.13.0）。没有把最后pipe的0当作导出成功。`npm run typecheck`（apps/web）exit0，vue-tsc --noEmit；新增DTO和API路径已进入原生成类型文件。

## 迁移、资源与清理

0058是实施前唯一head；0059静态literal DDL运行时不导入当前ORM。复用本次独占Supervisor PG先upgrade至head，再downgrade0058→upgrade head成功；真实绑定非空后尝试降级被原保护拒绝，读取alembic_version仍0059，不清数据来通关，不削弱任何历史非空downgrade保护。

所有新真实测试复用Task4 `Supervisor`、`OwnedContainers`、`OwnedProcess`和5a owned fixture；仅持久ControlledGmailTransport外部响应合成。真实SENT均走原公开登记/认证/预热/Prospecting验证/Demand信号假设/Campaign审批激活/发送HTTP流程，没有预插SENT、Message、Need或Opportunity。只读核验带tenant。故障只作用本owner PG连接或MinIO对象；没有真实邮箱或客户操作。

独立进程测试实际调用原`Supervisor.restart`（也是既有HUP处理调用的方法），验证本批应用停启与耐久恢复；**本批没有另称发送了OS SIGHUP**。Task4已有OS信号/anchor验收不重做。测试finally与owned fixture检查`cleanup_errors == []`及私有config已删除；模型生命周期、对象transport、engine和本owner进程/容器对称释放。数据库迁移AppleDouble仅通过原`remove_appledouble_version_sidecars`删除versions目录已知sidecar；未repair/repack/delete共享.git、未读.env或既有凭证、未pkill/prune/动其他owner资源。

暂存检查发现新迁移literal SQL行尾空格，已仅去除无语义空格；最终`git diff --cached --check`与迁移ruff/扫描均exit0。报告提交前再次增量敏感扫描与diffcheck通过。独立审查尚未进行；真实Provider/not_run、Task6完整分类/Task7工作台/Task8 UI/Task9恢复页面仍按各批范围交接。

### 静态Python作用集

```text
apps/api/composition/runtime.py
apps/api/controlled.py
apps/api/dependencies.py
apps/api/main.py
apps/api/runtime.py
apps/scheduler_worker/controlled.py
apps/scheduler_worker/main.py
apps/scheduler_worker/runtime.py
domains/conversations/service.py
domains/conversations/source_access.py
domains/sending_identity/permissions.py
domains/sending_identity/service.py
domains/sending_identity/service_impl.py
infra/db/tables.py
apps/api/routers/email_inbound.py
apps/composition_support/email_inbound.py
apps/scheduler_worker/inbound_driver.py
infra/db/email_inbound_uow.py
infra/db/repositories/email_inbound.py
migrations/versions/0059_email_inbound.py
tests/integration/test_email_inbound_access.py
tests/integration/test_email_inbound_driver.py
tests/integration/test_email_inbound_page.py
tests/unit/test_email_inbound_page.py
tool_gateway/handlers/email_inbound_raw.py
workflows/reply_qualification/inbound.py
workflows/reply_qualification/inbound_contracts.py
workflows/reply_qualification/inbound_management.py
```

## Fix1：I1提交阶段取消与关闭错误叠加（2026-09-06）

**范围：只处理Important I1，状态ADDRESSED，等待同reviewer仅修复范围复审。** 起点 `f432415da070d4de254c9930aa1469bc9e713260`；Fix1源码HEAD `f3e2a942a91730c3c339dd82ecb7598744c6337a`。Minor M1没有修改，按控制器裁定由Task8消费前修正下载schema；本修未改API、Web类型、正式规格、controller ledger、其他brief或整体plan。

I1的根因是`__aexit__`只保留入参是否有异常的布尔值。业务块正常返回后，提交或审计刷新阶段出现的新`CancelledError`没有成为primary；若close再失败，取消就被`InboundCommitUnknown`覆盖，已耐久时processor甚至返回replayed。

本修将primary改为实际首个异常对象。提交/审计刷新阶段捕获到新BaseException时先保存，再原样raise；finally仍清空审计缓冲并关闭session，关闭故障不能替换已有primary，只记录固定安全日志。只有没有primary时的关闭失败保持原提交未知处理。进入`__aexit__`前已存在的业务/取消异常、提交未知核实与独立运行日志语义均保持。

### RED：四个组合故障先于修复执行

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_email_inbound_page.py tests/integration/test_email_inbound_page.py -k 'new_cancellation or cancel_before_commit_close or cancel_after_commit_close' -q --tb=short
```

退出码1：`4 failed, 25 deselected in 14.78s`。

- 单元测试调用真实UoW的`__aexit__`：commit取消+close错误、audit flush取消+close错误均错误地抛`InboundCommitUnknown`。修复后还断言向上传播的是原来同一个取消对象、确实调用close并清空审计缓冲。
- 真实PG提交前取消+真实close结束后再抛错误：错误地转换为`InboundCommitUnknown`。
- 真实PG原`super().commit()`已耐久后取消+真实close结束后再抛错误：`DID NOT RAISE CancelledError`，确认被processor新事务核实后吞成正常重放。

### GREEN：修复后的相关事务组

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_email_inbound_page.py tests/integration/test_email_inbound_page.py -k 'new_cancellation or uncertain_commit or postcommit_log or real_pg_wait or second_receipt or buffered_domain' -q --tb=short
```

退出码0：`12 passed, 17 deselected in 11.87s`。随后仅ruff整理新增测试换行，未改变执行语义。

作用集：两个新最小取消组合；原事务故障组扩为六种（提交前/后普通错误、单独close故障、单独commit取消、提交前取消叠加close错误、已真实提交后取消叠加close错误）；原提交后日志失败、真实PG锁等待取消、页第二条receipt故障、域audit buffer第二项故障。

两项真实PG新增组合都先断言`CancelledError`传播。提交前场景独立读取cursor仍为原值、Message/outbox/receipt/review计数全0，再以正常外层UoW重做一次；提交后场景独立读取next cursor/version与Message/outbox/receipt已存在，正常processor同页恢复返回replayed。两者再次重放后都严格为`(1,1,1,0)`，没有重复副作用。没有种结果态或使用旧共享DB。

### 静态、自审与清理

精确改动文件只有 `infra/db/email_inbound_uow.py`、`tests/unit/test_email_inbound_page.py`、`tests/integration/test_email_inbound_page.py`，以及本报告追加。

```bash
.venv/bin/python -m ruff check infra/db/email_inbound_uow.py tests/unit/test_email_inbound_page.py tests/integration/test_email_inbound_page.py
.venv/bin/python -m mypy infra/db/email_inbound_uow.py
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/scan_sensitive.py infra/db/email_inbound_uow.py tests/unit/test_email_inbound_page.py tests/integration/test_email_inbound_page.py
```

各命令退出码0；ruff `All checks passed!`，mypy `Success: no issues found in 1 source file`，结构自检七项全部通过，增量扫描无新命中。git操作使用Python subprocess捕获stderr；`git diff --check`与暂存后`git diff --cached --check`均exit0。报告追加后另行扫描本报告并检查暂存差异，均exit0。

自审确认：普通body primary仍由原async-with传播；commit普通异常仍进入既有提交未知路径；commit/flush新取消在finally之前已成为primary；新取消和close故障同时出现时不再返回replayed。没有改原Outreach/Conversations接口或全局异常策略。

RED与GREEN均复用5a/5b的owned fixture、Task4 Supervisor/OwnedContainers/OwnedProcess；真实PG、MinIO、公开发送业务流程、Gateway与原领域服务不替换。故障seam只在真实AsyncSession提交前/真实提交后抛取消，close先调用真实super().close再抛固定错误。finally和fixture按原流程释放资源，cleanup_errors为空且私有config已删除；单元测试另断言close被执行和审计缓冲已清空。没有再次执行46全套、Web/schema、真实Provider、push、merge或部署，没有新增子代理。
