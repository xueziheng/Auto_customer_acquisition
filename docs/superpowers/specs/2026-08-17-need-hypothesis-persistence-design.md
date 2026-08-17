# Need Hypothesis Persistence Design（NeedHypothesis + ValidatedNeed 生命周期最小切片）

> 输入：只读发现报告（2026-08-17）+ 用户「APPROVED APPROACH A」批示（仅设计 spec 任务）。
> 前置：demand 信号持久化切片已交付（HEAD `7d68199c`，0021 迁移 + capture/discard +
> 完整度推导 794764d + 下一问 5d62767 + classify/apply_actions 0839d99）。
> 本片范围：**demand 域假设侧持久化/生命周期基础**——迁移 0022（**恰 3 表**）、租户安全
> 仓储/UoW、`domains/demand/service.py` 已声明的生命周期服务方法（**6 个**）、字段级
> Provenance/历史、确定性置信度/完整度（无存储概率）、metadata-only 已注册事件（**3 个**）。

## 1. 决策记录（发现阶段每个歧义 → 本片定稿）

| # | 歧义（发现来源） | 本片决定（权威） |
|---|---|---|
| D1 | `NeedHypothesis.evidence()` 的仓储访问设计未知（completeness plan 156 行标注「阻塞假设侧切片」） | **纯域内、零仓储 IO**：`evidence()` 只读 `reasoning.based_on`（EvidenceItem 快照列表），按 `(source_type, source_id)` 去重（组内保留等级最高一条，并列取先出现顺序第一条）后返回。信号实体**不反查**——证据在假设创建时快照进 `reasoning.based_on` 并持久化（JSONB），此后不依赖信号行存活 |
| D2 | 活跃假设去重的状态集、key、并发语义（repository.py `find_active_by_account_and_category` docstring「避免重复创建…被联系多次」） | key = `(tenant_id, account_id, category)`；活跃 = status ∈ {INFERRED, CONTACTING}（VALIDATED 已晋升不重复建、REJECTED 允许重新假设）；并发 = 部分唯一索引 `uq_need_hypotheses_active_account_category`（`WHERE status IN ('inferred','contacting')`）+ **`pg_insert(...).on_conflict_do_nothing(index_elements=["tenant_id", "account_id", "category"], index_where=<与迁移部分索引谓词逐字一致的表达式>)`**——**禁用 `constraint=`**（`constraint=` 只能按完整约束名匹配，无法表达部分索引目标；`index_elements`+`index_where` 才能精确命中部分唯一索引）→ rowcount=0 → 同事务 `find_active_by_account_and_category` FOR UPDATE 重读胜者 → **并入**（见 D5）。谓词字面量 `"status IN ('inferred','contacting')"` 由 parity 测试同时断言迁移 `postgresql_where` 与仓储 `index_where`（§9.1） |
| D3 | 信号关联不可变性与 FK/on-delete 策略 | `need_hypotheses.signal_ids` 为 JSONB 字符串数组，**无 FK**（JSONB 无法直接外键；`demand_signals` 行永不删除——discard 只流转状态）；语义不可变 = 只允许追加去重后的新项、永不删除既有项；**被丢弃（DISCARDED）的信号不得支撑假设**——create_hypothesis 入参含 DISCARDED 信号 → `ValidationError("需求信号已丢弃")`（finding 5；mutation M7） |
| D4 | hypothesis→validated_need 的 FK/on-delete（簇整体非本片范围） | 建立 FK（`need_hypotheses.validated_need_id → validated_needs.need_id`），**无 CASCADE**（默认 NO ACTION；两表均无删除路径——状态机终态保留行）。`validated_need.cluster_id` 保持模型层 None，**本片不持久化该列**（见 §2 非目标） |
| D5 | 「把新信号并入既有假设的证据」的确切语义（service.py create_hypothesis docstring） | 并入 = 以 `(source_type, source_id)` 为键做**幂等并集**：新证据与既有 `based_on` 去重合并 → 替换 `reasoning` 为新 `InferredField`（value 同、based_on=并集、inferred_by 同、inferred_at=now）→ `signal_ids` 追加去重。**不发布新 `NeedHypothesisCreated`**（并入是同一假设的更新，事件语义是「生成了需求假设」） |
| D6 | promote 门槛与错误类型（service.py docstring「不通过就抛 InsufficientEvidenceError」） | 用 **既有** `domains.demand.errors.InsufficientEvidenceError`（祖先链 `PolicyViolation → TradeOSError`，shared/errors.py:66）——**不新增任何 shared 错误类**（finding 2）；门槛判定委托 `NeedHypothesis.can_promote_to_validated()`（唯一通过条件：存在证据等级 ≥ CUSTOMER_INTEREST_REPLY 且 source_type ∈ {CONVERSATION, UPLOAD, EMPLOYEE_INPUT}）；错误 docstring 要求消息说清当前最高证据等级、要求、还需什么证据——固定中文摘要 + 等级值（不回显正文） |
| D7 | promote/reject 并发转换（两个事务同时 promote） | 转换路径统一 `get_for_update`（`SELECT ... FOR UPDATE`，新增协议方法，同 discard 先例）：先到者置 VALIDATED + 建 need + 发事件；后到者锁后重读 snapshot VALIDATED → **幂等返回既有 validated_need_id**（不建第二 need、不发第二事件） |
| D8 | reject 幂等/冲突 | 已 REJECTED 且同 reason → 幂等 no-op；已 REJECTED 且不同 reason → `InvalidStateTransition("拒绝原因冲突，拒绝覆盖")`（同 discard first-reason 先例）；已 VALIDATED → **`HypothesisAlreadyResolvedError`（既有域错误）**；reason 词表**本片不校验**（不 import opportunities 域、不复制枚举防漂移；只做 str/strip/≤200 校验，诚实标注） |
| D9 | update_need_fields 状态允许集、字段历史形状、自动状态推进 | 允许状态 = {VALIDATED, SOURCING_READY, HANDED_TO_SOURCING}（终态 FULFILLED/WITHDRAWN/LOST 冻结 → `InvalidStateTransition("需求已终结，字段不可变更")`）；字段白名单 = 10 个业务字段（application/material/size_spec/quantity/packaging/destination/required_by/target_price/current_supply_issue/certification_required），未知字段 → `ValidationError("未知需求字段")`；历史 = 独立 append-only 表（形状见 §7，含旧值 provenance 丢失的诚实标注，finding 8）；更新后重算完整度，**仅当当前状态 == VALIDATED 且跨过 3 级门槛（<3 → ≥3）才自动置 SOURCING_READY；SOURCING_READY/HANDED_TO_SOURCING 保持不变（绝不降级、不重复推进）**（finding 7）；不发新事件（catalog 无该事件 schema，诚实标注最小语义） |
| D10 | sourcing-ready 门禁 | `is_sourcing_ready()` = completeness ≥ 3（已交付）；`mark_sourcing_ready`：completeness < 3 → **`SourcingThresholdNotMetError`（既有域错误，消息含 `missing_fields_for_sourcing()` 结果）**；已 SOURCING_READY/HANDED_TO_SOURCING → 幂等 no-op；终态 → 拒绝；VALIDATED → 置 SOURCING_READY |
| D11 | 租户不可见语义 | 与 discard 先例一致：repo 参数租户与绑定租户不匹配 → `TenantIsolationViolation` + critical 审计（action + 绑定租户，无内容）；service 层「不存在/跨租户不可见」统一固定摘要（`"需求假设不存在"`/`"已验证需求不存在"`），不抛隔离违规 |
| D12 | 事务/outbox 原子性 | `DemandUnitOfWork` 协议扩展 `hypotheses`/`needs` 属性（**无 clusters**；契约纠偏，同 Task 2 先例）；事件同事务 `bus.publish` → outbox；bus 失败 → 整个 UoW 回滚（测试注入 `_FailingBus` 先例） |
| D13 | FactualField 的持久化形状（10 字段 × 8 provenance 列不可行） | **每业务字段一列 JSONB**（`application`/`material`/`size_spec`/`quantity`/`packaging`/`destination`/`required_by`/`target_price`/`current_supply_issue`/`certification_required`），结构 = `{"value": …, "provenance": {source_type, source_id, extracted_by, extracted_at, confirmed_by, confirmed_at, source_url, page_hash}}`；value 序列化：Money → `{"amount": Decimal-str, "currency": str}`（ADR 0008 先例）、date → ISO、int → int、str → str；反序列化回 `FactualField` 并过其 `__post_init__` 校验 |
| D14 | View 组装（account_name）依赖 organization 域 | `list_hypotheses_for_outreach`/`get_need`/`get_cluster`（返回 View，含 account_name）**不在本片**——organization 域未实现，不发明 account_name 来源；诚实标注为后续 UI 切片前置。本片 service_impl 新增 **6 个方法**：`create_hypothesis`/`promote_to_validated`/`reject_hypothesis`/`update_need_fields`/`mark_sourcing_ready`/`get_confidence`（方法数与 §10 文件地图一致） |
| D15 | `get_confidence` 语义 | `derive_confidence(hypothesis.evidence(), now=now)` 现算返回 `ConfidenceResult`（tier/has_conflict/is_stale/explanation/applied_rules）；**无 confidence 数值列**（硬边界 3）；空证据（构造已保证非空，防御性抛 `ValidationError("证据列表不能为空…")` 由 derive_confidence 自身承担） |
| D16 | 状态机「contacting」转换 | 模型/表支持 INFERRED→CONTACTING（数据层允许），但本片 service **不提供** contacting 转换方法（service.py 无声明——不发明 API；`list_for_outreach` repo 层返回 inferred+contacting） |
| D17 | 迁移编号与表集 | `0022_need_hypotheses.py`，down_revision=`0021`，**恰 3 表**：`need_hypotheses`/`validated_needs`/`validated_need_field_history`；downgrade 依次 drop 3 表回 0021；往返测试 0022→0021→0022 |
| D18 | `docs/architecture/01-domain-model.md` §四状态机与 models.py/AGENTS 冲突（陈旧文档） | 随本片同步：Need Hypothesis 的 `outreach_queued/contacted/discarded` → `inferred → contacting → validated / rejected`；Validated Need 的 `sourcing_in_progress/quoted/paused/closed` → `validated → sourcing_ready（完整度 ≥ 3）→ handed_to_sourcing` + 终态 `fulfilled/withdrawn/lost`（仅文案，无架构变更；completeness plan 同步 line 146 先例，详见 §13） |
| D19 | `domains/demand/service.py` 的陈旧契约行（final review finding 9 系列）——授权实现同步 | **契约纠偏授权（本片范围内）**：(a) `update_need_fields` 返回类型 `ValidatedNeedView → None`——零调用方，且 View 组装需 organization 域 account_name（本片明确排除），避免被禁的跨域视图组装；(b) `mark_sourcing_ready` docstring 的错误类型 `InvalidStateTransition` → 既有 `SourcingThresholdNotMetError`；(c) `promote_to_validated` docstring 的「尝试归入需求簇（失败不阻塞主流程）」子句删除——簇整体排除，归簇尝试**延后至被排除的簇切片**；(d) `update_need_fields` docstring 的「完整度变化跨过 3 级门槛时，发布状态转换事件」改为「**不发事件**——catalog 无匹配 schema（最小语义，同 D9）」；(e) `promote_to_validated` docstring 的 extracted_fields 语义补全为 **11 键**（§6.2）。方法签名其余不动 |

## 2. 范围与边界（已有契约 vs 本片新增）

**已有（只读，除 D19 列明的契约纠偏行外）**：`NeedHypothesis`/`ValidatedNeed` 模型字段与枚举（models.py:122-263；`NeedCluster` 存在但非本片范围）；`NeedHypothesisRepository`/`ValidatedNeedRepository` Protocol（repository.py:91-171；`NeedClusterRepository` 存在但本片不使用）；service.py 方法签名与 docstring（`update_need_fields` 返回类型与四处 docstring 行按 D19 纠偏；其余只读）；catalog 三事件字段（NeedHypothesisCreated/NeedHypothesisRejected/NeedValidated；`NeedClusterFormed` 不在本片）；`derive_confidence`/`ConfidenceTier`/`EvidenceItem`/`FactualField`/`InferredField`/`Provenance`（shared）；`ValidatedNeed.completeness/is_sourcing_ready/missing_fields_for_sourcing`（已交付）。

**本片新增（契约级）**：
1. `domains/demand/repository.py` 契约纠偏（同 Task 2 先例）：`NeedHypothesisRepository` 增 `get_for_update(tenant_id, hypothesis_id)`；`ValidatedNeedRepository` 增 `get_for_update(tenant_id, need_id)`；`DemandUnitOfWork` 增 `hypotheses`/`needs` 属性。
2. `infra/db/outbox.py` `EVENT_REGISTRY` 注册三事件（NeedHypothesisCreated/NeedHypothesisRejected/NeedValidated；加新事件不需 ADR；白名单测试同步，131cbe5/db6577a 先例）。
3. `docs/architecture/01-domain-model.md` 状态图同步（D18 陈旧文档）。
4. `domains/demand/service.py` 契约纠偏（D19）：`update_need_fields` 返回类型 `ValidatedNeedView → None` + 三处 docstring 同步（mark_sourcing_ready 错误类型 / promote 归簇子句删除 / update 跨门槛事件改为不发）+ extracted_fields 11 键补全。

**明确不做（非目标）**：
- ❌ **NeedCluster 整体**（finding 1）：`need_clusters` 表、`NeedClusterRepository`、`try_assign_cluster`/`get_cluster`、`NeedClusterFormed` 事件、cluster 相关测试/迁移/文件地图条目——全部不在本片；`validated_need.cluster_id` 保持模型层 None，**本片不持久化该列**（迁移 0022 恰 3 表，见 D17）
- ❌ reply_qualification 的 `extract_need`/`decide_next` 工作流接线（后续切片，本片只交付 demand 域侧能力）
- ❌ `DemandSignal.evidence_level` 的**规格内容升级**（finding 4）：本片映射仅「公开 RFQ/入站询盘/招标公告 → `CUSTOMER_INTEREST_REPLY`」，不因信号含具体规格而升级到更高等级（升级属后续明确非目标；models.py docstring 同步属实现任务，不属本 spec）
- ❌ UI/API；模型 provider/凭证（ADR 未决，与本片无关）；prospecting 清理
- ❌ `list_hypotheses_for_outreach`/`get_need`/`get_cluster` View 组装（D14）

## 3. 状态机（本片落地的确定性转换；与 models.py/AGENTS 一致，旧文档见 D18）

```text
NeedHypothesis（HypothesisStatus）：
  inferred ──→ contacting（数据层允许；本片无 service 转换方法，D16）
  inferred/contacting ──→ validated（唯一门槛：can_promote_to_validated，D6）
                       └─→ rejected（必带 reason；同 reason 幂等、异 reason 拒绝覆盖，D8）
  validated ──→ 不可再变（reject/再次 promote 均拒绝）

ValidatedNeed（NeedStatus）：
  validated ──→ sourcing_ready（完整度 ≥ 3；promote 时初始完整度 ≥3 直接以
               SOURCING_READY 创建，或 update 自动推进/显式 mark_sourcing_ready，D9/D10）
  sourcing_ready ──→ handed_to_sourcing（数据层允许；本片无 service 转换方法——无声明，不发明）
  fulfilled / withdrawn / lost：终态冻结（字段不可变更；转换方法不在本片 service 范围）
```

## 4. 表结构（0022，**恰 3 表**；列类型对齐 demand_signals 先例）

### 4.1 need_hypotheses

| 列 | 类型 | 约束 |
|---|---|---|
| tenant_id | String(40) | PK 之一，非空 |
| hypothesis_id | String(40) | PK 之一（`new_id("hyp")` 前缀 + 26 字符 ULID） |
| account_id | String(40) | 非空 |
| category | String(200) | 非空；CHECK 非空白 |
| reasoning | JSONB | CHECK `jsonb_typeof = 'object'`；InferredField 快照（D13 同款序列化 + based_on 数组） |
| signal_ids | JSONB | CHECK `jsonb_typeof = 'array'`；元素为字符串数组 |
| status | String(20) | CHECK ∈ ('inferred','contacting','validated','rejected') |
| rejection_reason | Text | 可空；CHECK `(status='rejected') = (rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')` |
| validated_need_id | String(40) | 可空；FK → validated_needs.need_id（NO ACTION）；CHECK `(status='validated') = (validated_need_id IS NOT NULL)` |
| created_at | DateTime(timezone=True) | 非空 |

索引：PK `pk_need_hypotheses (tenant_id, hypothesis_id)`；部分唯一 `uq_need_hypotheses_active_account_category (tenant_id, account_id, category) WHERE status IN ('inferred','contacting')`（D2；谓词字面量与仓储 `index_where` 逐字一致，parity 测试见 §9.1）。

### 4.2 validated_needs

| 列 | 类型 | 约束 |
|---|---|---|
| tenant_id / need_id | String(40) | PK（`new_id("need")`） |
| account_id | String(40) | 非空 |
| product_category | JSONB | CHECK object（必填 FactualField，完整度 1 级门槛） |
| source_message_id | String(40) | 非空 |
| source_conversation_id | String(40) | 可空 |
| status | String(20) | CHECK ∈ ('validated','sourcing_ready','handed_to_sourcing','fulfilled','withdrawn','lost') |
| created_at | DateTime(timezone=True) | 非空 |
| application / material / size_spec / quantity / packaging / destination / required_by / target_price / current_supply_issue / certification_required | JSONB | 可空；CHECK `(col IS NULL) OR jsonb_typeof(col) = 'object'`（每列一个，10 个同名 CHECK `ck_validated_needs_<field>_jsonb`） |
| confirmed_by | String(40) | 可空 |

**无 `cluster_id` 列**（finding 1：模型层字段保持 None，本片不持久化）。

### 4.3 validated_need_field_history（append-only，无 UPDATE/DELETE 路径）

| 列 | 类型 | 约束 |
|---|---|---|
| tenant_id / history_id | String(40) | PK（`new_id("vh")`） |
| need_id | String(40) | 非空；FK → validated_needs.need_id（NO ACTION） |
| field_name | String(64) | 非空；CHECK 非空白 |
| old_value | Text | 可空（首次填写时 old 为 NULL） |
| new_value | Text | 非空；CHECK 非空白 |
| source_message_id | String(40) | 非空 |
| changed_by | String(40) | 可空（`extracted_by` 或 `confirmed_by`） |
| changed_at | DateTime(timezone=True) | 非空 |

索引：PK `pk_validated_need_field_history (tenant_id, history_id)`；查询索引 `(tenant_id, need_id, changed_at)`（非唯一，供员工复盘）。

## 5. Repository / UoW 组织（infra/db/repositories/need_hypotheses.py 新文件；同 demand 先例）

- `_HypothesisRepository` 基座：`_tenant_matches`/`_require_tenant`（`TenantIsolationViolation` + critical 审计，action 名：`need_hypothesis_add/get/update/get_for_update/find_active_by_account_and_category/list_for_outreach`、`validated_need_add/get/update/get_for_update/append_field_history/list_sourcing_ready/list_by_account`）；全部查询显式绑定租户过滤（硬边界 8）。
- 实现类两个：`NeedHypothesisRepositoryImpl`、`ValidatedNeedRepositoryImpl`（同文件，镜像 demand 先例）。
- JSONB 序列化器（域内 `_row_to_*`/`_*_to_row` 模块函数）：FactualField/InferredField/EvidenceItem ↔ JSON（§1 D13 决定；Money 按 ADR 0008；反序列化失败 → `ValidationError` 固定摘要，不回显）。
- 假设 `add`：`pg_insert(...).on_conflict_do_nothing(index_elements=["tenant_id", "account_id", "category"], index_where=text("status IN ('inferred','contacting')"))` 返回 `bool`（True=新插入，False=活跃冲突，D2）；**不使用 `constraint=`**（finding 3）。
- `get_for_update` ×2：`SELECT ... FOR UPDATE`（转换路径行锁，D7）。
- `find_active_by_account_and_category`：普通 SELECT（service 首查）；并入路径用 `get_for_update` 锁行重读（D5）。
- `append_field_history`：INSERT 一行（调用方保证同事务内先捕获 old 再 update 当前值，D9）。
- `list_for_outreach`：WHERE status IN ('inferred','contacting') ORDER BY created_at LIMIT ?；抑制名单/Ownership Lock 过滤**不做**（repository docstring 已明示属跨域，service 层组合属后续切片）。
- `list_sourcing_ready`：WHERE status = 'sourcing_ready' ORDER BY created_at LIMIT ?。

## 6. Service 语义（`DemandServiceImpl` 新增 **6 个方法**；输入校验全部在开 UoW 前完成）

统一输入规则（沿用 capture 定稿）：所有 str 输入 `item == item.strip()`、原值精确存储；固定中文错误摘要不回显。

### 6.1 create_hypothesis(tenant_id, account_id, category, signal_ids, reasoning, inferred_by) -> NeedHypothesisId
1. 校验：account_id str/strip/≤40 → `"目标企业无效"`；category str/strip/≤200 → `"需求类别无效/超长"`；signal_ids 非空 list 且元素全为 str/strip 非空/≤40 → `"需求信号不能为空"`/`"需求信号标识无效"`；reasoning str/strip 非空 → `"推断理由无效"`；inferred_by str/strip 非空/≤64 → `"推断者无效"`；时钟 `_validate_now`。
2. UoW 内：逐条 `signals.get(tenant_id, DemandSignalId(sid))`，任一不存在 → `ValidationError("需求信号不存在")`（租户不可见同此摘要，D11）；**任一 `status == DISCARDED` → `ValidationError("需求信号已丢弃")`**（finding 5；mutation M7）；构造 `EvidenceItem(level=signal.evidence_level, source_type=signal.provenance.source_type.value, source_id=signal.provenance.source_id, observed_at=signal.observed_at, summary=signal.raw_observation)`——**需要 `DemandSignal.evidence_level` 实现**（models.py:107-119 桩，本片实现）：纯映射常量表，**仅**「PUBLIC_RFQ/INBOUND_INQUIRY/TENDER_NOTICE → `CUSTOMER_INTEREST_REPLY`」（finding 4，不做规格内容升级）；企业变化类 → `PUBLIC_COMPANY_EVENT`；产业链推断 → `AGENT_INDUSTRY_INFERENCE`。models.py 该 property 的 docstring 同步属实现任务（与映射一致，不升级语义）。
3. `hypotheses.add(假设)` → True：发布 `NeedHypothesisCreated(hypothesis_id, account_id, category, confidence_tier=derive_confidence(evidence, now=now).tier)`，返回新 ID。
4. False（活跃冲突）：`find_active_by_account_and_category` FOR UPDATE 重读 → 并入（D5：based_on 并集 + signal_ids 追加去重 + reasoning 替换）→ `hypotheses.update` → 返回既有 ID，**不发事件**。

### 6.2 promote_to_validated(tenant_id, hypothesis_id, source_message_id, extracted_fields, confirmed_by=None) -> ValidatedNeedId
1. 校验：hypothesis_id/source_message_id str/strip/≤40 → `"需求假设标识无效"`/`"来源消息无效"`；extracted_fields dict 非空且键 ⊆ **11 键白名单 = {product_category} ∪ 10 个可变更业务字段**（application/material/size_spec/quantity/packaging/destination/required_by/target_price/current_supply_issue/certification_required）→ `"未知需求字段"`/`"提取字段不能为空"`；confirmed_by 可空 str。
2. UoW 内：`hypotheses.get_for_update` → 不存在 → `ValidationError("需求假设不存在")`；已 VALIDATED → 幂等返回既有 validated_need_id（D7）；已 REJECTED → `HypothesisAlreadyResolvedError`（既有域错误）；`can_promote_to_validated()` 不通过 → `InsufficientEvidenceError("证据不足，不可晋升为已验证需求")`（D6；消息含当前最高证据等级与所需等级；不落库、不发事件、事务回滚）。
3. 通过：每个 extracted_fields 值包 `FactualField(value, Provenance(source_type=CONVERSATION, source_id=source_message_id, extracted_by=confirmed_by or "human", extracted_at=now, confirmed_by, confirmed_at=now if confirmed_by else None))`；`product_category` 必填（缺失 → `ValidationError("产品类别不能为空")`）且**创建后不可变**（不在 update_need_fields 白名单，见 6.4）；构造 `ValidatedNeed`——**初始完整度（由 extracted_fields 推导）≥ 3 → status=SOURCING_READY；否则 status=VALIDATED**（finding 6）；confirmed_by 透传；`needs.add`；假设置 VALIDATED + `validated_need_id`；`hypotheses.update`；发布 `NeedValidated(need_id, account_id, category, evidence_level=<命中最高证据等级>, completeness=<初始完整度>)`。**不做归簇尝试**（service.py promote docstring 的「尝试归入需求簇（失败不阻塞主流程）」子句已按 D19 删除——簇整体排除，归簇延后至被排除的簇切片）。
4. 并发：后到者 get_for_update 读到 VALIDATED → 幂等返回（D7）。

### 6.3 reject_hypothesis(tenant_id, hypothesis_id, loss_reason, rejected_by=None) -> None
校验同先例；UoW 内 get_for_update：不存在 → `ValidationError("需求假设不存在")`；已 VALIDATED → `HypothesisAlreadyResolvedError`；已 REJECTED 同 reason → 幂等 no-op；异 reason → `InvalidStateTransition("拒绝原因冲突，拒绝覆盖")`；否则置 REJECTED + rejection_reason，发布 `NeedHypothesisRejected(hypothesis_id, reason)`。reason 词表不校验（D8）。

### 6.4 update_need_fields(tenant_id, need_id, fields, source_message_id, updated_by=None) -> None
（**契约纠偏 D19(a)**：service.py 现声明 `-> ValidatedNeedView`——零调用方，且 View 组装需 organization 域 account_name（本片明确排除）——返回类型改为 `None`，实现不组装视图。）
UoW 内 `needs.get_for_update`：不存在 → `ValidationError("已验证需求不存在")`；终态 → 冻结拒绝（D9）；字段白名单 = **10 个可变更业务字段（不含 product_category——创建后不可变）**，未知字段 → `ValidationError("未知需求字段")`；每字段：旧值捕获（FactualField 值序列化为 str，None → NULL）→ `append_field_history(tenant_id, need_id, field_name, old, new, source_message_id, changed_by=updated_by)` → 写新 FactualField（provenance.source_id=source_message_id）；重算 completeness：**仅当当前状态 == VALIDATED 且 <3 → ≥3 跨门槛时自动置 SOURCING_READY；SOURCING_READY/HANDED_TO_SOURCING 状态保持不变（不降级、不重复推进）**（finding 7）；**跨门槛不发任何事件**（service.py docstring「完整度变化跨过 3 级门槛时，发布状态转换事件」已按 D19(d) 同步——catalog 无匹配 schema，最小语义）；`needs.update`。字段值校验由 FactualField `__post_init__` 承担（value 类型：quantity int、required_by date、target_price Money、其余 str）。

### 6.5 mark_sourcing_ready(tenant_id, need_id) -> None
UoW 内 get_for_update；D10 分支；`needs.update`。（service.py:171 docstring 的 `InvalidStateTransition` 已按 D19(b) 同步为既有 `SourcingThresholdNotMetError`。）

### 6.6 get_confidence(tenant_id, hypothesis_id) -> ConfidenceResult
UoW 内 `hypotheses.get`：不存在 → `ValidationError("需求假设不存在")`；`derive_confidence(hypothesis.evidence(), now=now)` 现算返回（D15；假设表无 confidence 列）。

## 7. 字段历史形状（D9 定稿；含旧 provenance 丢失的诚实标注）

`validated_need_field_history` 行 = `(tenant_id, history_id, need_id, field_name, old_value, new_value, source_message_id, changed_by, changed_at)`；**append-only**（无更新/删除 API；DB 无约束禁止物理删除，靠「无路径」约定 + 测试断言）；old_value/new_value 为显示值字符串（Money → Decimal-str、date → ISO、int → str）。

**旧值 provenance 丢失风险（finding 8，显式选择）**：被覆盖字段的旧 `FactualField` 完整 provenance（extracted_by/confirmed_by/extracted_at 等）**不保留**——当前行被新 FactualField 替换，历史行只存显示值字符串与本**次变更**的 `source_message_id`。追溯旧值来源的唯一路径 = 历史行自身的 `source_message_id` 链 + 既往 `NeedValidated` 事件（completeness 快照）。选择理由：每历史行冗余存全套 provenance 会双写漂移且无人消费；`source_message_id` 已指向证据链起点（消息已落库，正文/来源可查）。如需完整旧 provenance 快照（如审计强需求）属后续独立切片，本片诚实标注此限制。

顺序查询按 changed_at ASC, history_id ASC（确定性）。

## 8. 事件（metadata-only；**3 个**，注册 + 白名单）

三事件结构不动（catalog 权威）：`NeedHypothesisCreated`/`NeedHypothesisRejected`/`NeedValidated`；`EVENT_REGISTRY` 增这三项（**无 `NeedClusterFormed`**——簇非本片范围）；`test_event_registry_is_explicit_whitelist` 期望集同步（既有模式）。事件 payload 键 ⊆ 既有 schema：`NeedValidated` 含 need_id/account_id/category/evidence_level/completeness（**不含**字段值/正文/provenance 内容）；生产内容 marker（raw_observation/字段值/reason/消息正文）不得出现在 outbox/log/error（caplog 断言；审计日志仅 action + 绑定租户）。

**`domains/demand/events.py` 措辞（只读，不改）**：其 `PUBLISHES` 实际含五事件——三本片事件 + `DemandSignalCaptured`（已注册）+ `NeedClusterFormed`（**超本片范围，本片不注册**）；本片不因该声明改动 events.py，注册以 `EVENT_REGISTRY` 白名单为准（模块 docstring 已明示白名单为手工维护的权威集合）。

## 9. 测试与 mutation 矩阵

### 9.1 迁移测试（test_migrations.py 增补）
- head 0021→0022 全局更新（revision 字面量与 RED 文案）；`EXPECTED_TABLES` 增 3 表；0022 契约 parity（列/类型/PK/全部 CHECK/JSONB CHECK/FK 存在性——FK 用 information_schema 校验；**部分唯一索引 parity：迁移 `postgresql_where` 与仓储 `index_where` 的谓词文本均须等于字面量 `"status IN ('inferred','contacting')"`**，finding 3 专用测试）；0022→0021→0022 往返。

### 9.2 单元/契约测试
- `tests/unit/test_demand_signal_contracts.py` 扩展：两 repo Protocol 参数签名精确断言（含新增 `get_for_update`）+ UoW 协议成员 {signals, hypotheses, needs, bus}；三事件注册断言（`NeedClusterFormed` 不在断言集）。
- `tests/unit/test_need_hypothesis_models.py`（新）：`evidence()` 去重（同 (source_type, source_id) 两条只留等级高者；跨 source_id 保留）；`can_promote_to_validated` 门禁表（AGENT_INDUSTRY_INFERENCE/PUBLIC_COMPANY_EVENT 拒；CUSTOMER_INTEREST_REPLY×CONVERSATION/UPLOAD/EMPLOYEE_INPUT 过；WEB_PAGE 拒——source_type 不在白名单）；`DemandSignal.evidence_level` 映射表（**仅**：public RFQ/入站询盘/招标公告 → CUSTOMER_INTEREST_REPLY，含规格内容也不升级——finding 4 断言）；`InsufficientEvidenceError`/`SourcingThresholdNotMetError`/`HypothesisAlreadyResolvedError` 祖先链 ∈ TradeOSError。

### 9.3 集成测试（`tests/integration/test_need_hypotheses.py` 新文件，真实 PostgreSQL + 真实 UoW/仓储，零 mock；15 项）
1. create_hypothesis 往返：JSONB（reasoning.based_on EvidenceItem、signal_ids）全字段 roundtrip；返回 `hyp_` 前缀 ID。
2. 活跃去重：同 (account, category) 第二次 create（新 signal）→ 返回既有 ID、1 行、1 事件、based_on 为并集、signal_ids 追加。
3. 并发 create 同 key：`asyncio.gather` 两 UoW → 恰 1 行 1 事件、两结果 ID 相同（index_elements+index_where ON CONFLICT + 重读）。
4. create 门槛：signal_ids 空 → `ValidationError("需求信号不能为空")`；信号不存在/跨租户 → `ValidationError("需求信号不存在")`；**DISCARDED 信号 → `ValidationError("需求信号已丢弃")`**（finding 5）；校验先于 UoW。
5. promote 门禁：AGENT 推断证据 → `InsufficientEvidenceError`（行保持 inferred、无 need、无事件）；CUSTOMER_INTEREST_REPLY+CONVERSATION → 成功。
6. promote 成功状态：extracted_fields 含 **product_category + 至少一个 mutable 字段**（成功测试必备 product_category）；缺 product_category → `ValidationError("产品类别不能为空")`；**初始完整度 ≥ 3 → need 状态 SOURCING_READY；否则 VALIDATED**（finding 6）；need 落库（product_category 与 mutable 字段的 FactualField provenance 均指向 source_message_id）、假设 VALIDATED + validated_need_id、`NeedValidated` 事件（completeness/evidence_level 正确）、outbox metadata-only、**无归簇尝试**。
7. promote 幂等与并发：二次 promote → 同 need_id、1 need 行、1 事件；并发 promote → 恰 1 need、1 事件。
8. reject：同 reason 幂等；异 reason 冲突且 first 保留；validated → `HypothesisAlreadyResolvedError`；事件含 reason。
9. update_need_fields：历史行 append（old→new + source_message_id + changed_by）；**跨 3 级自动推进仅当当前状态 VALIDATED（SOURCING_READY/HANDED_TO_SOURCING 不被改写）**（finding 7）；**跨门槛不发事件**（D19(d)）；终态冻结；未知字段拒绝（含 **product_category——不在 10 字段白名单、创建后不可变** → `ValidationError("未知需求字段")`）；quantity 类型校验（非 int → ValidationError）。
10. mark_sourcing_ready：completeness<3 → `SourcingThresholdNotMetError`（消息含缺失字段）；≥3 → SOURCING_READY；幂等。
11. get_confidence：tier 与 evidence 对应（无存储列）；假设不存在 → 固定摘要。
12. list_sourcing_ready / list_by_account / list_for_outreach：过滤与排序正确（不含 rejected）。
13. 租户：跨租户 repo 调用 → `TenantIsolationViolation` + critical 审计日志仅 action/绑定租户（无内容）；service 不可见 → 固定摘要。
14. outbox/日志 marker 零泄漏：reason/字段值/正文不进 outbox/log/error（caplog 全程零记录）。
15. 原子性：bus 失败（`_FailingBus` wrapper）→ 整个 UoW 回滚（假设行不落/need 不落/事件不落）。

### 9.4 mutation 矩阵（复审通过后、提交前，每项临时 apply_patch → 精确测试 RED → 恢复 → GREEN → `git diff` 零残留）

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M1 | 假设 `add` 去掉 `.on_conflict_do_nothing(...)`（普通 INSERT） | 9.3-3（并发 create 同 key） | gather 结果含 IntegrityError（部分唯一冲突），`all(isinstance(r, str))` 失败 |
| M2 | 活跃 key 改错：`index_elements` 去掉 `"account_id"` | 9.3-2（活跃去重） | 不同企业同 category 被误并 → 行数/事件数断言失败 |
| M3 | `promote_to_validated` 删除 `can_promote_to_validated()` 检查 | 9.3-5（promote 门禁） | AGENT 推断证据不再被拒 → `pytest.raises(InsufficientEvidenceError)` DID NOT RAISE |
| M4 | `update_need_fields` 去掉 `append_field_history` 调用 | 9.3-9（字段历史） | 历史表为空 → 断言失败 |
| M5 | `reject_hypothesis` 不同 reason 改无条件返回 | 9.3-8（reject 冲突） | `pytest.raises(InvalidStateTransition)` DID NOT RAISE |
| M6 | 并入时也发布 `NeedHypothesisCreated` | 9.3-2（活跃去重） | 事件数 2（断言 1 失败） |
| M7 | `create_hypothesis` 删除 DISCARDED 信号检查 | 9.3-4（create 门槛） | DISCARDED 信号不再被拒 → `pytest.raises(ValidationError)` DID NOT RAISE（finding 5） |

## 10. 文件地图

```text
docs/superpowers/specs/2026-08-17-need-hypothesis-persistence-design.md   本文件
migrations/versions/0022_need_hypotheses.py                             新迁移（恰 3 表 + 部分唯一索引 + FK）
domains/demand/service.py                                               改：契约纠偏（D19：update_need_fields → None + 三处 docstring 同步 + extracted_fields 11 键）
infra/db/repositories/need_hypotheses.py                                新：两个 RepositoryImpl（NeedHypothesis/ValidatedNeed，同 demand 先例）
infra/db/demand_uow.py                                                   改：UoW 增 hypotheses/needs
domains/demand/repository.py                                             改：契约纠偏（get_for_update ×2 + UoW 属性）
domains/demand/service_impl.py                                           改：新增 6 个服务方法
domains/demand/models.py                                                 改：evidence_level / evidence() / can_promote_to_validated() 实现
infra/db/outbox.py                                                       改：EVENT_REGISTRY 注册三事件
domains/demand/events.py                                                 只读（PUBLISHES 含三本片事件 + DemandSignalCaptured + NeedClusterFormed——后者超范围不注册）
tests/integration/test_migrations.py                                     改：head 0022 + 3 表契约/往返 + 部分索引 parity
tests/unit/test_demand_signal_contracts.py                               改：repo/UoW 契约扩展 + 三事件注册
tests/unit/test_need_hypothesis_models.py                                新：纯单元（evidence/门槛/映射）
tests/integration/test_need_hypotheses.py                                新：15 项集成 + mutation 目标
docs/architecture/01-domain-model.md                                     改：状态图同步（D18）
```

## 11. 错误摘要总表（固定中文，不回显输入）

| 场景 | 错误 |
|---|---|
| 假设不存在/跨租户不可见 | `ValidationError("需求假设不存在")` |
| 已验证需求不存在/不可见 | `ValidationError("已验证需求不存在")` |
| create 引用 DISCARDED 信号 | `ValidationError("需求信号已丢弃")` |
| promote 证据不足 | `InsufficientEvidenceError`（既有域错误，`PolicyViolation → TradeOSError`；消息含当前最高证据等级与所需等级） |
| promote/reject 遇已终态假设 | `HypothesisAlreadyResolvedError`（既有域错误） |
| mark_sourcing_ready 完整度不足 | `SourcingThresholdNotMetError`（既有域错误；消息含 `missing_fields_for_sourcing()` 结果） |
| reject 异 reason | `InvalidStateTransition("拒绝原因冲突，拒绝覆盖")` |
| 终态字段变更 | `InvalidStateTransition("需求已终结，字段不可变更")` |
| 输入（account/category/signal_ids/reasoning/inferred_by/ids/reason/fields） | `ValidationError("<固定摘要>无效/超长/不能为空/未知需求字段")` |

## 12. 九条硬边界逐项

1. 凭证：本片无凭证路径（无 provider、无外部 SDK）✓；2. 金额：`target_price` 用 `Money`（Decimal，ADR 0008 JSON 字符串）✓；3. 置信度：无 confidence 数值列（表结构无；CHECK 无），`get_confidence` 现算 ✓；4. Provenance：10 个业务字段全为 FactualField（JSONB 快照），历史表 source_message_id 指向证据链（旧值完整 provenance 的已知限制见 §7，finding 8）✓；5. 事实/推断分离：FactualField/InferredField 类型强制（FactualField 拒 AGENT_INFERENCE），promote 门槛唯一 ✓；6. 可达性：本片不进入发送序列（prospecting 清理明确排除）✓；7. 价格：target_price 不进入客户可见报价（本片无报价）✓；8. 租户：3 表全带 tenant_id、全部查询绑定过滤、repo 参数越界 TenantIsolationViolation ✓；9. 依赖：domains/demand → shared 仅（reason 词表不校验即不 import opportunities；organization 不 import）✓。

## 13. 陈旧文档同步（D18）

`docs/architecture/01-domain-model.md` §四状态机与 models.py/AGENTS 冲突（陈旧）：Need Hypothesis 的 `outreach_queued/contacted/discarded` → 改为 `inferred → contacting → validated / rejected`（rejected 必带原因）；Validated Need 的 `sourcing_in_progress/quoted/paused/closed` → 改为 `validated → sourcing_ready（完整度 ≥ 3）→ handed_to_sourcing` + 终态 `fulfilled/withdrawn/lost`（与 models.py NeedStatus 一致）。仅文案，无架构变更（completeness plan 同步 line 146 先例）。

## 14. 交付门禁

- 每个任务：RED（本地确认预期失败原因：缺表/缺实现/缺方法）→ 最小 GREEN → focused pytest `-W error` → ruff（涉及文件）→ mypy `domains/demand infra/db` → `check_boundaries.py` → `scan_sensitive.py` → `git diff --check` → **停止等待监督方复审** → 复审通过 → 单任务 commit/push/exact-HEAD CI success（禁 force）。
- 新文件 `git add --chmod=-x` + `ls-files --stage` 验 100644；AppleDouble 清零后才暂存。
- 全片完成后再跑 Task 5 式最终门禁（focused + 全量非 e2e + ruff . + 全量 mypy + boundaries + sensitive + web 块 + diff-check + `TRADEOS_REQUIRE_E2E=1` e2e）。
- 本片不产生 UI/API/web 变更；不改 AGENTS.md/HANDBOOK/ROADMAP/GLOSSARY。

## 15. 自查（一致性、范围、边界、陈旧文档）

- 一致性：状态枚举（HypothesisStatus/NeedStatus）与 models.py 逐字一致；表名/列名/CHECK 名在 §4 与迁移任务间一致；错误摘要与 §11 一致；事件字段与 catalog 逐字一致；D1-D19 决定与 §5/§6 实现语义一一对应；**方法数（6）、表数（3）、事件数（3）、集成测试（15）、mutation（7）在 §1/§6/§8/§9/§10 间完全一致**；`NeedClusterFormed`/`need_clusters`/`try_assign_cluster` 零内容残留（仅排除/边界声明语境）。
- 范围：仅 demand 域 + infra 适配 + 事件注册 + 文档同步；无 workflow/UI/API/provider/prospecting/Phase 2/**无任何簇内容**（finding 1）。
- 边界：§12 九条逐项；`list_for_outreach` 不做跨域过滤（不越权）；View 组装明确排除；错误类全部复用既有域错误（finding 2）；ON CONFLICT 仅用 `index_elements`+`index_where`（finding 3）；evidence_level 仅三映射不升级（finding 4）；DISCARDED 信号拒绝（finding 5）；promote 初始 SOURCING_READY（finding 6）；自动推进仅 VALIDATED 不降级（finding 7）；旧 provenance 丢失显式标注（finding 8）。
- 陈旧契约（已解决，如实列明，不再声称无冲突）：**service.py 的 `update_need_fields` 返回类型与三处 docstring 行（mark_sourcing_ready 错误类型 / promote 归簇子句 / update 跨门槛事件）为已列明并授权纠偏的陈旧行**（D19），随实现同步；01-domain-model.md 状态图（§13，D18）随本片同步；其余无冲突（GLOSSARY 术语一致；models.py/AGENTS 为权威契约）。
