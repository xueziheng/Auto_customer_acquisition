# TradeOS 企业 Agent 升级实施计划

> 实施技能：superpowers:subagent-driven-development；逐任务实现、测试、提交及评审。用户已批准整体升级和测试，不再重复征求方案批准。

**Goal:** 让企业资料、区域与自然语言搜索、客户准备、受批触达和持续普通邮件对话连成可恢复的 Codex CLI Agent。

**Architecture:** 保留现有领域、Postgres 工作流、Codex 受限工具循环及 Gateway。企业私有目标和知识与公共网页候选分开，任务状态与副作用留在可信代码。平台管理模型连接，企业操作业务。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLAlchemy/Postgres、Vue 3/TypeScript、Codex CLI。

**Spec:** [已确认产品要求与审计](../specs/2026-10-21-tradeos-agent-upgrade-assessment.md)。用户进一步明确保留原 UI、DeepSeek 等 API 配置、全部获客源和 Obsidian 数据；区域可选、输入大致搜索要求后提交给 Agent。

## Global Constraints

- 只在 `codex/tradeos-agent-upgrade` 的独立工作区实施。原工作区和验收源码只读，不重置、不删除数据、不部署生产、不发真实测试邮件。
- 九条硬边界和逐次商业审批保持。模型无凭证；原公开邮箱不等于已验证；确认企业资料不等于发布正式 Product、批准 Campaign 或允许对外承诺。
- 复用持久企业模型连接和来源配置，不重置开关/限额/注释或索要现有密钥。用户于 2026-10-22 明确要求新增默认 DeepSeek 搜索路径、Tavily 保留为可选来源；已有模型连接和密钥不因此被静默替换。
- 接口兼容旧请求/记录。新增私有目标放企业任务，不进入公共目录。新迁移必须唯一且往返测试，禁止覆盖旧编号。
- 普通对话必须当前授权、已验证联系人、获批范围、抑制与商业承诺检查、幂等发送。未知外部结果保留核对，不自动重复调用。
- UI 原位改进；类型从 OpenAPI 生成。原始资料与 Obsidian 版本保留，模型只读授权版本，事实与推断分开。
- 业务流程测试使用合成资料、受控连接器和本地数据库；用户于 2026-10-22 追加授权对免费或已确认有免费额度的获客源做少量只读联网测试。无法确认免费额度的源不自动进入付费调用；测试不发客户邮件、不部署生产，明确区分本地测试、真实来源读取与生产交付。

## Review Focus

关注端到端接线，不能只增加 DTO/提示词就称完成。核查两企业隔离、撤权、旧版本重放、外部未知结果、重启幂等、预算累计、资料来源、自动回复商业审批、已有源与设置兼容。所有代码提交前执行 `python scripts/check_boundaries.py`。

### Task 1: 冻结现有功能并修复未完成的普通回复传输

**Files:** 累积验收源导入（来源清单另存）；`shared/schemas/business_reply.py`、`connectors/qq_mail/reply_source.py`、`reply_client.py`；已有两个 `test_qq_business_reply_*` 测试；`docs/operations/agent-upgrade-baseline.md`。

**Interfaces:** `SMTPReplyRequest`、`parse_reply_headers`、`QQBusinessReplyConnector.send_once/reconcile_once`，遵循现有测试及同类 SMTP 传输，不扩大私有测试授权或建立业务发送旁路。

- [ ] 冻结并校验源文件，规范行尾但不删除旧文件；记录旧工作区独有变更与编号冲突。
- [ ] 先运行现有 RED 测试，再实现缺失契约及严格 MIME/thread/hash 匹配；unknown 不重发。
- [ ] 基线收集、相关测试、结构检查；导入未验收部分明确标识。

### Task 2: 企业已确认资料驱动区域和自然语言搜索

**Files:** `domains/prospecting/inventory*.py`、`domains/products/knowledge_service.py`及窄读取契约、`apps/api/routers/prospect_inventory.py`、`apps/scheduler_worker/inventory_model.py`、`inventory_query.py`、`standalone_inventory.py`；相关 infra adapter；`apps/web/src/views/customer-discovery/InventoryPanel.vue`；生成 API 类型；相关单元/集成/UI 测试。

**Interfaces:** 向旧 InventoryQuery 兼容增加 intent、regions/countries、已确认资料 revision refs。私有目标保留在 tenant job；搜索引擎只接收公开可搜索标签/市场，确认资料作为私有判断上下文。当前员工、tenant、revision 每次重新授权，不能把原文直接外发搜索或写公共候选。

- [ ] RED：多地区、自然语言、旧请求兼容、错企业/未确认/撤回资料拒绝、私有资料不进入公共查询与目录。
- [ ] 在现有资料确认流程上提供明确可选的已确认资料，不新建另一个知识库；独立上下文 reader 接入真实 InventoryDiscoveryModel。
- [ ] 原搜索表单增加区域选择和需求描述，保留数量、提交、任务状态和分配；生成类型并测试。
- [ ] 保留 Obsidian 原件/版本和隔离，文档说明历史资料通过现有上传入口导入，不扫描其他企业 vault。

### Task 3: 搜索任务从完成来源边界恢复

**Files:** `apps/scheduler_worker/inventory_acquisition.py`、`domains/prospecting/inventory*.py`、`infra/db`库存任务适配和表、唯一新迁移、采集 driver 单元与 Postgres 集成测试。

**Interfaces:** tenant/job/lease 绑定的来源计划与阶段检查点；派发前持久标记进行中，结果提交后标记完成/失败，累计已保留来源预算和期限。恢复跳过完成来源，进行中未知来源不得再次派发，只允许剩余尚未派发来源；旧 token 无写权限。

- [ ] RED：完成首来源后重启、保存候选后中断、未知调用、旧租约、预算/期限不重置、无剩余来源。
- [ ] 复用既有 source attempts/ledger，尽量不重复建表；固定来源计划并保持最后探索来源。
- [ ] 迁移 roundtrip（若需要）、真实 Postgres 重启与幂等回归。

### Task 4: 搜索结果接入已有客户准备与受批自动跟进

**Files:** 现有 customer preparation/followup/verification 应用层、scheduler composition、prospecting/outreach 公共服务；对应工作流与测试。

**Interfaces:** 授权目标触发的自动准备步骤复用 account→signal→hypothesis→contact→verification→approved enrollment。记录 agent 提取者与真实网页 artifact，不伪造 human/legal basis；缺必要授权/依据/负责人/验证时留明确待办，不静默跳过。

- [ ] RED：合成搜索候选进入客户并验证；无批准 Campaign/未验证/抑制/无处理依据阻断；重启不重复建档或发送。
- [ ] 使用现有审批和 owner 入口设置自动开发范围，不添加平台模型设置到企业界面。
- [ ] 在客户详情沿用现有状态显示等待原因及下一步；维持员工权限与已有手动操作兼容。

### Task 5: 普通邮件持续问答与商业审批接线

**Files:** `apps/scheduler_worker/reply_actions.py`及新对话 driver/planner composition、`workflows/reply_qualification/`、现有 ReplyWork repository/表、发送 Gateway plugin、相关普通入站回复实现和测试/evals。

**Interfaces:** 消费现有待回复任务，加载当前 owner、受权会话、Need 和已确认/可对外使用产品事实；有界 `DialoguePlan` 只给英文普通说明/需求问题或移交原因。分类与 Need 提取仍只认客户原文。使用现有 Gateway、抑制、身份、审批、持久幂等与未知结果核对。

- [ ] RED：询盘→普通回复→下一封回复→补全 Need；重启/重复事件只发送一次；退订/拒绝/人工接管立即停；价格/交期/认证请求转现有审批。
- [ ] 明确每次任务轮数/预算和无进展上限；没有待办时不空转模型。
- [ ] 跨企业/撤权/资料更新/提示注入测试，既有真实 SMTP 协议替身及本地 Gateway 端到端测试。
- [ ] 接入 scheduler 实际构造入口，而非仅测试中的孤立类。

### Task 7: DeepSeek 搜索作为主路径，Tavily 可选与免费来源实测

**新增时间：** 2026-10-22，来自用户执行中的明确调整。执行顺序为 Task 4 → Task 7 → Task 5 → Task 6，已完成任务不重跑。

**Files:** DeepSeek 官方搜索连接器、现有搜索 Gateway 与库存调度装配、来源能力/成本配置及必要迁移、相关 CLI/连接器/数据库测试与受控实测脚本。

**Interfaces:** Codex CLI 负责规划和选择受权搜索工具，DeepSeek 自有搜索接口提供真实网页结果，结果仍经原证据投影/归档/私有适配/恢复链。移除 Tavily 作为全局前置条件，但保留原适配器、配置、密钥引用与来源定义。

- [ ] 核实官方 Search/Fetch 与 Responses 内置 web_search 的实际差异：原生工具目前仅 deepseek-v4-pro 支持，Flash 忽略；不得将模型生成的链接视为搜索记录。
- [ ] 沿原平台固定连接复用 DeepSeek 凭证，搜索请求只发送公开条件；不得为开搜索而将私有企业上下文交给未经校验的服务端任意搜索或把凭证交给 CLI。
- [ ] 不依赖 Tavily 也能通过实际 API/worker 跑完搜索/页面证据/库存分配；未知结果不重复派发，配额和预算持久化，保留原来源和显式回退选择。
- [ ] 免费或有免费额度来源做有界只读实测，结果记录：是否配置、是否有额度、实际请求数、真实结果/失败、证据 URL、是否计费可确认。无免费额度或凭证不可用明确记未运行，不暗中替换付费源。
- [ ] 免费期有时限，不写成永久免费；保存核查日期和官方依据，最终文档与用户界面不夸大可用性。

### Task 6: 兼容、整体测试与交付

**Files:** 全部改动关联测试、`docs/operations/agent-upgrade-verification.md`、版本迁移说明；原工作区独有服务计划功能按语义合并，禁止同号覆盖。

- [ ] 核对原获客源 registry/持久配置/API 密钥引用/员工邮箱/Obsidian 和原界面路由未丢失；独有代码逐项说明保留或合并位置。
- [ ] 单元、相关 Postgres 集成、业务 evals、前端类型/构建/交互；两企业完整合成任务演练。
- [ ] 全分支独立评审，修复关键问题；结构检查及差异卫生。
- [ ] 记录实际执行命令/结果和未运行 live 项，不把模拟通过写成生产自动发信已启用。交付本地分支及可复核文档。
