# 领域模型

四层需求分离是本项目最重要的设计。**不要把它们合并成一张表加一个 status 字段**——它们的证据要求、可信度、可执行动作完全不同，合并后事实与推断会混在一起，硬边界 5 就守不住了。

---

## 一、四层需求

### 1. Demand Signal 需求信号

公开可观察的线索，暗示某处**可能**存在采购需求。只是线索，不代表客户会买。

必须记录来源 URL、观察时间、页面哈希——否则事后无法验证，也无法回答老板「你凭什么这么判断」。

```yaml
signal_type: product_line_expansion
source_url: https://...
observed_at: 2026-08-07T10:00:00Z
page_hash: sha256:...
entity: Acme Manufacturing
possible_need: stainless steel components
```

**注意**：此处**没有** `confidence` 字段。置信度由代码依据证据等级推导，模型不得输出概率值（硬边界 3）。

信号来源分六类，越往上越有价值：

| 类别 | 例子 | 转化速度 |
|---|---|---|
| 直接需求 | 公开 RFQ、招标公告、采购平台询价、客户提交的询价表、公司邮箱询盘、历史未成交需求、供应商转来的线索 | 最快 |
| 企业变化 | 工厂扩建、新仓库、新门店、新产品系列、进入新国家、获得大额项目、换经销商、招聘采购岗、新认证、并购、新融资 | 中 |
| 产业链推断 | 发现家具制造商 → 可能需要铰链、滑轨、把手、脚轮、包装、紧固件 | 中 |
| 相邻品类 | 客户在卖 A → 可能需要 A 的配件、耗材、包装、替换件、升级版、捆绑品、低价替代品 | 中 |
| 供应不满 | 质量不稳、交付慢、MOQ 太高、价格高、缺认证、退货率高、无法定制 | 较快 |
| 卖家反向发现 | Shopify / TikTok Shop / Amazon / Temu 卖家在售什么、评价里有什么问题 | 中 |

### 2. Need Hypothesis 需求假设

Agent 依据一条或多条 Signal 提出的推断。**仍然不是事实。**

Agent 不应断言「这家公司一定需要某产品」，只能表达「这家公司出现了某种变化，因此可能存在某类采购需求，值得联系验证」。

```yaml
buyer: Acme Manufacturing
hypothesis_category: stainless steel hinges
reasoning:
  - 新增户外橱柜产品线
  - 网站没有相关自产能力
  - 现有产品使用大量金属连接件
evidence: [signal_id_1, signal_id_2]
evidence_level: public_company_event   # 代码据此推导置信度
status: inferred
```

每条结论必须可回答**这是哪一类**：事实 / 推断 / 客户明确说过 / 员工推测 / Agent 推测。

### 3. Validated Need 已验证需求

只有客户回复、提交表单、发送规格表，或员工沟通确认后才能进入此状态。

```yaml
product_category: stainless steel hinge
material: 316 stainless steel
size: 100 mm
quantity: 5000
destination: California
packaging: custom logo
required_by: 2026-10
provenance:
  source_type: conversation
  source_id: msg_123
  extracted_by: model_v3
  confirmed_by: 张三
  confirmed_at: 2026-08-07T12:00:00Z
```

每个影响商业决策的字段都要带 Provenance（硬边界 4）。

### 4. Trade Opportunity 贸易机会

系统的中心对象。一条合格机会需同时满足：

```text
可接触的客户 × 真实需求 × 能找到的供应 × 可接受的利润 × 能执行的团队
```

字段至少包含：谁可能需要、需要什么、为什么判断他需要、需求有多真实、预计数量、规格、用途、目的地、时间要求、目标价格、决策人、当前使用的供应方案、现有方案有什么问题、我们能否找到货、预计成本、预计利润、负责员工、下一步动作。

---

## 二、证据等级与置信度推导

**这是硬边界 3 的落地机制。** 模型只做一件事：判断某条证据属于哪个等级。置信度由确定性代码依据「集齐了哪些等级的证据」推出。

| 证据等级 | 标识 | 强度 |
|---|---|---|
| Agent 从行业推断 | `agent_industry_inference` | 低 |
| 公开企业变化信号 | `public_company_event` | 中低 |
| 员工认为可能需要 | `employee_guess` | 中 |
| 客户回复表示感兴趣 | `customer_interest_reply` | 中高 |
| 客户明确给出规格 | `customer_specification` | 高 |
| 客户给出数量、时间、目的地 | `customer_quantity_and_timing` | 很高 |
| 客户请求样品或正式报价 | `customer_sample_or_quote_request` | 极高 |

这个分级同时决定：需求能否升级为 Validated Need、机会能否过硬门槛、以及是否触发人工接管。

---

## 三、需求完整度 0–5

衡量需求信息够不够进入下一步。它是**门槛**，不是评分。

| 级别 | 含义 | 可做什么 |
|---|---|---|
| 0 | 只有模糊兴趣 | 继续提问 |
| 1 | 产品类别明确 | 继续提问 |
| 2 | 用途或规格初步明确 | 可开始内部预研 |
| 3 | 数量明确 | 可评估订单价值 |
| 4 | 时间和目的地明确 | 可估算物流与到岸成本 |
| 5 | 具备寻源或报价条件 | 可启动 Sourcing Case |

**Agent 提问时不要一次问十几个问题**，要选当前最关键的缺失信息。客户只说 "We may need hinges." 时，该问用途和大致尺寸范围，而不是立刻问价格、包装、付款和交期。

---

## 四、状态机

### Demand Signal

```text
observed → linked_to_hypothesis
        └→ discarded（无效来源 / 过期 / 重复）
```

### Need Hypothesis

```text
inferred → contacting → validated
        │              └→ rejected（客户明确表示无需求；rejected 必带原因）
```

### Validated Need

```text
validated → sourcing_ready（完整度 ≥ 3）→ handed_to_sourcing
         └→ fulfilled / withdrawn / lost（终态）
```

### Trade Opportunity

```text
qualified → assigned → in_progress → quoted → won
                                           └→ lost（必须带 Loss Reason）
                     └→ stalled（超期无进展，触发经理介入）
```

**状态转换必须由确定性代码执行**，模型只能建议。每次转换写审计事件。

---

## 五、Loss Reason

机会终止必须归因。没有它，「根据结果改进下一轮策略」只能靠感觉。现在加几乎零成本，事后补要重跑历史数据。

```text
unreachable            联系不上（邮箱无效、无人应答）
no_reply               发完序列无回复
need_not_real          需求不真实（假设被证伪）
no_supply_found        找不到能供的货
price_gap              价格谈不下来
lost_to_competitor     输给竞争对手
customer_went_silent   中途失联
compliance_blocked     命中禁售或高风险类别
internal_capacity      我方无力承接
duplicate              与既有机会重复
```

每条 lost 记录同时保存：死在哪一步（状态机位置）、当时的证据等级、已消耗成本。

---

## 六、Need Cluster 需求簇

多个客户出现相似需求时聚成一簇。

```yaml
need_cluster:
  category: stainless steel marine hinges
  countries: [United States, Australia]
  accounts: 8
  total_potential_quantity: 23000
  recurring_demand: likely
```

价值在于集中寻源、提高谈价能力、摊薄 MOQ 风险、可能沉淀为正式产品。**Phase 1 只建模型和归簇逻辑**；「寻源队列按簇排序」和「簇达规模自动提议入正式目录」是 Phase 2——Phase 1 寻源人工且样本太少，聚不出东西。

---

## 七、供应侧：三个池

`domains/products` 与 `domains/suppliers` 共同构成供应能力中心。它是**能力清单，不是获客起点**。

| 池 | 内容 |
|---|---|
| 正式产品 | 规格、图片、供应商、成本、MOQ、交期、质量、可销售市场全部确认 |
| 候选产品 | 来自历史寻源，未完全验证（`source_only` / `partial` / `not_approved`） |
| 供应能力 | 没有固定产品但有能力：金属加工、注塑、包装、OEM、小批量定制、整合多个供应商 |

现有产品可获得「供应准备度加成」（供货确定性、报价速度、图片完整度、利润可信度更高），**但不能主导探索方向**。

### 匹配梯子

需求验证后按顺序解决，不要跳级：

```text
1. 正式产品完全匹配
2. 正式产品可修改后匹配
3. 候选产品匹配
4. 现有供应商有类似产品
5. 供应商可定制
6. 需要公开寻源
7. 需要寻找全新工厂
```

匹配结果**不能只给相似度百分比**。必须说明：哪些规格完全匹配、哪些不同、哪些字段未知、是否需要客户确认、能否替代、替代会带来什么影响。

---

## 八、产品的三个视图

同一个产品对不同角色显示不同字段，这是权限模型在数据层的体现（见 [03-permissions.md](03-permissions.md)）。

| 视图 | 可见 |
|---|---|
| 内部（老板 / 产品 / 采购） | 供应商、真实采购成本、内部利润、风险、历史问题 |
| 销售 | 允许销售的价格范围、卖点、MOQ、交期范围、常见问题、可发送图片 |
| 客户 | 图片、名称、规格、可选型号、客户可见介绍、询价与样品入口 |

客户视图**永不显示供应商与内部成本**。
