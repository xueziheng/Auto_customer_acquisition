# TradeOS Agent 项目架构搭建方案

## 目标

在清空后的仓库中，按你确认的 TradeOS Agent 设计稿搭建**完整的项目架构骨架**：
- 多层 AGENTS.md 治理文档（Codex 风格，中文为主）
- 明确的项目边界、权限约束、模块依赖规则
- 核心链路深、外围浅的接口 stub（不写实现，只有签名 + 职责 docstring + `NotImplementedError`）
- 以"事件驱动 + 插件注册"为核心的解耦机制，保证以后加功能不用大改

预计产出约 **140 个文件**。

---

## 第 0 步：清空仓库

- `git rm -r docs/`（旧 Node.js MVP 文档，按你的要求全部删除）
- 重写 `.gitignore`（适配新技术栈：Python、Node、Rust、Docker）
- 注意：我在 worktree 分支上操作，删除会随本分支合并回主仓库；主仓库目录我不直接动

---

## 第 1 步：根级治理文档（5 个文件）

| 文件 | 内容 |
|---|---|
| `AGENTS.md` | **总纲**。项目是什么（一段话）、核心数据链路（Demand Signal → … → Deal Outcome）、目录地图、**全局硬性边界**（模型永远不碰密钥/金额用 Decimal 确定性计算/外部动作只走 Tool Gateway/所有关键字段带 Provenance）、模块依赖方向规则、编码约定、哪些改动必须人工审批、如何找到子目录的 AGENTS.md |
| `CLAUDE.md` | 一行指向 AGENTS.md（让 Claude Code 也读同一套规则，不维护两份） |
| `README.md` | 面向人的项目介绍、快速导航、如何启动（占位） |
| `ROADMAP.md` | 分期路线图：Phase 1 单租户核心链路（发现→验证→寻源→报价→人工接管）→ Phase 2 多租户+积分+Campaign 自动化 → Phase 3 桌面端+WhatsApp+专属部署。每期明确"做什么/不做什么" |
| `GLOSSARY.md` | 术语表：Demand Signal / Need Hypothesis / Validated Need / Trade Opportunity / Sourcing Case / Commitment / Directive / Run … 中英对照 + 一句话定义，全库唯一用词标准 |

---

## 第 2 步：架构文档 `docs/`（14 个文件）

```text
docs/
├── architecture/
│   ├── 00-overview.md          # 总体架构图、七个应用进程、模块化单体+独立 Worker
│   ├── 01-domain-model.md      # 核心对象与状态机：四层需求（Signal→Hypothesis→Validated→Opportunity）、
│   │                           #   证据等级表、Opportunity Score 组成、需求完整度 0-5 级
│   ├── 02-boundaries.md        # ★ 模块边界与依赖规则（解耦核心，见下文"解耦机制"）
│   ├── 03-permissions.md       # RBAC+ABAC 模型、角色矩阵、Context Builder 按人裁剪数据、Lead Ownership Lock
│   ├── 04-tool-gateway.md      # 工具调用检查管线（租户→权限→Playbook→积分→审批→幂等→国家政策→频率）
│   ├── 05-data-plane.md        # 四层数据平面：原始资料/结构化业务/语义检索/来源审计；Provenance 字段规范
│   ├── 06-agent-runtime.md     # Trade Manager+9 个专业能力；"模型负责什么/确定性代码负责什么"分工表
│   ├── 07-billing-credits.md   # 会员+积分模型、钱包、预留-结算-退回生命周期、失败退费规则
│   ├── 08-compliance.md        # 邮件（CAN-SPAM/国家政策包）、WhatsApp opt-in、图片权属规则、Playwright 禁区
│   ├── 09-deployment.md        # Managed Shared / Managed Dedicated 同代码两部署
│   └── 10-database.md          # 设计稿第 41 节全部表清单，按域分组 + 关键表字段说明（不写 DDL）
├── adr/
│   ├── template.md
│   ├── 0001-modular-monolith.md      # 为什么不上微服务
│   ├── 0002-postgres-not-graph.md    # 为什么初期不用图数据库
│   └── 0003-temporal-for-workflows.md
```

---

## 第 3 步：代码骨架

目录结构完全按设计稿第 40 节，放在仓库根。**每个顶层目录都有自己的 AGENTS.md**（Codex 就近生效约定），说明该目录职责、允许依赖谁、禁止依赖谁、新增子模块的步骤。

### 深度标记说明
- **（深）**：完整接口 stub —— Pydantic 模型字段、Protocol/ABC 方法签名、类型注解、中文 docstring 写清"要实现什么、边界是什么、为什么"
- **（浅）**：AGENTS.md 职责文档 + 一个顶层接口文件（只有类名和核心方法名）

### 3.1 `shared/` —— 解耦地基（深，新增于设计稿，见"建设性调整"）

```text
shared/
├── AGENTS.md
├── events/
│   ├── bus.py            # EventBus Protocol：publish/subscribe，域间通信唯一通道
│   └── catalog.py        # 全部领域事件的类型定义（NeedValidated、OpportunityQualified、
│                         #   QuoteApproved、CreditExhausted…），事件即契约
├── schemas/
│   ├── provenance.py     # Provenance 记录：source_type/source_id/extracted_by/confirmed_by
│   ├── money.py          # Money(Decimal)、FxRate、禁止 float 的说明
│   └── identifiers.py    # TenantId、RunId 等强类型 ID
└── errors.py             # 全局错误基类
```

### 3.2 `domains/` —— 16 个领域模块

每个域统一内部结构（解耦关键：**域之间禁止互相 import，只能通过 shared/events 或对方 service.py 声明的公共接口**）：

```text
domains/<name>/
├── AGENTS.md        # 职责、状态机、依赖白名单、禁止事项
├── models.py        # 实体定义 stub
├── schemas.py       # 对外 DTO
├── service.py       # 领域服务接口（Protocol）
├── repository.py    # 存储接口（Protocol，实现放 Phase 1）
├── events.py        # 本域发布/订阅哪些事件
└── errors.py
```

**深（核心链路 + 强约束域，8 个）**：
- `demand/` —— Signal/Hypothesis/ValidatedNeed/NeedCluster 四层模型、证据等级、状态转换表
- `opportunities/` —— Trade Opportunity 完整字段、Opportunity Score 计算接口、需求完整度
- `sourcing/` —— Sourcing Case 状态机、候选供应商核验规则（拒绝诱导价）、证据快照接口
- `costing/` —— Deal Cost Sheet 全部成本项枚举、Estimated/Quoted/Actual 三版本、Decimal 确定性计算接口
- `quotations/` —— 报价版本化、审批前置约束（哪些承诺永远不能自动做）
- `outreach/` —— Campaign 边界模型（发信上限、stop_on_reply、积分限额）、序列状态机、抑制名单
- `directives/` —— Boss Directive 解析结果模型、版本化、生效/回滚
- `approvals/` —— 审批包、审批状态机、哪些 change 类型必须审批的注册表

**浅（8 个）**：`organization/`、`employees/`（含 Territory Matrix）、`prospecting/`、`conversations/`（含智能收件箱回复分类枚举——这个枚举会写全）、`products/`（三视图：内部/销售/客户）、`suppliers/`、`commitments/`（承诺账本模型会写全，其余浅）、`billing/`

### 3.3 横切基础设施（4 个目录）

- `tool-gateway/`（深）—— 检查管线每个 stage 一个接口文件 + `manifest.py`（工具注册清单 schema：权限、风险级、成本类、是否需审批）。**新增工具 = 写一个 manifest + handler，不改管线**
- `credit-ledger/`（深）—— Wallet/Reservation/Settlement/退费规则接口
- `notification-gateway/`（浅）—— 统一通知事件模型 + 渠道适配器 Protocol（站内/邮件/macOS/企业微信）
- `artifact-store/`(浅) —— 原始资料存取接口、文件哈希、不可变性约定

### 3.4 `agent-runtime/`

- （深）`context-builder/` —— 按用户/角色/任务裁剪上下文的接口（allowed_tools/blocked_tools/数据范围）
- （深）`skill-router/` —— Skill manifest 加载、按 trigger 选择、一次任务只加载所需技能
- （深）`guardrails/` —— 事实/推断分离检查、无证据断言拦截、禁止承诺清单
- （浅）`trade-manager/` + 9 个专业能力目录 —— 每个一份 AGENTS.md（输入/输出/可用工具/禁用工具）+ 统一的 `CapabilityAgent` 基类接口
- （浅）`model-router/`、`evals/`

### 3.5 `skills/`

- `manifests/schema.yaml` —— 设计稿第 29 节的 Skill 标准结构，完整字段定义
- `canonical/` —— 3 个示例 manifest 写全（`demand.infer_buyer_need`、`outreach.draft_discovery_email`、`sourcing.verify_supplier_candidate`），作为后续所有技能的模板
- `upstream-nexscope/`、`references/`、`evals/` —— AGENTS.md 说明 8 个技能包的映射关系与整理规则

### 3.6 `connectors/`

- `base.py`（深）—— Connector Protocol + 注册表：**新增连接器 = 新建文件夹实现 Protocol + 注册 manifest，零核心改动**
- `gmail/`（深，作为参考实现范式）—— 完整接口：认证边界、幂等、退信/投诉回调
- 其余 13 个（浅）：每个一份 AGENTS.md（能力范围、合规约束、密钥归属）+ 空接口文件

### 3.7 `workflows/`

- `AGENTS.md` —— Temporal 分工总则：Workflow 保持确定性，LLM/DB/API 全部进 Activity
- （深）`outreach-campaign/`、`sourcing-case/` —— 状态定义、Activity 接口清单、等待/超时/人工审批节点
- （浅）其余 6 条：AGENTS.md 描述触发条件、主要状态、涉及哪些域

### 3.8 `apps/`

- `api/`（深）—— FastAPI 路由骨架：按 16 个产品页面分组的 router 文件（只有路径+请求/响应 schema 引用+权限装饰器占位）
- `agent-worker/`、`workflow-worker/`、`browser-worker/`、`notification-worker/`（浅）—— 各一份 AGENTS.md + 入口 stub
- `web/`（浅）—— Vue3 目录骨架 + AGENTS.md（16 个页面对应的 views 目录、类型定义引自 API schema）
- `desktop-tauri/`（占位）—— 仅 AGENTS.md：Phase 3 才建，写清届时的职责边界

### 3.9 `tests/` 与 `infra/`

- `tests/AGENTS.md` —— 测试分层约定 + **业务评估集清单**（设计稿第 45.9 节的 11 类样本，建目录占位）
- `infra/docker-compose.yml` —— PostgreSQL+pgvector、Redis、MinIO、Temporal 服务骨架（可直接起）
- `infra/.env.example` —— 全部环境变量清单（只有名字和注释，无值）

---

## 解耦机制（回应你"以后加功能不能大改"的要求）

写进 `docs/architecture/02-boundaries.md` 并在各 AGENTS.md 强制：

1. **依赖只有一个方向**：`apps → workflows/agent-runtime → domains → shared`。反向 import 视为违规（Phase 1 引入 import-linter 自动检查，先在文档立规矩）
2. **域间零直接依赖**：域之间只通过 `shared/events` 的事件通信；确需同步调用时，只能调对方 `service.py` 里显式导出的接口
3. **四个插件点**，加功能只加文件、不改核心：
   - 新连接器 → 实现 Connector Protocol + manifest
   - 新技能 → 新增 skill manifest + prompt
   - 新工具 → Tool Gateway manifest + handler
   - 新通知渠道 → 通知适配器 Protocol
4. **契约先行**：跨模块数据结构只定义在 `shared/schemas` 和各域 `schemas.py`，改契约必须留 ADR

---

## 建设性调整（相对你的设计稿，我做的三处小改动）

1. **新增 `shared/` 目录**：设计稿第 40 节没有公共层，16 个域的事件、Money、Provenance 若各自定义必然发散——这是解耦要求的前提
2. **`tradeos/` 不再套一层**：仓库根即项目根（仓库本身就是这个项目）
3. **desktop-tauri 只留占位**：Tauri/Rust 骨架在 Phase 3 之前是纯维护负担，AGENTS.md 写清边界即可

---

## 执行顺序

1. 删除旧文件、重写 `.gitignore`
2. 根级治理文档（AGENTS.md 是全库总纲，最先写）
3. `docs/architecture/` + ADR
4. `shared/` → `domains/`（深→浅）→ 横切设施 → `agent-runtime/` → `skills/` → `connectors/` → `workflows/` → `apps/` → `tests/` + `infra/`
5. 最终自检：每个目录都有 AGENTS.md、每个 stub 文件都有职责说明、依赖规则无遗漏
6. 分批 git commit（治理文档 / 架构文档 / 各代码层各一个 commit）
