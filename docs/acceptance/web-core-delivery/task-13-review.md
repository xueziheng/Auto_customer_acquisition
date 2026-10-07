# Task13 独立审查 · 最终限定复审

日期：2026-09-06。限定复审 FIX BASE `fe6f7dc2622391c9359866dd90fafbf3573ebdb5` → REPORT HEAD `febf7e0eeb20282b511b9e5bc48c760a0a325ad6`，FIX SOURCE `041bc741e0cd57dc9ed942d3a53f116b4fdffcbd`。同一审查者只检查 I1/M1/M2 修复及直接新增内容；下方首轮发现和 Changes requested 保留为历史，由本节当前结论替代。

## 当前双结论

- **Spec compliance：Approved。** I1、M1、M2 全部关闭；静止 owned 恢复、五项桌面接口、能力与平台限制和证据交付范围保持不变。
- **Code quality：Approved。** 本轮未发现新增 Critical、Important 或 Minor；无未关闭的 Task13 审查项。这只是 Task13 结论，不表示最终全分支审查、部署或真实业务启用已通过。

## 修复核对

| 原发现 | 静态核对与已有验证证据 | 裁定 |
| --- | --- | --- |
| I1 / P2 | `_artifact` 使用 AsyncExitStack，engine 创建后、settings/transport 构造前即登记 dispose，transport 创建后即登记 aclose；`_restore` 每创建一个 client 就登记 close，再创建下一个；`_empty_target` 的 client 也使用同一机制。独立 callback 捕获普通关闭异常并只追加固定类别，其他 callback 仍执行。存在主失败时 stack 保留主失败；无主失败而有关闭错误时固定 `backup_resource_cleanup_failed`。报告新增 `resource_cleanup_errors`，不会因关闭失败误报正常成功。新增三个无DB替身用例分别覆盖 transport close失败仍dispose、第二client构造失败且first close也失败时保留主失败、first close失败仍关闭second；检查明确的关闭次数/次序与安全类别 | 已关闭 |
| M1 / P3 | logging恢复已移动到测试最外层finally，与Supervisor清理、JSON创建/写入及结果断言解耦。证据写入普通异常经过固定化；已有主失败不被写入失败覆盖。新增无DB替身用例同时注入source构造和证据写入失败，断言保留固定主失败且logging恢复原值 | 已关闭 |
| M2 / P3 | 正式索引仅删去历史表中空行，Task3a/3b/5a/5b八条与前面表格连续；没有改32份历史报告或其hash | 已关闭 |

`ExitStack` 逆序关闭，target client先于source client关闭，与原目标“任意一个关闭失败不跳过其余已建资源”一致。这里只保证每个已登记资源的关闭被尝试并记录失败，不声称发生真实SDK关闭故障后资源必然释放成功。未修改生产Connector、Supervisor、业务权限或备份产品边界。

## 本轮证据绑定

- 已完整检查提供的58420字节限定diff；首轮归档正文/原完整diff没有重读，32份archive没有重复全量hash审计。新增正式报告与scratch报告字节相同，diff十个路径工作树均与index一致，HEAD为上述指定版本。
- `task-13-fix1-gates.json` 中两个测试SHA256均匹配当前源码；`backup-restore.json.test_source_sha256`亦匹配恢复测试。FIX SOURCE清单只含七项指定源码/文档/证据路径；报告包随后单列，不把报告HEAD误作另一次运行源码。
- `backup-restore-pre-review.json` 与 FIX BASE 中原 `backup-restore.json` 精确字节一致，保留旧9.31s正常路径及其当时的证据边界；Git读取该安全JSON的stderr仅计7行，未回显或维修。
- 新正常证据 source `25cfa8c8ff3445cfa969c9e5474df64f`、target `f9e358faf5424a4cb2e6e619d36c225e` 不同owner；source/target安全metadata与原件hash完全相同，source不变、误目标拒绝。`resource_cleanup_errors=[]`，两owner的cleanup errors为空、私有config删除、进程空、listener fd全为-1。
- 实施者已报告无DB替身真实RED 4 failed/0.48s → 最终4 passed/0.44s，正常恢复新owner 1 passed/9.67s；相关boundary七项、ruff、敏感扫描及格式检查通过。本轮只审源码和安全证据，未重新运行这些命令。source门禁记录当时66个索引链接，报告包新增链接后的67个是后续结果，不混写成同一时点，也不累计测试数。

## 当前 Cannot verify

本次未运行测试、数据库、Docker、业务或浏览器进程，未故障注入真实SDK，未派子代理；不把静态复审称作新增实测。已报告替身覆盖原来的普通关闭/构造/写入故障；不保证操作系统强杀、永久SDK阻塞或真正无法释放资源时仍能成功回收。现有安全证据只证明新owner的静止PG与一份原件恢复，仍不证明PITR、运营中PG/S3一致性快照、备份保留/轮换、生产恢复或外部邮箱/模型/游标状态恢复。

没有读取实际config、.env、真实DSN、token/cookie、SQLite、dump或原始敏感日志；没有提交、push、merge、部署或外发。Task12双Approved基线与Mac/Linux、真实Provider、共享认证、通用Agent/Browser/Catalog后续消费者和桌面限制沿用，未重开旧任务。最终全分支审查、控制者裁定追加和scratch收口仍待独立完成。

---

# 首轮独立审查历史（已由上述限定复审关闭）

审查日期：2026-09-06。范围为 BASE `aa24508dc77fac1e6cc2e21225cb3dcd21dd240b` → HEAD `fe6f7dc2622391c9359866dd90fafbf3573ebdb5`，实现 SOURCE `0bfb4d11a8e3e8eb7678d48be091736bd399870b`。本报告只裁定 Task13，不是最终全分支审查，不重开已 Approved 的 Task0–12。

## 结论

- **Spec compliance：Changes requested。** 文档、五项客户端边界、历史证据持久化、Task7 docstring 和静止 owned 恢复范围符合要求；恢复验收尚有 I1 的异常资源收口缺口，不能将正常路径的清理结果扩称所有失败路径均已收口。
- **Code quality：Changes requested。** 1 项 Important/P2、2 项 Minor/P3；没有 Critical。I1 修复后可做限定复审，无需因此重跑 Task12 未变全仓。

## 分级发现

### I1 / Important / P2：一项关闭失败会跳过后续资源释放

位置：`tests/integration/test_web_core_backup_restore.py:230–232`；同类位置 `:277–278`、`:297–299`。

`_artifact` 在同一个 finally 中顺序执行 `await transport.aclose()` 和 `await engine.dispose()`。具名核对现有 `connectors/object_store/s3.py:80–85,87–100`：`aclose()` 通过 `_call` 关闭 SDK，SDK 关闭失败会转换为 `TransientError`，所以这不是一个被契约保证不会抛错的关闭动作。该异常会跳过 `engine.dispose()`；外层 finally 只持有 Supervisor，并不知道这里的 engine，因而不能补做释放。此时数据库连接池/连接可能尚未释放，就开始删除 owned 数据库。类似地，`_restore` 的第二个 client 构造失败时第一个 client 尚未进入 finally；第一个 `close()` 失败也会跳过第二个 client 的关闭。

这不否定已报告正常路径的 `1 passed / 9.31s` 和两组 `cleanup.errors=[]`，也没有发现误删其他 owner 的正常路径；缺口是本批明确要求的“客户端/连接先收口，再精确清理 owned 资源”在关闭失败时不成立。建议每创建一项资源就登记其清理动作，用嵌套 finally 或 ExitStack/AsyncExitStack 保证其余 close/dispose 仍被尝试；各清理失败保留固定安全类别，保留主失败，不回显 SDK/SQL 原始异常。用受控关闭失败替身覆盖该顺序即可，不需要扩大为生产备份服务或重跑旧全量。

### M1 / Minor / P3：证据写入失败会遗留全局日志禁用状态

位置：`tests/integration/test_web_core_backup_restore.py:394–396`。

测试在入口把全局 logging 禁用至 CRITICAL，但恢复原值放在证据目录创建/写入之后。如果此处发生只读目录、权限或磁盘写入失败，`logging.disable(prior_logging)` 不会执行；同一 pytest 进程的后续用例会继续被全局静音，影响诊断和日志行为验证。建议把日志设置恢复放到最外层、必执行的独立 finally，不能依赖证据写入成功。这是失败路径问题，当前安全 JSON 已成功写入并不证明该路径。

### M2 / Minor / P3：正式历史导航表的后八项被空行拆出表格

位置：`docs/acceptance/web-core-delivery/README.md:93–101`。

第93行空行结束前一个 Markdown 表格，Task3a/3b/5a/5b 的八行后续内容没有新的表头和分隔行，按 GFM 会呈现为普通管道文本，无法保持前面“历史原文 / SHA256”的两列导航。链接和 hash 本身有效，没有证据缺失。建议去掉这一空行，或给第二张表补齐表头与分隔行。

## 已核符合项

1. 先读 brief、review-context、根与就近 AGENTS、HANDBOOK、Task13正式spec和完整实施报告；按提供的完整 diff 文件顺序检查本批变更。历史精确复制按 manifest 程序核验，不重新审旧实现；因单次工具显示截断，仅补读未显示段落，没有重生成完整 diff。
2. 当前 HEAD 精确为指定版本；diff 中61个路径的工作树 bytes 均与 index blob 一致。Task13两份报告内容相同，恢复测试当前 SHA256 与安全 `backup-restore.json` 记录一致。
3. `archive-manifest.json` 的32份档案均匹配记录的 SHA256 与字节数。31份当前 source 与 archive 完全相等；progress 在截止快照之后追加，archive 是当前 source 的精确前缀，旧内容无改写。截止为 `2026-09-06T11:53:12.025277+00:00`，补录报告时间另列；归档中含 Ruling 的行数为87，正式索引没有将其当作问题数量。
4. 六张 Catalog archive SHA 匹配；原六路径分别与 BASE SHA 匹配。三张历史代表图的归档 SHA 也匹配。没有改动其他 output，没有维修共享 Git；六次读取 BASE 图片的 AppleDouble stderr 各7行，只计数。
5. README、ROADMAP、HANDBOOK、apps事实、操作说明和主矩阵明确区分本机受控闭环、Phase1运营未验收、已验Phase2切片和共享部署未验收。48e4465 的9318全量与7e10383的1条修复后主链分列，未合计；未将后者称作全量。历史报告的 pending/failed 有明确时点解释，Task13及最终全分支裁定未提前写通过。
6. 四应用HUP与PG随机HostPort stop/start限制分开；A5仅固定端点pause/unpause。Mac统一入口的完整quotation/自动寻源准入未配置，独立Linux报价/PDF不同owner、Need/Opportunity；没有拼成Mac整条报价。真实provider、共享登录、桌面、通用Agent生产消费者、Browser任务源和Catalog培养消费者均诚实关闭或not_run。研究PG quota与联系人内存限流、回复模型局部计数与总token/费率/工时unknown分开，未造业务效率数字。
7. 客户端五项边界记录真实现有接口、owner、授权、输入/输出和失败范围；未来设备能力不能授予tenant、employee、文件、Secret引用或业务审批权限，没有Tauri/虚假IPC/新launcher/迁移。Task7两处 diff 只有docstring，授权后未分类与无权/跨tenant/不存在语义分开。
8. 恢复测试只能自己新建source和另一新owned target，无任意运行库/dump/配置参数。source仅迁移/合成员工/一份合成原件，无应用写入者；target不迁移、不初始化，public空关系检查在pg_restore之前，同owner与恢复后非空目标拒绝，bucket复制前要求空。未关闭不可变约束，原RawArtifactStore读取重算hash，业务SQL带tenant；schema版本和空库关系查询是基础设施检查。
9. 安全恢复证据记录source/target metadata与原件hash相同、source不变、两个不同owner、两组cleanup errors为空、私有config删除、进程空、listener fd全为-1。该正常路径证据有效；范围仅静止PG与一份原件，不扩成PITR、运营中跨PG/S3快照、受控邮箱/模型/游标灾备或自动恢复launcher。

## 具名域外契约核对

| 风险 | 定向源码 | 结果 |
| --- | --- | --- |
| 误目标、误删资源及应用写入者 | `scripts/controlled_web_supervisor.py` 的构造/start_infrastructure/run_once/close；`infra/controlled/resources.py` 的 OwnedProcess、OwnedContainers | owner随机生成，固定本机Docker socket；创建记录精确ID，操作核label+ID，清理复核并记录安全失败。迁移/初始化通过PID出生与anchor收口，完成后移出processes；测试没有start_apps。正常目标防误边界成立 |
| 私有配置进入模型/报告 | `infra/controlled/config.py` 源码的 read/write/resolve | 0600文件、0700目录、非symlink、当前OS owner与路径owner校验；配置只由测试进程内存处理。未读取任何实际config文件；新增测试外层将普通运行异常变固定stage失败，安全JSON无DSN/key/raw bytes |
| 原件比对是否只信metadata | `artifact_store/service_impl.py:205–257` | get经tenant UoW取record，重新读取对象并验证长度/hash；恢复测试确实走此路径 |
| SDK关闭是否可能中断engine释放 | `connectors/object_store/s3.py:80–100` | 原aclose允许固定TransientError，确认I1；不修改或重审旧connector行为 |
| 上传/设备自报是否能成为权限 | `apps/api/routers/work_uploads.py`、`apps/api/identity.py:133–177` | 上传tenant/员工/UserId来自服务端identity；dev=false拒绝，dev下重读员工及active/manager范围。新文档没有授予设备注册/文件夹权限 |
| Browser与Capability是否真实存在 | `apps/browser_worker/main.py:31–108`、`shared/schemas/runtime_capabilities.py`、`apps/api/routers/health.py` | 具名Protocol/DTO和enabled/disabled/configuration_error枚举准确；只是可信内部组合边界，不是客户端授权端点 |
| 通知/Secret是否虚构接口 | `notification_gateway/models.py:68–78`、`connectors/object_store/s3.py:23–26`、`infra/controlled/config.py:79–83` | NotificationChannel、ObjectStoreSecretResolver、ControlledConfig.resolve真实存在；原生通知/Keychain仅未来责任 |
| Task7说明是否改变既有分类行为 | `domains/conversations/service_impl.py:580–590`及本批两个docstring diff | 先actor/message授权，再检查分类；说明与行为一致，diff无业务修改 |

## Cannot verify / 验证边界

- 按委派约束未启动测试、业务、DB、浏览器或外部服务；未派子代理。结构检查、ruff、敏感扫描、文档链接、Task12全仓/Web和恢复演练的实际运行结果沿用已审阅安全报告，未重复执行，也不把静态审查写成新的运行通过。
- 本轮只验证归档hash与导航/状态语义，未重新打开历史原文或重审已Approved旧实现；图片仅核SHA和归档关系，未新增视觉验收结论。原截图的可见性说明沿用实施者/root已查看证据。
- I1/M1是静态可达失败路径，本轮未故障注入；目前不能证明关闭失败后所有client/engine及全局logging均能收口。
- 没有读取.env、真实DSN、运行config/token/cookie、SQLite、dump或原始敏感日志；没有提交、push、merge、部署、外发或供应商接触。
- 当前正常恢复证据不证明持续运营备份保留/加密轮换、PITR、跨库一致性、外部邮箱状态、真实provider或多人部署。Task13修复复审、最终全分支审查、最终裁定追加与scratch收口仍由控制者安排。
