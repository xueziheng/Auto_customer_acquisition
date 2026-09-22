# Web 核心完整化：最终全分支审查

审查完成日期：2026-09-07。此报告是关机暂停后恢复的**同一个、唯一最终全分支审查席位**的结论；不是第二次全分支审查。

- 固定 BASE：`ec801a87270503b92d3c70389cbcd4b92807dbb9`。
- 固定 HEAD：`b2c64dbf42f2a4cdbade1df16eece8500749d4d1`。
- **Spec：Needs fixes。Quality：Needs fixes。Ready to merge：No，完成下述 Important 修复及限定复审后再判断。**
- 完整清单：Critical 0；Important 1；Minor 3（其中 2 项为已披露、继续保留的直接测试覆盖缺口）。
- 结论只针对本机受控 Web 范围。没有执行 merge、push、部署、真实客户发送、真实供应商联系或桌面工作。

## Strengths：已经成立的部分

1. 受控外部响应与核心业务执行有明确分界。最终主链经过真实联系人 Gateway/单 Provider、可达性持久事实、独立审批、Campaign 发送、入站、回复确认与接管；research_only 使用独立场景，没有将研究假设直接提升为已验证需求或 Campaign。当前开发身份选择仍明确标为开发入口。
2. 回复解析保留完整候选安全检查、当前表达/引用隔离、逐字证据、原件有界读取与模型安全投影。HTML void 补修和原件追溯能解决具体误拒与来源问题。但这些内容安全检查不能替代下述员工授权。
3. API/Web 对未知命令结果、当前版本、精确对象、幂等 payload/key 与迟到响应做了实质处理。Sourcing canonical 恢复没有借机创造新业务命令；HTTP 请求模型 422 与进入业务后的 400 已正确区分。来源记录文案不再把任意 Provenance 等同于已验证事实。
4. 生命周期修复关注实际所有者：advisory lock 的原连接、借用客户端、线程/进程退出、owner 资源和失败清理。Task13 的恢复目标是另一个新空 owned 环境，未将静态演练扩称在线生产备份。
5. 观测保留 token、实际费用、人工工时和合格机会口径的 unknown；窗口内创建记录的当前累计状态没有被包装成历史实际发生量。正式交付说明保留 Mac/Linux、真实/合成及当前未实现能力的界限。

## Issues

### Critical（必须立即修复）

未发现达到本范围 Critical 级别的问题。没有据此推断不存在所有潜在缺陷。

### Important R1：受托回复在当前员工授权之前读取原件、调用模型并记录分类

**位置：** [reply_composition.py:64](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/apps/scheduler_worker/reply_composition.py:64)、[message_content_reader.py:89](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/apps/scheduler_worker/adapters/message_content_reader.py:89)、[steps.py:143](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/workflows/reply_qualification/steps.py:143)。当前授权发生在 [reply_composition.py:126](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/apps/scheduler_worker/reply_composition.py:126)。

**实际路径及前提：** `CurrentEmployeeReplyFactory` 保存受信配置给出的 `tenant_id`、`employee_id`（受控装配在 `apps/scheduler_worker/controlled.py:79` 使用本 owner 配置的首个员工 ID）；这只是标识绑定，不是当前权限事实。Factory 将 `resources.bounded_raw_store` 直接交给 `ArtifactMessageContentReader`。`ClassifyStep.execute` 校验 context 后，143 行 load，148 行模型分类，182 行 `record_classification`。Reader 只按租户查询 Message、检查入站及 Artifact 类型/预算/内容，再直接读取 Raw；该段没有配置员工的当前 active/role 检查，也没有 Gateway 调用及其 ledger。

真正的当前员工查询只在后续业务动作的 `CurrentEmployeeReplyActions._run`：打开 `core.employee_scope(tenant)`，通过 `EmployeeService.get_employee(..., actor=system:reply-actor)` 获取配置 ID 对应员工，构造 `QuoteEmployeeFact`，144 行调用 `require_reply_internal_access(action="qualify")`。`domains/conversations/source_access.py:41` 要求同租户、当前 active boss。这里的 system actor 仅用于受信员工事实查询，不能提前授权原件/模型读取。

**真实后果：** 当该配置员工已停用、降权或不存在，而先前已排队的回复开始 classify 时，仍能读取原件、把清洗后的客户当前表达交给模型，并记录分类；后面的业务动作才会拒绝。租户过滤、输入护栏和发送关联均不能回答“当前这个受托员工是否仍有权处理该内容”。当前授权范围内已可触发该行为，不需要假设部署或攻击凭证；本轮没有复现运行或读取真实邮件。

**契约判断：** 根硬边界要求外部动作经 Gateway；入站规格明确 Raw 读取在 Gateway 授权之后。旧 ArtifactStore 设计虽然允许 app/workflow 使用受信存储端口，但 `docs/superpowers/specs/2026-08-13-artifact-store-design.md:285` 明确要求“调用之前由对应 app/domain/workflow 完成 actor 授权”。因此旧基础设施架构不能构成当前 actor 授权豁免。Task6 的直接 bounded 端口装配与这项总体约束存在缺口，即使其局部规格曾允许该资源接缝，也必须补齐实际闸门。

**最小修复建议：**

- 将现有当前 active boss 的 `qualify` 检查抽成受信窄适配，并在开始原件读取前执行；不使用客户端角色、缓存的旧员工角色或固定 system 权限代替。
- 原件通过已存在的窄 Gateway 插件读取。`tool_gateway/handlers/inbox_evidence.py:167` 的 `ToolGatewayInboxEvidenceReader.read` 已提供 actor/message/task 绑定、当前证据授权、大小/类型/hash 校验、读后再授权、仅在 Gateway `SUCCEEDED` 后领取同调用 bytes、finally 清 slot。`prepare:114` 也绑定 tenant/user/message。先确认该 Inbox action 与受托 qualify 读的既有语义完全一致；可一致复用时直接接线，否则增加同样窄的 qualify 读 manifest/handler，复用现有检查/slot模式，不改 Gateway 核心或扩 manager/sales 权限。
- `email.inbound.raw.read` 是技术 review_id 权限入口，不能伪造 review 来替代 Message 授权。
- 有界读取的 await 返回后、模型调用前重读当前授权；其他有副作用的 async 边界（例如模型返回后持久分类）也应保持当前权限契约。不能只在 Factory 创建时检查一次。
- 定向回归：配置员工停用/降权/缺失/跨租户时 Raw、模型、分类写入均为 0；读取期间撤权阻止模型；Gateway deny/failed 不交付 bytes；正常路径仍沿原分类与真实业务链。

**现有证据不足：** 本次完整新增测试已读，Inbox 原件下载/下一问存在撤权测试，但没有覆盖本受托 classify 的配置员工撤权窗口。它们不能替代此路径的授权证明。

### Minor M1：受控回复模型的 SQLite 连接没有确定关闭

**位置：** [reply_model.py:21](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/infra/controlled/reply_model.py:21)，同文件 47、62、75 行。

四个方法均执行 `with self._connect() as connection`，没有 `close`/`closing`。SQLite Connection 的上下文只处理事务提交/回滚，不关闭连接。因此每次配置、调用、统计创建的连接释放依赖对象回收；长期反复演练时，文件描述符/数据库句柄释放时点不确定。这只涉及合成受控模型，未观察到实际耗尽，也没有打开 SQLite 检查；不升级为生产故障或 Important。

建议保持原事务语义，统一用 `contextlib.closing` 加连接事务上下文，或明确 `finally: close`。若本统一修复波处理，使用可计数 close 的连接替身验证正常/异常都关闭即可，不必启动数据库或大规模资源测试。Task12 已补 `reply-model.sqlite` 文件及侧文件在停止后的清理；文件清理与这里的连接释放是两件不同的事。

### Minor M2：技能目录精确资源上限缺直接边界覆盖（保留项）

**位置：** [service.py:31](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/agent_runtime/skill_router/service.py:31)，同文件 234–249；对应 `tests/unit/test_skill_router.py`。

实现已经显式限制深度 64、条目 10000，拒绝目录 symlink、非法层级 manifest、缺 manifest 的 SemVer 目录。已有真实危险结构测试；仍没有直接覆盖 64/65 与 10000/10001 两侧。实际后果是阈值 off-by-one 的回归保护有限，**未发现实现错误**。

独立接受原 parked 理由：不为本机受控目标在 T7 制造上万无关真实文件。可用有意义的受控遍历枚举验证真实计数逻辑，或在技能加载规则下一次变化时补；这是可保留的 Minor，不是当前业务功能未完成，也不是 R1 的合并阻断替代项。

### Minor M3：WorkerContextAdapter 内部 await 取消缺直接覆盖（保留项）

**位置：** [context_adapter.py:100](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/apps/agent_worker/context_adapter.py:100)、同文件 169；[test_agent_worker.py:249](/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/tests/unit/test_agent_worker.py:249)。

现有取消测试用 `CancellingContexts` 替身，验证 Worker readiness/signals/runtime 清理及 consumer/gate 未执行，但不经过真实 descriptor/policy/facts 到 ContextBuilder 的新增 await 链。适配器捕获 `Exception`，当前取消会传播；**未发现实现错误**。遗漏的直接回归可能使未来异常包装误吞取消不易被发现。

独立接受原 parked 范围：通用 Agent Worker 尚 disabled，组件行为与外层清理已覆盖。下一次接入生产 provider、改变异常分类或启用任务来源前，应在实际 adapter 的 descriptor/facts 挂起点取消，断言原样传播及不消费。无需为当前报告重跑既有相同版本测试。

## 遗留项、疑点与裁定的独立结论

此前记录的 87 条 Ruling 及全部 deferred/parked 已纳入本次审视。控制器批准不等于授权或实现正确性的豁免；R1 正是对既有 Task6 装配的独立否决。历史每条原因/成本继续保存在正式 `rulings-progress-history.md`，本报告按技术结果归组，不抹去旧失败和临时裁定。

| 范围 | 最终裁定及保留代价 |
| --- | --- |
| Task1/2 技能与上下文 | 受信 descriptor/版本、每任务不可变 policy、当前事实适配与限制成立；eval_refs 仍是逻辑引用，不声称每个评估已运行。M2/M3 保留；通用 Agent 生产消费者仍未启用。 |
| Task3a/3b 组合与生命周期 | 原连接锁获取/取消/关闭、惰性 owned SDK 清理、真实 UserId→Employee 映射、同进程 canonical 实例与 narrow composition support 成立；不通过强转 ID 或演示事实假装当前权限。共享迁移隔离的早期失败保留在验收历史。 |
| Task4 入口及门禁噪声 | 原四进程受控入口成立；初期提案成功不等于研究执行。Task12 已补实际 research/contacts 接线。旧 172/120 lint、Router R0004、测试 fixture scanner 命中不能被历史摘要冲淡；当前最终证据见版本表。 |
| Task5a/5b 入站 | typed Connector/Gateway、初始游标、整页事务/幂等、永久 blocked 与 version retry、消费者就绪前 disabled 成立。运行日志 commit 后 flush 的失败只能固定可观测，不能将已提交事务伪称回滚。原件 binary OpenAPI 缺项已在生成 schema 补齐。 |
| Task6 回复 | 13 个 HTML void 标签回归与引用后当前片段隔离已补；投影不跨引用拼接。授权 Raw/模型前 R1 尚未关闭。下一问仅排队/建议，不能视为客户已确认或自动对外发送。 |
| Task7 当前权限/纠正 | 无权、跨租户、不存在统一 PermissionDenied，授权且尚未分类才 ValidationError；两处旧 docstring 已修，行为未改变。 |
| Task8 Web 与原件 | 发件身份冷启动、登记/认证/预热/绑定、生成 DTO、填充 Inbox/接管、跨通道 403 后旧响应隔离已读。旧截图仅证明旧轮次指定行为，不当最终全量证据。 |
| Task9 未知与恢复 | 精确原 payload/key、canonical 原命令、legacy header 的有限恢复规则成立；首次 HTTP 请求 422 可改，已有未知结果不能凭后续拒绝解冻。入站 status/bind/retry 共 generation 修复已读；503 测试标题第 40 行已准确收窄。 |
| Task10 跨页与报价 | 精确 ID/Need/来源/成本/Quote/Run/Approval 链，不猜反向路由。390 溢出及来源标题已修。Mac 无 quotation 配置、Linux 独立 owner/不同 Need 全链是实际边界，不能合并成 Mac 一站式报价。 |
| Task11 观测 | sourcing 成功绑定、workflow_version 不同、Opportunity.need_id 不同三参数回归已补。精确 Run subject/tenant、typed unknown、阶段各自时间字段成立；没有伪造合格机会、生产成本或 cohort 转化率。 |
| Task12 失败修复与完整验收 | 首轮 98 失败逐项映射到最终完整运行；SQL 迁移、Linux 就绪、严格 HTTP 白名单、schema/权限 fixture 等修复没有放松生产规则。I1 独立审批创建者/提议者/提交者绑定在后续两测试修复中补足，不冒称最初全量就有该证明。 |
| Task13 备份与交付 | 静态 owned PG + 一个合成 Raw、另一个新空 owned 目标、owner 拒绝/非空拒绝、摘要/hash、关闭客户端/清理修复成立；不代表 PITR、在线一致性备份、外部邮箱状态或共享部署恢复。 |
| ControlledModeBar 文案疑点 | “研究、联系人…外部场景未启用”可合理解释为真实外部服务，后文说明邮件进受控邮箱；与已启用合成研究/联系人不构成确认的功能缺陷。可选明确写“真实外部服务未启用”，不计入必须修复清单。 |
| Git AppleDouble/早期未知告警 | 仅捕获计数，不修共享 `.git`。早期 3/2 warnings 分类未知、Router 噪声、早期 cleanup_unknown 不由后续成功倒推为零；当前版本结果分别列示。 |

DB 暂停恢复仅证明同 owner、同端点的 pause/unpause 后原实例恢复。此前容器单独 stop/start 随机公开端口变化是真实失败边界；不能把它归因并修饰成“数据库透明重启已验证”。HUP 也仅覆盖应用进程生命周期。

## 证据版本、完整性及 Cannot verify

本审查没有重跑 pytest、Web 测试、lint、schema 导出、Docker、浏览器或服务；下表的运行结果来自已留存的安全证据，源码/diff/哈希与映射是本审查实际核验。没有将多轮局部数量相加为完整门禁。

| 版本/证据 | 可以支持的结论 | 不能支持的结论 |
| --- | --- | --- |
| `48e4465fc307212e794d6ed87501cb418f74d245` | 后端完整 `9318 passed / 0 failed / 0 skipped / 0 warnings`，required E2E 开启；Web 411 tests、typecheck/build/schema 与静态门禁通过。 | 不等于固定最终 HEAD 单次全量，也不覆盖 R1 撤权窗口。 |
| 同版本 lint attribution | 112 warnings、0 errors；43 max-attributes、66 singleline-content-newline、3 self-closing，共 112；三条归属提交早于本分支 BASE。 | 不能声称 Web lint 零 warnings；旧 172/120 不是当前数字。 |
| 首轮完整 backend 与 98 项 resolution | 首轮失败记录保留，98 个唯一失败项均有最终完整运行关联及具名修复类别。最终同版本完整通过解除当时未解组合/顺序风险。 | 不把每个早期迁移错误都断言同一个已证明根因；Linux relay 饱和未获证明。 |
| `7e10383` I1 主链 | 仅两个测试文件修正独立审批 actor 编排与绑定断言；新 owner 完整主链 1 passed，34.97 s。 | 不虚称此版本再跑后端全量；未改生产实现。 |
| `041bc741e0cd57dc9ed942d3a53f116b4fdffcbd` | 备份清理 4 个 unit 及新 owner 正常恢复 1 项，后者 9.67 s；source/target schema 0059、1 行、75-byte 合成 Raw 的元数据/bytes hash 一致。 | 不与原 9.31 s 轮次相加；不代表生产备份、运行中多表一致性或外部系统恢复。 |
| `b2c64db` 固定审查 HEAD | 包含上述生产代码、I1 两测试和备份后续修复及文档，完整 diff 已审查。Task13 两生产/测试说明修改经 AST 证据仅 docstring。 | 没有该 HEAD 的单轮全量测试证据，不需为数字整齐补一轮相同生产代码全量。 |
| `068eed58`、`42df08a7193bd15c480aee0d8afd33d1ca25cacb` | 暂停/恢复 controller 元数据；固定代码审查范围不变。controller 另报当前正式索引 69 链接无缺失。 | 不将 controller 状态更新当新生产实现或本审查亲自重跑门禁。 |

完整性核验结果：

- 固定 diff 共至 69008 行；手写源码、测试及新契约按块完整读一次，所有实质截断已补齐。生成区 14213–15709 按 exporter、生成命令、实际路由/schema/枚举/安全投影及同版本生成记录核验，未把机械声明逐字阅读当质量保证。
- `final-source-snapshot.json` 的 1630 个允许源码文件 hash 与 `48e4465` Git 对象全部相符。当前相对该 snapshot 的 4 个差异为两 docstring 与 I1 两测试，已核与固定 HEAD 一致；新增 Task13 恢复测试另按其版本/hash 核验。没有因此宣称所有证据都产生于最终 HEAD。
- `archive-manifest.json` 的 32 原文归档 SHA/长度全部匹配；16 源可从捕获 BASE 取回，15 当时未 tracked 的源与当前原文相符，1 可变 progress 按不可变捕获副本/hash核对，不要求当前 ledger hash 不变。32 个任务 report/review 正式副本与原文一致，避免重读相同内容。6 个 Catalog 副产物图及 3 个历史代表图 hash一致。
- Task13 两个测试的实际文件 hash 均与 fix gates相符；backup JSON 中 source/target 摘要相等、owner不同，test source hash相符。只读安全元数据，未读取 dump、SQLite、私有配置或真实凭证。
- Mac 最终主 owner 的 proof 包含真实合成研究/联系人调用、发送/回复、独立审批、接管 accepted 和无页面错误记录；Linux报价为另一独立 owner。截图/历史 cleanup manifest 只支持其记录时点。本审查未再次运行资源审计、未把截图中的待接管状态当 accept 的证明，也未把临时截图列表说成全部永久归档。

仍属 disabled/not_run：通用 Agent 的生产任务/policy/descriptor/模型消费者、Browser Worker 任务来源、Catalog 培养 queued 后消费者、真实 provider/Gmail/供应商联系、真实共享登录/会话/TLS/部署、Tauri/桌面。未运行能力不是因测试数量较多而自动获得验收。金额与概率没有从合成模型结果冒称生产值；合格机会需五条件合取，目前没有可核的成本分母。

## Recommendations 与 Assessment

收齐清单后只执行**一统一 fix 波 + 一限定复审**。R1 必须修复；M1 可在同波作窄资源修复；M2/M3 可继续保留明确原因和后续触发条件，或以小而有意义的定向测试关闭。不得借此重做旧模块或另派第二次全分支审查。

限定复审应读取统一 fix diff，确认原发现关闭及改动引入的新风险；重点验证当前 actor、Gateway 结果/ledger 与原件/模型调用顺序、撤权窗口、租户及正常回复主链。根据实际改动跑受影响回归和必要检查，报告精确新源码版本；只有新的跨模块失败/组合疑点才构成重跑完整后端的理由。

**最终判断：审查工作已完整完成，分支尚不能批准。** 受控 Web 的绝大部分规格与既有验收证据成立，但受托回复的当前权限在读取/模型之后才生效，违反权限与 Gateway 约束。修复 R1 并完成限定复审之前，Spec/Quality 均保持 Needs fixes。审查者未修改实现、index、HEAD 或提交；仅按委派维护本报告与原断点文件。
