# 回复模型网关接入设计

日期：2026-10-05。状态：接入方向已由用户确认；用户在审阅请求后要求继续，现进入实现计划阶段。

## 1. 目标与验收对象

用户目标是测通“回复识别 → 退订拦截 → 需求建档 → 人工接管”，并能在远端 Windows 的现有 TradeOS 网页中查看记录。

成功对象是同一条具有合法业务关联的入站回复：由实际部署的分类端口读取当前回复，经过模型网关分类、提取客户明确表达且带原话证据的字段，再由现有域与工作流执行抑制、Validated Need、Trade Opportunity 和 Handoff；最终真人在网页接受接管，数据库记录接管人。仅有受控模型输出或截图不足以证明真实模型链路完成。

本文沿用根 AGENTS.md、GLOSSARY.md 和 ADR0070；不改变九条硬边界。模型负责理解，确定性代码负责权限、金额、状态、退订、配额和幂等。

## 2. 已核实的现状

源码位置是远端 WSL 的 `/home/joyu/projects/TradeOS`，日常运行配置是 `/home/joyu/.local/share/tradeos/zzy-local/config.json`。审阅基线为 `2d5b15ab620faad0b7a09d1b9399c1a9ae2ed96d`，工作树已有大量未提交改动；实施必须逐文件保留这些改动。

- `apps/scheduler_worker/pilot.py` 显式注入规则分类器；该分类器不提取需求字段。
- `CurrentEmployeeReplyFactory` 已支持注入 `ReplyClassifier`；现有 QualificationAgent 与 StructuredReplyModelPort 可验证结构化分类和逐字证据。
- 独立模型入口已装配聊天和研究，尚未装配业务邮件回复。
- `ModelCapability` 尚无专用的回复能力；`model.generate` 的通用网关、权限检查、配额账本和 DeepSeek Connector 已存在。
- 当前日常租户没有模型配置记录。不得搜索环境文件、读取密钥内容或用测试额度代替部署配置。
- 当前员工、消息与出站关联已有读取前、模型前、落库前复核；新装配必须保留它们。
- `InboundMessageStored` 只为可关联原 Outreach Attempt 与 Enrollment 的入站邮件启动 `reply_qualification`。canonical 幂等键是 `reply:{message_id}`。
- “我的邮箱”是独立私人邮件镜像。普通邮件和本人诊断测试邮件不自动成为业务回复，更不自动成为已验证需求。

已有证据：相关集成回归39条通过、1条失败；失败发生在当前规则分类器处理具体规格回复时。受控浏览器链路已验证接受请求、队列刷新和接管人，演示实例按用户要求保留。对160条样本使用当前回复的中性主题评估，规则分类器36条分类正确、119条未识别、5条误分类；20条退订样本分类及抑制范围正确。上述结果不能用作真实模型验收。

## 3. 选择的接入方式

复用现有单副本 scheduler、reply_qualification 工作流、QualificationAgent 和 `model.generate`，在装配层增加回复专用的调用绑定。另建回复 worker 会引入新的生命周期和并发协调，本次不采用。直接调用模型 SDK 不满足网关约束，不是可用替代方案。

旧 pilot 继续保留原有网络边界和显式规则模式。模型回复只在具有明确模型配置的 standalone 入口装配。启动模型回复能力不等于批准 Campaign、发送客户邮件或修改发件身份。

```text
已关联业务邮件 → InboundMessageStored → canonical reply_qualification Run
    → 现有授权与原文读取/投影/输入护栏
    → 回复调用绑定 → model.generate → 既有 DeepSeek Connector
    → QualificationAgent 结构校验与逐字证据验证
    → Conversations 分类事实 → 既有确定性 REPLY_ACTIONS
    → 抑制/停止序列，或 Need/Opportunity/Handoff → 真人接受
```

## 4. 调用身份、权限与稳定绑定

### 4.1 专用能力

在共享 `ModelCapability` 中增加 `reply_qualification`，不借用 `research`、`business_read` 或聊天 turn 身份。该变更是公共契约的增量扩展；不修改事件字段、Provenance 结构、Gateway stage 顺序或核心管线。

回复调用身份固定包含 tenant、当前用户与员工、canonical Run、配置版本，以及 `sequence=0`；`turn_id` 必须为空。身份来自受信装配、当前 Employee 和持久业务关联，不能从模型输出或邮件正文取得。

### 4.2 绑定查询

新增窄读取端口，由 infra/db 实现。按显式 tenant 读取并核对：

1. Run 类型为 `reply_qualification`，当前步骤为 `classify`，状态仍允许执行；subject_ref、context.message_id 和 `reply:{message_id}` 幂等键一致。
2. Message 为同租户入站，出站关联非空；context 中的出站、Enrollment、Account、Contact ID 与持久记录一致。缺失或冲突一律拒绝。
3. 委托员工来自配置绑定，重新读取当前在职状态、角色和 user_id，再调用既有回复权限规则；不新增“system 可以读全部邮件”的旁路。
4. 当前模型配置、API/scheduler 版本和有效心跳、成功探测、资料外发许可及限额均由原模型配置服务检查。

这些查询不读取或打印凭证，不在独立连接上重新锁住 workflow 已持有的 Run 行。scheduler 单副本锁的校验与资源所有权保持原有顺序。

### 4.3 恢复与配置变化

现有 Gateway 的幂等键包含完整 InvocationIdentity，现有模型调用唯一约束也包含配置版本。因此仅重建“当前配置版本”的身份，会产生重新调用风险。

回复绑定读取器必须同时检查该 tenant、Run、回复 capability、sequence 对应的已有模型调用安全元数据。有历史调用时，用户/员工、模型和配置版本必须与原绑定一致；发现多个不一致绑定则关闭。配置更新或委托员工变更不能自动为同一回复创建新的付费身份。Gateway 继续持久预留及计量，回复装配不另外扣费或清零额度。

已发出后超时、崩溃或结果丢失遵守既有 unknown 语义。即使 Provider 已成功，但结果未安全落到业务分类，也不能靠再次调用取回结果。工作流保留失败与模型账本，等待明确的人工核对；本次不新增重放、退款、未知槽释放或自动补偿入口。

## 5. 组件职责

| 位置 | 变更与职责 |
|---|---|
| `shared/schemas/model_invocation.py` | 增加专用 capability 值，不改变现有身份字段 |
| `apps/scheduler_worker/reply_model_binding.py`（新） | 回复绑定端口、模型授权适配器、绑定 GatewayJsonModelClient 的 ReplyClassifier；每次调用核对当前事实 |
| `infra/db/reply_model_binding.py`（新） | 查询 canonical Run、消息关联和既有调用身份；所有业务查询带 tenant |
| `apps/scheduler_worker/standalone_reply.py`（新） | 机械组装授权、计量、Gateway 和既有回复 factory，不承载业务决策 |
| `apps/scheduler_worker/standalone.py` | 在显式配置允许时装配 Gmail 入站与回复；沿用同一 singleton、数据库和原件资源 |
| `infra/standalone/settings.py` | 增加明确的回复启用开关，默认关闭；开启时校验既有回复端口允许的输出 token 范围 |
| `tests/unit`、`tests/integration`、`tests/evals`、`tests/e2e` | 覆盖调用边界、完整业务效果、160条语料及网页最终状态 |

实际文件拆分可在实现计划中细化，但不能把领域规则移入装配层，不能使用 Connector 直调绕过 Gateway，也不修改 `tool_gateway/pipeline.py`。

新分类器通过现有 `classifier=` 插件点注入，复用 QualificationAgent 和 StructuredReplyModelPort。最大输出 token 取管理员显式配置，不采用端口默认值代替付费配置；启用回复时超出既有128–8192范围即配置失败，不静默截断或放宽。

## 6. 业务行为与失败语义

| 情况 | 必须产生的行为 |
|---|---|
| 明确退订 | 保存分类与联系人/账户范围，落抑制并停止相应序列；不创建 Need 或 Handoff |
| 投诉、自动回复、拒绝等 | 复用现有动作表；不把自动回复误当采购意向 |
| 客户提供规格 | 只有合法字段与原件中逐字证据通过后，才更新需求；缺失字段继续按现有规则处理 |
| Need 满足规则 | 由原 Demand、Opportunity 和 Handoff 服务推进，不能由模型指定晋升或接管动作 |
| 无模型配置、未探测、外发关闭、额度不足 | 不调用 Provider，不伪造分类、需求或成功状态 |
| 员工停用、跨租户、关联变更 | 原文/模型/结果应用相应阶段拒绝；调用途中撤权不应用已计费结果 |
| 模型输出格式错误或伪造原话 | 拒绝业务落库，保留固定安全失败信息 |
| Provider 结果不确定 | 不自动重发付费请求；不补零用量，不把工作流标为完成 |
| 重复入站、重启、重复投递事件 | 不重复创建分类、抑制、Need、Opportunity、Handoff 或付费调用 |

模型失败时，现有未分类回复的账户状态仍为 unknown，发送路径保守暂停；这不等于已经创建了人工审核任务，也不等于可从网页一键恢复。已有重分类接口不能被冒充为未分类消息的恢复入口。

数量、价格等继续使用现有确定性转换和 Decimal 规则。模型不能扩展字段词表、输出概率或承诺价格/交期；`unit` 与 `recurring_requirement` 保持现有禁止自动提取的边界。

## 7. 配置、启用与资源

复用明确路径的 StandaloneModelSettings，增加默认关闭的回复开关；无需新增供应商、模型路由或新凭证格式。开启回复要求 profile 已明确绑定 Gmail 和委托员工。启用方式、委托员工、模型或限额变更均使用新配置版本。API 与 scheduler 使用同一配置版本；Web 探测成功和各项授权成立后才允许真实模型调用。

操作者仍需提供：模型设置文件路径、准确模型 ID、配置版本、全部调用/并发/输入/输出/超时限额、业务资料外发许可，以及由可信 resolver 在进程内取得的密钥引用。不得把密钥发到聊天，不得扫描 `.env` 或替操作者编造限额。调用次数与 token 上限不等于固定人民币账单。

日常服务切换前先完成受控验收，再进行明确配置下的真实模型验收；只在既有运维入口管理进程，不启动第二个持锁 scheduler。模型接入本身不启动 Campaign，也不改发件身份、DNS、预热或退订公开地址。

当前用户正在查看的受控演示按要求保留，后续测试使用自己的独立资源。演示数据不能复制到日常租户充当真实业务事实；停止演示仍由其原有资源清理路径执行。

## 8. 验收证据

| 验收项 | 可接受的证据 |
|---|---|
| 原失败复现与修复 | 原规格回复用例保留完整 Run → Need → Handoff 正向业务断言，被测对象改为本次将部署的实际模型装配；规则模式另测无法提取时明确失败。不得删除正向断言、标记 xfail，或绕过 Gateway 注入固定分类答案消除失败 |
| 模型出口 | Provider spy 与持久 ledger 证明每次外部调用经过 Gateway；未授权、未配置、外发关闭均为零调用 |
| 身份与恢复 | 跨租户、伪造消息/Run/出站关联、撤权、配置变化、重启及结果丢失测试；不产生第二次付费发送 |
| 退订 | 现有20条退订语料及新增中英文变体；真实集成查询证明分类、抑制范围、序列停止和重复扫描幂等 |
| 需求证据 | 产品、数量等字段指向同一客户消息及原话；无证据、引用历史和恶意注入被拒绝 |
| 160条评估 | 使用真实配置模型逐条执行既有冻结语料，并按生产投影去掉主题标签提示；报告模型/版本、错误与未识别、分类、动作、抑制范围及字段提取结果，不只给总体分数 |
| 语义偏差 | 关键退订/投诉漏拦、自动回复误停、无证据建档或错范围抑制阻止验收；其余偏差逐条说明影响并列为未完成项，不能改期望数据或硬编码样本来变绿 |
| 网页接管 | Chromium桌面1440和手机390宽度；接受请求成功、最终提示与队列刷新、数据库 accepted_by 一致，并核对权限撤销后的内容清除 |
| 真实运行 | 明确标注实际模型是否调用、Gmail是否为受控端口、使用的profile/tenant和配置版本；真实模型未运行时目标仍未完成 |
| 结构与回归 | 相关单元/集成测试、评估、浏览器验收、Ruff及 `python3 scripts/check_boundaries.py`；不能以局部通过宣称全库通过 |

评估会消耗实际模型调用额度。每个评估样本使用独立、可审计的受控业务身份，经同一 Gateway/分类端口执行；不借用正式客户 Run，不把语料标签送给模型。先验证调用预算足以完成已批准范围，额度不足时记录未运行部分，不提高限额继续。原语料不原地修改；生产投影在评估输入适配层体现，原始与投影评估分别标记。

## 9. 不属于本次设计的扩展

本次不增加独立 worker、供应商回退、自动重试未知调用、付费钱包、公共部署、私人全邮箱自动分析、正式报价、自动客户回复或新的 Campaign。它们不能被用来替代本目标，也不随回复模型启用而获得授权。

本文审阅通过后，再形成具体实现计划；尚未取得的真实模型配置与付费限额继续作为真实验收的显式前置条件。
