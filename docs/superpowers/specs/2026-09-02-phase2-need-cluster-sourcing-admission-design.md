# Phase 2 NeedCluster 寻源准入设计

## 一、目标

把已达到寻源门槛的 `Validated Need` 从“事件到达即启动全部自动寻源”改为“先形成可审计的
`Sourcing Case`，再按 `Need Cluster` 规模有界准入”。排序必须可解释、可重放、可恢复，且不把
多个客户的需求事实合并成一条需求。

本设计只交付 `NeedCluster Sourcing Admission`。`Catalog Product Proposal` 是下一独立子项目：
它复用本批形成的簇事实和排序快照，但另行设计老板政策、产品负责人审核和正式产品创建边界。

## 二、已确认的业务语义

1. 一个 `Validated Need` 仍然对应一个 `Sourcing Case`。同一簇的多个 Need 不共享、覆盖或合并
   `need_snapshot`、数量、单位、国家、规格或 Provenance。
2. `Need Cluster` 只影响尚未启动的 Case 的准入顺序，不证明存在采购合单、重复采购、供应能力、
   报价或成交机会。
3. 排序使用确定性的字典序，不使用模型概率、加权分数或隐含业务阈值：
   `cluster_member_count DESC → ready_at ASC → need_id ASC`。
4. 未归簇 Need 的 `cluster_member_count` 固定解释为 `1`；这是其自身这一条已验证需求，不是模型推断。
5. 已启动或已终结的历史 Case/Run 不回收、不重排、不重放。簇扩大只影响仍等待准入的 Case。
6. 自动准入必须有当前生效、由老板确认的版本化指令。没有配置、配置关闭或读取状态未知时都不
   自动启动 Workflow。

## 三、非目标

- 不把多个 Need 改造成一个簇级 Sourcing Case。
- 不在本批实现候选、供应商或成本结果在簇成员间复用。
- 不实现“达到规模自动提议进入正式产品目录”；该能力没有老板确认的门槛，不能自造默认值。
- 不比较不同单位的潜在数量，不以 `total_potential_quantity` 排序。
- 不新增搜索、联系人、邮件、采购、报价或商业数据 Provider 调用。
- 不修改 Tool Gateway 核心检查管线，不引入 Temporal、Redis 队列或新基础设施。
- 不把部署环境中的 `batch_limit` 当成老板批准的寻源预算。

## 四、总体流程

```text
NeedValidated / NeedBecameSourcingReady
        ↓
读取 tenant-bound SourcingNeedSnapshot
        ↓
幂等创建 canonical Sourcing Case
        ↓
幂等创建 WAITING admission（不启动 Workflow）
        ↓
scheduler 单副本周期读取老板已确认的 active Directive
        ↓
读取由归簇事件持续刷新的 immutable priority snapshot
        ↓
按字典序、显式 batch_limit 执行 claim
        ↓
用稳定 idempotency key 启动原 sourcing_case V2 Workflow
        ↓
把 admission 标记 ADMITTED 并绑定 canonical Run
```

事件 handler 的职责止于开 Case 和入队。真正启动 Workflow 的唯一自动路径是准入 driver；人工准入
使用同一 application service 和同一幂等键，不能旁路持久 admission。

## 五、版本化老板配置

复用 `domains/directives` 已有“提案 → 老板确认 → immutable Directive version → rollback 产生新版本”
机制，在 `DirectiveContent` 增加可选 `sourcing_admission` 段：

```yaml
sourcing_admission:
  mode: cluster_ranked
  automatic_admission_enabled: true
  batch_limit: 3
```

约束如下：

- `mode` 本批只接受 `cluster_ranked`。
- `automatic_admission_enabled` 必须显式给出，不设默认值。
- `batch_limit` 必须是 `1..50` 的整数；即使自动准入关闭也必须明确给出，保证重新启用时仍需确认
  完整配置。
- 只有当前 active Directive 中存在该配置且自动准入开启，scheduler 才能 claim。
- 缺配置映射为 `policy_not_configured`；关闭映射为 `automatic_admission_disabled`；依赖读取失败映射为
  `policy_status_unknown`。三个状态都必须零 Workflow start。
- 指令的既有确认、权限、版本、回滚和 `DirectiveActivated` 语义不变；历史 Directive 缺少该段按
  “未配置”解释，不回填。
- `SourcingSettings` 仍只控制 V2 外部研究能力是否装配，不能授权自动准入或提供 batch limit。

## 六、领域模型与持久化

### 6.1 Sourcing Admission

在 `domains/sourcing` 增加 `SourcingAdmission`，一条 Case 最多一条当前 admission：

```text
tenant_id
admission_id
case_id
need_id
state                 waiting | starting | admitted | blocked
ready_at
current_snapshot_id
claim_token
claim_expires_at
workflow_run_id
blocked_reason
admitted_at
admitted_by
created_at
updated_at
```

状态规则：

```text
waiting ──claim──> starting ──canonical Run bound──> admitted
   ↑                   │
   └──lease expired────┘
   │
   └──invalid facts──> blocked ──facts repaired / explicit retry──> waiting
```

- `waiting` 才参与排序。
- `starting` 必须有 claim token 和 UTC lease；`admitted` 必须有 `workflow_run_id`、`admitted_at`、
  `admitted_by`。
- `blocked` 只接受固定原因，不保存底层异常文本。可修复原因包括 `priority_facts_unavailable`、
  `priority_facts_invalid`、`case_state_mismatch`；策略未配置/关闭/未知是 driver 周期级停止原因，
  不批量改写每条 admission。
- admission 的 `(tenant_id, case_id)` 与 `(tenant_id, need_id)` 均唯一；所有外键带 tenant。
- Case 仍使用既有业务状态机。admission 状态是“是否获准启动自动流程”，不新增或偷换 CaseState。

### 6.2 Immutable Priority Snapshot

每次当前排序输入变化时追加 `SourcingPrioritySnapshot`：

```text
tenant_id
snapshot_id
admission_id
case_id
need_id
cluster_id                  nullable
cluster_member_count
ready_at
ranking_version             need-cluster-admission-v1
facts_observed_at
facts_hash
created_at
```

`facts_hash` 对上述业务输入的 canonical JSON 计算 SHA-256；时间使用 UTC ISO 8601。相同 hash 不追加
重复快照。历史快照只读，当前 admission 只保存指向最新快照的外键。

`cluster_member_count` 只统计当前 tenant 内、成员链完整且确实指回该 cluster 的 Need。它必须是正整数。
`cluster_id = NULL` 时 count 必须为 1；有 cluster 时 count 必须等于经需求域服务核验的成员数。
v1 的成员数是该簇累计的已验证 Need 数，不减去已交接或已完成的历史成员；它表达已观察到的需求集中度，
不等于当前可合单数量。若未来要改成“当前活跃成员数”，必须发布新的 ranking version，不能改写 v1。

### 6.3 迁移

新增两张表和所需复合唯一约束、CHECK、tenant-bound FK 及索引。队列索引服务于
`tenant_id + state + claim_expires_at`；排序不依赖跨域 SQL join，而由应用层读取需求域安全事实后写入
snapshot，再在 sourcing repository 内排序。

迁移必须在隔离 PostgreSQL 执行 upgrade → downgrade → upgrade。downgrade 只删除本批空结构；若表中
已有业务记录则拒绝降级，避免丢失准入证据。

## 七、公共契约与依赖边界

### 7.1 Demand 公共读取

在 `domains/demand/service.py` 增加 tenant-bound 只读能力，返回公开 DTO
`NeedClusterPriorityFacts`：

```text
need_id
cluster_id | None
cluster_member_count
facts_observed_at
```

需求域负责核验 Need 存在、属于 tenant、已达到寻源门槛、cluster 成员链完整。它不生成排序键，也不
导入 sourcing。

需求每次首次归入新簇或既有簇时发布 `NeedClusterMembershipChanged`，包含 tenant、cluster、发生变化的
need、变更后的成员数和发生时间。既有 `NeedClusterFormed` 仍只表达“首次形成多成员簇”，不改变语义。
增加新事件不改写旧事件字段；具体兼容决定记录 ADR。

### 7.2 Sourcing 公共服务

`domains/sourcing/service.py` 增加入队、读取队列、claim、完成准入、释放过期 claim 和人工准入所需的
显式 DTO/方法。领域服务只维护自身不变量，不读取 Directive、Demand 或 Workflow。

### 7.3 Application 与 scheduler

跨域编排放在 `apps/scheduler_worker`：

- `SourcingTriggerHandler`：读 Need snapshot、开 Case、幂等 enqueue；不再直接 `engine.start`。
- `SourcingTriggerHandler` 入队前通过 demand 公共读取取得当前 priority facts；入队与 snapshot 写入均以
  Case/Need 业务键幂等。
- `SourcingClusterMembershipHandler`：消费 `NeedClusterMembershipChanged`，重新读取并核验该簇事实，给
  仍在 waiting/blocked 且属于该簇的 admission 追加新 snapshot；已 starting/admitted 项不变。事件早于
  admission 时允许 no-op，因为后续入队会读取当前事实。
- `SourcingAdmissionDriver`：读 active Directive，按 repository 中的当前 snapshot 全局排序并 claim，
  然后调用 `engine.start`。它不从一个未排序的有限样本中再做内存排序。
- driver 使用固定 idempotency key `sourcing-case:v2:{tenant_id}:{need_id}`；这与现有路径一致。
- `engine.start` 成功后返回 canonical Run ID，再由 sourcing service 持久绑定。若进程在 start 后、绑定前
  崩溃，lease 到期后以同一 key 重试，engine 必须返回同一 Run，随后补齐 admission。
- priority facts 暂时不可读时保留 `waiting` 并记录本轮固定停止原因，不把依赖故障固化成坏数据；成员链
  或类型损坏等永久无效事实才进入 `blocked`。
- `engine.start` 的已知 transient failure 释放 claim 回 `waiting`；结果不确定时保留 `starting` 到 lease
  到期，再以同一内部幂等键恢复，禁止立即重试或更换键。永久校验错误进入 `blocked`。
- scheduler 已有单副本 advisory lock 仍是第一道门；repository claim 继续使用
  `FOR UPDATE SKIP LOCKED` 和条件更新，保证测试/人工并发也不能重复准入。

## 八、排序、公平性与解释

排序键固定为：

```python
(-cluster_member_count, ready_at, str(need_id))
```

不用 `total_potential_quantity`，因为数量可能缺单位或单位不同；不用国家数、重复采购或利润，因为它们
不是当前已校准的准入门槛；不用模型评分。

API 对每项返回中文解释，例如“该需求簇当前有 8 条已验证需求；在同规模需求中等待时间最早”。解释由
确定性模板根据 snapshot 生成，不由模型自由生成。

该算法没有保证小簇在持续涌入更大簇时的严格有限等待，因此同时保留 `ready_at` 和等待时长指标，并在
验收报告显式记录这一风险。本批不增加未经老板确认的 aging boost。若真实运行出现饥饿，再以观测数据
设计新 ranking version，而不是静默修改 v1。

## 九、API 与界面

### 9.1 API

- 指令提案/详情/版本接口投影 `sourcing_admission` 配置及预计行为变化。
- 寻源列表增加独立的等待准入读取，不把它伪装成已启动 Case：返回 Case/Need 标识、cluster、成员数、
  ready time、等待时长、ranking version、解释、admission state、固定阻断原因和是否允许当前用户人工准入。
- boss 或获授权 sourcing 员工可人工准入单条 Case；请求必须带 request id，并复用同一 claim/start/bind
  application path。
- 错 tenant 在任何 repository 或 Workflow IO 前返回 403；不存在返回 404；策略停止状态使用结构化
  reason，不返回底层异常文本。

### 9.2 Web

- 指挥中心在确认前显示自动准入开关、每轮上限和预计影响；确认后显示生效 Directive version。
- 寻源中心分开显示“等待准入”和“处理中”。等待卡片展示簇成员数、等待时长、排序解释与停止原因。
- Case 详情展示实际使用的 immutable priority snapshot 和 admission actor/time；不显示 claim token、lease、
  Workflow context 或自由异常。
- 前端类型继续从 OpenAPI 生成，不手写重复 schema。

## 十、兼容与发布

- 已存在且已有 Workflow Run 的 Case 视为历史已准入，不补建 snapshot，不改变列表与状态。
- 已存在但没有 Run 的 OPENED Case 不自动猜测 ready time；由一次显式管理员迁移/修复命令按可核实的
  Case `opened_at` 建 admission，并输出逐条结果。普通 migration 不执行业务回填。
- 旧 Directive 和旧 Sourcing 配置继续可读；缺 admission 段表示没有自动准入授权。
- 新 trigger 行为和 driver 随新 composition 同时发布，禁止只发布“停止直接启动”而未装配 driver/API 的
  半成品。
- 为 `NeedClusterMembershipChanged`、Directive 新配置段、旧 trigger 从“直接启动”改为“持久入队”及
  V1/V2 历史兼容留下 ADR；不修改既有事件的字段或含义。
- 启用前由老板确认新 Directive；未确认时新 Case 正常入队但不会产生搜索、页面、模型或成本调用。
- 本批受控验收通过不等于真实 Sourcing V2 外部服务已启用，也不改变 Phase 1 真实运营状态。

## 十一、测试与验收

每个切片先写失败测试再实现。必须覆盖：

1. 归簇 8 条、归簇 3 条、未归簇 1 条按 `8 → 3 → 1` 排；同规模按 `ready_at`，再按 `need_id`。
2. 簇新增成员后，等待 admission 追加新 snapshot 并重排；已 admitted 项不改快照、不重启。
   归簇事件早于 admission 的乱序投递也必须得到相同最终 snapshot。
3. 不同国家、不同线路但同一 cluster 的 Need 保持各自 Case/Need snapshot 和 Provenance。
4. 数量不同单位或单位缺失完全不影响 v1 排序，也不被相加比较。
5. 未配置、关闭、指令读取失败分别返回三种停止原因，Workflow start 调用为零。
6. 老板确认新 Directive 后按精确 batch limit 启动；回滚产生新版本并在下一周期生效。
7. 两个并发 claim 最多一个成功；scheduler 重启、lease 过期、start 后绑定前崩溃都只产生一个 canonical
   Case 和一个 canonical Run。
8. transient、permanent、unknown 结果遵守各自恢复语义；日志、事件、API 不出现 DSN、token、claim token
   或底层异常原文。
9. 错 tenant 的列表、claim、人工准入和重试均在业务 IO 前拒绝；owner tenant 的 Case、admission、Run 与
   snapshot 不变。
10. 历史已启动 V1/V2 Case 不回填、不重排；旧 Directive 缺字段仍可读取。
11. Browser 真实数据态显示等待队列、大簇优先、确认配置后有界启动、人工准入和 immutable snapshot；
    桌面/移动视图 console error 为零。
12. migration upgrade → downgrade → upgrade、OpenAPI 无漂移、结构检查、相关后端测试、全量 pytest、
    前端测试/typecheck/lint/build 全部通过。

## 十二、后续子项目边界

`Catalog Product Proposal` 后续只能在用户再次确认独立规格后实施。它需要显式、版本化的最小客户数、
重复采购、跨国家及可选数量/统一单位政策；达到门槛也只创建人工审核提议，不直接创建正式产品，且
`source_only`/`INDICATIVE` 证据不能升级为已确认供应或 `QUOTED` 价格。
