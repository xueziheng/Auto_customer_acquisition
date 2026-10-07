# Phase 2 Catalog Product Proposal 设计

日期：2026-09-04

状态：已完成会话设计确认和规格自审，待用户书面复核

范围：受控工程闭环；不启用真实供应商、客户触达或生产策略

## 一、目标

当多个**不同客户**的已验证需求形成同一 `Need Cluster`，且满足一份已经人工审批的版本化策略时，
系统自动创建可审计的 `Catalog Product Proposal`。该提案必须再次经过人工审批，批准后只进入候选产品
培养队列，不创建或升级正式产品。

本子项目验证的是：系统能否把重复出现的真实需求转成一项可追溯、可拒绝、可过期、可重放的产品培养
建议。它不能把产品目录变成需求探索的起点，也不能把公开网页、模型推断或 `indicative` 价格升级为
客户需求、供应能力或 `quoted` 价格。

## 二、已确认决定

1. 采用独立产品域方案：策略、评估、提案和培养队列归 `domains/products` 所有。
2. 达到门槛后自动生成待审核提案；系统不自动批准。
3. 没有活动策略时自动评估关闭，生产环境不预置活动策略。
4. 策略是不可变版本，必须先走人工审批才能激活。
5. 提案批准后只创建候选产品培养队列项，不创建正式 `Product`。
6. 受控验收策略为三个不同客户、同一品类；复购、跨国家、数量和单位只展示，不作硬门槛。
7. 上述三个客户只是测试策略，不是生产阈值或市场结论。

## 三、前提纠正

当前模型已有 `NeedCluster.recurring_demand` 和 `NeedCluster.total_potential_quantity`，但二者不能直接作为
目录决策事实：

- `recurring_demand` 当前没有客户确认的写入链路，实际保持 `None`；
- `total_potential_quantity` 累加整数时没有验证单位，可能错误合并“辆”“件”“箱”；
- `NeedCluster.countries` 是展示聚合，历史账户可能没有国家 Provenance；
- `member_count` 是 Need 数量，不是去重客户数量；同一客户的多条 Need 不能被算成多个客户。

因此目录评估必须重新读取成员 Need 与账户事实，按 Provenance 生成新的不可变快照。旧聚合字段只保留
原有解释，不作为本子项目的决策输入。

## 四、范围边界

### 4.1 本子项目交付

- 版本化 Catalog Proposal Policy 及独立审批、激活、替换和陈旧保护；
- 带 Provenance 摘要的 `NeedClusterCatalogFacts`；
- 已验证需求的复购事实入口；
- 确定性规则评估及通过／未通过／未知解释；
- 不可变评估快照和 Catalog Product Proposal；
- 提案人工审批与唯一培养队列项；
- 内部 API、供应能力中心 UI、通知和受控验收；
- 事件重放、并发、进程重启、跨租户和陈旧审批保护。

### 4.2 明确不交付

- 自动创建或升级正式产品；
- 自动联系供应商、询价、承诺 MOQ、交期、认证或库存；
- 把 `source_only`、`partial` 或 `indicative` 证据升级为已确认供应或 `quoted` 价格；
- 单位换算、需求合单或订单合并；
- 模型评分、概率、自动阈值学习或 70/30 分配；
- 联系人多源瀑布、接管队列背压、商业数据源；
- 真实肯尼亚三轮车业务验收；
- 根路由和 Git AppleDouble 清理。这两项作为独立工程卫生任务处理。

## 五、架构与依赖

```text
NeedClusterMembershipChanged / NeedCatalogFactsChanged / AccountCountryFactsChanged
                              ↓
                  apps/scheduler_worker（宿主）
                              ↓
             workflows/catalog_product_proposal（跨域编排）
                 ↙              ↓               ↘
          DemandService     ProductService     ApprovalService
                 ↓              ↓               ↓
          需求事实快照       策略/评估/提案      审批包/决定
                                  ↓
                       CatalogCultivationCase
```

依赖仍为单向：

```text
apps → workflows → domains → shared
```

`domains/demand` 与 `domains/products` 不互相导入。workflow 只调用各域 `service.py` 的公共接口，
不能导入域内 `models.py` 或 `repository.py`。scheduler 只负责装配、调度和重试，不持有业务门槛。

### 5.1 Demand 域

Demand 负责读取并核验簇、成员 Need、账户绑定与字段 Provenance，输出产品无关的目录评估事实。
它不读取 Catalog Policy，也不判断是否该创建提案。

新增公共 DTO `NeedClusterCatalogFacts`。workflow 将其显式映射成 Products 公共命令，Products 不导入
Demand DTO。

账户国家通过 Demand 注入的窄端口读取。应用层适配器调用 `ProspectingService.get_account`，只接受
`ProspectAccountView.field_provenance["country"]` 完整且非 Agent 推断的国家事实。缺 Provenance 的
国家保持未知。

### 5.2 Products 域

Products 持有以下聚合：

- `CatalogProposalPolicyVersion`
- `CatalogProposalEvaluation`
- `CatalogProductProposal`
- `CatalogCultivationCase`

Products 使用纯确定性规则评估，不调用模型、外部 SDK 或其他业务域。

### 5.3 Approvals 域

新增两种 `ApprovalType`：

- `catalog_proposal_policy_change`
- `catalog_product_cultivation`

审批域继续只管理审批包及决定，不直接修改 Products。workflow 在目标业务效果持久化后才调用
`mark_applied`；不得先标记审批已应用。

### 5.4 Workflow 与宿主

`workflows/catalog_product_proposal/` 负责：

- 提交策略审批并应用批准版本；
- 读取需求事实、调用 Products 评估、提交提案审批；
- 消费审批决定并应用培养队列效果；
- 恢复“业务行已写入但响应丢失”等不确定结果。

`apps/scheduler_worker` 订阅相关事件并运行有界 reconciliation；`apps/api` 暴露内部命令与查询；
`apps/web` 只呈现和发起已授权动作。

## 六、策略模型

### 6.1 策略内容

`CatalogProposalPolicyContent` 使用严格、不可变、禁止额外字段的 DTO：

```text
minimum_distinct_accounts: int                必填，至少 2
minimum_recurring_accounts: int | None        None 表示不作硬门槛
minimum_distinct_countries: int | None        None 表示不作硬门槛
minimum_quantity_unit_accounts: int | None    None 表示不作硬门槛
require_unified_unit: bool                    仅数量单位门槛启用时可为 true
```

约束：

- 任一非空最小值不得大于 `minimum_distinct_accounts`，国家数除外；
- `minimum_distinct_countries` 非空时至少为 2；
- `require_unified_unit=true` 时 `minimum_quantity_unit_accounts` 必须非空；
- 所有数值拒绝 bool、零、负数和越界值；
- 内容以规范 JSON 计算 SHA-256 `content_hash`。

### 6.2 测试策略

受控验收显式创建并审批以下版本：

```text
minimum_distinct_accounts = 3
minimum_recurring_accounts = None
minimum_distinct_countries = None
minimum_quantity_unit_accounts = None
require_unified_unit = false
```

测试完成不会把该版本注册为生产默认配置。

### 6.3 生命周期

```text
pending_approval ──→ active ──→ superseded
       ├──────────→ rejected
       ├──────────→ expired
       └──────────→ stale
```

- 每个租户最多一个 `active` 版本；没有活动版本等于功能关闭；
- 每版保存 `base_active_version_id`，应用时必须仍与当前活动版本一致；
- 新版成功激活后，旧版在同一 Products 事务中转为 `superseded`；
- 拒绝、过期、陈旧均为终态，不可重新激活；
- Products 使用 `approval_id` 作为应用幂等键；业务提交后 Approvals 才记 `applied`。

策略审批有效期为七天。提议人与决定人不得相同；没有合格审批人时保持待审批，不提供单人绕过。

## 七、需求事实与 Provenance

### 7.1 复购事实

`ValidatedNeed` 增加：

```text
recurring_requirement: FactualField[bool] | None
```

- `True`：客户或员工确认存在重复采购；
- `False`：客户或员工确认是一次性采购；
- `None`：未知；
- 来源必须符合已验证需求的客户回复、表单、上传或员工确认规则；
- Agent 推断、网页描述和行业常识不得写入；
- 该字段不改变既有完整度 0–5、寻源门槛、报价准备度或历史 Need 状态。

同一客户在同一簇内有多条 Need 时，复购按客户归并：至少一条合格 `True` 即表示该客户有复购证据；
没有 `True` 且至少一条合格 `False` 时记为一次性；全部缺失时为未知。True 与 False 同时存在时还要显示
“混合事实”提示，但有明确 True 的客户仍可计入复购客户数，不能把 Need 条数当成复购客户数。

数量、单位或复购事实变化后发布 metadata-only 的 `NeedCatalogFactsChanged`，只含 tenant、need、cluster
和变化类别，不含字段值或客户原话。

### 7.2 账户国家

仅当 Prospect Account 的国家带合格 `field_provenance["country"]` 时计入已知国家。查询国家、TLD、
配送范围或模型推断不能代替企业所在地。

账户国家的持久更正发布 metadata-only 的 `AccountCountryFactsChanged`。若当前没有国家更正入口，
实现只需为既有创建/更新路径预留并注册事件，不得制造不可达的生产按钮。

### 7.3 数量与单位

- 数量来自 `ValidatedNeed.quantity: FactualField[int]`；
- 单位必须存在当前 `unit_confirmation_id`，并与当前 `quantity_fact_hash` 精确绑定；
- 失效或历史确认不计入覆盖；
- 不同单位不合计；第一版不做换算；
- 数量单位覆盖按去重客户统计；同一客户有多条簇内 Need 时，该客户的覆盖状态固定为未知，除非后续有
  独立的需求去重事实；
- 只有每个客户恰好对应一条簇内 Need、全部数量与单位绑定有效且规范单位完全相同时，才输出
  `safe_total_quantity`；否则为 `None`。这避免把同一客户的重复记录误算成新增需求。

### 7.4 `NeedClusterCatalogFacts`

快照至少包含：

```text
tenant_id
cluster_id
cluster_category
member_need_ids（稳定排序）
distinct_account_ids（稳定排序）
member_count
distinct_account_count
known_country_codes（稳定排序）
unknown_country_account_count
recurring_true_account_count
recurring_false_account_count
recurring_unknown_account_count
quantity_unit_covered_account_count
unified_unit: str | None
safe_total_quantity: int | None
evidence_summaries
facts_observed_at
facts_hash
```

`evidence_summaries` 只包含来源类型、来源 ID、提取者、确认人、确认时间和内容 hash；不复制客户原话。
所有成员必须属于同一 tenant，Need→Cluster 与 Cluster→Need 必须双向一致，每个成员品类必须与簇品类
一致，否则读取失败关闭。

`facts_hash` 是上述决策相关字段按稳定排序和规范 JSON 计算的 SHA-256。读取时钟不得进入 hash；
`facts_observed_at` 取参与快照的最新持久事实时间，不使用当前时间制造新版本。

## 八、评估模型

### 8.1 唯一键

```text
tenant_id + cluster_id + policy_version_id + facts_hash
```

同一事实与同一策略只产生一个 `CatalogProposalEvaluation`。数据库唯一约束是并发收敛的最终防线。

### 8.2 规则结果

每条规则返回：

```text
passed | failed | unknown | not_required
```

Products 不输出概率、综合置信度或模型解释。评估记录保存实际值、要求值和固定中文解释代码。

规则顺序：

1. 双向成员链与同品类校验；
2. 去重客户数；
3. 复购客户数；
4. 已知国家数；
5. 有效数量单位覆盖；
6. 统一单位要求。

任何被策略设为硬门槛的规则若为 `failed` 或 `unknown`，整体不通过。非必需规则的未知事实只展示，
不影响测试策略结果。

未通过的评估也必须持久化，使内部 UI 能回答“为什么没有生成提案”。

## 九、提案与培养队列

### 9.1 提案状态

```text
awaiting_approval_submission
              ↓
       pending_review ──→ cultivation_queued
            ├──────────→ rejected
            ├──────────→ expired
            └──────────→ stale
```

评估通过时在同一 Products 事务创建唯一提案。workflow 随后以确定性 `change_set_ref` 提交审批：

```text
catalog-cultivation:{proposal_id}:{policy_version_id}:{facts_hash}
```

响应丢失或 scheduler 重启时按相同引用恢复既有审批包。绑定审批 ID 后进入 `pending_review`。

提案审批有效期为三天。审批包必须包含：

- 采用的策略版本及 hash；
- 需求簇 facts hash；
- 各规则通过／未通过／未知结果；
- Evidence 引用；
- 批准后只进入培养队列的说明；
- 拒绝后本事实版本不再重复提议的说明；
- 不代表正式产品、供应确认或客户报价的醒目警告。

### 9.2 陈旧保护

应用批准结果前必须重新读取：

- 当前活动策略版本；
- 当前完整需求事实及 `facts_hash`；
- 审批包不可变 subject 与 request hash。

任一不一致都将提案转为 `stale`，不创建培养队列项。即使新事实仍满足门槛，也要以新快照生成新提案，
因为审批人批准的是旧证据集合。

### 9.3 培养队列

`CatalogCultivationCase` 是持久交接记录，初始状态固定为 `queued`。它至少绑定：

```text
tenant_id
cultivation_case_id
proposal_id
approval_id
cluster_id
policy_version_id
facts_hash
evidence_refs
queued_at
```

每个 `proposal_id` 最多一个培养 Case。创建 Case 不创建 Product，不修改既有 `source_only` / `partial`
产品，不开始寻源或询价。本子项目不定义后续培养状态机；后续子项目消费该队列时再独立设计。

## 十、事件、重放与恢复

新增 metadata-only 事件：

- `NeedCatalogFactsChanged`
- `AccountCountryFactsChanged`
- `CatalogProposalPolicyActivated`
- `CatalogProductProposalCreated`
- `CatalogCultivationQueued`

事件只携带消费者定位所需的 ID、版本和 hash，不携带客户原话、价格、数量明细或 Provenance 正文。

触发来源：

- 需求首次归簇或成员变化；
- 数量、单位、复购事实变化；
- 账户国家事实变化；
- 新策略激活后的全量有界回填。

scheduler 还运行带稳定游标的有界 reconciliation，覆盖事件投递中断和历史数据。reconciliation 读取
当前活动策略并重新构造事实；唯一键保证不会重复生成评估、提案或培养 Case。

恢复原则：

- 明确未提交：可以用同一幂等键重试；
- 提交结果未知：先读取 canonical 业务行或审批包，不盲目重做；
- 业务效果已提交：恢复后只补记审批 `applied`；
- 明确业务应用失败：标记 `apply_failed` 并通知人工，不无限重试。

## 十一、权限与安全

- 策略提交：内部 product / sourcing 负责人；策略决定：boss；
- 自动 Catalog Proposal 以系统 Run 为提议方；培养审批由具备权限且非 owner 的 boss 决定；
- 提议人、owner 与审批人不得相同；无合格审批人时保持 pending；
- 策略、评估、提案、培养 Case 的所有实体与仓储方法必须带 `tenant_id`；
- 跨租户与不存在对外统一为 Not Found，不泄漏对象存在性；
- Evidence 原件仍经过原域权限和 artifact 读取授权，不因审批包引用而获得读取权；
- API 不接受客户端提交 `tenant_id`、Provenance、facts hash、审批人或 actor 身份；这些由可信上下文绑定；
- 日志、事件、通知和错误不得包含客户原话、凭证、DSN、数据库异常或完整 Provenance 内容；
- 不新增外部工具，不经过 Tool Gateway 的外部动作保持为零。

## 十二、API 与内部 UI

内部 API 提供：

- 创建策略候选版本并提交审批；
- 读取活动策略和版本历史；
- 列出和查看评估；
- 列出和查看 Catalog Product Proposal；
- 列出和查看培养队列；
- 读取关联审批状态。

批准／拒绝继续复用 Approvals 的决定入口，不新增绕过中央审批的产品专用决定端点。

供应能力中心增加三个内部区域：

1. **策略版本**：活动规则、版本历史、审批状态和“未配置即关闭”提示；
2. **目录提案**：按 pending、rejected、expired、stale、cultivation_queued 分类；
3. **培养队列**：显示需求簇、去重客户数、证据覆盖、未知项和关联审批。

评估与提案详情逐项显示 `通过 / 未通过 / 未知 / 不要求`，不得显示概率。页面固定展示：

> 这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。

Evidence 链接只有在当前用户通过原件读取授权后才可打开。客户可见 API、销售视图和客户视图不增加
Catalog Proposal 字段。

## 十三、存储与迁移

新增迁移从当前链头继续，计划编号为 `0057`；实施前必须再次核对迁移 head，若已变化则顺延，不制造
双 head。

新增表：

- `catalog_proposal_policy_versions`
- `catalog_proposal_evaluations`
- `catalog_product_proposals`
- `catalog_cultivation_cases`

所有表含 `tenant_id`、UTC 时间、状态检查约束和必要唯一约束。关键唯一约束：

- 一个租户最多一个 active policy；
- evaluation：tenant + cluster + policy version + facts hash；
- proposal：tenant + evaluation；
- cultivation case：tenant + proposal；
- approval binding / application receipt：tenant + approval。

`validated_needs` 增加复购事实及完整 Provenance 持久字段，形状遵循现有 FactualField 存储约定。
迁移不从旧 `recurring_demand`、备注或模型结果回填；历史记录保持未知。

降级前若新表或复购事实已有业务数据，必须拒绝破坏性降级并要求显式归档授权；不能静默丢数据。

## 十四、失败语义

| 情况 | 结果 |
|---|---|
| 无活动策略 | 正常 no-op，记录稳定原因 |
| 成员链、租户或品类不一致 | 事实读取失败关闭，不创建 evaluation |
| 决策字段 Provenance 损坏 | blocked evaluation，不降级猜测 |
| 非硬门槛字段缺失 | 规则为 unknown，按策略决定是否阻断 |
| 审批提交暂时失败 | 保留 awaiting 状态，同引用恢复 |
| 审批过期或拒绝 | 对应终态，不自动重提同一快照 |
| 策略或事实陈旧 | stale，不应用旧批准 |
| 培养 Case 响应丢失 | 读取唯一业务行，复用既有 Case |
| 明确应用失败 | approval apply_failed，通知人工 |
| 跨租户访问 | 业务 IO 前拒绝，对外 Not Found |

错误响应只返回固定错误码和安全中文说明；底层异常原文只用于受控本地调试且不得进入持久审计字段。

## 十五、测试与受控验收

### 15.1 领域与存储测试

- 策略 DTO 严格校验、规范 hash、状态转换和 stale baseline；
- 三个不同 account、同品类通过；同一 account 多 Need 不虚增；
- 两个 account、品类不一致和成员链损坏不通过；
- recurrence 的 True / False / None 及 Provenance 门禁；
- 国家缺 Provenance 不计入已知国家；
- 数量单位当前绑定、stale 绑定、混合单位和安全合计；
- 所有规则的 passed / failed / unknown / not_required；
- tenant 过滤、唯一约束、并发和事务回滚；
- migration upgrade → downgrade guard → upgrade。

### 15.2 Workflow 与审批测试

- 没有活动策略时零提案；
- 策略批准后有界回填；
- 同事件重放、并发消费和 scheduler 重启只生成一个 evaluation、proposal 和 approval；
- 审批提交响应未知时恢复既有包；
- 自批、过期、跨租户和陈旧 subject 全部拒绝；
- 产品效果先提交、审批 applied 后补记；恢复不重复业务效果；
- 批准只创建 queued cultivation case，零 Product 状态变化、零寻源、零报价、零发送。

### 15.3 API 与 Browser

- OpenAPI 生成无漂移，客户端不得伪造 actor、tenant、hash 或 Provenance；
- 非授权角色不可创建策略或读取内部提案；
- 策略版本、提案状态、规则结果、Evidence 链和固定风险提示正确；
- 桌面与移动布局无错误遮罩，console error 为零；
- 至少完成一次“提交策略 → 审批 → 三客户达标 → 提案审批 → 培养入队”的受控浏览器链路；
- fixture 使用合成账户和需求，不调用真实搜索、模型、供应商、邮箱或客户渠道。

### 15.4 全库门禁

实施完成后至少运行：

```bash
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
python -m ruff check .
python -m mypy domains shared tool_gateway apps workflows notification_gateway infra
pytest -q -rs
cd apps/web && npm run gen:api
cd apps/web && git diff --exit-code -- src/api/api.d.ts
cd apps/web && npm test
cd apps/web && npm run typecheck
cd apps/web && npm run lint
cd apps/web && npm run build
```

不得以相关测试通过代替全量回归，也不得以受控 fixture 冒充真实生产验收。

## 十六、完成标准

本子项目只有同时满足以下条件才可标记完成：

1. 四个 Products 聚合和复购事实均持久化、租户隔离且可恢复；
2. 策略和培养两次人工审批都不能绕过、自批或补批；
3. 同一事实、策略和提案在并发、重放及重启下均收敛为唯一对象；
4. 陈旧事实或策略批准绝不产生培养 Case；
5. 受控三客户链路在真实 PostgreSQL、API、scheduler 和 Browser 上通过；
6. 批准后的唯一业务效果是 queued cultivation case；
7. 正式 Product、供应商联系、询价、报价和发送均为零；
8. 全量测试、前端构建和结构检查通过；
9. 验收记录明确区分受控事实、未运行项和生产剩余风险；
10. `ROADMAP.md` 只标记 Catalog Product Proposal 子项目完成，不勾完整个 Phase 2。

## 十七、后续边界

`CatalogCultivationCase` 的后续处理、真实供应商报价、联系人多源瀑布、70/30 自适应分配、接管队列
背压和商业数据源仍需分别设计与验收。该队列只是明确的挂载点，不能被解释为这些能力已经存在。
