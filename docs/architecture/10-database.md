# 数据库

PostgreSQL + pgvector。本文档给出表清单与关键字段说明，**不写 DDL**——迁移脚本在实现阶段生成。

---

## 全局约定

1. **每张业务表都有 `tenant_id`**（硬边界 8）。Repository 层统一注入租户过滤，不靠每个查询点自觉。
2. 主键用带前缀的字符串 ID（`opp_`、`need_`、`si_`），便于日志排查和跨系统引用。
3. 时间字段一律带时区，统一存 UTC。
4. 金额字段用 `NUMERIC`，**不用 `float`**（硬边界 2）。金额必须与币种字段成对出现。
5. 审计与 Provenance 类表**只增不改不删**。
6. 状态字段用受约束的枚举，状态转换由代码执行并写审计事件。
7. 软删除用 `deleted_at`；抑制名单相关记录**永不删除**，否则会重新联系已退订的人。

---

## 组织与员工

```text
tenants                  租户
users                    登录账号
employees                员工档案（可与 user 分离，含离职员工）
teams                    团队
roles                    角色定义（RBAC）
territory_assignments    业务分配矩阵：员工 × 国家 × 需求类别 × 客户类型
company_playbooks        公司规则：排除类别、最小交易额、可用货源、审批要求
boss_directives          老板指令（当前生效版本）
directive_versions       指令历史版本，可回滚
```

`territory_assignments` 是客户分配的依据，字段含员工、国家、产品类别、需求类别、客户类型、语言、时区、直属经理、备用员工、最大活跃客户数、优先级、有效时间。

---

## 需求发现

```text
demand_sources           数据来源登记（含可用性与合规状态）
demand_signals           需求信号：来源 URL、观察时间、页面哈希、实体、可能需求
need_hypotheses          需求假设：推断内容、依据的信号、证据等级、状态
need_evidence            证据条目，多对多连接假设与信号/消息/上传
validated_needs          已验证需求：品类、材质、规格、数量、目的地、包装、时间
need_clusters            需求簇（Phase 1 只建表与归簇，排序 Phase 2）
need_cluster_members     簇成员
market_hypotheses         市场级假设（哪类企业、哪个国家值得投入）
```

**`demand_signals` 表没有 `confidence` 数值列**（硬边界 3）。它有 `evidence_level` 枚举列，置信度由代码推导，不落库为小数。

`validated_needs` 的每个业务字段都要有配套的 Provenance 引用。

---

## 客户开发与触达

```text
prospect_accounts        潜在企业
prospect_contacts        潜在联系人
contact_points           联系方式（邮箱/电话/社交），含可达性验证状态与法律依据
contact_legal_basis      处理依据留痕：依据类型、主体类型、来源、评估引用
lead_scores              打分快照（不可变，含 gates_passed、factors、outcome 回填）
lead_ownership           客户归属锁（粒度是企业）
outreach_campaigns       Campaign current 状态、当前版本、审批绑定与轮询游标
outreach_campaign_versions 不可变 Campaign 边界版本
outreach_sequence_steps  版本化、规范化的 1–5 个序列步骤
outreach_enrollments     账户/联系人入组、绑定版本与当前状态
outreach_suppressions    联系人/企业级 append-only 全局抑制事实
outreach_daily_quotas    Campaign 每 UTC 日两类单调预留计数
outreach_message_attempts durable 发送准备记录（不是授权）
outreach_actions         append-only 触达业务动作
delivery_events          投递事件：送达、打开、退信、投诉
consent_records          同意记录（WhatsApp opt-in、表单同意）
```

`contact_points` 的可达性验证状态是发送前置条件（硬边界 6）。未验证的联系方式不得出现在 `sequence_enrollments` 中。

迁移 0009 的八张 `outreach_*` 表都用 tenant composite PK/FK/unique key。`outreach_enrollments` 有 `(tenant_id, account_id)` 的 active partial unique，只允许同租户同账户一条 `enrolled`/`in_sequence` 记录；所有查询仍显式带 tenant 过滤。

Campaign version、sequence step、suppression 与 action 由 trigger 禁止 UPDATE/DELETE；daily quota 只允许单调增加且禁止 DELETE。Enrollment、Attempt 的状态字段由 CHECK 保证终态/失败字段一致。Suppression 同一 tenant 的幂等键唯一，并有 tenant+contact、tenant+account 两条覆盖查询索引；发送路径的抑制查询失败必须 fail closed。

---

## 发件身份

```text
sending_identities       发件身份：域名、邮箱、用途、认证状态、信誉状态
sending_auth_checks      SPF/DKIM/DMARC 校验记录与时间
warmup_plans             预热计划与当前进度
sending_quotas           每日限额与已用量
reputation_snapshots     信誉指标快照（退信率、投诉率、按窗口聚合）
throttle_events          熔断与降额事件
```

`purpose` 字段区分 `cold_outreach` / `transactional` / `human_reply`——人工往来身份必须与冷开发身份分开，见 [07-sending-identity.md](07-sending-identity.md)。

---

## CRM 与商机

```text
companies                正式客户企业
contacts                 正式联系人
conversations            会话（跨渠道）
messages                 消息（含方向、语言、原文引用）
opportunities            贸易机会（中心对象）
opportunity_needs        机会与已验证需求的关联
requirements             机会下的具体要求条目
activities               活动流水
follow_up_tasks          跟进任务
handoffs                 人工接管记录（含接管包内容与等待时长）
loss_records             失败归因：Loss Reason、死在哪个状态、当时证据等级、已耗成本
```

`handoffs` 记录通知时间与接受时间，用于计算接管 SLA。

---

## 员工工作

```text
work_uploads             员工上传批次
raw_artifacts            原始文件（哈希、存储位置、不可变）
extracted_facts          Agent 提取结果
employee_confirmations   员工修改与确认（保留完整链条，不覆盖）
daily_work_packets       员工每日工作汇总
commitments              承诺账本（员工承诺与客户承诺）
manager_reviews          经理复核记录
```

提取链条必须完整保留：原始资料 → Agent 提取版本 → 员工修改版本 → 最终确认版本，含修改人与时间。

---

## 产品与供应

```text
products                 正式产品与候选产品（用状态区分）
product_variants         SKU / 规格变体
product_assets           图片、视频（含权属判断与处理记录）
product_documents        说明书、认证、测试报告
product_cards            三视图渲染配置（内部/销售/客户）
supply_capabilities      供应能力（无固定产品的加工与定制能力）
suppliers                供应商
supplier_products        供应商产品
supplier_evidence        供应商证据（网页快照、截图、观察时间、哈希）
```

`product_cards` 的客户视图**永不包含供应商与内部成本**，这要在序列化层强制。

---

## 寻源与报价

```text
sourcing_cases           寻源案例
supplier_candidates      候选供应商（最多保留三个合格候选）
sourcing_assets          寻源证据资产
fx_snapshots             汇率快照（报价时锁定）
price_snapshots          价格快照，含 basis 字段：indicative | quoted
cost_sheets              成本表（三版本：estimated / quoted / actual）
cost_items               成本项明细
quotes                   报价
quote_lines              报价行
quote_versions           报价版本（历史不可被供应商改价覆盖）
margin_rules             利润规则与底线
```

`price_snapshots.basis` 是硬边界 7 的落地字段：只有 `quoted` 能进客户可见报价。

`cost_items` 应覆盖设计稿列出的全部成本项：采购价、样品费、模具费、定制费、Logo 印刷、包装、质检、损耗、国内运输、国际运输、保险、报关、关税与不可抵扣税、目的地运输、仓储、支付手续费、销售佣金、获客费用、数据费用、广告分摊、Agent/API 成本分摊、退货与售后预留。

---

## Agent 与审计

```text
skills                   技能登记
skill_versions           技能版本
workflow_runs            Run 记录
workflow_steps           步骤与状态（Phase 1 的状态机存储）
tool_calls               工具调用流水（入参、结果、耗时、成本、证据）
artifacts                产出物索引
change_sets              业务变更集（应用前可审批、可回滚）
approval_packages        审批包
approvals                审批结果（不可删除）
provenance_records       Provenance 记录
audit_events             审计事件（只增）
```

`workflow_steps` 是 Phase 1 工作流引擎的核心表，由 `scheduler-worker` 定时扫描推进。表结构要能支持：状态、下次执行时间、重试次数、幂等键、等待人工标记。

---

## 计费（Phase 3，先不建表）

```text
subscriptions            订阅
credit_wallets           积分钱包
credit_ledger            积分流水
credit_reservations      预留
provider_usage_events    供应商用量事件
provider_costs           供应商成本
```

Phase 1 不建这些表，但 `tool_calls` 要记录成本字段，否则 Phase 3 无法回溯定价。语义见 [ROADMAP](../../ROADMAP.md#phase-3多租户计费与客户端)。

---

## 索引与检索

- 高频查询路径：租户 + 状态、租户 + 负责人 + 状态、租户 + 企业、幂等键唯一索引
- 抑制名单查询在发送关键路径上，必须有覆盖索引
- pgvector 用于历史对话、需求描述、产品描述的语义召回；**只用于召回候选，不用于下结论**

---

## 不引入图数据库

「客户—需求—产品—供应商」看似适合图，但初期关系表加 JSON 足够。等关系查询真正成为瓶颈再引入，理由见 [ADR 0006](../adr/0006-postgres-not-graph.md)。
