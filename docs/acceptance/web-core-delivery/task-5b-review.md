# Task5b 独立任务审查

## Spec Compliance

- ❌ Spec：发现一项取消传播缺陷。`infra/db/email_inbound_uow.py:190` 只记录进入 `__aexit__` 时的异常，未将提交期间的新取消纳入 primary；后续关闭失败可覆盖取消。违反正式子规格“取消仍向调用方传播”和清理保留 primary 的要求，见 Important I1。
- ✅ 整页锁、预检和提交边界：`workflows/reply_qualification/inbound.py:85` 在域写入前完成按 digest 排序的 receipt 预检；`infra/db/email_inbound_uow.py:87` 在独立 tenant 锁及 cursor 行锁后组装共用 session 的域 UoW，`infra/db/email_inbound_uow.py:196` 唯一提交。`infra/db/repositories/email_inbound.py:133` 以新事务核对 route、精确新 cursor/version 和全页 fingerprint。
- ✅ 人工绑定及恢复：`workflows/reply_qualification/inbound_management.py:76` 重读当前员工事实并调用公开权限端口，`:114` 经过 SendingIdentity 窄绑定授权；`infra/db/repositories/email_inbound.py:162`、`:187` 以旧快照/版本保护失败状态和原位重试，未到 Retry-After 不清期限。
- ✅ 本批最终消费者裁定：`apps/scheduler_worker/runtime.py:1836` 仅在入站组合和原回复组合同时存在时注入 driver；`:1884` 区分 disabled/required_ports_missing。没有放宽 root 硬边界、添加培养消费者或提前交付 Task6/8 能力。
- ⚠️ 真实 Provider、Task6 完整分类、Task8/9 页面及全仓集成门禁不由此 diff 验证，实施报告明确尚未运行或属于后续批次；控制器仍需在相应里程碑验收。生成文件证明新增 DTO 已进入 API 类型，但不能单凭 diff 证明实际 exporter/类型检查进程的退出码；本审查采用已提供的同版本执行证据，不重复执行。

## Strengths

- `workflows/reply_qualification/inbound.py:12` 显式构造包括隐藏原值 header、UTC 时间、Raw 元数据、完整 route 和版本的指纹，避免对脱敏序列化求 hash 丢失关联证据。重复 provider 的异内容在任何域写之前拒绝。
- `migrations/versions/0059_email_inbound.py:34` 的 Raw/Message 引用使用 tenant 复合外键，receipt/review 只增；`:79` 拒绝非空降级，没有清数据通过验收的旁路。
- `tool_gateway/handlers/email_inbound_raw.py:152` 核对 EMAIL_RAW、MIME、tenant、ID、hash、长度；`:167` 和 `:216` 在实际读取后及成功 ledger 后重新核对当前授权。HTTP 附件、nosniff、CSP sandbox 和 no-store 与安全下载目标一致（`apps/api/routers/email_inbound.py:134`）。
- `tests/integration/test_email_inbound_page.py:695`、`:750`、`:944` 包含真实 PG 第二项故障回滚、独立 session 竞争和真实锁等待取消；`tests/integration/test_email_inbound_driver.py:47` 经原 owned Supervisor 验证独立进程下缺消费者零抓取、绑定和重启保留。测试重点是耐久业务结果，不只是 mock 调用次数。
- `infra/db/email_inbound_uow.py:199` 保留既有提交后运行日志语义；日志 sink 故障固定可观测且不重做已提交业务，符合最终审计裁定。

## Issues

### Critical

无。

### Important

**I1：提交阶段的取消被关闭异常替换，可能被当作正常重放吞掉。**

- 位置：`infra/db/email_inbound_uow.py:190`、`:210`、`:218`；后续消费路径为 `workflows/reply_qualification/inbound.py:146`、`:152`。
- 触发：业务块正常结束，故 `primary=False`；`commit()` 内发生 `asyncio.CancelledError`，外层原本重新抛出它，但 finally 中 `close()` 再失败时依然使用旧的 `primary=False`，改抛 `InboundCommitUnknown`。新事务核验发现已提交时 processor 会返回 `replayed`；未提交时 driver 也会把它变成普通存储失败和等待，取消均未按约定传播。这是取消和连接清理故障叠加时的真实控制流错误。
- 独立最小核验：仅调用真实 `SqlAlchemyInboundPageUnitOfWork.__aexit__`，用内存 session seam 让 `commit` 抛取消、`close` 抛固定错误，输出为 `cancel_commit_and_close_failure=InboundCommitUnknown`，退出码 0。没有数据库、Provider、真实凭证或文件写入。此核验确认异常覆盖，不声称模拟了真实数据库提交结果。
- 修法：在 `__aexit__` 中维护实际首个 `BaseException`，包括提交/刷新阶段产生的取消；清理失败不得替换已存在的取消或其他 primary。仅在不存在 primary 的关闭失败中产生提交未知。补一个最小聚焦回归覆盖取消与关闭错误同时发生，并覆盖提交已耐久后收到取消的情况；断言取消仍向上传播，后续恢复依赖耐久事实。
- 现有证据的具体缺口：`tests/integration/test_email_inbound_page.py:780` 将 `cancel_commit` 与 `close` 作为互斥参数，分别通过不能回答叠加故障。

### Minor

**M1：原件下载的 OpenAPI 成功响应声明为空，与实际二进制响应不一致。**

- 位置：`apps/api/routers/email_inbound.py:117`；生成结果 `apps/web/src/api/api.d.ts:12295`。
- 实际影响：路由只指定无默认 media type 的 `Response` 类，因此生成的 200 类型为 `content?: never`，而实际返回邮件 bytes。Task8 消费生成契约时无法从该接口类型获得二进制响应事实。
- 修法：在路由的 OpenAPI responses 中声明 `application/octet-stream` 和 binary schema，继续使用原 exporter/generator 更新类型；不手写前端 DTO。可同时声明本接口实际固定错误状态。

## 审查范围与核验记录

- 基点 `a55e44a5db7bc2393ff948f827df0df26d0d772e`，HEAD `f432415da070d4de254c9930aa1469bc9e713260`，源码 `34be43465992844b9e61080bffab82eaea989308`。
- 已读任务模板、brief、完整正式子规格、实施报告、相关生效 AGENTS 及 progress 末尾具名裁定。唯一 5,974 行 diff 以连续分块完成全文审阅；首次合并输出截断的报告/规则前部单独补读，未遗漏尾部；未重新运行 git diff。最后仅按 diff 计算引用行号，没有进行第二轮全文审查。
- 聚焦风险 R1：candidate 是否可能合法缺少 `In-Reply-To` 而被整页误拒绝；读取 `connectors/gmail/inbound_mime.py:290`、`shared/schemas/email_inbound.py:1` 及具名字段定位。原解析器把缺失/非法引用变为 `INVALID_IN_REPLY_TO`，未形成该缺陷。
- 聚焦风险 R2：复用反馈解析接口是否实际核 SENT、tenant、identity；读取 `domains/outreach/service_impl.py:771` 的一个完整方法（第一次输出在方法中间，续读至结束）。真实 SENT 与 Enrollment 绑定及精确 scope 由原域校验；未以实施报告的说明代替代码证据。
- 聚焦风险 R3：非空回复组合是否确实对应原消费者注册；因 runtime diff hunk 在原注册逻辑前截断，定点读未改动的 `apps/scheduler_worker/runtime.py:514`、`:1490`、`:1766`，以及 `workflows/reply_qualification/flow.py:65`。组合校验和 `InboundMessageStored` 注册均存在，没有另读该 changed file 全文。
- 聚焦风险 R4：共享组合的 SecretResolver 调用是否能仅凭 diff 证明无 IO；只检查了 `connectors/gmail/client.py:60` 的 Protocol 签名，未读任何实际 resolver 配置或凭证。当前受控运行没有证据显示构造联网，不据接口的潜在实现推断一个缺陷。
- 未复跑已报告的 46 项聚焦合跑、ruff/mypy/边界/敏感扫描/TS/schema 或迁移。仅为 I1 执行一次内存异常语义核验，使用 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1`。既有 AppleDouble Git stderr 是实施报告明确标出的环境噪声，未修共享 `.git`，不将其冒充本批产品缺陷。
- 没有子代理；未修改生产、测试、index、HEAD 或其他任务文件；本报告是唯一写入。

## Assessment

**Task quality：Needs fixes。**

主体满足本批整页入站、受限管理和消费者前置的规格，事务及权限设计清晰。I1 破坏明确的取消传播保证，修复并完成该具名聚焦回归后才可通过任务门禁；M1 为非阻断契约文档修正。

---

## Fix1 限定复审（2026-09-06）

### Finding Verdicts

- **I1：提交/审计刷新阶段取消被后续关闭异常覆盖 — ADDRESSED。** `infra/db/email_inbound_uow.py:190` 现在保存实际 primary 异常对象；`:210` 捕获提交或审计刷新阶段的新 `BaseException` 时，先保存该对象再原样抛出；`:220` 仅在没有 primary 时将关闭失败转换为 `InboundCommitUnknown`。因此已有取消经过 finally 清理后仍传播，processor 不会因关闭错误将该取消误认成可正常返回的重放。
- **I1 回归覆盖充分。** `tests/unit/test_email_inbound_page.py:100` 分别覆盖 commit 与 audit flush 取消叠加 close 错误，断言同一个取消对象传播、关闭被调用、审计缓冲清空。`tests/integration/test_email_inbound_page.py:780` 扩展原事务参数组；`:845` 对真实 PG 提交前/已提交后取消叠加实际 close 后错误，分别核对独立读取的 cursor/version 与 Message/outbox/receipt/review 数量，再通过正常 processor 恢复和重放，确认没有重复业务效果。

### New Breakage in the Fix Diff

- 无新 Critical、Important 或 Minor。普通业务块 primary 仍由原 async-with 传播；普通 commit 异常仍进入原提交未知核验；无 primary 的单独 close 失败仍保留提交未知语义。修改没有扩大事务、API、权限或 scheduler 范围。

### Out-of-Scope Observations

- 无新增观察。原 Minor M1（`apps/api/routers/email_inbound.py:117` 的二进制下载 OpenAPI 声明）保持 deferred，按控制器裁定在 Task8 消费前修正；本次未修改、未重新审查，不阻断本修复轮。

### 核验记录

- Fix base：`f432415da070d4de254c9930aa1469bc9e713260`；HEAD：`a2b7a64c4c7022effb71a470c08a1df110e2e00a`；修复源码：`f3e2a942a91730c3c339dd82ecb7598744c6337a`。
- 已按 scoped re-review 模板读取唯一修复 diff 全部 299 行，核对原报告 Fix1 的完整命令、RED `4 failed, 25 deselected in 14.78s`、GREEN `12 passed, 17 deselected in 11.87s` 及静态检查/清理记录。新增测试与所报作用集一致，未将声明直接视为修复证据，而是逐项核对了修改后的异常控制流与测试断言。
- 没有现有证据无法回答的新疑点，因此未运行任何测试、Web/schema、Git 命令或 diff 外生产代码检查。没有子代理，仅追加本审查报告。

### Verdict

- **Fix round：All findings addressed, no new Critical/Important breakage。** I1 已关闭，无未解决阻断项。
- **更新 Spec：✅；Task quality：Approved。** 本结论承接首次任务审查，并以本轮 I1 修复为限定增量；既有非阻断 M1 继续按控制器登记交接 Task8。
